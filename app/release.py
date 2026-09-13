"""Multi-metric release decisions: one primary metric plus guardrails under
Holm/Bonferroni multiple-comparison correction.

Everything here is a pure function over immutable rows (the as-of exposure
and event rows gathered by the router). A snapshot therefore depends only on
exposures ``recorded_at < cutoff`` and events ``occurred_at < cutoff``;
re-requesting the same plan + cutoff re-serves the stored frozen snapshot
and later events can never change it.

Population and attribution
--------------------------
* A user enters a metric's population only when their **attribution window
  has fully elapsed** by the cutoff: ``first_enrolled_exposure + window <=
  cutoff``. Users whose window is still open at the cutoff are excluded
  (they may still convert afterwards, so counting them as non-converters
  would bias the rates); they are reported as ``window_incomplete`` — per
  metric, because windows differ across metrics.
* Events are attributed with exactly the regular effect-analysis rules:
  the single closest prior enrolled exposure across **all** versions decides
  the owning version, then the metric's attribution window and value checks
  apply. Events owned by other versions never enter this snapshot.

Decision framework
------------------
All effects are **oriented** by the metric direction: ``θ = orient ·
(target − control)`` with ``orient = +1`` for maximize and ``−1`` for
minimize, so a positive θ always means the target moved in the favorable
direction. Thresholds are expressed in the same oriented units.

* **Primary metric** — superiority test. ``p = 1 − Φ(θ̂/SE)``. It passes
  when the point estimate reaches the minimum favorable effect
  (``threshold_gap = θ̂ − min_effect ≥ 0``) **and** the corrected p-value is
  below ``alpha``.
* **Guardrail metric** — violation test against the non-inferiority margin
  ``m``: ``p = Φ((θ̂ + m)/SE)`` (small p = significant harm beyond the
  margin). A guardrail is *violated* when its corrected p-value is below
  ``alpha``; it is *within bound* when the point estimate has not crossed
  the margin (``threshold_gap = θ̂ + m ≥ 0``) and it is not violated; in
  between (estimate beyond the margin but not significantly) it is
  inconclusive.
* **Correction** — the raw p-values of every *evaluable* plan metric
  (primary superiority + guardrail violations) form one family adjusted
  with Holm's step-down procedure or the Bonferroni method, at the plan's
  ``alpha``. Metrics without an evaluable statistic report null p-values
  and stay out of the family.

Overall decision:

* ``do_not_ship`` — any guardrail is significantly violated;
* ``ship`` — the primary passes and every guardrail is within bound;
* ``insufficient_evidence`` — everything else (primary inconclusive, or a
  guardrail whose estimate crossed the margin without reaching
  significance, or a metric that cannot be evaluated yet).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import metrics as metrics_mod
from .repository import MetricDef
from .sequential import ndtr
from .time_utils import parse_iso

Z95 = metrics_mod.Z95  # 0.975 standard-normal quantile

# Roles
PRIMARY = "primary"
GUARDRAIL = "guardrail"

# Decisions
SHIP = "ship"
DO_NOT_SHIP = "do_not_ship"
INSUFFICIENT = "insufficient_evidence"

# Correction methods
HOLM = "holm"
BONFERRONI = "bonferroni"

# Per-metric statuses
STATUS_PASS = "pass"                       # primary: threshold met + significant
STATUS_BELOW_THRESHOLD = "below_threshold"  # primary: estimate under min effect
STATUS_NOT_SIGNIFICANT = "not_significant"  # primary: threshold met, p >= alpha
STATUS_WITHIN_BOUND = "within_bound"       # guardrail: estimate inside margin
STATUS_CROSSED = "crossed_not_significant"  # guardrail: estimate beyond margin
STATUS_VIOLATED = "violated"               # guardrail: significantly beyond margin
STATUS_NOT_EVALUABLE = "not_evaluable"

# Extra exclusion reasons on top of the regular attribution ones
WINDOW_INCOMPLETE = "window_incomplete_user"
VARIANT_NOT_IN_PLAN = "variant_not_in_plan"


# ---------------------------------------------------------------------------
# Multiple-comparison correction
# ---------------------------------------------------------------------------


def adjust_pvalues(p_values: list[float], method: str) -> list[float]:
    """Adjusted p-values for one family of raw p-values.

    Bonferroni: ``min(1, k·p_i)``. Holm (step-down): order
    ``p_(1) <= ... <= p_(k)`` and take
    ``p_adj,(i) = max_{j<=i} min(1, (k-j+1)·p_(j))``.
    """
    k = len(p_values)
    if k == 0:
        return []
    if method == BONFERRONI:
        return [min(1.0, k * p) for p in p_values]
    order = sorted(range(k), key=lambda i: (p_values[i], i))
    adjusted = [0.0] * k
    running = 0.0
    for rank, idx in enumerate(order):
        value = min(1.0, (k - rank) * p_values[idx])
        running = max(running, value)
        adjusted[idx] = running
    return adjusted


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _round(v: Optional[float], digits: int = 8) -> Optional[float]:
    return None if v is None else round(v, digits)


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.6f}"


def _moments(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    """(mean, sample variance with denominator n-1) of a value list."""
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, var


# ---------------------------------------------------------------------------
# Per-metric evaluation
# ---------------------------------------------------------------------------


def _evaluate_metric(metric: MetricDef, role: str, threshold: float,
                     control_key: str, target_key: str,
                     events: list[dict[str, Any]],
                     version_exposures: list[dict[str, Any]],
                     all_enrolled_exposures: list[dict[str, Any]],
                     published_versions: list[tuple[str, int]],
                     cutoff: datetime) -> dict[str, Any]:
    """Statistics for one plan metric on the two plan arms.

    ``threshold`` is the oriented decision threshold: the primary's minimum
    favorable effect, or ``-margin`` for a guardrail. Every input row is
    strictly before the cutoff; users whose attribution window has not
    fully elapsed by the cutoff are excluded from both arms.
    """
    arms = (control_key, target_key)
    window = timedelta(seconds=metric.attribution_window_seconds)
    version_number = metric.version_number

    enrolled_grouped = metrics_mod._group_by_user(version_exposures,
                                                  enrolled_only=True)
    records_grouped = metrics_mod._group_by_user(version_exposures,
                                                 enrolled_only=False)
    all_grouped = metrics_mod._group_by_user(all_enrolled_exposures,
                                             enrolled_only=True)

    def live_number_at(event_at: datetime) -> Optional[int]:
        cut = event_at.astimezone(timezone.utc).isoformat()
        idx = bisect_right(published_versions, (cut, 10 ** 18)) - 1
        return published_versions[idx][1] if idx >= 0 else None

    # ---- eligible population: window fully elapsed by the cutoff ---------
    # Assignment is deterministic within a frozen version, so the first
    # enrolled exposure fixes both the variant and the window anchor.
    eligible: dict[str, str] = {}
    incomplete_users = 0
    other_variant_users = 0
    for user, rows in enrolled_grouped.items():
        first = rows[0]
        variant = first["variant_key"]
        if variant not in arms:
            other_variant_users += 1
            continue
        t0 = parse_iso(first["recorded_at"])
        if t0 + window <= cutoff:
            eligible[user] = variant
        else:
            incomplete_users += 1

    # ---- events: regular cross-version attribution, then eligibility -----
    converters: dict[str, set[str]] = {a: set() for a in arms}
    values: dict[str, list[float]] = {a: [] for a in arms}
    exclusions = {metrics_mod.NO_EXPOSURE: 0,
                  metrics_mod.EVENT_BEFORE_EXPOSURE: 0,
                  metrics_mod.OUT_OF_WINDOW: 0,
                  metrics_mod.INVALID_VALUE: 0,
                  WINDOW_INCOMPLETE: 0,
                  VARIANT_NOT_IN_PLAN: 0}
    events_attributed = 0

    for event in events:
        verdict = metrics_mod.classify_for_version(
            event, metric, version_number, all_grouped, records_grouped,
            live_number_at(event["occurred_at_dt"]))
        kind = verdict["kind"]
        if kind in (metrics_mod.NOT_OWNED, metrics_mod.NO_VERSION):
            continue
        if kind == metrics_mod.SCOPE_NO_EXPOSURE:
            exclusions[verdict["reason"]] += 1
            continue
        reason = verdict["reason"]
        if reason in (metrics_mod.OUT_OF_WINDOW, metrics_mod.INVALID_VALUE):
            exclusions[reason] += 1
            continue
        variant = verdict["variant_key"]
        if variant not in arms:
            # Attributed to a third variant of the version: outside this
            # two-arm plan's statistics entirely.
            exclusions[VARIANT_NOT_IN_PLAN] += 1
            continue
        if event["user_key"] not in eligible:
            exclusions[WINDOW_INCOMPLETE] += 1
            continue
        events_attributed += 1
        if metric.metric_type == "binary":
            converters[variant].add(event["user_key"])
        else:
            values[variant].append(float(event["value"]))

    # ---- arm statistics ----------------------------------------------------
    n_c = sum(1 for v in eligible.values() if v == control_key)
    n_t = sum(1 for v in eligible.values() if v == target_key)
    orient = 1.0 if metric.direction == "maximize" else -1.0

    evaluable = True
    not_evaluable_reason: Optional[str] = None
    effect = se = None
    arm_rows: dict[str, dict[str, Any]] = {}

    if metric.metric_type == "binary":
        c_c, c_t = len(converters[control_key]), len(converters[target_key])
        p_c = c_c / n_c if n_c > 0 else None
        p_t = c_t / n_t if n_t > 0 else None
        arm_rows = {
            "control": {"variant_key": control_key, "role": "control",
                        "users": n_c, "conversions": c_c,
                        "observations": None, "value": _round(p_c)},
            "target": {"variant_key": target_key, "role": "target",
                       "users": n_t, "conversions": c_t,
                       "observations": None, "value": _round(p_t)},
        }
        if n_c < 1 or n_t < 1:
            evaluable, not_evaluable_reason = False, "empty_arm"
        else:
            se = math.sqrt(p_c * (1 - p_c) / n_c + p_t * (1 - p_t) / n_t)
            if se <= 0.0:
                evaluable, not_evaluable_reason = False, "zero_variance"
            else:
                effect = orient * (p_t - p_c)
        stat_formula = (
            f"p_c = {c_c}/{n_c} = {_fmt(p_c)}, p_t = {c_t}/{n_t} = "
            f"{_fmt(p_t)}; SE = sqrt(p_c(1-p_c)/n_c + p_t(1-p_t)/n_t) = "
            f"{_fmt(se)}")
    else:
        vals_c, vals_t = values[control_key], values[target_key]
        k_c, k_t = len(vals_c), len(vals_t)
        mean_c, var_c = _moments(vals_c)
        mean_t, var_t = _moments(vals_t)
        arm_rows = {
            "control": {"variant_key": control_key, "role": "control",
                        "users": n_c, "conversions": None,
                        "observations": k_c, "value": _round(mean_c)},
            "target": {"variant_key": target_key, "role": "target",
                       "users": n_t, "conversions": None,
                       "observations": k_t, "value": _round(mean_t)},
        }
        if k_c < 2 or k_t < 2:
            evaluable, not_evaluable_reason = False, "insufficient_observations"
        else:
            se = math.sqrt(var_c / k_c + var_t / k_t)
            if se <= 0.0:
                evaluable, not_evaluable_reason = False, "zero_variance"
            else:
                effect = orient * (mean_t - mean_c)
        stat_formula = (
            f"mean_c = {_fmt(mean_c)} (k={k_c} valid events), mean_t = "
            f"{_fmt(mean_t)} (k={k_t}); SE = sqrt(s_c²/k_c + s_t²/k_t) = "
            f"{_fmt(se)}")

    # ---- oriented effect, CI, p-value, threshold gap -----------------------
    ci_lo = ci_hi = p_raw = gap = None
    if evaluable:
        gap = effect - threshold
        ci_lo = effect - Z95 * se
        ci_hi = effect + Z95 * se
        if role == PRIMARY:
            p_raw = 1.0 - ndtr(effect / se)
            test_formula = (
                f"superiority p = 1 - Φ(θ̂/SE) = 1 - Φ({_fmt(effect)}/"
                f"{_fmt(se)}) = {_fmt(p_raw)}")
        else:
            margin = -threshold
            p_raw = ndtr((effect + margin) / se)
            test_formula = (
                f"violation p = Φ((θ̂ + m)/SE) = Φ(({_fmt(effect)} + "
                f"{_fmt(margin)})/{_fmt(se)}) = {_fmt(p_raw)}")
        formula = (
            f"{stat_formula}; oriented effect θ̂ = "
            f"{'+' if orient > 0 else '-'}1·(target - control) = "
            f"{_fmt(effect)}; 95% CI = θ̂ ± 1.96·SE = [{_fmt(ci_lo)}, "
            f"{_fmt(ci_hi)}]; {test_formula}; threshold = {_fmt(threshold)}, "
            f"gap = θ̂ - threshold = {_fmt(gap)}")
    else:
        formula = (
            f"{stat_formula}; not evaluable: {not_evaluable_reason} — the "
            "metric contributes no p-value to the correction family and "
            "cannot clear its threshold")

    return {
        "metric_key": metric.metric_key,
        "role": role,
        "metric_type": metric.metric_type,
        "direction": metric.direction,
        "event_name": metric.event_name,
        "attribution_window_seconds": metric.attribution_window_seconds,
        "arms": arm_rows,
        "effect": _round(effect),
        "standard_error": _round(se),
        "ci95": {"lower": _round(ci_lo), "upper": _round(ci_hi),
                 "level": 0.95},
        "threshold": _round(threshold),
        "threshold_gap": _round(gap),
        "p_value": _round(p_raw),
        "p_adjusted": None,  # filled after the family-wise correction
        "evaluable": evaluable,
        "not_evaluable_reason": not_evaluable_reason,
        "status": STATUS_NOT_EVALUABLE,  # refined after the correction
        "events_attributed": events_attributed,
        "exclusions": exclusions,
        "excluded_users": {"window_incomplete": incomplete_users,
                           "other_variant": other_variant_users},
        "formula": formula,
        # Unrounded values for the decision logic; stripped before output.
        "_raw": {"gap": gap, "p": p_raw},
    }


# ---------------------------------------------------------------------------
# Full snapshot evaluation
# ---------------------------------------------------------------------------


def evaluate_snapshot(plan: dict[str, Any], metrics: dict[str, MetricDef],
                      events_by_name: dict[str, list[dict[str, Any]]],
                      version_exposures: list[dict[str, Any]],
                      all_enrolled_exposures: list[dict[str, Any]],
                      published_versions: list[tuple[str, int]],
                      cutoff: datetime) -> dict[str, Any]:
    """Evaluate every plan metric as-of the cutoff and derive the decision.

    ``plan`` carries the variant pair, the primary metric key with its
    minimum favorable effect, the guardrails with their non-inferiority
    margins, the correction method and alpha. ``metrics`` maps metric_key
    to its immutable definition; ``events_by_name`` holds the deduplicated
    events (``occurred_at < cutoff``) for every event name the plan listens
    to. All exposure rows are ``recorded_at < cutoff``.
    """
    control_key = plan["control_variant_key"]
    target_key = plan["target_variant_key"]
    alpha = plan["alpha"]
    method = plan["correction"]

    specs: list[tuple[str, str, float]] = [
        (plan["primary_metric_key"], PRIMARY, plan["primary_min_effect"])]
    for g in plan["guardrails"]:
        specs.append((g["metric_key"], GUARDRAIL,
                      -g["non_inferiority_margin"]))

    results: list[dict[str, Any]] = []
    for metric_key, role, threshold in specs:
        metric = metrics[metric_key]
        results.append(_evaluate_metric(
            metric, role, threshold, control_key, target_key,
            events_by_name.get(metric.event_name, []),
            version_exposures, all_enrolled_exposures,
            published_versions, cutoff))

    # ---- family-wise correction over the evaluable metrics ----------------
    evaluable_idx = [i for i, r in enumerate(results) if r["evaluable"]]
    raw = [results[i]["_raw"]["p"] for i in evaluable_idx]
    adjusted = adjust_pvalues(raw, method)
    adjusted_by_idx = dict(zip(evaluable_idx, adjusted))
    for i, adj in adjusted_by_idx.items():
        results[i]["p_adjusted"] = _round(adj)

    # ---- per-metric statuses ------------------------------------------------
    for i, r in enumerate(results):
        if not r["evaluable"]:
            r["status"] = STATUS_NOT_EVALUABLE
            continue
        gap = r["_raw"]["gap"]
        p_adj = adjusted_by_idx[i]
        if r["role"] == PRIMARY:
            if gap < 0.0:
                r["status"] = STATUS_BELOW_THRESHOLD
            elif p_adj >= alpha:
                r["status"] = STATUS_NOT_SIGNIFICANT
            else:
                r["status"] = STATUS_PASS
        else:
            if p_adj < alpha:
                r["status"] = STATUS_VIOLATED
            elif gap < 0.0:
                r["status"] = STATUS_CROSSED
            else:
                r["status"] = STATUS_WITHIN_BOUND

    # ---- overall decision ----------------------------------------------------
    primary = results[0]
    guardrails = results[1:]
    violated = [r for r in guardrails if r["status"] == STATUS_VIOLATED]

    if violated:
        decision = DO_NOT_SHIP
        reasons = [f"guardrail_violated:{r['metric_key']}" for r in violated]
    elif (primary["status"] == STATUS_PASS
          and all(r["status"] == STATUS_WITHIN_BOUND for r in guardrails)):
        decision = SHIP
        reasons = (["primary_effect_threshold_met",
                    "primary_significant_after_correction"]
                   + [f"guardrail_within_bound:{r['metric_key']}"
                      for r in guardrails])
    else:
        decision = INSUFFICIENT
        reasons = []
        if primary["status"] == STATUS_NOT_EVALUABLE:
            reasons.append("primary_not_evaluable:"
                           f"{primary['not_evaluable_reason']}")
        elif primary["status"] == STATUS_BELOW_THRESHOLD:
            reasons.append("primary_effect_below_threshold")
        elif primary["status"] == STATUS_NOT_SIGNIFICANT:
            reasons.append("primary_not_significant_after_correction")
        for r in guardrails:
            if r["status"] == STATUS_NOT_EVALUABLE:
                reasons.append(f"guardrail_not_evaluable:{r['metric_key']}:"
                               f"{r['not_evaluable_reason']}")
            elif r["status"] == STATUS_CROSSED:
                reasons.append(
                    f"guardrail_crossed_bound_not_significant:"
                    f"{r['metric_key']}")

    # ---- decision formula with the actual correction arithmetic ------------
    if evaluable_idx:
        ordered = sorted(
            ((results[i]["_raw"]["p"], results[i]["metric_key"])
             for i in evaluable_idx), key=lambda t: (t[0], t[1]))
        raw_desc = ", ".join(f"{key}={p:.6f}" for p, key in ordered)
        if method == BONFERRONI:
            corr_desc = (f"Bonferroni: p_adj = min(1, k·p) with k="
                         f"{len(evaluable_idx)}")
        else:
            corr_desc = (f"Holm: sorted raw p ascending, p_adj,(i) = "
                         f"max over j<=i of min(1, (k-j+1)·p_(j)) with k="
                         f"{len(evaluable_idx)}")
        correction_formula = (
            f"family of k={len(evaluable_idx)} evaluable metric(s), raw "
            f"p-values [{raw_desc}]; {corr_desc}; significance level "
            f"alpha={alpha:g}")
    else:
        correction_formula = (
            "no evaluable metric in the family — no correction could be "
            "applied and no threshold can be cleared")

    decision_formula = (
        f"{correction_formula}; decision rule: do_not_ship if any guardrail "
        f"violated (adjusted violation p < alpha), else ship if primary "
        f"passes (gap >= 0 and adjusted p < alpha) and every guardrail is "
        f"within bound (gap >= 0), else insufficient_evidence -> {decision}")

    for r in results:
        del r["_raw"]

    return {
        "metrics": results,
        "decision": {
            "decision": decision,
            "reasons": reasons,
            "correction": method,
            "alpha": alpha,
            "family_size": len(evaluable_idx),
            "formula": decision_formula,
        },
    }


RELEASE_FORMULAS = {
    "orientation": (
        "θ̂ = orient·(target - control) with orient = +1 for maximize and "
        "-1 for minimize metrics; positive θ̂ is always favorable, and every "
        "threshold is expressed in these oriented units"),
    "eligibility": (
        "a user counts only when first_enrolled_exposure + "
        "attribution_window_seconds <= cutoff; users whose attribution "
        "window is still open at the cutoff are excluded from both arms "
        "(window_incomplete_user) so not-yet-convertible users never bias "
        "the rates"),
    "binary_effect": (
        "θ̂ = orient·(p_t - p_c), p = converting distinct eligible users / "
        "eligible users; SE = sqrt(p_c(1-p_c)/n_c + p_t(1-p_t)/n_t)"),
    "continuous_effect": (
        "θ̂ = orient·(mean_t - mean_c) over valid attributed events; "
        "SE = sqrt(s_c²/k_c + s_t²/k_t), sample variance with denominator "
        "n-1, k = valid observations per arm"),
    "ci95": "oriented effect 95% CI: θ̂ ± 1.96·SE",
    "primary_test": (
        "one-sided superiority p = 1 - Φ(θ̂/SE); the primary passes when "
        "θ̂ >= min_favorable_effect (threshold_gap >= 0) and the corrected "
        "p < alpha"),
    "guardrail_test": (
        "one-sided violation p = Φ((θ̂ + margin)/SE) against the "
        "non-inferiority bound θ = -margin; violated when the corrected "
        "p < alpha, within bound when θ̂ >= -margin and not violated, "
        "otherwise inconclusive"),
    "correction": (
        "the raw p-values of all evaluable plan metrics (primary "
        "superiority + guardrail violations) form one family: Bonferroni "
        "p_adj = min(1, k·p); Holm p_adj,(i) = max_{j<=i} min(1, "
        "(k-j+1)·p_(j)) over ascending raw p-values"),
    "decision": (
        "do_not_ship: any guardrail significantly violated; ship: primary "
        "passes and every guardrail within bound; otherwise "
        "insufficient_evidence"),
    "attribution": (
        "events use the regular cross-version ownership rule: the single "
        "closest prior enrolled exposure across all versions decides the "
        "owning version, then the metric attribution window and value "
        "checks apply; only exposures recorded before and events occurred "
        "before the cutoff are read"),
    "exclusions": (
        "no_exposure / event_before_exposure / out_of_window / "
        "invalid_value follow the regular attribution audit; "
        "window_incomplete_user: event from a user whose attribution "
        "window had not elapsed by the cutoff; variant_not_in_plan: event "
        "attributed to a variant outside the plan's control/target pair"),
}
