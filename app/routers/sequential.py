"""Sequential analysis plans and group-sequential checkpoints.

A plan is created on a published/draft version's metric BEFORE any exposure
on that version and can never be changed afterwards (the DB layer rejects
updates outright). Callers then submit checkpoints at cutoff instants: only
exposures/events strictly before the cutoff are read, and the full result is
frozen at submit time. Re-submitting the same cutoff returns the stored
snapshot; cutoffs must strictly increase in both time and information.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Optional

from fastapi import APIRouter

from .. import metrics as metrics_mod
from .. import repository as repo
from .. import sequential as seq
from ..errors import APIError, ConflictError, NotFoundError, ValidationRejected
from ..schemas import (
    CheckpointConditionalPower,
    CheckpointCreate,
    CheckpointCumulativeAlpha,
    CheckpointEffect,
    CheckpointHistoryResponse,
    CheckpointInformation,
    CheckpointOut,
    CheckpointResult,
    CheckpointStatistic,
    CheckpointSummaryRow,
    CheckpointArmRow,
    CheckpointBoundary,
    ConditionalPowerResult,
    DesignConditionalPower,
    MetricDefOut,
    PlannedBoundaryRow,
    SequentialPlanCreate,
    SequentialPlanListResponse,
    SequentialPlanOut,
)
from ..time_utils import parse_iso, to_iso, utcnow

# Experiment-scoped routes (creation/listing) and global plan-key routes.
router = APIRouter(prefix="/experiments", tags=["sequential-testing"])
plans_router = APIRouter(prefix="/sequential-plans",
                         tags=["sequential-testing"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue(code: str, message: str, location: Optional[str] = None) -> dict[str, Any]:
    return {"code": code, "message": message, "location": location}


def _load_chain(experiment_key: str, version: int, metric_key: str):
    loaded = repo.get_version(experiment_key, version)            # 404 chain
    metric = repo.get_metric(experiment_key, version, metric_key)
    return loaded, metric


def _state(checkpoints: list[dict[str, Any]]) -> str:
    if not checkpoints:
        return "active"
    return "terminated" if checkpoints[-1]["result"]["terminal"] else "active"


def _plan_out(plan: repo.SequentialPlan,
              checkpoints: list[dict[str, Any]]) -> SequentialPlanOut:
    metric = repo.get_metric(plan.experiment_key, plan.version_number,
                             plan.metric_key)
    return SequentialPlanOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number, metric=MetricDefOut(
            id=metric.id, experiment_key=metric.experiment_key,
            version_number=metric.version_number, metric_key=metric.metric_key,
            metric_type=metric.metric_type, event_name=metric.event_name,
            attribution_window_seconds=metric.attribution_window_seconds,
            direction=metric.direction,
            min_sample_size=metric.min_sample_size,
            srm_threshold=metric.srm_threshold, created_at=metric.created_at),
        control_variant_key=plan.control_variant_key,
        target_variant_key=plan.target_variant_key,
        hypothesis=plan.hypothesis, direction=plan.direction,
        alpha=plan.alpha, max_sample_size=plan.max_sample_size,
        planned_checks=plan.planned_checks,
        conditional_power_threshold=plan.conditional_power_threshold,
        boundary_type=plan.boundary_type,
        design_assumptions=plan.design_assumptions,
        allocation={"control": plan.control_share,
                    "target": plan.target_share},
        state=_state(checkpoints), checkpoints_recorded=len(checkpoints),
        planned_boundaries=[PlannedBoundaryRow(**r)
                            for r in plan.computed["planned_boundaries"]],
        created_at=plan.created_at)


def _plan_out_loaded(plan: repo.SequentialPlan) -> SequentialPlanOut:
    return _plan_out(plan, repo.list_checkpoints(plan.plan_key))


def _plan_dict(plan: repo.SequentialPlan) -> dict[str, Any]:
    da = plan.design_assumptions
    return {
        "plan_key": plan.plan_key,
        "experiment_key": plan.experiment_key,
        "version_number": plan.version_number,
        "metric_key": plan.metric_key,
        "metric_type": repo.get_metric(
            plan.experiment_key, plan.version_number,
            plan.metric_key).metric_type,
        "control_variant_key": plan.control_variant_key,
        "target_variant_key": plan.target_variant_key,
        "hypothesis": plan.hypothesis,
        "direction": plan.direction,
        "alpha": plan.alpha,
        "max_sample_size": plan.max_sample_size,
        "planned_checks": plan.planned_checks,
        "conditional_power_threshold": plan.conditional_power_threshold,
        "boundary_type": plan.boundary_type,
        "design_assumptions": da,
        "control_share": plan.control_share,
        "target_share": plan.target_share,
    }


# ---------------------------------------------------------------------------
# Plan creation / listing
# ---------------------------------------------------------------------------


@router.post("/{experiment_key}/versions/{version}/metrics/"
             "{metric_key}/sequential-plans",
             response_model=SequentialPlanOut, status_code=201,
             summary="Create an immutable pre-exposure sequential analysis plan")
def create_plan(experiment_key: str, version: int, metric_key: str,
                payload: SequentialPlanCreate) -> SequentialPlanOut:
    loaded, metric = _load_chain(experiment_key, version, metric_key)
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
    if not issues:
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

    # Design assumptions must match the metric type and optimization sense.
    da = payload.design_assumptions
    if da is not None:
        if da.kind != metric.metric_type:
            issues.append(_issue(
                "assumption_metric_type",
                f"design assumptions kind {da.kind!r} does not match metric "
                f"type {metric.metric_type!r}", "design_assumptions"))
        elif da.kind == "binary":
            target_rate = da.control_rate + da.absolute_effect
            if not 0.0 < target_rate < 1.0:
                issues.append(_issue(
                    "rate_out_of_range",
                    f"assumed target rate {target_rate:g} must lie within (0, 1)",
                    "design_assumptions.absolute_effect"))
            if metric.direction == "maximize" and da.absolute_effect <= 0:
                issues.append(_issue(
                    "effect_direction",
                    "an absolute_effect > 0 is required for a maximize metric",
                    "design_assumptions.absolute_effect"))
            if metric.direction == "minimize" and da.absolute_effect >= 0:
                issues.append(_issue(
                    "effect_direction",
                    "an absolute_effect < 0 is required for a minimize metric",
                    "design_assumptions.absolute_effect"))
        elif da.kind == "continuous":
            if metric.direction == "maximize" and da.absolute_effect <= 0:
                issues.append(_issue(
                    "effect_direction",
                    "an absolute_effect > 0 is required for a maximize metric",
                    "design_assumptions.absolute_effect"))
            if metric.direction == "minimize" and da.absolute_effect >= 0:
                issues.append(_issue(
                    "effect_direction",
                    "an absolute_effect < 0 is required for a minimize metric",
                    "design_assumptions.absolute_effect"))

    # The maximal sample must be enough to realize the planned number of looks
    # with at least one exposure per arm between them.
    min_required = 2 * payload.planned_checks
    if payload.max_sample_size < min_required:
        issues.append(_issue(
            "max_sample_too_small",
            f"max_sample_size {payload.max_sample_size} cannot support "
            f"{payload.planned_checks} strictly-informative looks "
            f"(minimum total sample: {min_required})", "max_sample_size"))

    if issues:
        raise ValidationRejected("invalid_sequential_plan",
                                 "sequential plan validation failed", issues)

    # The plan must predate every exposure of the analyzed version.
    existing = repo.count_exposures_before(experiment_key, version,
                                           to_iso(utcnow()))
    if existing > 0:
        raise ConflictError(
            "plan_after_exposure",
            f"version {version} already has {existing} enrolled exposure(s); "
            "a sequential analysis plan must be created before any exposure")

    if repo.get_sequential_plan_for_metric(experiment_key, version,
                                           metric_key) is not None:
        raise ConflictError(
            "sequential_plan_exists_for_metric",
            f"a sequential plan already exists for {experiment_key!r} v"
            f"{version} metric {metric_key!r}")

    shares = {payload.control_variant_key:
              variant_map[payload.control_variant_key].percentage / 100.0,
              payload.target_variant_key:
              variant_map[payload.target_variant_key].percentage / 100.0}

    plan_core = {
        "plan_key": payload.plan_key,
        "experiment_key": experiment_key,
        "version_number": version,
        "metric_key": metric_key,
        "control_variant_key": payload.control_variant_key,
        "target_variant_key": payload.target_variant_key,
        "hypothesis": payload.hypothesis,
        "direction": metric.direction,
        "alpha": payload.alpha,
        "max_sample_size": payload.max_sample_size,
        "planned_checks": payload.planned_checks,
        "conditional_power_threshold":
            payload.conditional_power_threshold,
        "boundary_type": payload.boundary_type,
        "design_assumptions": (da.model_dump() if da is not None else None),
        "control_share": shares[payload.control_variant_key],
        "target_share": shares[payload.target_variant_key],
    }
    boundaries = seq.planned_boundary_table(plan_core)
    plan_core["computed"] = {"planned_boundaries": boundaries}
    plan = repo.create_sequential_plan(plan_core)
    return _plan_out(plan, [])


@router.get("/{experiment_key}/sequential-plans",
            response_model=SequentialPlanListResponse,
            summary="List sequential plans of an experiment")
def list_plans(experiment_key: str) -> SequentialPlanListResponse:
    plans = repo.list_sequential_plans(experiment_key)
    return SequentialPlanListResponse(
        items=[_plan_out_loaded(p) for p in plans])


@plans_router.get("/{plan_key}", response_model=SequentialPlanOut,
                  summary="Fetch one immutable sequential plan")
def get_plan(plan_key: str) -> SequentialPlanOut:
    plan = repo.get_sequential_plan(plan_key)
    return _plan_out_loaded(plan)


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def _checkpoint_response(plan: repo.SequentialPlan, cutoff_iso: str,
                         row: dict[str, Any], duplicate: bool) -> CheckpointOut:
    checkpoints = repo.list_checkpoints(plan.plan_key)
    result = row["result"]
    cp = result["conditional_power"]
    out = CheckpointOut(
        plan_key=plan.plan_key, experiment_key=plan.experiment_key,
        version_number=plan.version_number, metric_key=plan.metric_key,
        cutoff_at=row["cutoff_at"], submitted_at=row["created_at"],
        duplicate=duplicate, state=_state(checkpoints),
        result=CheckpointResult(
            sequence=result["sequence"],
            information=CheckpointInformation(**result["information"]),
            arms=[CheckpointArmRow(**a) for a in result["arms"]],
            effect=CheckpointEffect(**result["effect"]),
            statistic=CheckpointStatistic(**result["statistic"]),
            cumulative_alpha=CheckpointCumulativeAlpha(
                **result["cumulative_alpha"]),
            boundary=CheckpointBoundary(**result["boundary"]),
            conditional_power=CheckpointConditionalPower(
                threshold=cp["threshold"], basis=cp["basis"],
                observed=(None if cp["observed"] is None
                          else ConditionalPowerResult(**cp["observed"])),
                design=(None if cp["design"] is None
                        else DesignConditionalPower(**cp["design"])),
                formula=cp["formula"]),
            recommendation=result["recommendation"],
            terminal=result["terminal"],
            stop_reasons=result["stop_reasons"],
            excluded=result["excluded"],
            events_attributed=result["events_attributed"]),
        formulas=seq.SEQ_FORMULAS)
    return out


@plans_router.post("/{plan_key}/checkpoints",
                   response_model=CheckpointOut, status_code=201,
                   summary="Submit an as-of checkpoint; the result is frozen")
def submit_checkpoint(plan_key: str,
                      payload: CheckpointCreate) -> CheckpointOut:
    plan = repo.get_sequential_plan(plan_key)
    prior = repo.list_checkpoints(plan.plan_key)

    cutoff = payload.cutoff_at
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc)
    cutoff_iso = to_iso(cutoff)

    # Idempotent replay takes precedence over EVERY other rule: a cutoff that
    # already has a frozen snapshot must return that snapshot verbatim, even
    # if the wall clock has since moved past it or the plan has terminated.
    existing = next((c for c in prior if c["cutoff_at"] == cutoff_iso), None)
    if existing is not None:
        return _checkpoint_response(plan, cutoff_iso, existing, True)

    # New cutoffs must strictly advance in time.
    if prior and cutoff_iso < prior[-1]["cutoff_at"]:
        raise APIError(
            422, "non_increasing_cutoff",
            f"cutoff {cutoff_iso} is earlier than the previous checkpoint "
            f"{prior[-1]['cutoff_at']}; back-dating checkpoints is not allowed")

    now = utcnow()
    if cutoff > now:
        raise APIError(
            422, "cutoff_in_future",
            f"cutoff {cutoff_iso} is later than the current server time "
            f"{to_iso(now)}; checkpoints can only freeze as-of slices of "
            "data already observed")

    # A cutoff at or before the plan's creation cannot be inspected: the plan
    # did not exist then.
    if cutoff <= parse_iso(plan.created_at):
        raise APIError(
            422, "cutoff_before_plan",
            f"cutoff {cutoff_iso} must be later than plan creation "
            f"{plan.created_at}")

    if prior and prior[-1]["result"]["terminal"]:
        raise ConflictError(
            "plan_terminated",
            f"plan {plan_key!r} already stopped at checkpoint "
            f"{prior[-1]['sequence']} ({prior[-1]['recommendation']}); "
            "further checkpoints are not accepted")

    sequence = len(prior) + 1
    if sequence > plan.planned_checks:
        raise ConflictError(
            "all_looks_used",
            f"plan {plan_key!r} allows {plan.planned_checks} checkpoints")

    # ---- as-of slice: every input row is strictly before the cutoff -------
    metric = repo.get_metric(plan.experiment_key, plan.version_number,
                             plan.metric_key)
    events = repo.events_before(plan.experiment_key, metric.event_name,
                                cutoff_iso)
    version_rows = repo.version_exposures_before(
        plan.experiment_key, plan.version_number, cutoff_iso)
    all_enrolled = repo.enrolled_exposures_before(
        plan.experiment_key, None, cutoff_iso)
    published = repo.published_versions(plan.experiment_key)

    agg = seq.aggregate_arms(metric, _plan_dict(plan), events,
                             version_rows, all_enrolled, published)
    prior_times = [c["information_time"] for c in prior]
    try:
        result = seq.evaluate_checkpoint(_plan_dict(plan), agg,
                                         prior_times, sequence)
    except seq.NotEvaluable as exc:
        raise APIError(422, exc.code, exc.message)

    inserted, row = repo.insert_checkpoint(
        plan.plan_key, sequence, cutoff_iso,
        result["information"]["information_time"],
        result["recommendation"], result)
    return _checkpoint_response(plan, cutoff_iso, row, duplicate=not inserted)


@plans_router.get("/{plan_key}/checkpoints",
                  response_model=CheckpointHistoryResponse,
                  summary="Checkpoint history with formulas and plugged values")
def checkpoint_history(plan_key: str) -> CheckpointHistoryResponse:
    plan = repo.get_sequential_plan(plan_key)
    rows = repo.list_checkpoints(plan.plan_key)
    summary: list[CheckpointSummaryRow] = []
    for c in rows:
        r = c["result"]
        cp = r["conditional_power"]
        combined_formula = (
            f"{r['statistic']['formula']} | {r['information']['formula']} | "
            f"{r['boundary']['formula']} | {r['cumulative_alpha']['formula']} | "
            f"{cp['formula']}")
        summary.append(CheckpointSummaryRow(
            sequence=r["sequence"], checkpoint_id=c["id"],
            cutoff_at=c["cutoff_at"],
            information_time=r["information"]["information_time"],
            recommendation=r["recommendation"],
            terminal=r["terminal"],
            z=r["statistic"]["z"],
            upper_z=r["boundary"]["upper_z"],
            lower_z=r["boundary"]["lower_z"],
            spent_alpha=r["cumulative_alpha"]["spent"],
            conditional_power_observed=(None if cp["observed"] is None
                                         else cp["observed"]["value"]),
            conditional_power_design=(None if cp["design"] is None
                                       else cp["design"]["value"]),
            control_n=r["information"]["control_n"],
            target_n=r["information"]["target_n"],
            formula=combined_formula))
    return CheckpointHistoryResponse(plan=_plan_out(plan, rows),
                                     checkpoints=summary)


@plans_router.get("/{plan_key}/checkpoints/{sequence}",
                  response_model=CheckpointOut,
                  summary="Fetch one frozen checkpoint snapshot by look number")
def get_checkpoint(plan_key: str, sequence: int) -> CheckpointOut:
    plan = repo.get_sequential_plan(plan_key)
    for c in repo.list_checkpoints(plan.plan_key):
        if c["sequence"] == sequence:
            return _checkpoint_response(plan, c["cutoff_at"], c, True)
    raise NotFoundError(
        f"checkpoint {sequence} not found for plan {plan_key!r}")
