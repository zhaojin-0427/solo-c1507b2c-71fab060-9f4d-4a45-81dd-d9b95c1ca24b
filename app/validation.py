"""Configuration validation.

Two layers:

* ``validate_structure`` — everything verifiable from one config payload:
  percentages sum to 100, exactly one control, whitelist targets existing
  variants, schedule windows well-formed and non-overlapping.
* ``validate_publish`` — additionally checks the mutex-namespace rule
  against stored state: the candidate must not, at any point in time, push
  the *total* namespace traffic above 100%. Overlaps are checked against
  every other experiment's latest published version via a sweep over the
  schedule timeline, so three experiments at 40% each are rejected even
  though no single pair exceeds 100%. An always-on version (no schedules)
  is active over the whole timeline.

Versions of the same experiment are immutable history; at decision time the
latest published version wins (unless the caller pins one), so publishing a
new version supersedes older ones rather than conflicting with them.
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
        if w.start_at.tzinfo is None or w.end_at.tzinfo is None:
            issues.append(_issue("schedule_timezone",
                                 "schedule timestamps must be timezone aware",
                                 f"schedules[{i}]"))
            continue
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


def _candidate_windows(config: VersionConfigIn) -> list[tuple[datetime, datetime]] | None:
    """None means 'always on'; otherwise the explicit half-open windows."""
    windows = [(w.start_at, w.end_at) for w in config.schedules]
    return windows or None


def _other_windows(row: dict[str, Any]) -> list[tuple[datetime, datetime]] | None:
    raw = row["config"].get("schedules", [])
    if not raw:
        return None
    return [(parse_iso(w["start_at"]), parse_iso(w["end_at"])) for w in raw]


def _peak_concurrent(base_traffic: float,
                     others: list[tuple[float, list[tuple[datetime, datetime]] | None]],
                     clip: tuple[datetime, datetime] | None = None) -> float:
    """Peak total traffic on a timeline.

    ``base_traffic`` is always-on traffic (e.g. an always-on candidate).
    Each other entry is (traffic_percentage, windows); None windows mean the
    entry is always on. ``clip`` restricts the sweep to one window.
    """
    # Always-on competitors contribute everywhere.
    always_on = sum(t for t, w in others if w is None)
    bounded = [(t, w) for t, w in others if w is not None]

    if clip is None:
        # Candidate is always on: the peak is the highest simultaneous
        # concurrency among bounded competitors over the whole timeline.
        events: list[tuple[datetime, int, float]] = []
        for traffic, windows in bounded:
            for s, e in windows:
                events.append((s, 1, traffic))   # start applies at t
                events.append((e, 0, traffic))   # end releases first (half-open)
        if not events:
            return base_traffic + always_on
        events.sort(key=lambda ev: (ev[0], ev[1]))
        current = 0.0
        peak = 0.0
        for _, kind, traffic in events:
            current += traffic if kind == 1 else -traffic
            peak = max(peak, current)
        return base_traffic + always_on + peak

    # Candidate is active only inside [clip_s, clip_e): clip every
    # competitor window to the candidate window and sweep that range.
    c_start, c_end = clip
    events = [(c_start, 1, 0.0), (c_end, 0, 0.0)]
    for traffic, windows in bounded:
        for s, e in windows:
            s = max(s, c_start)
            e = min(e, c_end)
            if s < e:
                events.append((s, 1, traffic))
                events.append((e, 0, traffic))
    events.sort(key=lambda ev: (ev[0], ev[1]))
    current = 0.0
    peak = 0.0
    for _, kind, traffic in events:
        current += traffic if kind == 1 else -traffic
        peak = max(peak, current)
    return base_traffic + always_on + peak


def validate_publish(config: VersionConfigIn, namespace: str,
                     other_versions: list[dict[str, Any]]) -> list[ValidationIssue]:
    """Check mutex-namespace conflicts against stored state.

    The namespace rule: at *no* point in time may the active experiments of
    one namespace direct more than 100% of traffic in total. This is a
    total-occupancy check (not pairwise), so three overlapping experiments
    at 40% each are rejected.

    Each item of ``other_versions`` is the *latest published* version of one
    *other* experiment in the namespace, with keys:
    ``experiment_key``, ``version``, ``namespace``, ``traffic_percentage``,
    ``config`` (decoded dict).
    """
    issues = list(validate_structure(config))
    if issues:
        return issues

    same_ns = [r for r in other_versions if r["namespace"] == namespace]
    others = [(r["traffic_percentage"], _other_windows(r)) for r in same_ns]

    new_windows = _candidate_windows(config)
    if new_windows is None:
        peak = _peak_concurrent(config.traffic_percentage, others)
        if peak > 100.0 + PERCENTAGE_TOLERANCE:
            issues.append(_issue(
                "namespace_traffic_conflict",
                (f"namespace {namespace!r}: publishing this always-on "
                 f"{config.traffic_percentage:g}% version would reach "
                 f"{peak:g}% total namespace traffic at peak (limit 100%)"),
                "traffic_percentage",
            ))
        return issues

    for s, e in new_windows:
        peak = _peak_concurrent(config.traffic_percentage, others, clip=(s, e))
        if peak > 100.0 + PERCENTAGE_TOLERANCE:
            active = sorted(
                f"{r['experiment_key']!r} v{r['version']} @ {r['traffic_percentage']:g}%"
                for r, (_t, w) in zip(same_ns, others)
                if w is None or any(not (e <= ws or s >= we) for ws, we in w))
            issues.append(_issue(
                "namespace_traffic_conflict",
                (f"namespace {namespace!r}: schedule window "
                 f"[{s.isoformat()}, {e.isoformat()}) would reach "
                 f"{peak:g}% total namespace traffic at peak (limit 100%); "
                 f"concurrent experiments: {', '.join(active) or 'n/a'}"),
                "traffic_percentage",
            ))
    return issues
