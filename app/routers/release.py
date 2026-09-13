"""Multi-metric release-decision plans and frozen decision snapshots.

A plan is created on a version BEFORE any exposure on that version and can
never be changed afterwards (the DB layer rejects updates outright). It pins
one control/target variant pair, exactly one primary metric with a minimum
favorable effect, any number of guardrail metrics with non-inferiority
margins, and the multiple-comparison correction (Holm or Bonferroni).
Metrics that live on another version, are referenced twice, or have an
unbounded attribution window are rejected, as is any version that already
has exposures.

Snapshots freeze the per-metric statistics and the ship / do-not-ship /
insufficient-evidence decision at a cutoff instant: only exposures recorded
before and events occurred before the cutoff are read, users whose
attribution window has not fully elapsed are excluded, and the result is
stored immutably — re-requesting the same plan + cutoff returns the stored
snapshot verbatim and later events never rewrite it.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Optional

from fastapi import APIRouter

from .. import release
from .. import repository as repo
from ..errors import APIError, ConflictError, NotFoundError, ValidationRejected
from ..schemas import (
    ConfidenceInterval,
    MetricDefOut,
    ReleaseArmRow,
    ReleaseDecisionBlock,
    ReleaseGuardrailOut,
    ReleaseMetricResult,
    ReleasePlanCreate,
    ReleasePlanListResponse,
    ReleasePlanOut,
    ReleasePrimaryOut,
    ReleaseSnapshotCreate,
    ReleaseSnapshotHistoryResponse,
    ReleaseSnapshotOut,
    ReleaseSnapshotResult,
    ReleaseSnapshotSummaryRow,
)
from ..time_utils import parse_iso, to_iso, utcnow

# Experiment-scoped routes (creation/listing) and global plan-key routes.
router = APIRouter(prefix="/experiments", tags=["release-decisions"])
plans_router = APIRouter(prefix="/release-plans", tags=["release-decisions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue(code: str, message: str,
           location: Optional[str] = None) -> dict[str, Any]:
    return {"code": code, "message": message, "location": location}


def _metric_out(m: repo.MetricDef) -> MetricDefOut:
    return MetricDefOut(
        id=m.id, experiment_key=m.experiment_key,
        version_number=m.version_number, metric_key=m.metric_key,
        metric_type=m.metric_type, event_name=m.event_name,
        attribution_window_seconds=m.attribution_window_seconds,
        direction=m.direction, min_sample_size=m.min_sample_size,
        srm_threshold=m.srm_threshold, created_at=m.created_at)


def _plan_metrics(plan: repo.ReleasePlan) -> dict[str, repo.MetricDef]:
    """Immutable metric definitions referenced by the plan."""
    keys = [plan.primary_metric_key] + [g["metric_key"]
                                        for g in plan.guardrails]
    return {key: repo.get_metric(plan.experiment_key, plan.version_number,
                                 key)
            for key in keys}


def _plan_out(plan: repo.ReleasePlan,
              snapshots: list[dict[str, Any]]) -> ReleasePlanOut:
    metrics = _plan_metrics(plan)
    return ReleasePlanOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number,
        control_variant_key=plan.control_variant_key,
        target_variant_key=plan.target_variant_key,
        primary=ReleasePrimaryOut(
            metric=_metric_out(metrics[plan.primary_metric_key]),
            min_favorable_effect=plan.primary_min_effect),
        guardrails=[ReleaseGuardrailOut(
            metric=_metric_out(metrics[g["metric_key"]]),
            non_inferiority_margin=g["non_inferiority_margin"])
            for g in plan.guardrails],
        correction=plan.correction, alpha=plan.alpha,
        snapshots_recorded=len(snapshots), created_at=plan.created_at)


def _plan_out_loaded(plan: repo.ReleasePlan) -> ReleasePlanOut:
    return _plan_out(plan, repo.list_release_snapshots(plan.plan_key))


def _plan_dict(plan: repo.ReleasePlan) -> dict[str, Any]:
    return {
        "plan_key": plan.plan_key,
        "experiment_key": plan.experiment_key,
        "version_number": plan.version_number,
        "control_variant_key": plan.control_variant_key,
        "target_variant_key": plan.target_variant_key,
        "primary_metric_key": plan.primary_metric_key,
        "primary_min_effect": plan.primary_min_effect,
        "correction": plan.correction,
        "alpha": plan.alpha,
        "guardrails": plan.guardrails,
    }


# ---------------------------------------------------------------------------
# Plan creation / listing
# ---------------------------------------------------------------------------


@router.post("/{experiment_key}/versions/{version}/release-plans",
             response_model=ReleasePlanOut, status_code=201,
             summary="Create an immutable pre-exposure release-decision plan")
def create_plan(experiment_key: str, version: int,
                payload: ReleasePlanCreate) -> ReleasePlanOut:
    loaded = repo.get_version(experiment_key, version)  # 404 chain
    issues: list[dict[str, Any]] = []

    variant_map = {v.key: v for v in loaded.config.variants}
    for field_name, key in (("control_variant_key",
                             payload.control_variant_key),
                            ("target_variant_key",
                             payload.target_variant_key)):
        if key not in variant_map:
            issues.append(_issue(
                "unknown_variant",
                f"{field_name} {key!r} is not a declared variant of "
                f"version {version}", field_name))
    if not any(i["code"] == "unknown_variant" for i in issues):
        if payload.control_variant_key == payload.target_variant_key:
            issues.append(_issue(
                "distinct_arms_required",
                "control_variant_key and target_variant_key must differ"))
        if payload.control_variant_key != loaded.config.control_variant_key:
            issues.append(_issue(
                "control_mismatch",
                f"control_variant_key must equal the version's control "
                f"variant {loaded.config.control_variant_key!r}",
                "control_variant_key"))
        if (variant_map[payload.control_variant_key].percentage <= 0
                or variant_map[payload.target_variant_key].percentage <= 0):
            issues.append(_issue(
                "zero_allocation_arm",
                "both plan arms must receive a positive configured percentage"))

    # Every referenced metric must be defined on THIS version (a metric
    # living on another version is a cross-version reference), at most once,
    # and must carry a bounded attribution window so the snapshot can wait
    # for every user's window to elapse.
    version_metrics = {m.metric_key: m
                       for m in repo.list_metrics(experiment_key, version)}
    referenced = ([("primary", payload.primary.metric_key)]
                  + [(f"guardrails.{i}", g.metric_key)
                     for i, g in enumerate(payload.guardrails)])
    seen: dict[str, str] = {}
    for location, metric_key in referenced:
        if metric_key in seen:
            issues.append(_issue(
                "duplicate_metric",
                f"metric {metric_key!r} is referenced more than once "
                f"(first at {seen[metric_key]}); each plan metric must be "
                "distinct", location))
            continue
        seen[metric_key] = location
        metric = version_metrics.get(metric_key)
        if metric is None:
            issues.append(_issue(
                "metric_not_on_version",
                f"metric {metric_key!r} is not defined on version {version}; "
                "cross-version metric references are not allowed in a "
                "release plan", location))
        elif metric.attribution_window_seconds <= 0:
            issues.append(_issue(
                "unbounded_attribution_window",
                f"metric {metric_key!r} has an unbounded attribution window "
                "(attribution_window_seconds = 0); a release plan requires a "
                "bounded window so snapshots can wait for every user's "
                "window to elapse", location))

    if issues:
        raise ValidationRejected("invalid_release_plan",
                                 "release plan validation failed", issues)

    # The plan must predate every exposure of the analyzed version.
    existing = repo.count_exposures_before(experiment_key, version,
                                           to_iso(utcnow()))
    if existing > 0:
        raise ConflictError(
            "plan_after_exposure",
            f"version {version} already has {existing} enrolled exposure(s); "
            "a release-decision plan must be created before any exposure")

    plan = repo.create_release_plan({
        "plan_key": payload.plan_key,
        "experiment_key": experiment_key,
        "version_number": version,
        "control_variant_key": payload.control_variant_key,
        "target_variant_key": payload.target_variant_key,
        "primary_metric_key": payload.primary.metric_key,
        "primary_min_effect": payload.primary.min_favorable_effect,
        "correction": payload.correction,
        "alpha": payload.alpha,
        "guardrails": [{"metric_key": g.metric_key,
                        "non_inferiority_margin": g.non_inferiority_margin}
                       for g in payload.guardrails],
    })
    return _plan_out(plan, [])


@router.get("/{experiment_key}/release-plans",
            response_model=ReleasePlanListResponse,
            summary="List release-decision plans of an experiment")
def list_plans(experiment_key: str) -> ReleasePlanListResponse:
    plans = repo.list_release_plans(experiment_key)
    return ReleasePlanListResponse(items=[_plan_out_loaded(p) for p in plans])


@plans_router.get("/{plan_key}", response_model=ReleasePlanOut,
                  summary="Fetch one immutable release-decision plan")
def get_plan(plan_key: str) -> ReleasePlanOut:
    return _plan_out_loaded(repo.get_release_plan(plan_key))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _snapshot_response(plan: repo.ReleasePlan, row: dict[str, Any],
                       duplicate: bool) -> ReleaseSnapshotOut:
    result = row["result"]
    return ReleaseSnapshotOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number,
        sequence=row["sequence"], cutoff_at=row["cutoff_at"],
        submitted_at=row["created_at"], duplicate=duplicate,
        result=ReleaseSnapshotResult(
            metrics=[ReleaseMetricResult(
                **{**m,
                   "arms": {role: ReleaseArmRow(**arm)
                            for role, arm in m["arms"].items()},
                   "ci95": ConfidenceInterval(**m["ci95"])})
                for m in result["metrics"]],
            decision=ReleaseDecisionBlock(**result["decision"])),
        formulas=release.RELEASE_FORMULAS)


@plans_router.post("/{plan_key}/snapshots",
                   response_model=ReleaseSnapshotOut, status_code=201,
                   summary="Request a frozen as-of release-decision snapshot")
def submit_snapshot(plan_key: str,
                    payload: ReleaseSnapshotCreate) -> ReleaseSnapshotOut:
    plan = repo.get_release_plan(plan_key)

    cutoff = payload.cutoff_at
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc)
    cutoff_iso = to_iso(cutoff)

    # Idempotent replay takes precedence over EVERY other rule: a cutoff that
    # already has a frozen snapshot must be returned verbatim even if the
    # wall clock has since moved or the submitted cutoff is now in the past.
    frozen = repo.get_release_snapshot_at(plan.plan_key, cutoff_iso)
    if frozen is not None:
        return _snapshot_response(plan, frozen, True)

    now = utcnow()
    if cutoff > now:
        raise APIError(
            422, "cutoff_in_future",
            f"cutoff {cutoff_iso} is later than the current server time "
            f"{to_iso(now)}; a snapshot can only freeze as-of slices of "
            "data already observed")
    if cutoff <= parse_iso(plan.created_at):
        raise APIError(
            422, "cutoff_before_plan",
            f"cutoff {cutoff_iso} must be later than plan creation "
            f"{plan.created_at}")

    metrics = _plan_metrics(plan)

    # ---- as-of slice: every input row is strictly before the cutoff -------
    event_names = {m.event_name for m in metrics.values()}
    events_by_name = {name: repo.events_before(plan.experiment_key, name,
                                               cutoff_iso)
                      for name in event_names}
    version_rows = repo.version_exposures_before(
        plan.experiment_key, plan.version_number, cutoff_iso)
    all_enrolled = repo.enrolled_exposures_before(
        plan.experiment_key, None, cutoff_iso)
    published = repo.published_versions(plan.experiment_key)

    result = release.evaluate_snapshot(
        _plan_dict(plan), metrics, events_by_name,
        version_exposures=version_rows,
        all_enrolled_exposures=all_enrolled,
        published_versions=published, cutoff=cutoff)

    inserted, row = repo.insert_release_snapshot(
        plan.plan_key, cutoff_iso, result["decision"]["decision"], result)
    return _snapshot_response(plan, row, duplicate=not inserted)


@plans_router.get("/{plan_key}/snapshots",
                  response_model=ReleaseSnapshotHistoryResponse,
                  summary="Frozen snapshot history for a release plan")
def snapshot_history(plan_key: str) -> ReleaseSnapshotHistoryResponse:
    plan = repo.get_release_plan(plan_key)
    rows = repo.list_release_snapshots(plan.plan_key)
    summary: list[ReleaseSnapshotSummaryRow] = []
    for c in rows:
        r = c["result"]
        summary.append(ReleaseSnapshotSummaryRow(
            sequence=c["sequence"], snapshot_id=c["id"],
            cutoff_at=c["cutoff_at"], decision=r["decision"]["decision"],
            primary_status=r["metrics"][0]["status"],
            guardrail_statuses={m["metric_key"]: m["status"]
                                for m in r["metrics"][1:]},
            formula=r["decision"]["formula"]))
    return ReleaseSnapshotHistoryResponse(plan=_plan_out(plan, rows),
                                          snapshots=summary)


@plans_router.get("/{plan_key}/snapshots/{sequence}",
                  response_model=ReleaseSnapshotOut,
                  summary="Fetch one frozen release-decision snapshot by sequence")
def get_snapshot(plan_key: str, sequence: int) -> ReleaseSnapshotOut:
    plan = repo.get_release_plan(plan_key)
    for c in repo.list_release_snapshots(plan.plan_key):
        if c["sequence"] == sequence:
            return _snapshot_response(plan, c, True)
    raise NotFoundError(
        f"release snapshot {sequence} not found for plan {plan_key!r}")
