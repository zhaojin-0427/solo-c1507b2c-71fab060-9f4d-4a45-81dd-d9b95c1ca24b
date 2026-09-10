"""Metric attribution and effect analysis.

Pure-ish logic over immutable rows (exposures, metric defs, result events):

* ``resolve_owner`` — the version that owns an event is the version of the
  user's single closest *prior enrolled exposure across ALL versions* of the
  experiment. An event therefore lands on exactly one version: after a user
  has been exposed to a newer version, later events can never be counted on
  an older version's analysis (history is never rewritten).
* ``analyze`` — per-variant samples, conversion rate / mean, relative lift
  vs. control with 95% confidence intervals, sample-ratio-mismatch and
  insufficient-sample flags, plus full audit counts and formulas.

Attribution is (re)computed at query time from immutable rows, so repeating
the same query over the same time range always yields the same result, and
publishing a new version can never alter an analysis pinned to an older
version.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .repository import MetricDef
from .time_utils import parse_iso, to_iso

Z95 = 1.959963984540054  # 0.975 quantile of the standard normal

# Exclusion reasons (also surfaced on the ingest-time preview)
NO_EXPOSURE = "no_exposure"
EVENT_BEFORE_EXPOSURE = "event_before_exposure"
OUT_OF_WINDOW = "out_of_window"
INVALID_VALUE = "invalid_value"

# Classification for an event relative to the analyzed version
OWNED = "owned"            # the global nearest prior exposure is this version
NOT_OWNED = "not_owned"    # nearest prior exposure belongs to another version
NO_VERSION = "no_version"  # the event is outside this version's audit scope
SCOPE_NO_EXPOSURE = "scope_no_exposure"  # in scope, no prior enrolled exposure


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _group_by_user(exposures: list[dict[str, Any]], *, enrolled_only: bool
                   ) -> dict[str, list[dict[str, Any]]]:
    """user_key -> exposures sorted by (recorded_at, id)."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in exposures:
        if enrolled_only and not row["enrolled"]:
            continue
        grouped[row["user_key"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: (parse_iso(r["recorded_at"]), r["id"]))
    return grouped


def _prior_record(rows: list[dict[str, Any]], event_at: datetime,
                  ) -> tuple[int, Optional[dict[str, Any]],
                             list[datetime]]:
    """Closest record at or before event_at (binary search on sorted rows)."""
    times = [parse_iso(r["recorded_at"]) for r in rows]
    idx = bisect_right(times, event_at) - 1
    return idx, (rows[idx] if idx >= 0 else None), times


def resolve_owner(event: dict[str, Any],
                  all_enrolled: dict[str, list[dict[str, Any]]]
                  ) -> Optional[dict[str, Any]]:
    """Nearest prior ENROLLED exposure across every version, or None.

    This single exposure decides which version owns the event, so an event
    can never be counted on two versions simultaneously.
    """
    rows = all_enrolled.get(event["user_key"], ())
    if not rows:
        return None
    event_at = event["occurred_at_dt"]
    times = [parse_iso(r["recorded_at"]) for r in rows]
    idx = bisect_right(times, event_at) - 1
    return rows[idx] if idx >= 0 else None


def classify_for_version(event: dict[str, Any], metric: MetricDef,
                         version_number: int,
                         all_enrolled: dict[str, list[dict[str, Any]]],
                         version_records: dict[str, list[dict[str, Any]]],
                         live_version_number: Optional[int],
                         ) -> dict[str, Any]:
    """Decide one event's fate for an analysis of one specific version.

    Returns ``{"kind": OWNED|NOT_OWNED|NO_VERSION|SCOPE_NO_EXPOSURE,
    "variant_key": ..., "exposure_id": ..., "reason": ...}``.

    Scope/ownership first (independent of the metric), then — only for the
    owning version — the time ordering, attribution window and value checks.
    ``live_version_number`` is the version that was the latest published one
    at the event's occurrence time (used to audit pure "ghost" events whose
    user has no exposure record at all).

    Audit scope for an unowned event (no prior enrolled exposure anywhere):
    the user has a record (enrolled or gate-miss) of THIS version, or this
    version was the live one when the event happened. When such a record
    exists but only after the event, the reason is
    ``event_before_exposure``; when a non-enrolled record precedes it, the
    reason is ``no_exposure``.
    """
    event_at = event["occurred_at_dt"]
    owner = resolve_owner(event, all_enrolled)

    if owner is not None:
        if owner["version_number"] != version_number:
            # Another version owns this event: entirely outside this scope.
            return {"kind": NOT_OWNED, "variant_key": None,
                    "exposure_id": None, "reason": None}
    else:
        rows = version_records.get(event["user_key"])
        if rows:
            _, prior, _ = _prior_record(rows, event_at)
            if prior is None:
                # The user belongs to this version, but this event predates
                # every exposure record they have for it.
                return {"kind": SCOPE_NO_EXPOSURE, "variant_key": None,
                        "exposure_id": None,
                        "reason": EVENT_BEFORE_EXPOSURE}
            if not prior["enrolled"]:
                return {"kind": SCOPE_NO_EXPOSURE, "variant_key": None,
                        "exposure_id": None, "reason": NO_EXPOSURE}
            # Defensive: an enrolled prior row should have made resolve_owner
            # return an owner; treat it as no_exposure if it did not.
            return {"kind": SCOPE_NO_EXPOSURE, "variant_key": None,
                    "exposure_id": None, "reason": NO_EXPOSURE}
        # No exposure record anywhere: audit against the then-live version.
        if live_version_number != version_number:
            return {"kind": NO_VERSION, "variant_key": None,
                    "exposure_id": None, "reason": None}
        return {"kind": SCOPE_NO_EXPOSURE, "variant_key": None,
                "exposure_id": None, "reason": NO_EXPOSURE}

    # This version owns the event; run the metric-level eligibility checks
    # against the owning exposure.
    if metric.attribution_window_seconds > 0:
        window_start = event_at - timedelta(
            seconds=metric.attribution_window_seconds)
        if parse_iso(owner["recorded_at"]) < window_start:
            return {"kind": OWNED, "variant_key": owner["variant_key"],
                    "exposure_id": owner["id"], "reason": OUT_OF_WINDOW}

    if metric.metric_type == "continuous" and not event["value_valid"]:
        return {"kind": OWNED, "variant_key": owner["variant_key"],
                "exposure_id": owner["id"], "reason": INVALID_VALUE}

    return {"kind": OWNED, "variant_key": owner["variant_key"],
            "exposure_id": owner["id"], "reason": "attributed"}


def attribute_event(event: dict[str, Any], metric: MetricDef,
                    user_enrolled: dict[str, list[dict[str, Any]]],
                    ) -> dict[str, Any]:
    """Ingestion-time preview for ONE metric version.

    Mirrors :func:`classify_for_version` when this version owns the event;
    events owned by another version (or with no prior exposure of this
    version) are reported as not attributable to this metric.
    """
    rows = user_enrolled.get(event["user_key"], ())
    if not rows:
        return {"attributed": False, "variant_key": None,
                "exposure_id": None, "reason": NO_EXPOSURE}
    event_at = event["occurred_at_dt"]
    times = [parse_iso(r["recorded_at"]) for r in rows]
    idx = bisect_right(times, event_at) - 1
    if idx < 0:
        return {"attributed": False, "variant_key": None,
                "exposure_id": None, "reason": EVENT_BEFORE_EXPOSURE}
    if metric.attribution_window_seconds > 0:
        window_start = event_at - timedelta(
            seconds=metric.attribution_window_seconds)
        if times[idx] < window_start:
            return {"attributed": False,
                    "variant_key": rows[idx]["variant_key"],
                    "exposure_id": rows[idx]["id"],
                    "reason": OUT_OF_WINDOW}
    if metric.metric_type == "continuous" and not event["value_valid"]:
        return {"attributed": False, "variant_key": rows[idx]["variant_key"],
                "exposure_id": rows[idx]["id"], "reason": INVALID_VALUE}
    return {"attributed": True, "variant_key": rows[idx]["variant_key"],
            "exposure_id": rows[idx]["id"], "reason": "attributed"}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _rate_ci(successes: int, n: int) -> tuple[Optional[float], Optional[float],
                                              Optional[float]]:
    """Wald normal-approximation interval for a binomial proportion."""
    if n <= 0:
        return None, None, None
    # Clamp defensively: with distinct converting users, 0 <= p <= 1.
    p = min(1.0, max(0.0, successes / n))
    se = math.sqrt(max(0.0, p * (1 - p) / n))
    lower = max(0.0, p - Z95 * se)
    upper = min(1.0, p + Z95 * se)
    return p, lower, upper


def _log_rate_ratio_ci(t_c: int, n_t: int, t_0: int, n_0: int
                       ) -> tuple[Optional[float], Optional[float]]:
    """95% CI of the rate ratio (treatment/control) on the log scale.

    Undefined when either arm has zero conversions.
    """
    if t_c <= 0 or t_0 <= 0 or n_t <= 0 or n_0 <= 0:
        return None, None
    se = math.sqrt(1.0 / t_c - 1.0 / n_t + 1.0 / t_0 - 1.0 / n_0)
    log_rr = math.log(t_c / n_t) - math.log(t_0 / n_0)
    return math.exp(log_rr - Z95 * se), math.exp(log_rr + Z95 * se)


def _mean_ci(values: list[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    n = len(values)
    if n == 0:
        return None, None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None, None
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    if var <= 0:
        return mean, mean, mean
    half = Z95 * math.sqrt(var / n)
    return mean, mean - half, mean + half


def _welch_diff_ci(mean_t: float, var_t: float, n_t: int,
                   mean_0: float, var_0: float, n_0: int
                   ) -> tuple[Optional[float], Optional[float]]:
    """95% CI of (treatment mean - control mean), Welch (unpooled)."""
    if n_t < 2 or n_0 < 2:
        return None, None
    se = math.sqrt(var_t / n_t + var_0 / n_0)
    diff = mean_t - mean_0
    return diff - Z95 * se, diff + Z95 * se


def _favorable(delta: float, direction: str) -> bool:
    return delta > 0 if direction == "maximize" else delta < 0


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze(metric: MetricDef,
            variant_keys: list[str],
            control_key: str,
            configured_shares: dict[str, float],
            events: list[dict[str, Any]],
            version_exposures: list[dict[str, Any]],
            all_enrolled_exposures: list[dict[str, Any]],
            published_versions: list[tuple[str, int]],
            ) -> dict[str, Any]:
    """Compute the full metric analysis for one version/metric.

    Parameters
    ----------
    version_exposures:
        ALL exposure rows of the analyzed version (enrolled and gate-miss),
        used for the denominator and for in-scope ``no_exposure`` auditing.
    all_enrolled_exposures:
        ENROLLED exposures across every version of the experiment; the
        nearest prior one globally decides which version owns each event.
    published_versions:
        ``(published_at_iso, version_number)`` for every published version,
        used to audit events of users with no exposure record at all
        against the version that was live when they happened.

    ``events`` are already filtered to the metric's event_name and query
    time range, and deduplicated (one row per event_key by schema).
    """
    is_control = {k: (k == control_key) for k in variant_keys}
    expected_share = configured_shares
    version_number = metric.version_number

    enrolled_grouped = _group_by_user(version_exposures, enrolled_only=True)
    records_grouped = _group_by_user(version_exposures, enrolled_only=False)
    all_enrolled_grouped = _group_by_user(all_enrolled_exposures,
                                          enrolled_only=True)

    def live_number_at(event_at: datetime) -> Optional[int]:
        """Latest version published at or before the event (binary search)."""
        cut = event_at.astimezone(timezone.utc).isoformat()
        idx = bisect_right(published_versions, (cut, 10**18)) - 1
        return published_versions[idx][1] if idx >= 0 else None

    # Denominator: one distinct enrolled user per variant (their latest
    # enrolled exposure of THIS version decides the assignment).
    exposures_per_variant: dict[str, set[str]] = defaultdict(set)
    for user, rows in enrolled_grouped.items():
        exposures_per_variant[rows[-1]["variant_key"]].add(user)

    # Binary: distinct CONVERTING users (the same user reporting two
    # different event keys still converts at most once).
    converters_per_variant: dict[str, set[str]] = defaultdict(set)
    # Continuous: every valid attributed event contributes a value.
    values_per_variant: dict[str, list[float]] = defaultdict(list)
    # Distinct attributed event keys per variant (audit field).
    attributed_events_per_variant: dict[str, set[str]] = defaultdict(set)
    variant_excluded: dict[str, dict[str, int]] = {
        k: {EVENT_BEFORE_EXPOSURE: 0, OUT_OF_WINDOW: 0, INVALID_VALUE: 0}
        for k in variant_keys}
    global_excluded = {NO_EXPOSURE: 0, EVENT_BEFORE_EXPOSURE: 0,
                       OUT_OF_WINDOW: 0, INVALID_VALUE: 0}
    events_attributed_total = 0
    events_in_scope = 0
    in_scope_event_keys: set[str] = set()

    for event in events:
        verdict = classify_for_version(
            event, metric, version_number, all_enrolled_grouped,
            records_grouped, live_number_at(event["occurred_at_dt"]))
        kind = verdict["kind"]
        if kind == NOT_OWNED or kind == NO_VERSION:
            continue  # belongs to another version / outside this audit scope
        events_in_scope += 1
        in_scope_event_keys.add(event["event_key"])
        reason = verdict["reason"]
        if kind == SCOPE_NO_EXPOSURE:
            # Either no_exposure (gate-miss) or event_before_exposure.
            global_excluded[reason] += 1
            continue
        if reason in (OUT_OF_WINDOW, INVALID_VALUE):
            global_excluded[reason] += 1
            variant_key = verdict["variant_key"]
            if variant_key in variant_excluded:
                variant_excluded[variant_key][reason] += 1
            continue

        events_attributed_total += 1
        variant_key = verdict["variant_key"]
        attributed_events_per_variant[variant_key].add(event["event_key"])
        if metric.metric_type == "binary":
            converters_per_variant[variant_key].add(event["user_key"])
        else:
            values_per_variant[variant_key].append(float(event["value"]))

    # Control statistics
    if metric.metric_type == "binary":
        c0 = len(converters_per_variant.get(control_key, ()))
        n0 = len(exposures_per_variant.get(control_key, ()))
        p0, l0, u0 = _rate_ci(c0, n0)
        control = {"n": n0, "value": p0, "conversions": c0, "lower": l0,
                   "upper": u0}
    else:
        vals0 = values_per_variant.get(control_key, [])
        m0, l0, u0 = _mean_ci(vals0)
        var0 = (sum((x - m0) ** 2 for x in vals0) / (len(vals0) - 1)
                if len(vals0) >= 2 else None)
        control = {"n": len(vals0), "value": m0, "conversions": None,
                   "lower": l0, "upper": u0, "var": var0}

    rows_out: list[dict[str, Any]] = []
    total_exposures_used = sum(len(s) for s in exposures_per_variant.values())
    total_valid_samples = 0
    any_srm = False
    any_insufficient = False

    for key in variant_keys:
        exp_used = len(exposures_per_variant.get(key, ()))
        expected = expected_share[key]
        observed = (exp_used / total_exposures_used
                    if total_exposures_used else 0.0)
        rel_dev = (abs(observed - expected) / expected
                   if total_exposures_used > 0 and expected > 0 else None)
        # Zero exposures on the version: no sample-ratio conclusion is possible.
        srm = (rel_dev is not None
               and rel_dev > metric.srm_threshold
               and total_exposures_used > 0)
        any_srm = any_srm or srm

        lift_report: Optional[dict[str, Any]] = None
        if metric.metric_type == "binary":
            conv = len(converters_per_variant.get(key, ()))
            n = exp_used  # binary denominator: distinct exposed users
            p, lo, hi = _rate_ci(conv, n)
            total_valid_samples += n
            insufficient = n < metric.min_sample_size
            any_insufficient = any_insufficient or insufficient
            formula = (
                f"rate = converting_users({conv}) / exposed_users({n}); "
                f"95% CI = rate ± {Z95:.3f}·sqrt(rate·(1-rate)/n) [Wald]")
            if not is_control[key] and n > 0:
                rel = None if control["value"] in (None, 0) else (
                    p - control["value"]) / abs(control["value"])
                rr_lo, rr_hi = _log_rate_ratio_ci(conv, n,
                                                   control["conversions"],
                                                   control["n"])
                lift_ci = (None if rr_lo is None
                           else {"lower": rr_lo - 1, "upper": rr_hi - 1,
                                 "level": 0.95})
                favorable = (None if rel is None or control["value"] is None
                             else _favorable(p - control["value"],
                                             metric.direction))
                lift_report = {"relative": rel, "ci95": lift_ci,
                               "favorable": favorable}
                formula += ("; lift = (rate - control_rate)/|control_rate|; "
                            "lift 95% CI = exp(log rate-ratio ± 1.96·"
                            "sqrt(1/c-1/n+1/c0-1/n0)) - 1")
            value, ci_low, ci_high = p, lo, hi
            events_attr = len(attributed_events_per_variant.get(key, ()))
        else:
            vals = values_per_variant.get(key, [])
            n = len(vals)
            mean, lo, hi = _mean_ci(vals)
            var = (sum((x - mean) ** 2 for x in vals) / (n - 1)
                   if n >= 2 else None)
            total_valid_samples += n
            insufficient = n < metric.min_sample_size
            any_insufficient = any_insufficient or insufficient
            formula = (
                f"mean = sum(values)/n (n={n}); "
                f"95% CI = mean ± {Z95:.3f}·s/sqrt(n), "
                "s² = sample variance (n-1)")
            if not is_control[key] and n >= 1:
                rel = None if not control["value"] else (
                    mean - control["value"]) / abs(control["value"])
                diff_lo = diff_hi = None
                if n >= 2 and control["n"] >= 2 and control["var"] is not None:
                    dlo, dhi = _welch_diff_ci(mean, var, n,
                                              control["value"], control["var"],
                                              control["n"])
                    if control["value"]:
                        diff_lo = dlo / abs(control["value"])
                        diff_hi = dhi / abs(control["value"])
                lift_ci = ({"lower": diff_lo, "upper": diff_hi, "level": 0.95}
                           if diff_lo is not None else None)
                favorable = (None if control["value"] in (None, 0)
                             else _favorable(mean - control["value"],
                                             metric.direction))
                lift_report = {"relative": rel, "ci95": lift_ci,
                               "favorable": favorable}
                formula += ("; lift = (mean - control_mean)/|control_mean|; "
                            "lift 95% CI = Welch difference CI / |control_mean|")
            value, ci_low, ci_high = mean, lo, hi
            events_attr = n

        rows_out.append({
            "variant_key": key,
            "is_control": is_control[key],
            "exposures_used": exp_used,
            "valid_samples": n,
            "value": None if value is None else round(value, 8),
            "ci95": {"lower": None if ci_low is None else round(ci_low, 8),
                     "upper": None if ci_high is None else round(ci_high, 8),
                     "level": 0.95},
            "lift": None if lift_report is None else _round_lift(lift_report),
            "sample_ratio": {
                "expected_share": round(expected, 8),
                "observed_share": round(observed, 8),
                "relative_deviation": (None if rel_dev is None
                                       else round(rel_dev, 8)),
                "srm": srm,
            },
            "insufficient_sample": insufficient,
            "events_attributed": events_attr,
            "exclusions": variant_excluded[key],
            "formula": formula,
        })

    totals = {
        "exposures_used": total_exposures_used,
        "events_in_window": events_in_scope,
        "events_distinct": len(in_scope_event_keys),
        "duplicates": 0,
        "events_attributed": events_attributed_total,
        "valid_samples": total_valid_samples,
        "excluded": global_excluded,
        "srm": any_srm,
        "insufficient_sample": any_insufficient,
    }
    # reconciliation identity: in-window == attributed + excluded
    assert totals["events_in_window"] == (
        events_attributed_total + sum(global_excluded.values()))
    return {"variants": rows_out, "totals": totals}


def _round_lift(lift: dict[str, Any]) -> dict[str, Any]:
    out = dict(lift)
    if out.get("relative") is not None:
        out["relative"] = round(out["relative"], 8)
    if out.get("ci95") is not None:
        out["ci95"] = {k: (round(v, 8) if isinstance(v, float) else v)
                       for k, v in out["ci95"].items()}
    return out


FORMULAS = {
    "attribution": (
        "single closest prior ENROLLED exposure across all versions decides "
        "the owning version; event_at in [exposure_at, exposure_at + "
        "attribution_window_seconds]"),
    "version_ownership": (
        "owner_version = version_number of max(recorded_at) enrolled "
        "exposure with recorded_at <= event_at; events never count on two versions"),
    "binary_rate": "converting distinct users / exposed distinct users",
    "binary_ci": "Wald: p ± 1.96·sqrt(p(1-p)/n)",
    "binary_lift_ci": (
        "log rate-ratio: exp(ln(p_t/p_c) ± 1.96·"
        "sqrt(1/c_t-1/n_t+1/c_c-1/n_c)) - 1"),
    "continuous_mean": "sum(valid attributed values) / valid attributed events",
    "continuous_ci": "mean ± 1.96·s/sqrt(n), s² with denominator n-1",
    "continuous_lift_ci": "Welch unpooled difference CI / |control mean|",
    "srm": ("|observed_share - configured_share| / configured_share > "
            "srm_threshold; never flagged when the version has zero exposures"),
    "insufficient_sample": "valid_samples < min_sample_size",
}

ATTRIBUTION_REASONS = {
    "attributed": "event linked to the nearest prior enrolled exposure inside window",
    NO_EXPOSURE: ("no prior enrolled exposure for this user; the event is in "
                  "this version's audit scope but excluded from statistics"),
    EVENT_BEFORE_EXPOSURE: "event occurred before any exposure for this user",
    OUT_OF_WINDOW: "nearest prior exposure is older than the attribution window",
    INVALID_VALUE: ("continuous metric event carries no finite numeric value; "
                    "binary metrics ignore the value field"),
    "not_owned": ("the user's nearest prior enrolled exposure belongs to "
                  "another version; the event is audited on that version only"),
}


# ---------------------------------------------------------------------------
# Ingestion-time preview
# ---------------------------------------------------------------------------


def preview_attributions(event: dict[str, Any], metrics: list[MetricDef],
                         all_enrolled_exposures: list[dict[str, Any]]
                         ) -> list[dict[str, Any]]:
    """Verdict for every currently-defined metric listening on the event name.

    Best-effort convenience only; authoritative attribution happens in
    ``analyze`` over immutable rows. Uses the same cross-version ownership
    rule: only the metric defined on the version owning the event can be
    attributed; the rest are excluded with ``not_owned`` / ``no_exposure``.
    """
    grouped = _group_by_user(all_enrolled_exposures, enrolled_only=True)
    owner = resolve_owner(event, grouped)
    previews: list[dict[str, Any]] = []
    for metric in metrics:
        if owner is not None and owner["version_number"] != metric.version_number:
            previews.append({
                "metric_key": metric.metric_key,
                "version_number": metric.version_number,
                "status": "excluded",
                "variant_key": None,
                "exposure_id": None,
                "reason": "not_owned",
            })
            continue
        verdict = attribute_event(
            event, metric,
            _group_by_user(
                [r for r in all_enrolled_exposures
                 if r["version_number"] == metric.version_number],
                enrolled_only=True))
        previews.append({
            "metric_key": metric.metric_key,
            "version_number": metric.version_number,
            "status": "attributed" if verdict["attributed"] else "excluded",
            "variant_key": verdict["variant_key"],
            "exposure_id": verdict["exposure_id"],
            "reason": verdict["reason"],
        })
    return previews


def normalize_window(start: Optional[datetime],
                     end: Optional[datetime]) -> tuple[Optional[str], Optional[str]]:
    """Validate + UTC-normalize an analysis time range (half-open)."""
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start is not None and end is not None and not (start < end):
        raise ValueError("time range must satisfy start_at < end_at")
    return (to_iso(start) if start else None, to_iso(end) if end else None)
