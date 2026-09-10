"""Decision engine: explainable, stable traffic splitting with mutex namespaces.

Decision order per request (each stage appends a trace step):

1. resolve version (caller-pinned published version or latest published)
2. schedule gate      — miss reason "not_in_schedule"
3. whitelist override — hit reason "whitelist", bypasses ring/audience gates
4. audience gate      — miss reason "audience_mismatch" (with evaluated tree)
5. variant bucket     — sha256(salt|version|experiment|user) % 10000
6. namespace ring     — one shared gate position per namespace; miss reasons
                        "mutex_excluded" (position belongs to another
                        experiment) or "not_in_traffic" (unclaimed tail)
7. variant assignment — reason "bucket"

The variant bucket and the namespace gate bucket are independent hashes.
Whitelist decisions record the bucket that was overridden but assign the
forced variant (a whitelisted user is deliberately forced into that
experiment regardless of namespace allocation).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import audience as audience_mod
from . import continuity as continuity_mod
from . import mutex as mutex_mod
from . import repository as repo
from .hashing import BUCKET_SPACE, assignment_bucket
from .mutex import NamespaceRing
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
    continuity_info: Optional[dict[str, Any]] = None

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


def stored_row_to_decision(row: dict[str, Any], idempotency_key: str,
                           exposure_recorded: bool) -> dict[str, Any]:
    """Serialize a persisted exposure row into the decision response shape.

    Used for idempotent replays: the response must be the decision exactly
    as first persisted (same version, variant, reason, trace), never a
    freshly recomputed one that could disagree with the database.
    """
    return {
        "experiment_key": row["experiment_key"],
        "version_id": row["version_id"],
        "version_number": row["version_number"],
        "user_key": row["user_key"],
        "bucket": row["bucket"],
        "gate_bucket": row.get("gate_bucket"),
        "enrolled": row["enrolled"],
        "variant_key": row["variant_key"],
        "reason": row["reason"],
        "trace": row["trace"],
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


def _variant_ranges(loaded: LoadedVersion,
                    order: list[str]) -> list[dict[str, Any]]:
    """Contiguous ranges on [0, 10000) matching declared percentages.

    Laid out in the effective variant order (the frozen continuity order
    for inherited versions, sorted-key order otherwise). The last range
    absorbs rounding so ranges cover the full space.
    """
    return continuity_mod.variant_ranges(loaded.config, order)


def decide(experiment_key: str, user_key: str,
           attributes: dict[str, Any], *,
           version: Optional[int] = None,
           at: Optional[datetime] = None,
           ring_cache: Optional[dict[str, Any]] = None) -> Decision:
    at = at or utcnow()
    loaded = repo.get_published_version(experiment_key, version)
    ring = _resolve_ring(loaded, user_key, at, version is not None, ring_cache)
    return decide_loaded(loaded, user_key, attributes, at=at, ring=ring)


def _resolve_ring(loaded: LoadedVersion, user_key: str, at: datetime,
                  pinned: bool,
                  ring_cache: Optional[dict[str, Any]]) -> NamespaceRing:
    """Build the namespace ring, honoring a batch cache and pinned versions.

    A pinned (historical) version replaces that experiment's latest version
    inside the ring, so the gate decision is made against the configuration
    the caller explicitly asked for.
    """
    namespace = loaded.namespace
    cache_key = (namespace, user_key, at.isoformat())
    if ring_cache is not None and cache_key in ring_cache:
        members = ring_cache[cache_key]
    else:
        members = repo.load_latest_published_in_namespace(namespace)
        if ring_cache is not None:
            ring_cache[cache_key] = members
    if pinned:
        members = [loaded if m.experiment_key == loaded.experiment_key else m
                   for m in members]
        if not any(m.experiment_key == loaded.experiment_key for m in members):
            members = [*members, loaded]
    return mutex_mod.build_ring(members, namespace, user_key, at)


def decide_loaded(loaded: LoadedVersion, user_key: str,
                  attributes: dict[str, Any], *,
                  at: Optional[datetime] = None,
                  ring: Optional[NamespaceRing] = None) -> Decision:
    at = at or utcnow()
    steps: list[TraceStep] = []
    params = continuity_mod.effective_assignment(loaded)
    steps.append(trace(
        "version_resolved", "ok",
        version_id=loaded.id, version_number=loaded.version,
        namespace=loaded.namespace, salt=loaded.salt,
        traffic_percentage=loaded.config.traffic_percentage,
        assignment_seed=params.seed,
        continuity_mode=(loaded.continuity or {}).get("mode"),
    ))

    # Variant bucket depends only on assignment_seed|experiment|user; it is
    # always present, even on misses, to show where the user would land.
    # An inherited seed reuses the source version's salt|version material,
    # which is what keeps users in their groups across versions.
    bucket = assignment_bucket(loaded.experiment_key, params.seed, user_key)
    ranges = _variant_ranges(loaded, params.variant_order)
    target = continuity_mod.variant_for_bucket(ranges, bucket)

    # Cross-version continuity (inherited versions only): compare the same
    # bucket against the source version's frozen ranges and explain why the
    # group did or did not move. Recorded before the gates so gate-misses
    # still carry their would-be migration explanation.
    continuity_info: Optional[dict[str, Any]] = None
    if params.inherited:
        continuity_info = continuity_mod.evaluate_continuity(
            loaded.continuity, ranges, bucket)
        steps.append(trace(
            "continuity",
            "retained" if continuity_info["status"] == "retained" else "switched",
            **continuity_info,
        ))

    steps.append(trace("bucket", "computed", bucket=bucket,
                       bucket_space=BUCKET_SPACE,
                       formula=f"sha256('{params.seed}|"
                               f"{loaded.experiment_key}|{user_key}') % {BUCKET_SPACE}",
                       assignment_seed=params.seed,
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
            gate_bucket=-1, enrolled=False, variant_key=None,
            reason="not_in_schedule", trace=steps,
            continuity_info=continuity_info)
    steps.append(trace("schedule", "active", at=at.isoformat(),
                       **(window_detail or {"always_on": True})))

    # 3. whitelist override (bypasses audience and namespace-ring gates)
    forced = _whitelist_lookup(loaded, user_key)
    if forced is not None:
        steps.append(trace("whitelist", "matched",
                           user_key=user_key, forced_variant=forced,
                           bucketed_variant=target["variant_key"]))
        if continuity_info is not None:
            # The whitelist overrides organic assignment. Report the group
            # switch truthfully: compared with the source group (after
            # renames), forcing a different variant is a switched decision
            # with reason whitelist_override; forcing the same group leaves
            # the underlying continuity status intact.
            organic_group = continuity_info["source_variant_after_rename"]
            if forced != organic_group:
                continuity_info = {
                    **continuity_info,
                    "variant_key": forced,
                    "status": "switched",
                    "change_reason": "whitelist_override",
                    "whitelist_forced_variant": forced,
                }
            else:
                continuity_info = {
                    **continuity_info,
                    "whitelist_forced_variant": forced,
                }
            for i, step in enumerate(steps):
                if step.step == "continuity":
                    steps[i] = trace(
                        "continuity", continuity_info["status"],
                        **continuity_info)
                    break
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=-1, enrolled=True, variant_key=forced,
            reason="whitelist", trace=steps,
            continuity_info=continuity_info)
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
                gate_bucket=-1, enrolled=False, variant_key=None,
                reason="audience_mismatch", trace=steps,
                continuity_info=continuity_info)
        steps.append(trace("audience", "matched", tree=tree))
    else:
        steps.append(trace("audience", "skipped", reason="no_audience_defined"))

    # 6. namespace mutex ring gate
    if ring is None:
        ring = _resolve_ring(loaded, user_key, at, False, None)
    own_slice = ring.slice_of(loaded.experiment_key)
    owner = ring.owner()
    steps.append(trace("mutex_ring", "evaluated",
                       namespace=ring.namespace,
                       gate_bucket=ring.gate_bucket,
                       gate_formula=f"sha256('ns|{ring.namespace}|{user_key}') "
                                    f"% {BUCKET_SPACE}",
                       slices=ring.describe()))
    if own_slice is None:
        # Inactive/zero-size slice while the user passed the schedule gate
        # (can only happen with a pinned historical version).
        steps.append(trace("traffic", "rejected",
                           reason="no_slice_in_ring", gate_bucket=ring.gate_bucket))
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=ring.gate_bucket, enrolled=False, variant_key=None,
            reason="not_in_traffic", trace=steps,
            continuity_info=continuity_info)
    if owner is None or owner.experiment_key != loaded.experiment_key:
        winner = None if owner is None else {
            "experiment_key": owner.experiment_key,
            "version_number": owner.version_number,
            "slice": {"start": owner.start, "end": owner.end},
        }
        reason = "mutex_excluded" if owner is not None else "not_in_traffic"
        steps.append(trace("traffic", "rejected",
                           reason=reason, gate_bucket=ring.gate_bucket,
                           own_slice={"start": own_slice.start,
                                      "end": own_slice.end},
                           winner=winner))
        return Decision(
            experiment_key=loaded.experiment_key, version_id=loaded.id,
            version_number=loaded.version, user_key=user_key, bucket=bucket,
            gate_bucket=ring.gate_bucket, enrolled=False, variant_key=None,
            reason=reason, trace=steps,
            continuity_info=continuity_info)

    steps.append(trace("traffic", "admitted",
                       traffic_percentage=loaded.config.traffic_percentage,
                       gate_bucket=ring.gate_bucket,
                       own_slice={"start": own_slice.start,
                                  "end": own_slice.end}))

    # 7. variant
    steps.append(trace("variant_assignment", "assigned",
                       variant_key=target["variant_key"],
                       range={"start": target["start"], "end": target["end"]}))
    return Decision(
        experiment_key=loaded.experiment_key, version_id=loaded.id,
        version_number=loaded.version, user_key=user_key, bucket=bucket,
        gate_bucket=ring.gate_bucket, enrolled=True,
        variant_key=target["variant_key"], reason="bucket", trace=steps,
        continuity_info=continuity_info)


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
                      idempotency_key: Optional[str] = None,
                      ring_cache: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    at = at or utcnow()
    loaded = repo.get_published_version(experiment_key, version)
    key = idempotency_key or default_idempotency_key(
        experiment_key, loaded.version, user_key)

    # Fast path: an existing record with this key is the authoritative first
    # decision. Return it verbatim instead of recomputing (which could use a
    # newer version and contradict the persisted row).
    from .errors import NotFoundError
    try:
        existing = repo.get_exposure_by_key(key)
    except NotFoundError:
        existing = None
    if existing is not None:
        return stored_row_to_decision(existing, key, False)

    ring = _resolve_ring(loaded, user_key, at, version is not None, ring_cache)
    decision = decide_loaded(loaded, user_key, attributes, at=at, ring=ring)
    trace_payload = json.dumps(
        [t.model_dump() for t in decision.trace], ensure_ascii=False)
    try:
        recorded, row = repo.insert_exposure(
            key, experiment_key, decision.version_id, decision.version_number,
            user_key, decision.bucket, decision.gate_bucket,
            decision.variant_key, decision.enrolled, decision.reason,
            trace_payload,
        )
    except Exception:
        # Race: another request inserted the same key concurrently.
        row = repo.get_exposure_by_key(key)
        return stored_row_to_decision(row, key, False)
    return stored_row_to_decision(row, key, recorded)
