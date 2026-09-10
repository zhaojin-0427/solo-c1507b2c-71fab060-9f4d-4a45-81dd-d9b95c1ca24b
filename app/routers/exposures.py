"""Exposure records: traces, summaries, idempotent lookup."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import repository as repo
from ..schemas import (
    ExposureListResponse,
    ExposureOut,
    ExposureSummaryResponse,
    VariantCount,
)

router = APIRouter(prefix="/experiments", tags=["exposures"])


@router.get("/{experiment_key}/exposures/summary",
            response_model=ExposureSummaryResponse,
            summary="Aggregated exposure counts per variant and miss reason")
def summary(experiment_key: str,
            version: int | None = Query(None, description="filter to one config version")) -> ExposureSummaryResponse:
    repo.get_experiment(experiment_key)  # 404 early
    data = repo.exposure_summary(experiment_key, version)
    return ExposureSummaryResponse(
        experiment_key=data["experiment_key"],
        version=data["version"],
        total_decisions=data["total_decisions"],
        enrolled=data["enrolled"],
        not_enrolled=data["not_enrolled"],
        by_variant=[VariantCount(**r) for r in data["by_variant"]],
        by_reason=data["by_reason"],
    )


@router.get("/{experiment_key}/exposures", response_model=ExposureListResponse,
            summary="Browse exposure records (traceable to the config version)")
def list_exposures(experiment_key: str,
                   variant: str | None = Query(None),
                   user_key: str | None = Query(None),
                   limit: int = Query(100, ge=1, le=1000)) -> ExposureListResponse:
    repo.get_experiment(experiment_key)
    rows = repo.list_exposures(experiment_key, variant=variant,
                               user_key=user_key, limit=limit)
    return ExposureListResponse(items=[ExposureOut.model_validate(r) for r in rows])


router_lookup = APIRouter(tags=["exposures"])


@router_lookup.get("/exposures/idempotency/{idempotency_key}",
                   response_model=ExposureOut,
                   summary="Retrieve a decision exactly by its idempotency key")
def exposure_by_key(idempotency_key: str) -> ExposureOut:
    return ExposureOut.model_validate(repo.get_exposure_by_key(idempotency_key))
