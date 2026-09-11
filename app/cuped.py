"""CUPED covariate adjustment for continuous metrics.

Everything here is a pure function over immutable rows (the as-of exposure
and event rows gathered by the router). A snapshot therefore depends only on:

* **covariate** events strictly *before the user's first enrolled exposure*
  of the analyzed version, inside the plan's fixed look-back window
  ``[first_exposure - W, first_exposure)`` — events at or after the exposure
  are never read, so treatment-period data cannot leak into ``X``;
* **target** events ``occurred_at < cutoff`` attributed by exactly the same
  cross-version ownership / window / value rules as the regular analysis;
* exposures ``recorded_at < cutoff``.

Re-requesting the same plan + cutoff re-serves the stored frozen snapshot;
later events can never change it.

Statistical framework
----------------------
For every included user one covariate ``X`` (sum or mean of the pre-exposure
covariate events) and one target ``Y`` (sum or mean of the attributed target
events) is formed. The adjustment coefficient is estimated once, on **all
valid pairs pooled across variants**:

    θ = Cov(X, Y) / Var(X),   x̄ = mean(X) over the complete pairs
    Y_cuped_i = Y_i - θ · (X_i - x̄)

With the pooled centering the overall (weighted) mean and the unbiased
between-arm difference are preserved, while residual variance drops by the
fraction of outcome variance explained by the pre-exposure covariate.
Per-variant means, Welch mean-difference 95% CIs and variance-reduction
rates are reported for both raw and adjusted series. When the adjustment
cannot be estimated (zero covariate variance / too few complete pairs) or
fails to reduce variance, the anomaly is flagged and the raw analysis —
which is always computed on the same paired sample — is never overwritten.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta, timezone
from typing import Any, Optional

from . import metrics as metrics_mod
from .repository import MetricDef
from .time_utils import parse_iso

Z95 = metrics_mod.Z95  # 0.975 standard-normal quantile

# Anomaly codes
ZERO_COVARIATE_VARIANCE = "zero_covariate_variance"
INSUFFICIENT_SAMPLE = "insufficient_sample"
ADJUSTED_VARIANCE_INCREASED = "adjusted_variance_increased"

EXCLUDE = "exclude"
POPULATION_MEAN = "population_mean"


# ---------------------------------------------------------------------------
# Small statistics helpers (stdlib math only; sample variance denominator n-1)
# ---------------------------------------------------------------------------


def _moments(values: list[float]) -> tuple[int, float, Optional[float]]:
    n = len(values)
    if n == 0:
        return 0, None, None
    mean = sum(values) / n
    if n < 2:
        return n, mean, None
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    return n, mean, var


def _mean_ci(mean: Optional[float], var: Optional[float], n: int
             ) -> tuple[Optional[float], Optional[float]]:
    if mean is None or n < 2 or var is None:
        return None, None
    half = Z95 * (var / n) ** 0.5
    return mean - half, mean + half


def _welch_diff_ci(diff: float, var_t: Optional[float], n_t: int,
                   var_c: Optional[float], n_c: int
                   ) -> tuple[Optional[float], Optional[float]]:
    if var_t is None or var_c is None or n_t < 2 or n_c < 2:
        return None, None
    half = Z95 * (var_t / n_t + var_c / n_c) ** 0.5
    return diff - half, diff + half


def _vr(numerator_var: Optional[float], denominator_var: Optional[float]
        ) -> Optional[float]:
    """Variance-reduction rate 1 - adjusted/raw; null when not definable."""
    if numerator_var is None or denominator_var is None or denominator_var <= 0.0:
        return None
    return 1.0 - numerator_var / denominator_var


def _round(v: Optional[float], digits: int = 8) -> Optional[float]:
    return None if v is None else round(v, digits)


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.6f}"


def _ci(lo: Optional[float], hi: Optional[float]) -> dict[str, Any]:
    return {"lower": _round(lo), "upper": _round(hi), "level": 0.95}


# ---------------------------------------------------------------------------
# Snapshot evaluation
# ---------------------------------------------------------------------------


def evaluate_snapshot(metric: MetricDef, plan: dict[str, Any],
                      variant_keys: list[str], control_key: str,
                      target_events: list[dict[str, Any]],
                      covariate_events: list[dict[str, Any]],
                      version_exposures: list[dict[str, Any]],
                      all_enrolled_exposures: list[dict[str, Any]],
                      published_versions: list[tuple[str, int]]
                      ) -> dict[str, Any]:
    """Build the paired sample and compute the full CUPED snapshot.

    See the module docstring for the leakage rules. ``target_events`` and
    ``covariate_events`` are already deduplicated and filtered to
    ``occurred_at < cutoff``; every exposure row is likewise ``< cutoff``.
    """
    from bisect import bisect_right

    version_number = metric.version_number
    window = timedelta(seconds=plan["preexposure_window_seconds"])
    target_agg = plan["target_aggregation"]
    cov_agg = plan["covariate_aggregation"]
    fill_missing = plan["missing_covariate_policy"] == POPULATION_MEAN

    enrolled_grouped = metrics_mod._group_by_user(version_exposures,
                                                  enrolled_only=True)
    records_grouped = metrics_mod._group_by_user(version_exposures,
                                                 enrolled_only=False)
    all_grouped = metrics_mod._group_by_user(all_enrolled_exposures,
                                             enrolled_only=True)

    def live_number_at(event_at) -> Optional[int]:
        cut = event_at.astimezone(timezone.utc).isoformat()
        idx = bisect_right(published_versions, (cut, 10 ** 18)) - 1
        return published_versions[idx][1] if idx >= 0 else None

    # ---- enrolled population: one first enrolled exposure per user --------
    # Assignment is deterministic within a frozen version, so the first and
    # last enrolled exposure always carry the same variant; the first one is
    # the leakage-safe anchor for the covariate window.
    first_exposure: dict[str, dict[str, Any]] = {}
    enrolled_per_variant: dict[str, set[str]] = defaultdict(set)
    for user, rows in enrolled_grouped.items():
        first = rows[0]
        first_exposure[user] = first
        enrolled_per_variant[first["variant_key"]].add(user)

    # ---- target Y: same classifier as the regular effect analysis --------
    target_values: dict[str, list[float]] = defaultdict(list)
    target_variant: dict[str, str] = {}
    target_excluded = {"no_exposure": 0, "event_before_exposure": 0,
                       "out_of_window": 0, "invalid_value": 0}
    target_attributed_events = 0

    for event in target_events:
        verdict = metrics_mod.classify_for_version(
            event, metric, version_number, all_grouped, records_grouped,
            live_number_at(event["occurred_at_dt"]))
        kind = verdict["kind"]
        if kind in (metrics_mod.NOT_OWNED, metrics_mod.NO_VERSION):
            continue
        if kind == metrics_mod.SCOPE_NO_EXPOSURE:
            target_excluded[verdict["reason"]] = \
                target_excluded.get(verdict["reason"], 0) + 1
            continue
        reason = verdict["reason"]
        if reason in (metrics_mod.OUT_OF_WINDOW, metrics_mod.INVALID_VALUE):
            target_excluded[reason] += 1
            continue

        target_attributed_events += 1
        user = event["user_key"]
        target_values[user].append(float(event["value"]))
        target_variant[user] = verdict["variant_key"]

    def _aggregate(values: list[float], how: str) -> float:
        return sum(values) if how == "sum" else sum(values) / len(values)

    users_with_target = set(target_values)

    # ---- covariate X: only events strictly before the first exposure -----
    cov_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in covariate_events:
        if event["user_key"] in first_exposure:
            cov_by_user[event["user_key"]].append(event)

    covariate_used_events = 0
    covariate_invalid_events = 0
    covariate_x: dict[str, float] = {}
    for user, exposure in first_exposure.items():
        t0 = parse_iso(exposure["recorded_at"])
        window_start = t0 - window
        vals: list[float] = []
        for event in cov_by_user.get(user, ()):
            at = event["occurred_at_dt"]
            if at < t0 and at >= window_start:
                if event["value_valid"]:
                    vals.append(float(event["value"]))
                else:
                    covariate_invalid_events += 1
        if vals:
            covariate_x[user] = _aggregate(vals, cov_agg)
            covariate_used_events += len(vals)

    # ---- paired sample -----------------------------------------------------
    observed_users = users_with_target & set(covariate_x)
    missing_cov_users = users_with_target - set(covariate_x)
    no_target_users = set(first_exposure) - users_with_target

    excluded_missing = 0
    included: list[str] = []
    filled: set[str] = set()
    for user in first_exposure:
        if user not in users_with_target:
            continue  # counted as no_target below
        if user in covariate_x:
            included.append(user)
        elif fill_missing:
            included.append(user)
            filled.add(user)
        else:
            excluded_missing += 1

    # ---- theta from complete pairs (pooled over all variants) -------------
    complete = [u for u in included if u not in filled]
    n_complete = len(complete)
    theta: Optional[float] = None
    cov_xy: Optional[float] = None
    var_x: Optional[float] = None
    mean_x: Optional[float] = None
    estimable = False

    if n_complete >= 2:
        xs = [covariate_x[u] for u in complete]
        ys = [_aggregate(target_values[u], target_agg) for u in complete]
        mean_x = sum(xs) / n_complete
        mean_y = sum(ys) / n_complete
        cov_xy = sum((x - mean_x) * (y - mean_y)
                     for x, y in zip(xs, ys)) / (n_complete - 1)
        var_x = sum((x - mean_x) ** 2 for x in xs) / (n_complete - 1)
        if var_x > 0.0:
            theta = cov_xy / var_x
            estimable = True

    fill_value = mean_x  # population (pooled complete-pair) covariate mean

    def y_of(user: str) -> float:
        return _aggregate(target_values[user], target_agg)

    def x_of(user: str) -> float:
        return covariate_x.get(user, fill_value if fill_value is not None else 0.0)

    def y_adj_of(user: str) -> float:
        return y_of(user) - theta * (x_of(user) - mean_x)

    # ---- per-variant statistics -------------------------------------------
    variant_rows: list[dict[str, Any]] = []
    raw_stats: dict[str, dict[str, Any]] = {}
    adj_stats: dict[str, dict[str, Any]] = {}
    any_insufficient = False

    for key in variant_keys:
        users = [u for u in included
                 if (target_variant.get(u)
                     or first_exposure[u]["variant_key"]) == key]
        n = len(users)
        insufficient = n < metric.min_sample_size
        any_insufficient = any_insufficient or insufficient

        raw_vals = [y_of(u) for u in users]
        rn, rmean, rvar = _moments(raw_vals)
        rlo, rhi = _mean_ci(rmean, rvar, rn)
        raw_stats[key] = {"n": rn, "mean": rmean, "var": rvar,
                          "lo": rlo, "hi": rhi}

        if estimable:
            adj_vals = [y_adj_of(u) for u in users]
            an, amean, avar = _moments(adj_vals)
            alo, ahi = _mean_ci(amean, avar, an)
            vrr = _vr(avar, rvar)
            adj_stats[key] = {"n": an, "mean": amean, "var": avar,
                              "lo": alo, "hi": ahi, "vrr": vrr}
            shown_mean, shown_lo, shown_hi, shown_var = amean, alo, ahi, avar
        else:
            adj_stats[key] = None
            shown_mean, shown_lo, shown_hi, shown_var = rmean, rlo, rhi, rvar
            vrr = None

        filled_here = sum(1 for u in users if u in filled)
        if estimable:
            formula = (
                f"n={n} paired users; raw mean = "
                f"{_fmt(rmean)} (s²={_fmt(rvar)}); "
                f"adjusted mean = mean(Y - θ(X-x̄)) = {_fmt(shown_mean)}, "
                f"θ={theta:.6f}, x̄={mean_x:.6f}, adjusted s²={_fmt(shown_var)}; "
                f"95% CI = mean ± 1.96·s/√n; variance reduction "
                f"1-s²_cuped/s²_raw = {_fmt(vrr)}")
        else:
            formula = (
                f"n={n} paired users; raw mean = {_fmt(rmean)}; CUPED θ not "
                "estimable (zero covariate variance or fewer than two "
                "complete pairs) — raw analysis retained, 95% CI = "
                "mean ± 1.96·s/√n")

        variant_rows.append({
            "variant_key": key,
            "is_control": key == control_key,
            "enrolled_users": len(enrolled_per_variant.get(key, ())),
            "paired_users": n,
            "filled_covariate_users": filled_here,
            "raw_mean": _round(rmean),
            "adjusted_mean": _round(shown_mean if estimable else None),
            "raw_variance": _round(rvar),
            "adjusted_variance": _round(shown_var if estimable else None),
            "variance_reduction_rate": _round(vrr),
            "ci95": _ci(shown_lo, shown_hi),
            "raw_ci95": _ci(rlo, rhi),
            "insufficient_sample": insufficient,
            "formula": formula,
        })

    # ---- comparisons vs control ------------------------------------------
    rc = raw_stats[control_key]
    ac = adj_stats[control_key]
    comparison_rows: list[dict[str, Any]] = []
    adjusted_increased = False

    for key in variant_keys:
        if key == control_key:
            continue
        rt = raw_stats[key]
        raw_diff = (rt["mean"] - rc["mean"]
                    if rt["mean"] is not None and rc["mean"] is not None else None)
        raw_dlo = raw_dhi = None
        if raw_diff is not None:
            raw_dlo, raw_dhi = _welch_diff_ci(raw_diff, rt["var"], rt["n"],
                                              rc["var"], rc["n"])
        adj_diff = adj_dlo = adj_dhi = None
        cvrr = None
        if estimable:
            at = adj_stats[key]
            if at["mean"] is not None and ac["mean"] is not None:
                adj_diff = at["mean"] - ac["mean"]
                adj_dlo, adj_dhi = _welch_diff_ci(
                    adj_diff, at["var"], at["n"], ac["var"], ac["n"])
            se_raw_sq = (_se_sq(rt, rc))
            se_adj_sq = (_se_sq(at, ac))
            if se_raw_sq is not None and se_raw_sq > 0.0:
                cvrr = 1.0 - se_adj_sq / se_raw_sq
                if cvrr < 0.0:
                    adjusted_increased = True
            shown_diff, shown_lo, shown_hi = adj_diff, adj_dlo, adj_dhi
        else:
            shown_diff, shown_lo, shown_hi = raw_diff, raw_dlo, raw_dhi

        favorable = None
        if shown_diff is not None:
            favorable = (shown_diff > 0 if metric.direction == "maximize"
                         else shown_diff < 0)

        if estimable:
            formula = (
                f"raw Δ = {_fmt(raw_diff)} with Welch 95% CI "
                f"[{_fmt(raw_dlo)}, {_fmt(raw_dhi)}]; CUPED Δ = "
                f"{_fmt(adj_diff)} with Welch 95% CI "
                f"[{_fmt(adj_dlo)}, {_fmt(adj_dhi)}], "
                "Δ_cuped = mean_t(Y-θ(X-x̄)) - mean_c(Y-θ(X-x̄)); "
                f"difference-SE variance reduction = {_fmt(cvrr)}")
        else:
            formula = (
                f"raw Δ = {_fmt(raw_diff)} "
                "with Welch 95% CI Δ ± 1.96·sqrt(s_t²/n_t + s_c²/n_c); "
                "no CUPED adjustment available")

        comparison_rows.append({
            "variant_key": key,
            "raw_difference": _round(raw_diff),
            "adjusted_difference": _round(adj_diff if estimable else None),
            "raw_ci95": _ci(raw_dlo, raw_dhi),
            "ci95": _ci(shown_lo, shown_hi),
            "variance_reduction_rate": _round(cvrr),
            "favorable": favorable,
            "formula": formula,
        })

    # ---- pooled variance reduction over every included user --------------
    def _sse(stats: dict[str, Any]) -> float:
        return sum(max(0, s["n"] - 1) * (s["var"] or 0.0)
                   for s in stats.values() if s is not None)

    overall_vrr: Optional[float] = None
    if estimable:
        sse_raw = _sse(raw_stats)
        sse_adj = _sse(adj_stats)
        if sse_raw > 0.0:
            overall_vrr = 1.0 - sse_adj / sse_raw
            if overall_vrr < 0.0:
                adjusted_increased = True

    # ---- anomalies ---------------------------------------------------------
    anomalies: list[str] = []
    if not estimable:
        anomalies.append(ZERO_COVARIATE_VARIANCE)
    if any_insufficient:
        anomalies.append(INSUFFICIENT_SAMPLE)
    if estimable and adjusted_increased:
        anomalies.append(ADJUSTED_VARIANCE_INCREASED)

    # ---- theta block -------------------------------------------------------
    if estimable:
        theta_formula = (
            f"θ = Cov(X,Y)/Var(X) = {cov_xy:.6f}/{var_x:.6f} = {theta:.6f}; "
            f"x̄ = {mean_x:.6f} pooled over {n_complete} complete pairs "
            "(all variants); adjustment Y_cuped = Y - θ·(X - x̄)")
    elif n_complete >= 2:
        theta_formula = (
            f"θ not estimable: Var(X) = {var_x:.6g} over {n_complete} "
            "complete pairs — the pre-exposure covariate carries no "
            "information; raw analysis retained")
    else:
        theta_formula = (
            f"θ not estimable: only {n_complete} complete pair(s); at least "
            "two users with both target and covariate are required — raw "
            "analysis retained")

    theta_block = {
        "value": _round(theta),
        "estimable": estimable,
        "covariance": _round(cov_xy),
        "covariate_variance": _round(var_x),
        "covariate_mean": _round(mean_x),
        "paired_users": n_complete,
        "formula": theta_formula,
    }

    totals = {
        "enrolled_users": len(first_exposure),
        "users_with_target": len(users_with_target),
        "paired_users": len(included),
        "excluded_users": {
            "total": len(no_target_users) + excluded_missing,
            "no_target": len(no_target_users),
            "missing_covariate": excluded_missing,
        },
        "filled_covariate_users": len(filled),
        "target_events_attributed": target_attributed_events,
        "target_events_excluded": target_excluded,
        "covariate_events_used": covariate_used_events,
        "covariate_events_invalid_value": covariate_invalid_events,
        "overall_variance_reduction_rate": _round(overall_vrr),
    }

    return {
        "theta": theta_block,
        "variants": variant_rows,
        "comparisons": comparison_rows,
        "totals": totals,
        "anomalies": anomalies,
        "adjusted": estimable,
    }


def _se_sq(arm_t: Optional[dict[str, Any]], arm_c: Optional[dict[str, Any]]
           ) -> Optional[float]:
    """Squared Welch standard error of the mean difference for two arms."""
    if not arm_t or not arm_c or arm_t["var"] is None or arm_c["var"] is None:
        return None
    if arm_t["n"] < 2 or arm_c["n"] < 2:
        return None
    return arm_t["var"] / arm_t["n"] + arm_c["var"] / arm_c["n"]


CUPED_FORMULAS = {
    "covariate_window": (
        "X uses only covariate events with occurred_at in "
        "[first_enrolled_exposure - preexposure_window_seconds, "
        "first_enrolled_exposure); events at or after the exposure are never "
        "read (no treatment-period leakage)"),
    "theta": (
        "θ = Cov(X, Y) / Var(X) estimated once on all complete pairs pooled "
        "across variants; x̄ is the pooled complete-pair covariate mean"),
    "adjustment": "Y_cuped = Y - θ·(X - x̄); pooled centering preserves the overall mean",
    "aggregation": (
        "one X per user = sum/mean of the valid pre-exposure covariate "
        "events; one Y per user = sum/mean of the valid attributed target "
        "events before the cutoff"),
    "mean_ci": "per-variant 95% CI: mean ± 1.96·s/√n, sample variance denominator n-1",
    "difference_ci": (
        "95% Welch CI of the treatment-control difference: "
        "Δ ± 1.96·sqrt(s_t²/n_t + s_c²/n_c), for raw and adjusted series"),
    "variance_reduction": (
        "1 - variance_cuped/variance_raw; per variant on the user variance, "
        "per comparison on the difference's squared standard error, overall "
        "on pooled within-arm sums of squared errors"),
    "missing_covariate": (
        "exclude: users without a valid pre-exposure X are dropped; "
        "population_mean: they are retained with X filled with the pooled "
        "complete-pair mean, so adjustment leaves their Y unchanged"),
    "anomalies": (
        "zero_covariate_variance: θ undefined, raw analysis retained; "
        "insufficient_sample: a variant has fewer paired users than "
        "min_sample_size; adjusted_variance_increased: pooled residual "
        "variance grew — flags are reported and never overwrite the raw result"),
    "target_attribution": (
        "target events use the regular cross-version ownership rule: the "
        "single closest prior enrolled exposure across all versions decides "
        "the owning version, then the metric attribution window applies"),
}
