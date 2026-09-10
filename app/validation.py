"""Configuration validation.

Two layers:

* ``validate_structure`` — everything verifiable from one config payload:
  percentages sum to 100, exactly one control, whitelist targets existing
  variants, schedule windows well-formed and non-overlapping.
* ``validate_publish`` — additionally checks the mutex-namespace rule
  against stored state: for every *other* experiment in the namespace, its
  latest published version and the candidate must not have overlapping
  active time while their traffic percentages sum beyond 100%.

Versions of the same experiment are immutable history; at decision time the
latest published version wins (unless the caller pins one), so publishing a
new version supersedes older ones rather than conflicting with them. An
always-on version (no schedules) is treated as active over the whole
timeline and therefore intersects every window.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from .schemas import (
    AudienceNode,
    ConditionSpec,
    ScheduleWindow,
    ValidationIssue,
    VersionConfigIn,
)
from .time_utils import parse_iso, windows_overlap

PERCENTAGE_TOLERANCE = 1e-6


def _issue(code: str, message: str, location: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, location=location)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


def validate_structure(config: VersionConfigIn) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    # variants: keys unique, percentages sum to 100, exactly one control
    keys = [v.key for v in config.variants]
    dupes = [k for k, c in Counter(keys).items() if c > 1]
    for d in dupes:
        issues.append(_issue("duplicate_variant", f"duplicate variant key: {d!r}",
                             f"variants.{d}"))

    total = sum(v.percentage for v in config.variants)
    if abs(total - 100.0) > PERCENTAGE_TOLERANCE:
        issues.append(_issue(
            "percentages_sum",
            f"variant percentages must sum to 100, got {total:g}",
            "variants.percentage",
        ))

    controls = [v.key for v in config.variants if v.is_control]
    if len(controls) != 1:
        issues.append(_issue(
            "control_count",
            f"exactly one control variant required, found {len(controls)}",
            "variants.is_control",
        ))
    if config.control_variant_key not in keys:
        issues.append(_issue(
            "unknown_control",
            f"control_variant_key {config.control_variant_key!r} is not a declared variant",
            "control_variant_key",
        ))

    # whitelist
    wl_users = [w.user_key for w in config.whitelist]
    for u, c in Counter(wl_users).items():
        if c > 1:
            issues.append(_issue(
                "duplicate_whitelist_user",
                f"user_key {u!r} appears more than once in whitelist",
                f"whitelist.{u}",
            ))
    variant_set = set(keys)
    for w in config.whitelist:
        if w.variant_key not in variant_set:
            issues.append(_issue(
                "unknown_whitelist_variant",
                f"whitelist for user {w.user_key!r} targets unknown variant {w.variant_key!r}",
                f"whitelist.{w.user_key}",
            ))

    issues.extend(_validate_audience(config.audience))
    issues.extend(_validate_schedules(config.schedules))
    return issues


def _validate_audience(node: AudienceNode | None, path: str = "audience") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if node is None:
        return issues
    if node.condition is not None:
        cond: ConditionSpec = node.condition
        if cond.op in ("in", "nin") and not isinstance(cond.value, list):
            issues.append(_issue(
                "audience_value_type",
                f"condition op {cond.op!r} on {cond.field!r} requires a list value",
                f"{path}.condition.value",
            ))
        if cond.op not in ("exists", "in", "nin") and cond.value is None:
            issues.append(_issue(
                "audience_value_missing",
                f"condition op {cond.op!r} on {cond.field!r} requires a value",
                f"{path}.condition.value",
            ))
        return issues
    if node.all is not None:
        for i, child in enumerate(node.all):
            issues.extend(_validate_audience(child, f"{path}.all[{i}]"))
    elif node.any is not None:
        for i, child in enumerate(node.any):
            issues.extend(_validate_audience(child, f"{path}.any[{i}]"))
    elif node.not_ is not None:
        issues.extend(_validate_audience(node.not_, f"{path}.not"))
    return issues


def _validate_schedules(windows: list[ScheduleWindow]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    parsed: list[tuple[datetime, datetime]] = []
    for i, w in enumerate(windows):
        if w.end_at <= w.start_at:
            issues.append(_issue("schedule_order",
                                 "schedule end_at must be after start_at",
                                 f"schedules[{i}]"))
            continue
        s, e = w.start_at, w.end_at
        for j, (ps, pe) in enumerate(parsed):
            if windows_overlap(s, e, ps, pe):
                issues.append(_issue(
                    "schedule_overlap",
                    f"schedule windows [{i}] and [{j}] overlap",
                    f"schedules[{i}]",
                ))
        parsed.append((s, e))
    return issues


# ---------------------------------------------------------------------------
# Cross-version / namespace validation (publish time)
# ---------------------------------------------------------------------------


def validate_publish(config: VersionConfigIn, namespace: str,
                     other_versions: list[dict[str, Any]]) -> list[ValidationIssue]:
    """Check mutex-namespace conflicts against stored state.

    Each item of ``other_versions`` is the *latest published* version of one
    *other* experiment in the same namespace, with keys:
    ``experiment_key``, ``version``, ``namespace``, ``traffic_percentage``,
    ``config`` (decoded dict).
    """
    issues = list(validate_structure(config))
    if issues:
        return issues

    new_windows = [(w.start_at, w.end_at) for w in config.schedules]

    for row in other_versions:
        other_windows = [
            (parse_iso(w["start_at"]), parse_iso(w["end_at"]))
            for w in row["config"].get("schedules", [])
        ]
        if namespace != row["namespace"] or not _windows_intersect(new_windows, other_windows):
            continue
        combined = config.traffic_percentage + row["traffic_percentage"]
        if combined > 100.0 + PERCENTAGE_TOLERANCE:
            issues.append(_issue(
                "namespace_traffic_conflict",
                (f"namespace {namespace!r}: overlapping schedule with experiment "
                 f"{row['experiment_key']!r} v{row['version']} would direct "
                 f"{config.traffic_percentage:g}% + {row['traffic_percentage']:g}% "
                 f"= {combined:g}% of traffic (limit 100%)"),
                "traffic_percentage",
            ))
    return issues


def _windows_intersect(a: list[tuple[datetime, datetime]],
                       b: list[tuple[datetime, datetime]]) -> bool:
    """True when two schedule sets overlap in time.

    An empty list means "always on" (active for the whole timeline), so an
    always-on version intersects everything.
    """
    if not a or not b:
        return True
    return any(windows_overlap(s1, e1, s2, e2) for s1, e1 in a for s2, e2 in b)
