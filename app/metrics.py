"""Metric attribution and effect analysis.

Pure-ish logic over immutable rows (exposures, metric defs, result events):

* ``attribute_event`` — where one result event lands for one metric: the
  closest *prior* enrolled exposure of the same user on the metric's version,
  inside the attribution window.
* ``analyze`` — per-variant samples, conversion rate / mean, relative lift
  vs. control with 95% confidence intervals, sample-ratio-mismatch and
  insufficient-sample flags, plus full audit counts and formulas.

Attribution is (re)computed at query time from immutable rows, so repeating
the same query over the same time range always yields the same result, and
publishing a new version can never alter an analysis pinned to an older
version (every join is scoped by ``version_number``).
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

EXCLUDED_REASONS = (EVENT_BEFORE_EXPOSURE, OUT_OF_WINDOW, INVALID_VALUE)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _user_enrolled_exposures(exposures: list[dict[str, Any]]
                             ) -> dict[str, list[dict[str, Any]]]:
    """user_key -> enrolled exposures sorted by (recorded_at, id).

    A user normally has one exposure per version (idempotency), but several
    idempotency keys for the same user are allowed; the latest one at the
    event time is the operative assignment.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in exposures:
        grouped[row["user_key"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: (parse_iso(r["recorded_at"]), r["id"]))
    return grouped


def attribute_event(event: dict[str, Any], metric: MetricDef,
                    user_exposures: dict[str, list[dict[str, Any]]],
                    ) -> dict[str, Any]:
    """Attribute one event for one metric.

    Returns ``{"attributed": bool, "variant_key": str|None,
    "exposure_id": int|None, "reason": str}``.

    Resolution order: prior enrolled exposure on the metric version; the
    most recent one; inside the attribution window; then value validity
    (continuous metrics require a finite numeric value; binary metrics
    count the event itself and ignore the value field).
    """
    rows = user_exposures.get(event["user_key"], ())
    if not rows:
        return {"attributed": False, "variant_key": None,
                "exposure_id": None, "reason": NO_EXPOSURE}

    event_at = event["occurred_at_dt"]
    times = [parse_iso(r["recorded_at"]) for r in rows]

    # closest exposure strictly at or before the event ([exposure, event] order)
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
                "exposure_id": rows[idx]["id"],
                "reason": INVALID_VALUE}

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
    p = successes / n
    se = math.sqrt(p * (1 - p) / n)
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


def analyze(loaded: LoadedVersion, metric: MetricDef,
            events: list[dict[str, Any]],
            exposures: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the full metric analysis for one version/metric.

    ``events`` are already filtered to the metric's event_name and query
    time range, and deduplicated (one row per event_key by schema).
    """
    config = loaded.config
    variants = sorted(config.variants, key=lambda v: v.key)
    variant_keys = [v.key for v in variants]
    is_control = {v.key: v.is_control for v in variants}
    control_key = config.control_variant_key
    expected_share = {v.key: v.percentage / 100.0 for v in variants}

    user_exposures = _user_enrolled_exposures(exposures)

    # Exposures used: one distinct user per variant (latest prior enrolled
    # exposure relative to each event time is irrelevant for the denominator;
    # a user's assignment is their latest enrolled exposure overall).
    exposures_per_variant: dict[str, set[str]] = defaultdict(set)
    for user, rows in user_exposures.items():
        exposures_per_variant[rows[-1]["variant_key"]].add(user)

    # Event bookkeeping
    attributed_per_variant: dict[str, set[str]] = defaultdict(set)  # binary: distinct event keys
    values_per_variant: dict[str, list[float]] = defaultdict(list)  # continuous
    variant_excluded: dict[str, dict[str, int]] = {
        k: {EVENT_BEFORE_EXPOSURE: 0, OUT_OF_WINDOW: 0, INVALID_VALUE: 0}
        for k in variant_keys}
    global_excluded = {NO_EXPOSURE: 0, EVENT_BEFORE_EXPOSURE: 0,
                       OUT_OF_WINDOW: 0, INVALID_VALUE: 0}
    events_attributed_total = 0

    for event in events:
        verdict = attribute_event(event, metric, user_exposures)
        if not verdict["attributed"]:
            reason = verdict["reason"]
            global_excluded[reason] = global_excluded.get(reason, 0) + 1
            variant_key = verdict.get("variant_key")
            if variant_key in variant_excluded and reason in EXCLUDED_REASONS:
                # out_of_window / invalid-value are attributable to a variant;
                # event_before_exposure has no variant yet.
                variant_excluded[variant_key][reason] += 1
            continue
        events_attributed_total += 1
        variant_key = verdict["variant_key"]
        attributed_per_variant[variant_key].add(event["event_key"])
        if metric.metric_type == "continuous":
            values_per_variant[variant_key].append(float(event["value"]))

    # Control statistics
    control = None
    if metric.metric_type == "binary":
        c0 = len(attributed_per_variant.get(control_key, ()))
        n0 = len(exposures_per_variant.get(control_key, ()))
        p0, l0, u0 = _rate_ci(c0, n0)
        control = {"n": n0, "value": p0, "conversions": c0, "lower": l0,
                   "upper": u0, "values": None}
    else:
        vals0 = values_per_variant.get(control_key, [])
        m0, l0, u0 = _mean_ci(vals0)
        var0 = (sum((x - m0) ** 2 for x in vals0) / (len(vals0) - 1)
                if len(vals0) >= 2 else None)
        control = {"n": len(vals0), "value": m0, "conversions": None,
                   "lower": l0, "upper": u0, "values": vals0, "var": var0}

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
        rel_dev = (abs(observed - expected) / expected if expected > 0 else None)
        srm = rel_dev is not None and rel_dev > metric.srm_threshold
        any_srm = any_srm or srm

        lift_report: Optional[dict[str, Any]] = None
        if metric.metric_type == "binary":
            conv = len(attributed_per_variant.get(key, ()))
            n = exp_used  # binary denominator: distinct exposed users
            p, lo, hi = _rate_ci(conv, n)
            total_valid_samples += n
            insufficient = n < metric.min_sample_size
            any_insufficient = any_insufficient or insufficient
            formula = (
                f"rate = conversions({conv}) / exposed_users({n}); "
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
            events_attr = conv
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

    distinct_event_keys = {e["event_key"] for e in events}
    totals = {
        "exposures_used": total_exposures_used,
        "events_in_window": len(events),
        "events_distinct": len(distinct_event_keys),
        "duplicates": len(events) - len(distinct_event_keys),
        "events_attributed": events_attributed_total,
        "valid_samples": total_valid_samples,
        "excluded": global_excluded,
        "srm": any_srm,
        "insufficient_sample": any_insufficient,
    }
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
        "nearest prior enrolled exposure of the same user & version; "
        "event_at in [exposure_at, exposure_at + attribution_window_seconds]"),
    "binary_rate": "conversions (distinct attributed users) / exposed users",
    "binary_ci": "Wald: p ± 1.96·sqrt(p(1-p)/n)",
    "binary_lift_ci": (
        "log rate-ratio: exp(ln(p_t/p_c) ± 1.96·"
        "sqrt(1/c_t-1/n_t+1/c_c-1/n_c)) - 1"),
    "continuous_mean": "sum(valid attributed values) / valid attributed events",
    "continuous_ci": "mean ± 1.96·s/sqrt(n), s² with denominator n-1",
    "continuous_lift_ci": "Welch unpooled difference CI / |control mean|",
    "srm": "|observed_share - configured_share| / configured_share > srm_threshold",
    "insufficient_sample": "valid_samples < min_sample_size",
}

ATTRIBUTION_REASONS = {
    "attributed": "event linked to nearest prior enrolled exposure inside window",
    NO_EXPOSURE: "no enrolled exposure for this user on the metric version",
    EVENT_BEFORE_EXPOSURE: "event occurred before any exposure for this user",
    OUT_OF_WINDOW: "nearest prior exposure is older than the attribution window",
    INVALID_VALUE: ("continuous metric event carries no finite numeric value; "
                    "binary metrics ignore the value field"),
}


# ---------------------------------------------------------------------------
# Ingestion-time preview
# ---------------------------------------------------------------------------


def preview_attributions(event: dict[str, Any], metrics: list[MetricDef],
                         exposures_by_version: dict[int, list[dict[str, Any]]]
                         ) -> list[dict[str, Any]]:
    """Verdict for every currently-defined metric listening on the event name.

    Best-effort convenience only; authoritative attribution happens in
    ``analyze`` over immutable rows.
    """
    previews: list[dict[str, Any]] = []
    for metric in metrics:
        grouped = _user_enrolled_exposures(
            exposures_by_version.get(metric.version_number, []))
        verdict = attribute_event(event, metric, grouped)
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
