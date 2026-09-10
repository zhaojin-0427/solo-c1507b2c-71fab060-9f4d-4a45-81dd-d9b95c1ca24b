"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .time_utils import utcnow

# ---------------------------------------------------------------------------
# Configuration primitives
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VariantSpec(StrictModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    percentage: float = Field(ge=0, le=100)
    is_control: bool = False


class WhitelistEntry(StrictModel):
    user_key: str = Field(min_length=1, max_length=256)
    variant_key: str = Field(min_length=1, max_length=64)


class ScheduleWindow(StrictModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _check_window(self) -> "ScheduleWindow":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at/end_at must be timezone aware")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


FieldOp = Literal[
    "eq", "ne", "in", "nin", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "starts_with", "ends_with", "exists",
]


class ConditionSpec(StrictModel):
    field: str = Field(min_length=1, max_length=256)
    op: FieldOp
    value: Optional[Any] = None


class AudienceNode(StrictModel):
    """Recursive audience tree.

    Inner nodes use ``all`` / ``any`` with child nodes. Leaf nodes carry a
    single ``condition``. ``not`` wraps exactly one child.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    all: Optional[list["AudienceNode"]] = Field(default=None, min_length=1)
    any: Optional[list["AudienceNode"]] = Field(default=None, min_length=1)
    not_: Optional["AudienceNode"] = Field(
        default=None, alias="not", serialization_alias="not")
    condition: Optional[ConditionSpec] = None

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> "AudienceNode":
        kinds = [k for k in ("all", "any", "not_", "condition")
                 if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(
                "audience node must contain exactly one of: all, any, not, condition"
            )
        return self


AudienceNode.model_rebuild()


class VersionConfigIn(StrictModel):
    """Payload for creating a new version of an experiment."""

    traffic_percentage: float = Field(default=100.0, ge=0, le=100,
                                      description="fraction of eligible traffic admitted into the experiment (0-100)")
    variants: list[VariantSpec] = Field(min_length=1)
    control_variant_key: str
    whitelist: list[WhitelistEntry] = Field(default_factory=list)
    audience: Optional[AudienceNode] = None
    schedules: list[ScheduleWindow] = Field(default_factory=list)


class ExperimentCreate(StrictModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    name: str = Field(min_length=1, max_length=200)
    namespace: Optional[str] = Field(default=None, max_length=64,
                                     pattern=r"^[A-Za-z0-9_.\-]+$")
    salt: Optional[str] = Field(default=None, max_length=128)
    config: Optional[VersionConfigIn] = None
    publish: bool = False


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------


class ExperimentOut(StrictModel):
    id: int
    key: str
    name: str
    namespace: str
    salt: str
    created_at: datetime
    latest_version: Optional[int] = None
    published_version: Optional[int] = None


class VersionOut(StrictModel):
    id: int
    experiment_key: str
    version: int
    status: Literal["draft", "published"]
    traffic_percentage: float
    namespace: str
    config: VersionConfigIn
    created_at: datetime
    published_at: Optional[datetime] = None


class ValidationIssue(StrictModel):
    code: str
    message: str
    location: Optional[str] = None


class PreflightResponse(StrictModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Decide / batch / simulate
# ---------------------------------------------------------------------------


class TraceStep(StrictModel):
    step: str
    result: str
    detail: dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(StrictModel):
    model_config = ConfigDict(extra="forbid")

    experiment_key: str
    version_id: int
    version_number: int
    user_key: str
    bucket: int = Field(description="variant bucket on [0,10000)")
    gate_bucket: int = Field(description="traffic-gate bucket on [0,10000)")
    enrolled: bool
    variant_key: Optional[str]
    reason: str
    trace: list[TraceStep]
    idempotency_key: Optional[str] = None
    exposure_recorded: bool = False


class BatchDecideRequest(StrictModel):
    user_key: str = Field(min_length=1, max_length=256)
    experiment_keys: list[str] = Field(min_length=1, max_length=500)
    attributes: dict[str, Any] = Field(default_factory=dict)
    version: Optional[int] = Field(default=None, description="pin decision to a specific published version for every experiment")
    at: Optional[datetime] = None
    record_exposure: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class BatchItem(StrictModel):
    experiment_key: str
    decision: Optional[DecisionResponse] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


class BatchDecideResponse(StrictModel):
    results: list[BatchItem]


class SimulateRequest(StrictModel):
    users: int = Field(default=10_000, ge=1, le=1_000_000)
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="attributes applied to every simulated user",
    )
    user_key_prefix: str = "sim"
    at: Optional[datetime] = None


class VariantDistributionRow(StrictModel):
    variant_key: str
    users: int
    configured_percentage: float
    actual_percentage: float = Field(
        description="share among enrolled users (comparable to configured_percentage)")
    overall_percentage: float = Field(
        description="share among all simulated users")


class SimulateResponse(StrictModel):
    experiment_key: str
    version_number: int
    users: int
    enrolled: int
    enrolled_percentage: float
    variants: list[VariantDistributionRow]
    miss_reasons: dict[str, int]
    not_enrolled: int


# ---------------------------------------------------------------------------
# Exposures
# ---------------------------------------------------------------------------


class VariantCount(StrictModel):
    variant_key: Optional[str]
    count: int


class ExposureSummaryResponse(StrictModel):
    experiment_key: str
    version: Optional[int] = None
    total_decisions: int
    enrolled: int
    not_enrolled: int
    by_variant: list[VariantCount]
    by_reason: dict[str, int]


class ExposureOut(StrictModel):
    id: int
    idempotency_key: str
    experiment_key: str
    version_id: int
    version_number: int
    user_key: str
    bucket: Optional[int]
    gate_bucket: Optional[int] = None
    variant_key: Optional[str]
    enrolled: bool
    reason: str
    trace: list[TraceStep]
    recorded_at: datetime


class ExposureListResponse(StrictModel):
    items: list[ExposureOut]
