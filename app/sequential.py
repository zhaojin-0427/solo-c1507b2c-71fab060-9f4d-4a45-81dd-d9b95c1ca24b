"""Group-sequential testing: alpha-spending boundaries, checkpoint statistics
and conditional-power stopping decisions.

Everything here is a pure function over immutable inputs (the as-of exposure
and event rows gathered by the router). A checkpoint therefore depends only
on data with ``recorded_at`` / ``occurred_at`` strictly before the submitted
cutoff; re-submitting the same cutoff returns the stored snapshot byte for
byte, and events arriving later can never change it.

Statistical framework
----------------------
* Canonical Brownian motion parametrization: ``S_k = sqrt(t_k)·Z_k`` is a
  Brownian motion with variance ``t_k`` at information time ``t_k``;
  increments are ``N(0, t_k - t_{k-1})`` under H0. Information time is the
  fraction of the planned maximal information:

      t = (1/s_c + 1/s_t) / ((1/n_c + 1/n_t) · N_max)

  with configured allocation shares ``s`` and observed arm sizes ``n``
  (50/50 allocation reduces this to total_sample / N_max).
* Alpha is spent with the Lan–DeMets spending-function implementations of the
  Pocock and O'Brien–Fleming boundaries. The critical value at each look is
  solved by propagating the killed boundary-crossing density on an S-space
  grid, so the actual (irregular) information times of the submitted
  checkpoints are used directly — the boundary at look k is the value for
  which the probability of *first* crossing at look k, conditional on not
  crossing earlier, makes cumulative spent alpha equal to ``alpha(t_k)``.
* Conditional power is the probability, under a drift hypothesis, of crossing
  the upper boundary at any future look. Future information times are spaced
  linearly from the current time to t = 1 and their boundaries are solved by
  continuing the very same killed-density recursion, keeping the alpha budget
  internally consistent. Drifts: the fixed design assumption
  (``theta = design_delta / SE_at_max_information``) when the plan carries
  one, and the current-trend estimate ``theta = Z / sqrt(t)``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timezone
from typing import Any, Optional

from . import metrics as metrics_mod
from .repository import MetricDef
from .time_utils import parse_iso

POCOCK = "pocock"
OBF = "obrien_fleming"

ONE_SIDED = "one_sided"
TWO_SIDED = "two_sided"

# Recommendations returned to the caller
CONTINUE = "continue"
SIGNIFICANT_WIN = "significant_win"
SIGNIFICANT_HARM = "significant_harm"
FUTILITY_STOP = "futility_stop"

# ---------------------------------------------------------------------------
# Normal distribution helpers (stdlib math only)
# ---------------------------------------------------------------------------

_SQRT2 = math.sqrt(2.0)


def ndtr(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


# Acklam's rational approximation to the inverse normal CDF (|err| < 1.2e-7)
_A = [-3.969683028665376e+01, 2.209460984245205e+02,
      -2.759285104469687e+02, 1.383577518672690e+02,
      -3.066479806614716e+01, 2.506628277459239e+00]
_B = [-5.447609879822406e+01, 1.615858368580409e+02,
      -1.556989798598866e+02, 6.680131188771972e+01,
      -1.328068155288572e+01]
_C = [-7.784894002430293e-03, -3.223964580411365e-01,
      -2.400758277161838e+00, -2.549732539343734e+00,
      4.374664141464968e+00, 2.938163982698783e+00]
_D = [7.784695709041462e-03, 3.224671290700398e-01,
      2.445134137142996e+00, 3.754408661907416e+00]


def ndtri(p: float) -> float:
    """Standard normal quantile."""
    p_low, p_high = 0.02425, 0.97575
    if p <= 0.0 or p >= 1.0:
        if p == 0.0:
            return -math.inf
        if p == 1.0:
            return math.inf
        raise ValueError("ndtri: p must lie in (0, 1)")
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4])
                * q + _C[5]) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q
                                + _D[3]) * q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4])
                * r + _A[5]) * q / (((((_B[0] * r + _B[1]) * r + _B[2])
                                      * r + _B[3]) * r + _B[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -((((( _C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4])
             * q + _C[5]) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q
                              + _D[3]) * q + 1.0)


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / 2.5066282746310002


# ---------------------------------------------------------------------------
# Alpha spending functions (Lan–DeMets formulations)
# ---------------------------------------------------------------------------


def alpha_spent(boundary_type: str, hypothesis: str, t: float,
                alpha: float) -> float:
    """Cumulative type-I error spent by information time ``t``."""
    if t <= 0.0:
        return 0.0
    t = min(1.0, t)
    if boundary_type == POCOCK:
        # Pocock-type spending: alpha(t) = alpha * ln(1 + (e-1) t)
        return alpha * math.log1p((math.e - 1.0) * t)
    # O'Brien–Fleming-type spending
    if hypothesis == TWO_SIDED:
        z = ndtri(1.0 - alpha / 2.0)
        return 2.0 * ndtr(-z / math.sqrt(t))
    z = ndtri(1.0 - alpha)
    return ndtr(-z / math.sqrt(t))


# ---------------------------------------------------------------------------
# Killed-boundary recursion on a fixed Brownian-motion grid
# ---------------------------------------------------------------------------

# S-space discretization. Boundaries in S never exceed ~3 in realistic plans
# (terminal S boundary = z_{alpha/2} ≈ 1.96; early OBF S boundary is ~2.0).
# The grid reaches ±8 so the sparse Gaussian increment kernel can reach 6
# sigma from any support region while still truncating below 2e-9 per step.
_GRID_H = 0.025
_GRID_B = 8.0
_GRID = [-_GRID_B + i * _GRID_H
         for i in range(int(round(2 * _GRID_B / _GRID_H)) + 1)]
_N = len(_GRID)


def _kernel(dt: float, drift: float = 0.0,
            reach: int | None = None) -> list[tuple[int, float]]:
    """Sparse Gaussian increment kernel (index offset, weight).

    A source grid point ``i`` contributes to ``j = i + m0 + m`` with a
    density-step weight for an increment of mean ``drift`` and variance
    ``dt``. Offsets are kept within ``reach`` cells on each side (default:
    the whole grid), so propagation iterates only over offsets that can land
    in reachable support. Weights are raw (not renormalized per source): a
    boundary-tail event itself sits many sigma out, so per-source
    renormalization would silently fold tail mass back into the center.
    Global mass conservation is restored by the caller's overall rescale.
    """
    sigma = math.sqrt(dt)
    m0 = int(round(drift / _GRID_H))
    residual = drift - m0 * _GRID_H
    if reach is None:
        m_max = int(round(_GRID_B / _GRID_H))
    else:
        m_max = reach
    out: list[tuple[int, float]] = []
    for m in range(-m_max, m_max + 1):
        w = _GRID_H * _phi((m * _GRID_H - residual) / sigma) / sigma
        if w > 0.0:
            out.append((m0 + m, w))
    return out


def _support_bounds(p: list[float]) -> tuple[int, int]:
    """First/last nonzero indices (kernel support stays finite)."""
    lo = 0
    while lo < _N and p[lo] == 0.0:
        lo += 1
    hi = _N - 1
    while hi > lo and p[hi] == 0.0:
        hi -= 1
    return lo, hi


def _propagate(p: list[float], dt: float, drift: float = 0.0,
               rescale_to: float | None = None) -> list[float]:
    """Convolve the (possibly killed) density with one Brownian increment.

    Sparse in both sources (only nonzero cells) and offsets (only within
    6 sigma or the distance to the nearest grid edge, whichever is smaller);
    ``rescale_to``, when given, conserves total mass against grid/window
    truncation (boundary recursion uses the unconditional surviving mass).
    """
    sigma = math.sqrt(dt)
    s_lo, s_hi = _support_bounds(p)
    if s_hi < s_lo:
        return [0.0] * _N
    out = [0.0] * _N
    # A source cell can contribute to a boundary crossing even when the
    # boundary lies many sigma away (that crossing is exactly the small tail
    # probability being measured); reach therefore covers the distance to
    # either grid edge in cells, with a floor of 6 sigma for ordinary
    # Gaussian support near the center.
    base_reach = max(48, int(math.ceil(6.0 * sigma / _GRID_H)) + 4)
    for i in range(s_lo, s_hi + 1):
        mass = p[i]
        if mass == 0.0:
            continue
        src_reach = min(int(round(_GRID_B / _GRID_H)),
                        max(base_reach, i, _N - 1 - i))
        kernel = _kernel(dt, drift, reach=src_reach)
        for off, w in kernel:
            j = i + off
            if 0 <= j < _N:
                out[j] += mass * w
    if rescale_to is not None:
        mass = _GRID_H * (0.5 * out[0] + sum(out[1:-1]) + 0.5 * out[-1])
        if mass > 1e-300:
            scale = rescale_to / mass
            out = [v * scale for v in out]
    return out


def _kill_tails(p: list[float], lb: Optional[float],
                ub: float, normalize: bool = True) -> list[float]:
    """Zero density outside (lb, ub), truncating at the exact boundaries.

    The boundary values are inserted as explicit vertices into an augmented
    piecewise-linear grid (so two-sided plans never double-adjust the shared
    inner cell vertex), the outside vertices are killed, and the result is
    interpolated back onto the fixed grid. With ``normalize=True`` the
    survivors integrate to 1 (the conditional law given no crossing); with
    ``False`` unconditional mass is retained (used for conditional power,
    where mass collected as crossings accumulates separately).
    """
    import bisect

    def value_at(x: float) -> float:
        k = bisect.bisect_right(_GRID, x) - 1
        if k < 0:
            return p[0]
        if k + 1 >= _N:
            return p[-1]
        frac = (x - _GRID[k]) / _GRID_H
        return p[k] + frac * (p[k + 1] - p[k])

    cuts = [ub] if lb is None else sorted({lb, ub})
    # Augmented abscissae: fixed grid + boundary points, retaining order.
    xs = sorted(set(_GRID + cuts))
    dens = []
    for x in xs:
        if x > ub or (lb is not None and x < lb):
            # strictly outside the open survival interval (-lb, ub); grid
            # points landing exactly on the boundary survive as vertices
            dens.append(0.0)
        else:
            dens.append(value_at(x))
    # Map back onto the fixed grid: linear interpolation within augmented cells.
    out: list[float] = []
    for s in _GRID:
        if s > ub or (lb is not None and s < lb):
            out.append(0.0)
        else:
            j = bisect.bisect_right(xs, s) - 1
            if j < 0:
                out.append(dens[0])
            elif j + 1 >= len(xs):
                out.append(dens[-1])
            else:
                frac = (s - xs[j]) / (xs[j + 1] - xs[j])
                out.append(dens[j] + frac * (dens[j + 1] - dens[j]))
    integral = _GRID_H * (0.5 * out[0] + sum(out[1:-1]) + 0.5 * out[-1])
    if normalize and integral > 1e-300:
        scale = 1.0 / integral
        out = [v * scale for v in out]
    return out


class BoundaryRecursion:
    """Sequential solve of group-sequential boundaries at arbitrary times.

    State: the H0 density of S at the latest look, killed (set to zero) at
    every previously solved boundary, plus cumulative alpha already spent.
    ``advance`` solves the boundary for one more look; callers may ``clone``
    the state to solve hypothetical future looks without mutating history.
    """

    def __init__(self, boundary_type: str, hypothesis: str, alpha: float):
        self.boundary_type = boundary_type
        self.hypothesis = hypothesis
        self.alpha = alpha
        self.two_sided = hypothesis == TWO_SIDED
        self.p: Optional[list[float]] = None
        self.t_prev = 0.0
        self.spent = 0.0
        self.records: list[dict[str, float]] = []

    def clone(self) -> "BoundaryRecursion":
        other = BoundaryRecursion(self.boundary_type, self.hypothesis,
                                  self.alpha)
        other.p = None if self.p is None else list(self.p)
        other.t_prev = self.t_prev
        other.spent = self.spent
        other.records = [dict(r) for r in self.records]
        return other

    def _initial_density(self, t: float) -> list[float]:
        sd = math.sqrt(t)
        return [_phi(s / sd) / sd for s in _GRID]

    def _tail(self, p: list[float], sb: float) -> float:
        """Probability mass beyond the candidate S boundary at this look.

        Density is integrated piecewise-linearly between grid points (with a
        partial trapezoid straddling the boundary), so the boundary is solved
        with sub-grid accuracy even where the tail contains only a point or
        two at early looks.
        """
        if self.two_sided:
            return self._upper_tail(p, sb) + self._lower_tail(p, -sb)
        return self._upper_tail(p, sb)

    @staticmethod
    def _upper_tail(p: list[float], sb: float) -> float:
        """∫_{sb}^{∞} p(s) ds, piecewise-linear density between grid points."""
        import bisect
        k = bisect.bisect_right(_GRID, sb) - 1  # last grid point <= sb
        if k + 1 >= _N:
            return 0.0
        if k >= 0:
            # linear density at sb within cell [x_k, x_{k+1}], partial trapezoid
            frac = (sb - _GRID[k]) / _GRID_H
            d_at = p[k] + frac * (p[k + 1] - p[k])
            partial = 0.5 * (_GRID[k + 1] - sb) * (d_at + p[k + 1])
        else:
            partial = 0.0
        # full cells [x_{k+1}, x_{k+2}], ...; half weight on the first full
        # endpoint is already supplied by the partial segment above
        return partial + _GRID_H * (0.5 * p[k + 1] + sum(p[k + 2:]))

    @staticmethod
    def _lower_tail(p: list[float], lb: float) -> float:
        """∫_{-∞}^{lb} p(s) ds, piecewise-linear density."""
        import bisect
        k = bisect.bisect_left(_GRID, lb)  # first grid point >= lb
        if k == 0:
            return 0.0
        if k < _N:
            frac = (lb - _GRID[k - 1]) / _GRID_H
            d_at = p[k - 1] + frac * (p[k] - p[k - 1])
            partial = 0.5 * (lb - _GRID[k - 1]) * (p[k - 1] + d_at)
        else:
            partial = 0.0
        return partial + _GRID_H * (sum(p[:k - 1]) + 0.5 * p[k - 1])

    def advance(self, t: float) -> dict[str, float]:
        t = min(1.0, max(self.t_prev + 1e-12, t))
        survival = 1.0 if self.p is None else 1.0 - self.spent
        if self.p is None:
            p = self._initial_density(t)
            mass = _GRID_H * (0.5 * p[0] + sum(p[1:-1]) + 0.5 * p[-1])
            if 0.0 < mass != survival:
                p = [v * survival / mass for v in p]
        else:
            # Unconditional surviving mass is restored globally; the sparse
            # kernel itself is not renormalized per source, so the tiny
            # boundary-tail mass is preserved rather than folded inward.
            p = _propagate(self.p, t - self.t_prev, rescale_to=survival)

        target = alpha_spent(self.boundary_type, self.hypothesis, t,
                             self.alpha) - self.spent
        if target <= 1e-13:
            sb = _GRID_B
            exit_mass = self._tail(p, sb)
        else:
            lo, hi = 0.0, _GRID_B
            for _ in range(42):
                mid = 0.5 * (lo + hi)
                if self._tail(p, mid) > target:
                    lo = mid
                else:
                    hi = mid
            sb = 0.5 * (lo + hi)
            # actual crossing probability at the solved boundary (bisection
            # converges to within ~1e-13; use the measured value so later
            # recursion steps carry the true surviving mass)
            exit_mass = self._tail(p, sb)

        record = {
            "information_time": t,
            "boundary_s": sb,
            "boundary_z": sb / math.sqrt(t),
            "lower_z": -(sb / math.sqrt(t)) if self.two_sided else None,
            "spent_alpha": self.spent + exit_mass,
            "incremental_alpha": exit_mass,
        }
        self.records.append(record)
        self.spent += exit_mass

        # Kill crossing mass; the density fed to the next look is the
        # SURVIVAL density normalized to 1 (conditional on no earlier
        # crossing). The cell straddling the boundary is truncated
        # fractionally (a boundary vertex is inserted into the piecewise
        # linear density), so early OBF looks whose tail is a sliver of one
        # cell do not over-kill the whole cell.
        self.p = _kill_tails(p, -sb if self.two_sided else None, sb)
        self.t_prev = t
        return record

    def future_boundaries(self, current_t: float, remaining_looks: int
                          ) -> list[dict[str, float]]:
        """Solve hypothetical later looks, linearly spaced to t = 1.

        Continues the same killed-density recursion (hence the same alpha
        budget) from the current state.
        """
        sim = self.clone()
        out: list[dict[str, float]] = []
        for j in range(1, remaining_looks + 1):
            t = current_t + j * (1.0 - current_t) / remaining_looks
            out.append(sim.advance(t))
        return out


def conditional_power(S0: float, theta: float,
                      future: list[dict[str, float]],
                      two_sided: bool, current_t: float) -> float:
    """Probability of ever crossing the upper boundary at a future look.

    The density starts as a point mass at the current S value ``S0`` and
    evolves with drift ``theta`` per unit information; at every future look
    mass above the upper boundary is collected as success and (for two-sided
    plans) mass below the lower boundary is absorbed as failure.
    """
    j0 = min(range(_N), key=lambda j: abs(_GRID[j] - S0))
    p = [0.0] * _N
    p[j0] = 1.0 / _GRID_H  # unit point mass represented as density
    t_prev = current_t
    cp = 0.0
    for rec in future:
        t = rec["information_time"]
        p = _propagate(p, t - t_prev, drift=theta * (t - t_prev),
                       rescale_to=None)
        ub = rec["boundary_s"]
        cp += BoundaryRecursion._upper_tail(p, ub)
        if two_sided:
            p = _kill_tails(p, -ub, ub, normalize=False)
        else:
            p = _kill_tails(p, None, ub, normalize=False)
        t_prev = t
    return min(1.0, max(0.0, cp))


# ---------------------------------------------------------------------------
# As-of arm aggregation (reuses the immutable-row attribution classifier)
# ---------------------------------------------------------------------------


def aggregate_arms(metric: MetricDef, plan: dict[str, Any],
                   events: list[dict[str, Any]],
                   version_exposures: list[dict[str, Any]],
                   all_enrolled_exposures: list[dict[str, Any]],
                   published_versions: list[tuple[str, int]]
                   ) -> dict[str, Any]:
    """Binary/conversion or continuous statistics for the two plan arms.

    Inputs are already filtered to rows strictly before the checkpoint
    cutoff. The cross-version ownership / window / value rules are exactly
    those of the regular effect analysis, so a checkpoint is an as-of slice
    of the same deterministic attribution.
    """
    from bisect import bisect_right

    control_key = plan["control_variant_key"]
    target_key = plan["target_variant_key"]
    version_number = metric.version_number
    arms = (control_key, target_key)

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

    exposed: dict[str, set[str]] = defaultdict(set)
    for user, rows in enrolled_grouped.items():
        variant = rows[-1]["variant_key"]
        if variant in arms:
            exposed[variant].add(user)

    converters: dict[str, set[str]] = defaultdict(set)
    values: dict[str, list[float]] = defaultdict(list)
    excluded = {"no_exposure": 0, "event_before_exposure": 0,
                "out_of_window": 0, "invalid_value": 0}
    attributed_events = 0

    for event in events:
        verdict = metrics_mod.classify_for_version(
            event, metric, version_number, all_grouped, records_grouped,
            live_number_at(event["occurred_at_dt"]))
        kind = verdict["kind"]
        if kind in (metrics_mod.NOT_OWNED, metrics_mod.NO_VERSION):
            continue
        if kind == metrics_mod.SCOPE_NO_EXPOSURE:
            excluded[verdict["reason"]] = excluded.get(verdict["reason"], 0) + 1
            continue
        reason = verdict["reason"]
        if reason in (metrics_mod.OUT_OF_WINDOW, metrics_mod.INVALID_VALUE):
            excluded[reason] += 1
            continue
        variant = verdict["variant_key"]
        if variant not in arms:
            # Event attributed to a third variant of the version: outside
            # this two-arm plan's statistics entirely.
            continue
        attributed_events += 1
        if metric.metric_type == "binary":
            converters[variant].add(event["user_key"])
        else:
            values[variant].append(float(event["value"]))

    out_arms: dict[str, Any] = {}
    for role, key in (("control", control_key), ("target", target_key)):
        n = len(exposed.get(key, ()))
        if metric.metric_type == "binary":
            conv = len(converters.get(key, ()))
            rate = conv / n if n > 0 else None
            out_arms[role] = {
                "variant_key": key, "role": role, "n": n,
                "conversions": conv, "value": rate,
            }
        else:
            vals = values.get(key, [])
            n_obs = len(vals)
            mean = sum(vals) / n_obs if n_obs else None
            var = None
            if n_obs >= 2:
                var = sum((x - mean) ** 2 for x in vals) / (n_obs - 1)
            out_arms[role] = {
                "variant_key": key, "role": role, "n": n,
                "observations": n_obs, "value": mean, "variance": var,
                "sum": sum(vals) if vals else 0.0,
            }
    return {"arms": out_arms, "excluded": excluded,
            "events_attributed": attributed_events,
            "metric_type": metric.metric_type}


class NotEvaluable(Exception):
    """Raised when the as-of data cannot yet define a test statistic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Information time, test statistic and full checkpoint evaluation
# ---------------------------------------------------------------------------


def information_time(plan: dict[str, Any], n_c: int, n_t: int) -> float:
    s_c, s_t = plan["control_share"], plan["target_share"]
    n_max = plan["max_sample_size"]
    if n_c <= 0 or n_t <= 0:
        return 0.0
    inv = (1.0 / s_c + 1.0 / s_t) / (
        (1.0 / n_c + 1.0 / n_t) * n_max)
    return max(0.0, min(1.0, inv))


def design_se_max(plan: dict[str, Any]) -> Optional[float]:
    """Standard error of the arm difference at maximal planned information,
    under the design assumptions."""
    da = plan.get("design_assumptions")
    if not da:
        return None
    s_c, s_t = plan["control_share"], plan["target_share"]
    n_max = plan["max_sample_size"]
    if plan["metric_type"] == "binary":
        p_c = da["control_rate"]
        p_t = p_c + da["absolute_effect"]
        p_bar = s_c * p_c + s_t * p_t
        c2 = p_bar * (1.0 - p_bar) * (1.0 / s_c + 1.0 / s_t)
    else:
        c2 = da["standard_deviation"] ** 2 * (1.0 / s_c + 1.0 / s_t)
    return math.sqrt(c2 / n_max)


def _round(v: Optional[float], digits: int = 8) -> Optional[float]:
    return None if v is None else round(v, digits)


def evaluate_checkpoint(plan: dict[str, Any], agg: dict[str, Any],
                        prior_times: list[float], sequence: int
                        ) -> dict[str, Any]:
    """Compute the complete, self-contained result snapshot for one look."""
    c = agg["arms"]["control"]
    t_arm = agg["arms"]["target"]
    n_c, n_t = c["n"], t_arm["n"]
    info_t = information_time(plan, n_c, n_t)
    if info_t <= 0.0:
        raise NotEvaluable(
            "insufficient_information",
            "both plan arms need at least one enrolled exposure before a "
            "checkpoint statistic can be computed")
    if prior_times and info_t <= prior_times[-1] + 1e-12:
        raise NotEvaluable(
            "non_increasing_information",
            f"information time {info_t:.6f} is not strictly greater than at "
            f"the previous checkpoint ({prior_times[-1]:.6f})")

    direction = plan["direction"]
    orient = 1.0 if direction == "maximize" else -1.0
    mtype = plan["metric_type"]

    # ---- effect + test statistic ------------------------------------------
    if mtype == "binary":
        c_c, c_t = c["conversions"], t_arm["conversions"]
        p_c, p_t = c["value"], t_arm["value"]
        p_bar = (c_c + c_t) / (n_c + n_t)
        se = math.sqrt(p_bar * (1.0 - p_bar) * (1.0 / n_c + 1.0 / n_t))
        if se == 0.0:
            raise NotEvaluable(
                "zero_variance",
                "pooled variance is zero (all arms show identical 0% or 100% "
                "rates); the Z statistic is undefined until outcomes vary")
        z_raw = (p_t - p_c) / se
        effect = {
            "kind": "risk_difference",
            "control_rate": _round(p_c), "target_rate": _round(p_t),
            "difference": _round(p_t - p_c),
            "relative_change": _round(None if p_c in (None, 0.0)
                                      else (p_t - p_c) / abs(p_c)),
            "oriented_difference": _round(orient * (p_t - p_c)),
        }
        stat_formula = (
            f"z = (p_t - p_c) / sqrt(p̄(1-p̄)(1/n_c+1/n_t)) = "
            f"({p_t:.6f} - {p_c:.6f}) / sqrt({p_bar:.6f}·{1 - p_bar:.6f}·"
            f"(1/{n_c}+1/{n_t})) = {orient * z_raw:.6f} after "
            f"{'favorable' if orient > 0 else 'unfavorable'}-direction sign; "
            f"raw z = {z_raw:.6f}")
    else:
        if c.get("variance") is None or t_arm.get("variance") is None:
            raise NotEvaluable(
                "insufficient_information",
                "continuous metrics need at least two valid observations in "
                "each arm for a sample variance (Welch Z)")
        m_c, m_t = c["value"], t_arm["value"]
        v_c, v_t = c["variance"], t_arm["variance"]
        # The Welch standard error denominates by the number of VALID
        # observations (attributed events with a finite value) in each arm —
        # not by the number of enrolled exposures, which is a property of
        # randomization and may exceed the observed response count.
        k_c, k_t = c["observations"], t_arm["observations"]
        if k_c < 2 or k_t < 2:
            raise NotEvaluable(
                "insufficient_information",
                "continuous metrics need at least two valid observations in "
                "each arm for a sample variance (Welch Z)")
        se = math.sqrt(v_c / k_c + v_t / k_t)
        z_raw = (m_t - m_c) / se
        effect = {
            "kind": "mean_difference",
            "control_mean": _round(m_c), "target_mean": _round(m_t),
            "difference": _round(m_t - m_c),
            "relative_change": _round(None if not m_c
                                      else (m_t - m_c) / abs(m_c)),
            "oriented_difference": _round(orient * (m_t - m_c)),
        }
        stat_formula = (
            f"z = (mean_t - mean_c) / sqrt(s_t²/k_t + s_c²/k_c) = "
            f"({m_t:.6f} - {m_c:.6f}) / sqrt({v_t:.6f}/{k_t} + "
            f"{v_c:.6f}/{k_c}) = {orient * z_raw:.6f} after "
            f"{'favorable' if orient > 0 else 'unfavorable'}-direction sign "
            f"(k = valid observations, exposures n_c={n_c}, n_t={n_t}); "
            f"raw z = {z_raw:.6f}")

    z = orient * z_raw  # > 0 always means target moves in the metric direction
    S0 = math.sqrt(info_t) * z

    s_c, s_t = plan["control_share"], plan["target_share"]
    info_formula = (
        "t = (1/s_c + 1/s_t) / ((1/n_c + 1/n_t) · N_max) = "
        f"(1/{s_c:g} + 1/{s_t:g}) / ((1/{n_c} + 1/{n_t}) · "
        f"{plan['max_sample_size']}) = {info_t:.6f}")

    # ---- boundary at the actual information time --------------------------
    times = prior_times + [info_t]
    rec = BoundaryRecursion(plan["boundary_type"], plan["hypothesis"],
                            plan["alpha"])
    current = None
    for tt in times:
        current = rec.advance(tt)
    spent_now = alpha_spent(plan["boundary_type"], plan["hypothesis"],
                            info_t, plan["alpha"])
    upper_z = current["boundary_z"]
    lower_z = current["lower_z"]

    if plan["boundary_type"] == POCOCK:
        z_ref = (ndtri(1.0 - plan["alpha"] / 2.0)
                 if plan["hypothesis"] == TWO_SIDED
                 else ndtri(1.0 - plan["alpha"]))
        spend_formula = (
            f"α(t) = α·ln(1 + (e-1)·t) = {plan['alpha']:g}·"
            f"ln(1 + (e-1)·{info_t:.6f}) = {spent_now:.6f}")
        btype_name = "Pocock"
    else:
        if plan["hypothesis"] == TWO_SIDED:
            z_ref = ndtri(1.0 - plan["alpha"] / 2.0)
            spend_formula = (
                f"α(t) = 2·(1 - Φ(z_(α/2)/√t)) = "
                f"2·(1 - Φ({z_ref:.4f}/{math.sqrt(info_t):.6f})) "
                f"= {spent_now:.6f}")
        else:
            z_ref = ndtri(1.0 - plan["alpha"])
            spend_formula = (
                f"α(t) = 1 - Φ(z_α/√t) = 1 - Φ({z_ref:.4f}/"
                f"{math.sqrt(info_t):.6f}) = {spent_now:.6f}")
        btype_name = "O'Brien–Fleming"
    sides_note = ("two-sided symmetric boundary ±c_k"
                  if plan["hypothesis"] == TWO_SIDED
                  else "one-sided upper boundary c_k")
    boundary_formula = (
        f"c_k solved on the killed Brownian grid so that first-crossing "
        f"probability at look {sequence} equals α(t_k)-α(t_{sequence-1}); "
        f"{btype_name} {sides_note}: c = {upper_z:.6f}"
        + (f", -c = {lower_z:.6f}" if lower_z is not None else ""))

    # ---- recommendation ----------------------------------------------------
    recommendation = CONTINUE
    stop_reasons: list[str] = []
    terminal = False
    at_max_info = info_t >= 1.0 - 1e-12
    looks_remaining = plan["planned_checks"] - sequence

    if z > upper_z:
        recommendation = SIGNIFICANT_WIN
        stop_reasons.append("upper_boundary_crossed")
        terminal = True
    elif lower_z is not None and z < lower_z:
        recommendation = SIGNIFICANT_HARM
        stop_reasons.append("lower_boundary_crossed")
        terminal = True

    # ---- conditional power (only while future looks remain) ---------------
    cp_block: dict[str, Any] = {"threshold": plan["conditional_power_threshold"]}
    theta_obs = z / math.sqrt(info_t)
    se_max = design_se_max(plan)
    if plan.get("design_assumptions"):
        raw_delta = plan["design_assumptions"]["absolute_effect"]
        # The Brownian drift must point in the FAVORABLE direction: a
        # minimize plan stores a negative absolute_effect, and conditional
        # power is the probability of crossing the UPPER (win) boundary for
        # the oriented statistic. Using the signed raw delta would turn a
        # beneficial downward movement into a predicted failure.
        delta = orient * raw_delta
        theta_design = delta / se_max
    else:
        raw_delta = None
        delta = None
        theta_design = None

    if not terminal and looks_remaining > 0 and not at_max_info:
        future = rec.future_boundaries(info_t, looks_remaining)
        cp_obs = conditional_power(S0, theta_obs, future,
                                   plan["hypothesis"] == TWO_SIDED, info_t)
        cp_design = (conditional_power(S0, theta_design, future,
                                       plan["hypothesis"] == TWO_SIDED,
                                       info_t)
                     if theta_design is not None else None)
        future_desc = ", ".join(
            f"t_{sequence + j + 1}={r['information_time']:.4f}→c={r['boundary_z']:.4f}"
            for j, r in enumerate(future))
        cp_formula = (
            f"CP = Σ_j Pr(S_j > c_j | S_{sequence} = {S0:.4f}); "
            f"future boundaries: {future_desc}; "
            f"observed drift θ=z/√t={theta_obs:.4f} → CP_obs={cp_obs:.4f}"
            + (f"; design drift θ=Δ/SE_max={delta:.4f}/{se_max:.4f}"
               f"={theta_design:.4f} → CP_design={cp_design:.4f}"
               if theta_design is not None else ""))
        cp_block.update({
            "basis": ("design_absolute_effect" if theta_design is not None
                      else "observed_effect"),
            "observed": {"drift": _round(theta_obs, 6),
                         "value": _round(cp_obs, 6)},
            "design": (None if theta_design is None
                       else {"drift": _round(theta_design, 6),
                             "se_at_max_information": _round(se_max, 6),
                             "value": _round(cp_design, 6)}),
            "formula": cp_formula,
        })
        cp_decision = cp_design if cp_design is not None else cp_obs
        if recommendation == CONTINUE and cp_decision < plan["conditional_power_threshold"]:
            recommendation = FUTILITY_STOP
            stop_reasons.append("conditional_power_below_threshold")
            terminal = True
    else:
        cp_block.update({"basis": None, "observed": None, "design": None,
                         "formula":
                             "no conditional power computed: the plan has "
                             "reached its terminal look or maximal information"})

    if recommendation == CONTINUE and (at_max_info or looks_remaining <= 0):
        recommendation = FUTILITY_STOP
        stop_reasons.append("max_information_reached" if at_max_info
                            else "all_planned_looks_used")
        terminal = True

    # ---- arm rows with plugged formulas ------------------------------------
    arm_rows = []
    for role in ("control", "target"):
        a = agg["arms"][role]
        if mtype == "binary":
            formula = (
                f"p_{role} = conversions / n = {a['conversions']}/{a['n']}"
                f" = {a['value']:.6f}" if a["n"] else
                f"p_{role} = {a['conversions']}/0 (no exposed users)")
            row = {"variant_key": a["variant_key"], "role": role,
                   "n": a["n"], "conversions": a["conversions"],
                   "value": _round(a["value"]), "formula": formula}
        else:
            if a["observations"]:
                formula = (
                    f"mean_{role} = Σx/n_obs = {a['sum']:.6f}/"
                    f"{a['observations']} = {a['value']:.6f}; "
                    f"s² = Σ(x-mean)²/(n_obs-1) = "
                    f"{a['variance'] if a['variance'] is not None else 0:.6f}")
            else:
                formula = f"mean_{role}: no valid attributed observations"
            row = {"variant_key": a["variant_key"], "role": role,
                   "n": a["n"], "observations": a["observations"],
                   "value": _round(a["value"]),
                   "variance": _round(a.get("variance")),
                   "sum": _round(a.get("sum"), 6), "formula": formula}
        arm_rows.append(row)

    return {
        "sequence": sequence,
        "information": {
            "information_time": _round(info_t, 6),
            "max_sample_size": plan["max_sample_size"],
            "control_n": n_c,
            "target_n": n_t,
            "total_n": n_c + n_t,
            "formula": info_formula,
        },
        "arms": arm_rows,
        "effect": effect,
        "statistic": {
            "z": _round(z, 6),
            "raw_z": _round(z_raw, 6),
            "standard_error": _round(se, 8),
            "direction_applied": direction,
            "formula": stat_formula,
        },
        "cumulative_alpha": {
            "spent": _round(spent_now, 8),
            "spent_by_boundary_recursion": _round(current["spent_alpha"], 8),
            "incremental_at_look": _round(current["incremental_alpha"], 8),
            "overall_alpha": plan["alpha"],
            "formula": spend_formula,
        },
        "boundary": {
            "upper_z": _round(upper_z, 6),
            "lower_z": _round(lower_z, 6),
            "boundary_type": plan["boundary_type"],
            "hypothesis": plan["hypothesis"],
            "formula": boundary_formula,
        },
        "conditional_power": cp_block,
        "recommendation": recommendation,
        "terminal": terminal,
        "stop_reasons": stop_reasons,
        "excluded": agg["excluded"],
        "events_attributed": agg["events_attributed"],
    }


# ---------------------------------------------------------------------------
# Plan-time outputs
# ---------------------------------------------------------------------------


def planned_boundary_table(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Boundaries at the planned equally-spaced looks t = j/K."""
    rec = BoundaryRecursion(plan["boundary_type"], plan["hypothesis"],
                            plan["alpha"])
    rows = []
    for j in range(1, plan["planned_checks"] + 1):
        r = rec.advance(j / plan["planned_checks"])
        rows.append({"look": j,
                     "information_time": round(j / plan["planned_checks"], 6),
                     "upper_z": round(r["boundary_z"], 6),
                     "lower_z": (None if r["lower_z"] is None
                                 else round(r["lower_z"], 6)),
                     "cumulative_alpha": round(
                         alpha_spent(plan["boundary_type"], plan["hypothesis"],
                                     j / plan["planned_checks"],
                                     plan["alpha"]), 8)})
    return rows


SEQ_FORMULAS = {
    "information_time": (
        "t = (1/s_control + 1/s_target) / "
        "((1/n_control + 1/n_target) · max_sample_size); "
        "equals total_sample / max_sample_size under 50/50 allocation"),
    "binary_statistic": (
        "pooled two-proportion Z: (p_target - p_control) / "
        "sqrt(p̄(1-p̄)(1/n_t + 1/n_c)), p̄ pooled rate; sign oriented so Z>0 "
        "means the target moves in the metric optimization direction"),
    "continuous_statistic": (
        "Welch Z: (mean_target - mean_control) / "
        "sqrt(s_t²/n_t + s_c²/n_c), sample variance with denominator n-1; "
        "sign oriented by the metric direction"),
    "pocock_spending": "α(t) = α · ln(1 + (e-1)·t)",
    "obf_spending_two_sided": "α(t) = 2·(1 - Φ(z_(1-α/2)/√t))",
    "obf_spending_one_sided": "α(t) = 1 - Φ(z_(1-α)/√t)",
    "boundary_solve": (
        "S_k = √t_k·Z_k behaves as Brownian motion; the killed boundary "
        "density is propagated on a fixed S-space grid and the boundary c_k "
        "is solved (bisection) so first-crossing probability at look k equals "
        "α(t_k) - α(t_{k-1})"),
    "conditional_power": (
        "CP = Σ over future looks of Pr(S_j > c_j | current S, drift); "
        "design drift θ = Δ / SE(max information), current-trend drift "
        "θ = Z / √t; future information times are linearly spaced to t = 1 "
        "and their boundaries continue the same alpha-spending recursion"),
    "decision": (
        "Z > upper boundary → significant win; two-sided Z < lower boundary "
        "→ significant harm; otherwise conditional power (design drift, else "
        "current trend) below the threshold → futility stop; else continue"),
}
