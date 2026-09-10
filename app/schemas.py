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
    # Note: start/end ordering is deliberately NOT enforced at the Pydantic
    # layer — an inverted window must parse successfully so that /preflight
    # can report it as a structured validation issue (code schedule_order)
    # instead of failing request parsing. Timezone-awareness is likewise
    # checked structurally (see validation.py).
    start_at: datetime
    end_at: datetime


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


# ---------------------------------------------------------------------------
# Metrics: definitions, result events, attribution analysis
# ---------------------------------------------------------------------------

MetricType = Literal["binary", "continuous"]
OptimizationDirection = Literal["maximize", "minimize"]


class MetricSpec(StrictModel):
    """A metric bound to one experiment version.

    Immutable after creation: changing a metric means defining it on a new
    version (keyed by ``(experiment_key, version_number, metric_key)``).
    """

    metric_key: str = Field(min_length=1, max_length=64,
                            pattern=r"^[A-Za-z0-9_.\-]+$")
    metric_type: MetricType
    event_name: str = Field(min_length=1, max_length=128)
    attribution_window_seconds: int = Field(
        ge=0, le=60 * 60 * 24 * 366,
        description="look-back window [event_at - window, event_at]; 0 means unbounded")
    direction: OptimizationDirection
    min_sample_size: int = Field(
        default=100, ge=1, le=1_000_000_000,
        description="minimum valid samples per variant before the result is trusted")
    srm_threshold: float = Field(
        default=0.05, gt=0, le=1,
        description="flag sample-ratio mismatch when |actual-expected|/expected exceeds this")


class MetricDefOut(StrictModel):
    id: int
    experiment_key: str
    version_number: int
    metric_key: str
    metric_type: MetricType
    event_name: str
    attribution_window_seconds: int
    direction: OptimizationDirection
    min_sample_size: int
    srm_threshold: float
    created_at: datetime


class ResultEventIn(StrictModel):
    event_key: str = Field(min_length=1, max_length=128)
    user_key: str = Field(min_length=1, max_length=256)
    event_name: str = Field(min_length=1, max_length=128)
    occurred_at: Optional[datetime] = Field(
        default=None, description="defaults to current UTC time")
    # Raw JSON number; NaN/Infinity are rejected at the JSON layer, and the
    # router additionally guards against float('nan') payloads.
    value: Optional[float] = None


class MetricAttributionPreview(StrictModel):
    """Attribution verdict for one currently-defined matching metric.

    Computed at ingestion time only as a convenience; analysis re-runs the
    same deterministic attribution against immutable rows, so the stored
    event stays valid even if more metrics are defined later.
    """

    metric_key: str
    version_number: int
    status: Literal["attributed", "excluded"]
    variant_key: Optional[str] = None
    exposure_id: Optional[int] = None
    reason: str


class ResultEventOut(StrictModel):
    event_key: str
    experiment_key: str
    user_key: str
    event_name: str
    occurred_at: datetime
    value: Optional[float] = None
    value_present: bool
    value_valid: bool
    received_at: datetime
    duplicate: bool = False
    attributions: list[MetricAttributionPreview] = Field(default_factory=list)


class ResultEventListResponse(StrictModel):
    items: list[ResultEventOut]


class AnalysisWindow(StrictModel):
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class ConfidenceInterval(StrictModel):
    lower: Optional[float] = None
    upper: Optional[float] = None
    level: float = 0.95


class LiftReport(StrictModel):
    relative: Optional[float] = Field(
        default=None, description="(treatment - control) / |control|")
    ci95: ConfidenceInterval = Field(default_factory=ConfidenceInterval)
    favorable: Optional[bool] = Field(
        default=None,
        description="whether the observed movement agrees with the metric direction")


class SampleRatioRow(StrictModel):
    expected_share: float
    observed_share: float
    relative_deviation: Optional[float]
    srm: bool


class VariantAnalysisRow(StrictModel):
    variant_key: str
    is_control: bool
    exposures_used: int = Field(
        description="distinct enrolled users of this version assigned to this variant")
    valid_samples: int = Field(
        description="binary: distinct converting users; continuous: valid attributed events")
    value: Optional[float] = Field(
        default=None, description="conversion rate (binary) or mean value (continuous)")
    ci95: ConfidenceInterval = Field(default_factory=ConfidenceInterval)
    lift: Optional[LiftReport] = None
    sample_ratio: SampleRatioRow
    insufficient_sample: bool
    events_attributed: int = Field(
        description="distinct attributed events counted for this variant/metric")
    exclusions: dict[str, int] = Field(
        description="event-before-exposure / out-of-window / invalid-value counts for this variant")
    formula: str


class AnalysisTotals(StrictModel):
    exposures_used: int
    events_in_window: int
    events_distinct: int
    duplicates: int
    events_attributed: int
    valid_samples: int
    excluded: dict[str, int]
    srm: bool
    insufficient_sample: bool


class MetricAnalysisResponse(StrictModel):
    experiment_key: str
    version_number: int
    metric: MetricDefOut
    window: AnalysisWindow
    metric_type: MetricType
    control_variant_key: str
    variants: list[VariantAnalysisRow]
    totals: AnalysisTotals
    formulas: dict[str, str]
    attribution: dict[str, str]


# ---------------------------------------------------------------------------
# Sequential testing: immutable analysis plans + checkpoints
# ---------------------------------------------------------------------------

BoundaryType = Literal["pocock", "obrien_fleming"]
HypothesisSide = Literal["one_sided", "two_sided"]
Recommendation = Literal["continue", "significant_win",
                         "significant_harm", "futility_stop"]


class BinaryDesignAssumption(StrictModel):
    kind: Literal["binary"] = "binary"
    control_rate: float = Field(ge=0.0, le=1.0,
                                description="assumed control conversion rate")
    absolute_effect: float = Field(
        description="assumed absolute treatment-minus-control rate difference")

    @model_validator(mode="after")
    def _rates_in_range(self) -> "BinaryDesignAssumption":
        target = self.control_rate + self.absolute_effect
        if not 0.0 < target < 1.0:
            raise ValueError(
                "control_rate + absolute_effect must lie strictly within (0, 1)")
        return self


class ContinuousDesignAssumption(StrictModel):
    kind: Literal["continuous"] = "continuous"
    standard_deviation: float = Field(gt=0.0,
                                      description="assumed common response SD")
    absolute_effect: float = Field(
        description="assumed treatment-minus-control mean difference; may be "
                    "negative when optimizing downward")


DesignAssumptions = BinaryDesignAssumption | ContinuousDesignAssumption


class SequentialPlanCreate(StrictModel):
    """Immutable pre-exposure analysis plan for one version/metric."""

    plan_key: str = Field(min_length=1, max_length=64,
                          pattern=r"^[A-Za-z0-9_.\-]+$")
    control_variant_key: str = Field(min_length=1, max_length=64)
    target_variant_key: str = Field(min_length=1, max_length=64)
    hypothesis: HypothesisSide
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0,
                         description="overall (family-wise) significance level")
    max_sample_size: int = Field(
        ge=2, le=10_000_000_000,
        description="maximal total enrolled sample across the two plan arms")
    planned_checks: int = Field(default=5, ge=1, le=20,
                                description="planned number of interim looks")
    conditional_power_threshold: float = Field(
        default=0.1, gt=0.0, lt=1.0,
        description="stop for futility when conditional power drops below this")
    boundary_type: BoundaryType = "obrien_fleming"
    design_assumptions: Optional[DesignAssumptions] = Field(
        default=None,
        description="fixed design effect for design-drift conditional power; "
                    "when omitted, conditional power uses the current trend")


class PlannedBoundaryRow(StrictModel):
    look: int
    information_time: float
    upper_z: float
    lower_z: Optional[float] = None
    cumulative_alpha: float


class SequentialPlanOut(StrictModel):
    plan_key: str
    experiment_key: str
    version_number: int
    metric: MetricDefOut
    control_variant_key: str
    target_variant_key: str
    hypothesis: HypothesisSide
    direction: OptimizationDirection
    alpha: float
    max_sample_size: int
    planned_checks: int
    conditional_power_threshold: float
    boundary_type: BoundaryType
    design_assumptions: Optional[dict[str, Any]] = None
    allocation: dict[str, float]
    state: Literal["active", "terminated"]
    checkpoints_recorded: int
    planned_boundaries: list[PlannedBoundaryRow]
    created_at: datetime


class SequentialPlanListResponse(StrictModel):
    items: list[SequentialPlanOut]


class CheckpointCreate(StrictModel):
    cutoff_at: datetime = Field(
        description="as-of instant: only exposures recorded before and events "
                    "occurred before this instant are used")


class CheckpointArmRow(StrictModel):
    variant_key: str
    role: Literal["control", "target"]
    n: int
    conversions: Optional[int] = None
    observations: Optional[int] = None
    value: Optional[float] = None
    variance: Optional[float] = None
    sum: Optional[float] = None
    formula: str


class CheckpointInformation(StrictModel):
    information_time: float
    max_sample_size: int
    control_n: int
    target_n: int
    total_n: int
    formula: str


class CheckpointStatistic(StrictModel):
    z: float
    raw_z: float
    standard_error: float
    direction_applied: OptimizationDirection
    formula: str


class CheckpointEffect(StrictModel):
    kind: Literal["risk_difference", "mean_difference"]
    control_rate: Optional[float] = None
    target_rate: Optional[float] = None
    control_mean: Optional[float] = None
    target_mean: Optional[float] = None
    difference: float
    relative_change: Optional[float] = None
    oriented_difference: float


class CheckpointCumulativeAlpha(StrictModel):
    spent: float
    spent_by_boundary_recursion: float
    incremental_at_look: float
    overall_alpha: float
    formula: str


class CheckpointBoundary(StrictModel):
    upper_z: float
    lower_z: Optional[float]
    boundary_type: BoundaryType
    hypothesis: HypothesisSide
    formula: str


class ConditionalPowerResult(StrictModel):
    drift: float
    value: float


class DesignConditionalPower(StrictModel):
    drift: float
    se_at_max_information: float
    value: float


class CheckpointConditionalPower(StrictModel):
    threshold: float
    basis: Optional[Literal["design_absolute_effect", "observed_effect"]] = None
    observed: Optional[ConditionalPowerResult] = None
    design: Optional[DesignConditionalPower] = None
    formula: str


class CheckpointResult(StrictModel):
    sequence: int
    information: CheckpointInformation
    arms: list[CheckpointArmRow]
    effect: CheckpointEffect
    statistic: CheckpointStatistic
    cumulative_alpha: CheckpointCumulativeAlpha
    boundary: CheckpointBoundary
    conditional_power: CheckpointConditionalPower
    recommendation: Recommendation
    terminal: bool
    stop_reasons: list[str]
    excluded: dict[str, int]
    events_attributed: int


class CheckpointOut(StrictModel):
    plan_key: str
    experiment_key: str
    version_number: int
    metric_key: str
    cutoff_at: datetime
    submitted_at: datetime
    duplicate: bool = False
    state: Literal["active", "terminated"]
    result: CheckpointResult
    formulas: dict[str, str]


class CheckpointSummaryRow(StrictModel):
    sequence: int
    checkpoint_id: int
    cutoff_at: datetime
    information_time: float
    recommendation: Recommendation
    terminal: bool
    z: float
    upper_z: float
    lower_z: Optional[float]
    spent_alpha: float
    conditional_power_observed: Optional[float]
    conditional_power_design: Optional[float]
    control_n: int
    target_n: int
    formula: str


class CheckpointHistoryResponse(StrictModel):
    plan: SequentialPlanOut
    checkpoints: list[CheckpointSummaryRow]
