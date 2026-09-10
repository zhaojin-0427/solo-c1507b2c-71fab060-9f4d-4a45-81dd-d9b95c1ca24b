"""Metric definitions, result-event ingestion and effect analysis."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query

from .. import metrics as metrics_mod
from .. import repository as repo
from ..errors import APIError
from ..schemas import (
    AnalysisTotals,
    AnalysisWindow,
    ConfidenceInterval,
    LiftReport,
    MetricAnalysisResponse,
    MetricAttributionPreview,
    MetricDefOut,
    MetricSpec,
    ResultEventIn,
    ResultEventListResponse,
    ResultEventOut,
    SampleRatioRow,
    VariantAnalysisRow,
)
from ..time_utils import to_iso, utcnow

router = APIRouter(prefix="/experiments", tags=["metrics"])


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


def _metric_out(m: repo.MetricDef) -> MetricDefOut:
    return MetricDefOut(
        id=m.id, experiment_key=m.experiment_key,
        version_number=m.version_number, metric_key=m.metric_key,
        metric_type=m.metric_type, event_name=m.event_name,
        attribution_window_seconds=m.attribution_window_seconds,
        direction=m.direction, min_sample_size=m.min_sample_size,
        srm_threshold=m.srm_threshold, created_at=m.created_at,
    )


@router.post("/{experiment_key}/versions/{version}/metrics",
             response_model=MetricDefOut, status_code=201,
             summary="Define an immutable metric for one experiment version")
def create_metric(experiment_key: str, version: int,
                  payload: MetricSpec) -> MetricDefOut:
    return _metric_out(repo.create_metric(experiment_key, version, payload))


@router.get("/{experiment_key}/metrics",
            response_model=list[MetricDefOut],
            summary="List metric definitions (optionally for one version)")
def list_metrics(experiment_key: str,
                 version: Optional[int] = Query(None)) -> list[MetricDefOut]:
    return [_metric_out(m)
            for m in repo.list_metrics(experiment_key, version)]


# ---------------------------------------------------------------------------
# Result events
# ---------------------------------------------------------------------------


def _event_out(row: dict[str, Any], *, duplicate: bool = False,
               attributions: Optional[list[dict[str, Any]]] = None
               ) -> ResultEventOut:
    return ResultEventOut(
        event_key=row["event_key"], experiment_key=row["experiment_key"],
        user_key=row["user_key"], event_name=row["event_name"],
        occurred_at=row["occurred_at"], value=row["value"],
        value_present=row["value_present"], value_valid=row["value_valid"],
        received_at=row["received_at"], duplicate=duplicate,
        attributions=attributions or [],
    )


@router.post("/{experiment_key}/events",
             response_model=ResultEventOut,
             summary="Report a result event (deduplicated by event_key)")
def report_event(experiment_key: str,
                 payload: ResultEventIn) -> ResultEventOut:
    repo.get_experiment(experiment_key)  # 404 early
    if payload.value is not None and not math.isfinite(payload.value):
        # NaN/Infinity accepted by Python's float() but never legal stats input
        raise APIError(400, "invalid_value",
                       "event value must be a finite number")

    occurred = payload.occurred_at or utcnow()
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    inserted, row = repo.insert_result_event(
        payload.event_key, experiment_key, payload.user_key,
        payload.event_name, to_iso(occurred), payload.value)

    attributions: list[dict[str, Any]] = []
    if inserted:
        # Best-effort preview against metrics defined at ingestion time.
        matching = repo.list_matching_metrics(experiment_key,
                                              payload.event_name)
        if matching:
            versions = {m.version_number for m in matching}
            exposures = {v: repo.enrolled_exposures(experiment_key, v)
                         for v in versions}
            attributions = metrics_mod.preview_attributions(
                row, matching, exposures)

    return _event_out(row, duplicate=not inserted, attributions=attributions)


@router.get("/{experiment_key}/events/{event_key}",
            response_model=ResultEventOut,
            summary="Retrieve one reported event by its unique event key")
def get_event(experiment_key: str, event_key: str) -> ResultEventOut:
    return _event_out(repo.get_result_event_by_key(event_key))


@router.get("/{experiment_key}/events",
            response_model=ResultEventListResponse,
            summary="Browse reported events")
def list_events(experiment_key: str,
                event_name: Optional[str] = Query(None),
                user_key: Optional[str] = Query(None),
                limit: int = Query(100, ge=1, le=1000)
                ) -> ResultEventListResponse:
    repo.get_experiment(experiment_key)
    rows = repo.list_result_events(experiment_key, event_name=event_name,
                                   user_key=user_key, limit=limit)
    return ResultEventListResponse(items=[_event_out(r) for r in rows])


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@router.get("/{experiment_key}/versions/{version}/metrics/{metric_key}/analysis",
            response_model=MetricAnalysisResponse,
            summary="Attribution and effect analysis for one version/metric")
def analyze(experiment_key: str, version: int, metric_key: str,
            start_at: Optional[datetime] = Query(None),
            end_at: Optional[datetime] = Query(None)
            ) -> MetricAnalysisResponse:
    loaded = repo.get_version(experiment_key, version)  # 404 chain
    metric = repo.get_metric(experiment_key, version, metric_key)

    try:
        start_iso, end_iso = metrics_mod.normalize_window(start_at, end_at)
    except ValueError as exc:
        raise APIError(400, "invalid_time_range", str(exc))

    events = repo.events_for_analysis(experiment_key, metric.event_name,
                                      version, start_iso, end_iso)
    exposures = repo.enrolled_exposures(experiment_key, version)
    result = metrics_mod.analyze(loaded, metric, events, exposures)

    variant_rows = [
        VariantAnalysisRow(
            variant_key=r["variant_key"],
            is_control=r["is_control"],
            exposures_used=r["exposures_used"],
            valid_samples=r["valid_samples"],
            value=r["value"],
            ci95=ConfidenceInterval(**r["ci95"]),
            lift=None if r["lift"] is None else LiftReport(
                relative=r["lift"]["relative"],
                ci95=ConfidenceInterval(**r["lift"]["ci95"])
                if r["lift"]["ci95"] else ConfidenceInterval(),
                favorable=r["lift"]["favorable"]),
            sample_ratio=SampleRatioRow(**r["sample_ratio"]),
            insufficient_sample=r["insufficient_sample"],
            events_attributed=r["events_attributed"],
            exclusions=r["exclusions"],
            formula=r["formula"],
        )
        for r in result["variants"]
    ]

    return MetricAnalysisResponse(
        experiment_key=experiment_key,
        version_number=version,
        metric=_metric_out(metric),
        window=AnalysisWindow(start_at=start_at, end_at=end_at),
        metric_type=metric.metric_type,
        control_variant_key=loaded.config.control_variant_key,
        variants=variant_rows,
        totals=AnalysisTotals(**result["totals"]),
        formulas=metrics_mod.FORMULAS,
        attribution=metrics_mod.ATTRIBUTION_REASONS,
    )
