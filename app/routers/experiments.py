"""Experiment / version / decision endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Query
from pydantic import Field

from .. import engine, repository as repo, services
from ..errors import ConflictError, ValidationRejected
from ..schemas import (
    BatchDecideRequest,
    BatchDecideResponse,
    BatchItem,
    DecisionResponse,
    ExperimentCreate,
    ExperimentOut,
    PreflightResponse,
    SimulateRequest,
    SimulateResponse,
    StrictModel,
    ValidationIssue,
    VersionConfigIn,
    VersionOut,
)
from ..validation import validate_structure

router = APIRouter(prefix="/experiments", tags=["experiments"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _experiment_out(row: dict[str, Any]) -> ExperimentOut:
    return ExperimentOut(
        id=row["id"], key=row["key"], name=row["name"],
        namespace=row["namespace"], salt=row["salt"],
        created_at=row["created_at"],
        latest_version=row.get("latest_version"),
        published_version=row.get("published_version"),
    )


def _version_out(loaded: repo.LoadedVersion) -> VersionOut:
    return VersionOut(
        id=loaded.id, experiment_key=loaded.experiment_key,
        version=loaded.version, status=loaded.status,
        traffic_percentage=loaded.traffic_percentage,
        namespace=loaded.namespace, config=loaded.config,
        created_at=loaded.created_at, published_at=loaded.published_at,
    )


def _reject_if_issues(issues: list[ValidationIssue], code: str = "invalid_config") -> None:
    if issues:
        raise ValidationRejected(
            code,
            "; ".join(i.message for i in issues),
            issues=[i.model_dump() for i in issues],
        )


# ---------------------------------------------------------------------------
# experiment + version management
# ---------------------------------------------------------------------------


@router.post("", response_model=ExperimentOut, status_code=201,
             summary="Create an experiment (optionally with its first version)")
def create_experiment(payload: ExperimentCreate) -> ExperimentOut:
    namespace = payload.namespace or payload.key
    row = repo.create_experiment(payload.key, payload.name, namespace, payload.salt)

    if payload.config is not None:
        if payload.publish:
            issues = services.run_preflight(payload.key, payload.config)
            _reject_if_issues(issues)
            status = "published"
        else:
            issues = validate_structure(payload.config)
            _reject_if_issues(issues)
            status = "draft"
        repo.create_version(payload.key, payload.config, status)

    full = repo.list_experiments()
    return _experiment_out(next(e for e in full if e["key"] == payload.key))


@router.get("", response_model=list[ExperimentOut], summary="List experiments")
def list_experiments() -> list[ExperimentOut]:
    return [_experiment_out(r) for r in repo.list_experiments()]


@router.get("/{experiment_key}", response_model=ExperimentOut)
def get_experiment(experiment_key: str) -> ExperimentOut:
    row = repo.get_experiment(experiment_key)
    rows = {r["key"]: r for r in repo.list_experiments()}
    return _experiment_out(rows[experiment_key])


@router.post("/{experiment_key}/versions", response_model=VersionOut,
             status_code=201,
             summary="Create a new immutable version (draft unless publish=true)")
def create_version(experiment_key: str, payload: VersionConfigIn,
                   publish: bool = Query(False)) -> VersionOut:
    repo.get_experiment(experiment_key)  # 404 early
    if publish:
        issues = services.run_preflight(experiment_key, payload)
    else:
        issues = validate_structure(payload)
    _reject_if_issues(issues)
    status = "published" if publish else "draft"
    return _version_out(repo.create_version(experiment_key, payload, status))


@router.get("/{experiment_key}/versions", response_model=list[VersionOut],
            summary="List all versions (audit history)")
def list_versions(experiment_key: str) -> list[VersionOut]:
    return [_version_out(v) for v in repo.list_versions(experiment_key)]


@router.get("/{experiment_key}/versions/{version}", response_model=VersionOut)
def get_version(experiment_key: str, version: int) -> VersionOut:
    return _version_out(repo.get_version(experiment_key, version))


@router.post("/{experiment_key}/versions/{version}/publish",
             response_model=VersionOut,
             summary="Validate against mutex rules and publish a draft version")
def publish_version(experiment_key: str, version: int) -> VersionOut:
    loaded = repo.get_version(experiment_key, version)
    if loaded.status == "published":
        raise ConflictError("version_already_published",
                            f"version {version} is already published and immutable")
    issues = services.run_preflight(experiment_key, loaded.config)
    _reject_if_issues(issues)
    return _version_out(repo.publish_version(experiment_key, version))


@router.post("/{experiment_key}/preflight", response_model=PreflightResponse,
             summary="Validate a candidate config without storing it")
def preflight(experiment_key: str, payload: VersionConfigIn) -> PreflightResponse:
    issues = services.run_preflight(experiment_key, payload)
    return PreflightResponse(valid=not issues, issues=issues)


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


class DecideRequest(StrictModel):
    user_key: str = Field(min_length=1, max_length=256)
    attributes: dict[str, Any] = Field(default_factory=dict)
    version: Optional[int] = Field(
        default=None, description="pin to a specific published version")
    at: Optional[datetime] = None
    record_exposure: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


@router.post("/{experiment_key}/decide", response_model=DecisionResponse,
             summary="Single-user traffic decision with full trace")
def decide(experiment_key: str, payload: DecideRequest) -> DecisionResponse:
    if payload.record_exposure:
        result = engine.decide_and_record(
            experiment_key, payload.user_key, payload.attributes,
            version=payload.version, at=payload.at,
            idempotency_key=payload.idempotency_key,
        )
    else:
        result = engine.decide(
            experiment_key, payload.user_key, payload.attributes,
            version=payload.version, at=payload.at,
        ).to_dict()
    return DecisionResponse.model_validate(result)


@router.post("/decide/batch", response_model=BatchDecideResponse,
             summary="Decide one user across up to 500 experiments")
def decide_batch(payload: BatchDecideRequest) -> BatchDecideResponse:
    items: list[BatchItem] = []
    for key in payload.experiment_keys:
        try:
            if payload.record_exposure:
                result = engine.decide_and_record(
                    key, payload.user_key, payload.attributes,
                    version=payload.version, at=payload.at,
                    idempotency_key=(
                        f"{payload.idempotency_key}:{key}"
                        if payload.idempotency_key else None),
                )
            else:
                result = engine.decide(
                    key, payload.user_key, payload.attributes,
                    version=payload.version, at=payload.at,
                ).to_dict()
            items.append(BatchItem(experiment_key=key,
                                   decision=DecisionResponse.model_validate(result)))
        except Exception as exc:  # per-item isolation
            status = getattr(exc, "status_code", 500)
            code = getattr(exc, "code", "internal_error")
            items.append(BatchItem(experiment_key=key,
                                   error=f"[{status}] {exc}" if status >= 400 else str(exc),
                                   error_code=code))
    return BatchDecideResponse(results=items)


@router.post("/{experiment_key}/simulate", response_model=SimulateResponse,
             summary="Monte-Carlo the split distribution without recording exposures")
def simulate(experiment_key: str, payload: SimulateRequest,
             version: Optional[int] = Query(None)) -> SimulateResponse:
    result = services.simulate(
        experiment_key, payload.users, payload.attributes,
        payload.user_key_prefix, at=payload.at, version=version,
    )
    return SimulateResponse.model_validate(result)
