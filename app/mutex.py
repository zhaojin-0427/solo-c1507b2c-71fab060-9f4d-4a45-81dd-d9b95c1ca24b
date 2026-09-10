"""Decision-time mutual exclusion within a namespace.

Publish-time validation only bounds the *total configured traffic* of
concurrently active experiments. It cannot prevent one concrete user from
landing in several of them when experiments are decided independently,
because every experiment has its own gate hash. This module enforces the
real invariant at decision time:

    A user is enrolled in at most one experiment per namespace at a time.

Mechanism — the namespace ring:

* every experiment that is **active right now** (within one of its schedule
  windows; no windows means always-on) claims one contiguous slice of the
  [0, 10000) ring, sized by its ``traffic_percentage``;
* slices are placed in a stable order — sorted experiment key — and never
  overlap; the unclaimed tail (if any) is excluded traffic;
* the user's position on the ring comes from a **namespace-scoped** hash
  ``sha256(namespace | user_key)``, shared by every experiment in the
  namespace. A single user therefore has a single position and can fall
  inside at most one slice.

The position intentionally omits the version number: all active versions
must agree on the user's ring position, or two experiments could admit the
same user by disagreement. The *composition* of the ring (which experiments
claim which slices) is deterministic given the published configuration set
and changes only when an experiment is published/unpublished/scheduled,
which is exactly when traffic allocation is allowed to move.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .hashing import BUCKET_SPACE
from .repository import LoadedVersion
from .time_utils import window_active


@dataclass(frozen=True)
class RingSlice:
    experiment_key: str
    version_number: int
    traffic_percentage: float
    start: int  # inclusive
    end: int    # exclusive


@dataclass(frozen=True)
class NamespaceRing:
    namespace: str
    gate_bucket: int
    slices: tuple[RingSlice, ...]

    def slice_of(self, experiment_key: str) -> Optional[RingSlice]:
        for s in self.slices:
            if s.experiment_key == experiment_key:
                return s
        return None

    def owner(self) -> Optional[RingSlice]:
        """The single experiment whose slice contains the gate bucket."""
        for s in self.slices:
            if s.start <= self.gate_bucket < s.end:
                return s
        return None

    def describe(self) -> list[dict]:
        return [{
            "experiment_key": s.experiment_key,
            "version_number": s.version_number,
            "traffic_percentage": s.traffic_percentage,
            "start": s.start, "end": s.end,
        } for s in self.slices]


def namespace_gate_bucket(namespace: str, user_key: str) -> int:
    material = f"ns|{namespace}|{user_key}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def _is_active(loaded: LoadedVersion, at: datetime) -> bool:
    windows = loaded.config.schedules
    if not windows:
        return True
    return any(window_active(w.start_at, w.end_at, at) for w in windows)


def build_ring(members: list[LoadedVersion], namespace: str,
               user_key: str, at: datetime) -> NamespaceRing:
    """Build the namespace ring from active members.

    ``members`` are the latest published versions of every experiment in the
    namespace, plus an overriding pinned version when the caller pinned
    one. Only members active at ``at`` claim slices. Slices are laid out in
    sorted experiment-key order; the last member absorbs rounding.
    """
    active = sorted(
        (m for m in members if m.namespace == namespace and _is_active(m, at)),
        key=lambda m: m.experiment_key,
    )
    gate = namespace_gate_bucket(namespace, user_key)

    # Place contiguous slices in stable (sorted-key) order. Each end is
    # clamped to BUCKET_SPACE so slices can never overlap even if the active
    # members claim more than the ring (publish validation normally keeps
    # the total at <= 100%; clamping is the safety net for pinned versions).
    slices: list[RingSlice] = []
    cursor = 0
    for m in active:
        width = round(m.config.traffic_percentage * BUCKET_SPACE / 100.0)
        end = min(BUCKET_SPACE, cursor + width)
        if end > cursor:
            slices.append(RingSlice(
                experiment_key=m.experiment_key,
                version_number=m.version,
                traffic_percentage=m.config.traffic_percentage,
                start=cursor, end=end,
            ))
        cursor = end
    return NamespaceRing(namespace=namespace, gate_bucket=gate,
                         slices=tuple(slices))
