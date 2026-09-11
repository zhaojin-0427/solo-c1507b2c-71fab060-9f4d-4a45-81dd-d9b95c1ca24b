"""CUPED covariate-adjustment plans and frozen as-of snapshots.

A plan is created on a published/draft version's **continuous** metric
BEFORE any exposure on that version and can never be changed afterwards
(the DB layer rejects updates outright). Callers then request snapshots at
cutoff instants: covariate events are read only from each user's fixed
pre-exposure window, target events only when occurred strictly before the
cutoff, and the full result (including θ, CIs and variance-reduction rates)
is frozen at submit time. Re-requesting the same plan + cutoff returns the
stored snapshot; later events never change it.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Optional

from fastapi import APIRouter

from .. import cuped
from .. import repository as repo
from ..errors import APIError, ConflictError, NotFoundError, ValidationRejected
from ..schemas import (
    CupedComparisonRow,
    CupedPlanCreate,
    CupedPlanListResponse,
    CupedPlanOut,
    CupedSnapshotCreate,
    CupedSnapshotHistoryResponse,
    CupedSnapshotOut,
    CupedSnapshotResult,
    CupedSnapshotSummaryRow,
    CupedThetaBlock,
    CupedTotals,
    CupedVariantRow,
    ConfidenceInterval,
    MetricDefOut,
)
from ..time_utils import parse_iso, to_iso, utcnow

router = APIRouter(prefix="/experiments", tags=["cuped"])
plans_router = APIRouter(prefix="/cuped-plans", tags=["cuped"])


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


def _plan_out(plan: repo.CupedPlan,
              snapshots: list[dict[str, Any]]) -> CupedPlanOut:
    metric = repo.get_metric(plan.experiment_key, plan.version_number,
                             plan.metric_key)
    return CupedPlanOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number, metric=_metric_out(metric),
        covariate_event_name=plan.covariate_event_name,
        preexposure_window_seconds=plan.preexposure_window_seconds,
        target_aggregation=plan.target_aggregation,
        covariate_aggregation=plan.covariate_aggregation,
        missing_covariate_policy=plan.missing_covariate_policy,
        snapshots_recorded=len(snapshots), created_at=plan.created_at)


def _plan_out_loaded(plan: repo.CupedPlan) -> CupedPlanOut:
    return _plan_out(plan, repo.list_cuped_snapshots(plan.plan_key))


def _plan_dict(plan: repo.CupedPlan) -> dict[str, Any]:
    return {
        "plan_key": plan.plan_key,
        "experiment_key": plan.experiment_key,
        "version_number": plan.version_number,
        "metric_key": plan.metric_key,
        "covariate_event_name": plan.covariate_event_name,
        "preexposure_window_seconds": plan.preexposure_window_seconds,
        "target_aggregation": plan.target_aggregation,
        "covariate_aggregation": plan.covariate_aggregation,
        "missing_covariate_policy": plan.missing_covariate_policy,
    }


# ---------------------------------------------------------------------------
# Plan creation / listing
# ---------------------------------------------------------------------------


@router.post("/{experiment_key}/versions/{version}/metrics/"
             "{metric_key}/cuped-plans",
             response_model=CupedPlanOut, status_code=201,
             summary="Create an immutable pre-exposure CUPED adjustment plan")
def create_plan(experiment_key: str, version: int, metric_key: str,
                payload: CupedPlanCreate) -> CupedPlanOut:
    repo.get_version(experiment_key, version)  # 404 chain
    metric = repo.get_metric(experiment_key, version, metric_key)

    issues: list[dict[str, Any]] = []
    if metric.metric_type != "continuous":
        issues.append(_issue(
            "metric_not_continuous",
            f"metric {metric_key!r} is {metric.metric_type!r}; CUPED "
            "covariate adjustment requires a continuous metric",
            "metric_key"))
    if payload.preexposure_window_seconds <= 0:
        # Structured form of the schema-side bound, kept for a stable code.
        issues.append(_issue(
            "invalid_preexposure_window",
            "preexposure_window_seconds must be a positive number of seconds",
            "preexposure_window_seconds"))
    if issues:
        raise ValidationRejected("invalid_cuped_plan",
                                 "cuped plan validation failed", issues)

    # The plan must predate every exposure of the analyzed version.
    existing = repo.count_exposures_before(experiment_key, version,
                                           to_iso(utcnow()))
    if existing > 0:
        raise ConflictError(
            "plan_after_exposure",
            f"version {version} already has {existing} exposure record(s); "
            "a CUPED plan must be created before the version produces any "
            "exposure so its pre-exposure window cannot leak treatment data")

    if repo.get_cuped_plan_for_metric(experiment_key, version,
                                      metric_key) is not None:
        raise ConflictError(
            "cuped_plan_exists_for_metric",
            f"a CUPED plan already exists for {experiment_key!r} v{version} "
            f"metric {metric_key!r}")

    plan = repo.create_cuped_plan({
        "plan_key": payload.plan_key,
        "experiment_key": experiment_key,
        "version_number": version,
        "metric_key": metric_key,
        "covariate_event_name": payload.covariate_event_name,
        "preexposure_window_seconds": payload.preexposure_window_seconds,
        "target_aggregation": payload.target_aggregation,
        "covariate_aggregation": payload.covariate_aggregation,
        "missing_covariate_policy": payload.missing_covariate_policy,
    })
    return _plan_out(plan, [])


@router.get("/{experiment_key}/cuped-plans",
            response_model=CupedPlanListResponse,
            summary="List CUPED plans of an experiment")
def list_plans(experiment_key: str) -> CupedPlanListResponse:
    plans = repo.list_cuped_plans(experiment_key)
    return CupedPlanListResponse(items=[_plan_out_loaded(p) for p in plans])


@plans_router.get("/{plan_key}", response_model=CupedPlanOut,
                  summary="Fetch one immutable CUPED plan")
def get_plan(plan_key: str) -> CupedPlanOut:
    return _plan_out_loaded(repo.get_cuped_plan(plan_key))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _snapshot_response(plan: repo.CupedPlan, row: dict[str, Any],
                       duplicate: bool) -> CupedSnapshotOut:
    result = row["result"]
    return CupedSnapshotOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number, metric_key=plan.metric_key,
        sequence=row["sequence"], cutoff_at=row["cutoff_at"],
        submitted_at=row["created_at"], duplicate=duplicate,
        result=CupedSnapshotResult(
            theta=CupedThetaBlock(**result["theta"]),
            variants=[CupedVariantRow(
                **{**v,
                   "ci95": ConfidenceInterval(**v["ci95"]),
                   "raw_ci95": ConfidenceInterval(**v["raw_ci95"])})
                for v in result["variants"]],
            comparisons=[CupedComparisonRow(
                **{**c,
                   "ci95": ConfidenceInterval(**c["ci95"]),
                   "raw_ci95": ConfidenceInterval(**c["raw_ci95"])})
                for c in result["comparisons"]],
            totals=CupedTotals(**result["totals"]),
            anomalies=result["anomalies"],
            adjusted=result["adjusted"]),
        formulas=cuped.CUPED_FORMULAS)


@plans_router.post("/{plan_key}/snapshots",
                   response_model=CupedSnapshotOut, status_code=201,
                   summary="Request a frozen as-of CUPED snapshot")
def submit_snapshot(plan_key: str,
                    payload: CupedSnapshotCreate) -> CupedSnapshotOut:
    plan = repo.get_cuped_plan(plan_key)

    cutoff = payload.cutoff_at
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc)
    cutoff_iso = to_iso(cutoff)

    # Idempotent replay takes precedence over EVERY other rule: a cutoff that
    # already has a frozen snapshot must be returned verbatim even if the
    # wall clock has since moved or the submitted cutoff is now in the past.
    frozen = repo.get_cuped_snapshot_at(plan.plan_key, cutoff_iso)
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

    loaded = repo.get_version(plan.experiment_key, plan.version_number)
    metric = repo.get_metric(plan.experiment_key, plan.version_number,
                             plan.metric_key)
    variants = sorted(loaded.config.variants, key=lambda v: v.key)

    # ---- as-of slice: every input row is strictly before the cutoff -------
    target_events = repo.events_before(plan.experiment_key,
                                       metric.event_name, cutoff_iso)
    covariate_events = repo.events_before(plan.experiment_key,
                                          plan.covariate_event_name,
                                          cutoff_iso)
    version_rows = repo.version_exposures_before(
        plan.experiment_key, plan.version_number, cutoff_iso)
    all_enrolled = repo.enrolled_exposures_before(
        plan.experiment_key, None, cutoff_iso)
    published = repo.published_versions(plan.experiment_key)

    result = cuped.evaluate_snapshot(
        metric, _plan_dict(plan),
        variant_keys=[v.key for v in variants],
        control_key=loaded.config.control_variant_key,
        target_events=target_events,
        covariate_events=covariate_events,
        version_exposures=version_rows,
        all_enrolled_exposures=all_enrolled,
        published_versions=published)

    inserted, row = repo.insert_cuped_snapshot(
        plan.plan_key, cutoff_iso,
        result["theta"]["value"], result["totals"]["paired_users"],
        result["anomalies"], result)
    return _snapshot_response(plan, row, duplicate=not inserted)


@plans_router.get("/{plan_key}/snapshots",
                  response_model=CupedSnapshotHistoryResponse,
                  summary="Frozen snapshot history for a CUPED plan")
def snapshot_history(plan_key: str) -> CupedSnapshotHistoryResponse:
    plan = repo.get_cuped_plan(plan_key)
    rows = repo.list_cuped_snapshots(plan.plan_key)
    summary: list[CupedSnapshotSummaryRow] = []
    for c in rows:
        r = c["result"]
        theta = r["theta"]
        summary.append(CupedSnapshotSummaryRow(
            sequence=c["sequence"], snapshot_id=c["id"],
            cutoff_at=c["cutoff_at"], theta=theta["value"],
            paired_users=r["totals"]["paired_users"],
            anomalies=r["anomalies"], adjusted=r["adjusted"],
            overall_variance_reduction_rate=(
                r["totals"]["overall_variance_reduction_rate"]),
            formula=theta["formula"]))
    return CupedSnapshotHistoryResponse(plan=_plan_out(plan, rows),
                                        snapshots=summary)


@plans_router.get("/{plan_key}/snapshots/{sequence}",
                  response_model=CupedSnapshotOut,
                  summary="Fetch one frozen CUPED snapshot by sequence number")
def get_snapshot(plan_key: str, sequence: int) -> CupedSnapshotOut:
    plan = repo.get_cuped_plan(plan_key)
    for c in repo.list_cuped_snapshots(plan.plan_key):
        if c["sequence"] == sequence:
            return _snapshot_response(plan, c, True)
    raise NotFoundError(
        f"cuped snapshot {sequence} not found for plan {plan_key!r}")
