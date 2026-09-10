"""Decision engine: explainable, stable traffic splitting.

Decision order per request (each stage appends a trace step):

1. resolve version (caller-pinned published version or latest published)
2. schedule gate      — miss reason "not_in_schedule"
3. whitelist override — hit reason "whitelist", bypasses audience + traffic
4. audience gate      — miss reason "audience_mismatch" (with evaluated tree)
5. variant bucket     — sha256(salt|version|experiment|user) bytes 0..7 % 10000
6. traffic gate       — independent gate bucket (bytes 8..15); miss "not_in_traffic"
7. variant assignment — reason "bucket"

Both buckets are always computed (even for whitelist / misses) so the trace
shows where the user *would* have landed. Whitelist decisions record the
bucket that was overridden but assign the forced variant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import audience as audience_mod
from . import repository as repo
from .hashing import BUCKET_SPACE
from .repository import LoadedVersion
from .schemas import TraceStep
from .time_utils import utcnow, window_active


def trace(step: str, result: str, **detail: Any) -> TraceStep:
    return TraceStep(step=step, result=result, detail=detail)


@dataclass
class Decision:
    experiment_key: str
    version_id: int
    version_number: int
    user_key: str
    bucket: int
    gate_bucket: int
    enrolled: bool
    variant_key: Optional[str]
    reason: str
    trace: list[TraceStep] = field(default_factory=list)

    def to_dict(self, idempotency_key: Optional[str] = None,
                exposure_recorded: bool = False) -> dict[str, Any]:
        return {
            "experiment_key": self.experiment_key,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "user_key": self.user_key,
            "bucket": self.bucket,
            "gate_bucket": self.gate_bucket,
            "enrolled": self.enrolled,
            "variant_key": self.variant_key,
            "reason": self.reason,
            "trace": [t.model_dump() for t in self.trace],
            "idempotency_key": idempotency_key,
            "exposure_recorded": exposure_recorded,
        }


def _schedule_active(loaded: LoadedVersion, at: datetime) -> tuple[bool, Any]:
    windows = loaded.config.schedules
    if not windows:
        return True, None
    for w in windows:
        if window_active(w.start_at, w.end_at, at):
            return True, {"start_at": w.start_at.isoformat(),
                          "end_at": w.end_at.isoformat()}
    return False, {"windows": [
        {"start_at": w.start_at.isoformat(), "end_at": w.end_at.isoformat()}
        for w in windows]}


def _whitelist_lookup(loaded: LoadedVersion, user_key: str) -> Optional[str]:
    for entry in loaded.config.whitelist:
        if entry.user_key == user_key:
            return entry.variant_key
    return None


def _variant_ranges(loaded: LoadedVersion) -> list[dict[str, Any]]:
    """Contiguous ranges on [0, 10000) matching declared percentages.

    The last variant absorbs rounding so ranges cover the full space.
    """
    ranges: list[dict[str, Any]] = []
    cursor = 0
    sorted_variants = sorted(loaded.config.variants, key=lambda v: v.key)
    for i, v in enumerate(sorted_variants):
        if i == len(sorted_variants) - 1:
            width = BUCKET_SPACE - cursor
        else:
            width = round(BUCKET_SPACE * v.percentage / 100.0)
        ranges.append({
            "variant_key": v.key,
            "start": cursor,
            "end": cursor + width,
            "percentage": v.percentage,
        })
        cursor += width
    return ranges


def _variant_for_bucket(ranges: list[dict[str, Any]], bucket: int) -> dict[str, Any]:
    for r in ranges:
        if r["start"] <= bucket < r["end"]:
            return r
    return ranges[-1]  # bucket == 10000 can't happen (mod 10000), safety net


def decide(experiment_key: str, user_key: str,
           attributes: dict[str, Any], *,
           version: Optional[int] = None,
           at: Optional[datetime] = None) -> Decision:
    at = (at or utcnow())
    loaded = repo.get_published_version(experiment_key, version)
    return decide_loaded(loaded, user_key, attributes, at=at)


def decide_loaded(loaded: LoadedVersion, user_key: str,
                  attributes: dict[str, Any], *,
                  at: Optional[datetime] = None) -> Decision:
    at = at or utcnow()
    steps: list[TraceStep] = []
    steps.append(trace(
        "version_resolved", "ok",
        version_id=loaded.id, version_number=loaded.version,
        namespace=loaded.namespace, salt=loaded.salt,
        traffic_percentage=loaded.config.traffic_percentage,
    ))

    # Both buckets depend only on salt|version|experiment|user, so they can
    # be computed before every gate and are present on every decision trace.
    from .hashing import gate_bucket as compute_gate_bucket
    from .hashing import variant_bucket as compute_variant_bucket
    bucket = compute_variant_bucket(loaded.experiment_key, loaded.salt,
                                    loaded.version, user_key)
    gate = compute_gate_bucket(loaded.experiment_key, loaded.salt,
                               loaded.version, user_key)
    ranges = _variant_ranges(loaded)
    target = _variant_for_bucket(ranges, bucket)
    steps.append(trace("bucket", "computed", bucket=bucket,
                       gate_bucket=gate, bucket_space=BUCKET_SPACE,
                       formula=f"sha256('{loaded.salt}|{loaded.version}|"
                               f"{loaded.experiment_key}|{user_key}')",
                       bucketed_variant=target["variant_key"],
                       ranges=ranges))

    # 2. schedule gate
    active, window_detail = _schedule_active(loaded, at)
    if not active:
        steps.append(trace("schedule", "inactive", at=at.isoformat(),
                           **window_detail))
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=gate, enrolled=False, variant_key=None,
            reason="not_in_schedule", trace=steps)
    steps.append(trace("schedule", "active", at=at.isoformat(),
                       **(window_detail or {"always_on": True})))

    # 3. whitelist override (bypasses audience and traffic gates)
    forced = _whitelist_lookup(loaded, user_key)
    if forced is not None:
        steps.append(trace("whitelist", "matched",
                           user_key=user_key, forced_variant=forced,
                           bucketed_variant=target["variant_key"]))
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=gate, enrolled=True, variant_key=forced,
            reason="whitelist", trace=steps)
    steps.append(trace("whitelist", "miss"))

    # 4. audience gate
    if loaded.config.audience is not None:
        tree = audience_mod.evaluate(loaded.config.audience, attributes)
        if not tree["matched"]:
            steps.append(trace("audience", "mismatch", tree=tree,
                               attributes=attributes))
            return Decision(
                experiment_key=loaded.experiment_key, version_id=loaded.id,
                version_number=loaded.version, user_key=user_key, bucket=bucket,
                gate_bucket=gate, enrolled=False, variant_key=None,
                reason="audience_mismatch", trace=steps)
        steps.append(trace("audience", "matched", tree=tree))
    else:
        steps.append(trace("audience", "skipped", reason="no_audience_defined"))

    # 6. traffic gate (uses the independent gate bucket)
    cutoff = round(loaded.config.traffic_percentage * BUCKET_SPACE / 100.0)
    admitted = gate < cutoff
    steps.append(trace("traffic", "admitted" if admitted else "rejected",
                       traffic_percentage=loaded.config.traffic_percentage,
                       cutoff=cutoff, gate_bucket=gate,
                       variant_bucket=bucket))
    if not admitted:
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=gate, enrolled=False, variant_key=None,
            reason="not_in_traffic", trace=steps)

    # 7. variant
    steps.append(trace("variant_assignment", "assigned",
                       variant_key=target["variant_key"],
                       range={"start": target["start"], "end": target["end"]}))
    return Decision(
        experiment_key=loaded.experiment_key, version_id=loaded.id,
        version_number=loaded.version, user_key=user_key, bucket=bucket,
        gate_bucket=gate, enrolled=True, variant_key=target["variant_key"],
        reason="bucket", trace=steps)


# ---------------------------------------------------------------------------
# Exposure recording
# ---------------------------------------------------------------------------


def default_idempotency_key(experiment_key: str, version_number: int,
                            user_key: str) -> str:
    raw = f"{experiment_key}|v{version_number}|{user_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def decide_and_record(experiment_key: str, user_key: str,
                      attributes: dict[str, Any], *,
                      version: Optional[int] = None,
                      at: Optional[datetime] = None,
                      idempotency_key: Optional[str] = None) -> dict[str, Any]:
    decision = decide(experiment_key, user_key, attributes,
                      version=version, at=at)
    key = idempotency_key or default_idempotency_key(
        experiment_key, decision.version_number, user_key)
    import json
    trace_payload = json.dumps(
        [t.model_dump() for t in decision.trace], ensure_ascii=False)
    recorded, _row = repo.insert_exposure(
        key, experiment_key, decision.version_id, decision.version_number,
        user_key, decision.bucket, decision.gate_bucket,
        decision.variant_key, decision.enrolled, decision.reason,
        trace_payload,
    )
    return decision.to_dict(idempotency_key=key, exposure_recorded=recorded)
