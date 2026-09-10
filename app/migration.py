"""Cross-version migration preview.

Given up to 10 000 users with attributes, compare the decision of two
*published* versions of one experiment and classify every user:

* ``entered``    — not enrolled on the old version, enrolled on the new one;
* ``exited``     — enrolled on the old version, not enrolled on the new one;
* ``retained``   — enrolled in both and still on the same (possibly renamed) variant;
* ``switched``   — enrolled in both but on a different variant;
* ``not_enrolled`` — enrolled in neither.

The preview is strictly read-only: decisions are evaluated in memory and no
exposure rows are ever written. It is also deterministic — the same payload
always yields the same counts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import continuity as continuity_mod
from . import engine, mutex, repository as repo
from .errors import APIError
from .time_utils import utcnow

MAX_SAMPLE_PER_CATEGORY = 20
SWITCH_REASON_RESHUFFLE = "reshuffled"


def _require_published(experiment_key: str, version: int) -> repo.LoadedVersion:
    loaded = repo.get_version(experiment_key, version)  # 404 if missing
    if loaded.status != "published":
        raise APIError(
            409, "version_not_published",
            f"migration preview requires published versions; "
            f"v{version} is {loaded.status}")
    return loaded


def preview_migration(experiment_key: str, from_version: int, to_version: int,
                      users: list[Any],
                      at: Optional[datetime] = None) -> dict[str, Any]:
    at = at or utcnow()
    old = _require_published(experiment_key, from_version)
    new = _require_published(experiment_key, to_version)

    # Namespace members are identical for both versions except that each
    # decision pins its own version into the ring; preload the set once.
    members = repo.load_latest_published_in_namespace(new.namespace)

    def pinned_members(loaded: repo.LoadedVersion) -> list[repo.LoadedVersion]:
        replaced = [loaded if m.experiment_key == experiment_key else m
                    for m in members]
        if not any(m.experiment_key == experiment_key for m in replaced):
            replaced = [*replaced, loaded]
        return replaced

    old_members = pinned_members(old)
    new_members = pinned_members(new)

    counts = {"entered": 0, "exited": 0, "retained": 0,
              "switched": 0, "not_enrolled": 0}
    switch_reasons: dict[str, int] = {}
    samples: dict[str, list[dict[str, Any]]] = {k: [] for k in counts}

    for item in users:
        user_key = item.user_key
        attrs = item.attributes
        ring_old = mutex.build_ring(old_members, old.namespace, user_key, at)
        ring_new = mutex.build_ring(new_members, new.namespace, user_key, at)
        d_old = engine.decide_loaded(old, user_key, attrs, at=at, ring=ring_old)
        d_new = engine.decide_loaded(new, user_key, attrs, at=at, ring=ring_new)

        category = continuity_mod.migration_status(
            new.continuity, d_old.variant_key, d_new.variant_key,
            d_old.enrolled, d_new.enrolled)
        counts[category] += 1

        change_reason: Optional[str] = None
        if category == "switched":
            if new.continuity and new.continuity.get("mode") == "inherit":
                # Explain the move relative to the EXPLICIT from_version,
                # which may differ from the target's frozen chain anchor.
                change_reason = continuity_mod.switch_reason_between(
                    old, new, d_old.variant_key, d_new.variant_key,
                    user_key)
            else:
                change_reason = SWITCH_REASON_RESHUFFLE
            switch_reasons[change_reason] = \
                switch_reasons.get(change_reason, 0) + 1

        if len(samples[category]) < MAX_SAMPLE_PER_CATEGORY:
            samples[category].append({
                "user_key": user_key,
                "from_enrolled": d_old.enrolled,
                "to_enrolled": d_new.enrolled,
                "from_variant_key": d_old.variant_key,
                "to_variant_key": d_new.variant_key,
                "from_reason": d_old.reason,
                "to_reason": d_new.reason,
                "bucket": d_new.bucket,
                "category": category,
                "change_reason": change_reason,
            })

    return {
        "experiment_key": experiment_key,
        "from_version": from_version,
        "to_version": to_version,
        "users": len(users),
        "counts": counts,
        "switch_reasons": switch_reasons,
        "samples": samples,
        "exposures_written": False,
    }
