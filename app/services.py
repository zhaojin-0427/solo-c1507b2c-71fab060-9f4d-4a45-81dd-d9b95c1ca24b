"""Cross-cutting services: distribution simulation and preflight."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Optional

from . import engine, mutex, repository as repo
from .schemas import ValidationIssue, VersionConfigIn
from .validation import validate_publish


def run_preflight(experiment_key: str, config: VersionConfigIn) -> list[ValidationIssue]:
    experiment = repo.get_experiment(experiment_key)
    others = repo.latest_published_versions_in_namespace(
        experiment["namespace"], exclude_experiment=experiment_key)
    return validate_publish(config, experiment["namespace"], others)


def validate_candidate_for_namespace(key: str, namespace: str,
                                     config: VersionConfigIn) -> list[ValidationIssue]:
    """Preflight for an experiment that does not exist yet (create flow)."""
    others = repo.latest_published_versions_in_namespace(
        namespace, exclude_experiment=key)
    return validate_publish(config, namespace, others)


def simulate(experiment_key: str, users: int, attributes: dict[str, Any],
             prefix: str, at: Optional[datetime] = None,
             version: Optional[int] = None) -> dict[str, Any]:
    loaded = repo.get_published_version(experiment_key, version)
    at = at or datetime.now()

    # Preload the namespace members once; rebuild the ring per user (the
    # ring position is a function of the user key).
    members = repo.load_latest_published_in_namespace(loaded.namespace)
    if version is not None:
        members = [loaded if m.experiment_key == loaded.experiment_key else m
                   for m in members]
        if not any(m.experiment_key == loaded.experiment_key for m in members):
            members = [*members, loaded]

    variant_users: Counter[str] = Counter()
    miss_reasons: Counter[str] = Counter()
    enrolled = 0

    for i in range(users):
        user_key = f"{prefix}-{i}"
        ring = mutex.build_ring(members, loaded.namespace, user_key, at)
        decision = engine.decide_loaded(loaded, user_key, attributes,
                                        at=at, ring=ring)
        if decision.enrolled:
            enrolled += 1
            variant_users[decision.variant_key] += 1
        else:
            miss_reasons[decision.reason] += 1

    configured = {v.key: v.percentage for v in loaded.config.variants}
    rows = []
    for key in sorted(configured):
        count = variant_users.get(key, 0)
        rows.append({
            "variant_key": key,
            "users": count,
            "configured_percentage": configured[key],
            "actual_percentage": round(100.0 * count / enrolled, 4) if enrolled else 0.0,
            "overall_percentage": round(100.0 * count / users, 4) if users else 0.0,
        })

    return {
        "experiment_key": experiment_key,
        "version_number": loaded.version,
        "users": users,
        "enrolled": enrolled,
        "enrolled_percentage": round(100.0 * enrolled / users, 4) if users else 0.0,
        "variants": rows,
        "miss_reasons": dict(miss_reasons),
        "not_enrolled": users - enrolled,
    }
