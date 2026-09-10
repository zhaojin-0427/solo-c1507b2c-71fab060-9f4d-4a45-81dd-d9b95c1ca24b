"""Cross-version assignment continuity.

Publishing a new version is normally an intentional reshuffle: the version
number participates in the bucket hash (see ``hashing.py``). A continuity
version instead *inherits* the assignment seed and variant order of one
already-published version of the same experiment, so users keep their group
when the configuration is effectively unchanged:

* identical variant set and weights ⇒ identical buckets ⇒ same variant;
* weight changes only move users whose bucket crosses a moved boundary
  (``weight_boundary_crossed``); every other bucket keeps its variant;
* a renamed variant keeps its position through the rename mapping
  (``retained``);
* added variants inherit no old position — the buckets that now fall inside
  their new ranges are reported as ``variant_added``;
* removed variants leave their old buckets behind — those users fall into a
  surviving range and are reported as ``variant_removed``.

Eligibility (audience, schedule windows, whitelist, the namespace traffic
gate and the overall traffic percentage) is always decided by the *target*
version; continuity only governs the variant dimension.

The frozen continuity block is resolved once at create time and stored with
the (immutable) version, so migration configuration is frozen at the same
instant as the rest of the version config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .hashing import BUCKET_SPACE, assignment_bucket
from .schemas import ValidationIssue, VersionConfigIn
from .validation import _issue  # reuse the structured-issue constructor


# ---------------------------------------------------------------------------
# Resolution (create/preflight time)
# ---------------------------------------------------------------------------


def explicit_mode(declaration: Any) -> Optional[str]:
    """Return the declared continuity mode, if any."""
    return None if declaration is None else declaration.mode


def resolve_continuity(experiment_key: str, target_version: Optional[int],
                       config: VersionConfigIn, declaration: Any,
                       get_version: Any) -> tuple[Optional[dict[str, Any]],
                                                  list[ValidationIssue]]:
    """Resolve the frozen continuity block for a candidate version.

    ``get_version(version_number)`` returns a ``LoadedVersion`` or raises
    ``NotFoundError``. Returns ``(block, issues)``; when issues is non-empty
    the block is None and the caller must reject the request with the
    structured issues (HTTP 422), mirroring structural validation.

    Modes:

    * no declaration / ``reshuffle`` — ordinary independent shuffle; the
      stored block is ``{"mode": "reshuffle"}``;
    * ``inherit`` — inherit seed + order of one published version of the
      same experiment, with an optional stable rename mapping.
    """
    if declaration is None:
        return None, []

    if declaration.mode == "reshuffle":
        if declaration.renames:
            return None, [_issue(
                "continuity_renames_without_inherit",
                "variant renames require mode 'inherit'; a reshuffle version "
                "cannot carry a rename mapping",
                "continuity.renames")]
        return {"mode": "reshuffle"}, []

    if declaration.mode != "inherit":  # pragma: no cover - guarded by Literal
        return None, [_issue("continuity_mode",
                             f"unknown continuity mode {declaration.mode!r}",
                             "continuity.mode")]

    issues: list[ValidationIssue] = []
    if declaration.source_version is None:
        issues.append(_issue(
            "continuity_source_required",
            "continuity mode 'inherit' requires a source_version",
            "continuity.source_version"))
        return None, issues

    # Source must be a *published* version of the same experiment.
    from .errors import NotFoundError
    try:
        source = get_version(declaration.source_version)
    except NotFoundError:
        return None, [_issue(
            "continuity_source_not_found",
            f"source version {declaration.source_version} of experiment "
            f"{experiment_key!r} does not exist",
            "continuity.source_version")]

    if declaration.source_experiment is not None and \
            declaration.source_experiment != experiment_key:
        issues.append(_issue(
            "continuity_cross_experiment",
            (f"continuity source must belong to the same experiment; "
             f"got {declaration.source_experiment!r}, expected {experiment_key!r}"),
            "continuity.source_experiment"))
    if source.experiment_key != experiment_key:  # storage-level safety net
        issues.append(_issue(
            "continuity_cross_experiment",
            "continuity source must belong to the same experiment",
            "continuity.source_version"))
    if source.status != "published":
        issues.append(_issue(
            "continuity_source_draft",
            (f"source version {declaration.source_version} is {source.status}; "
             f"only a published version can anchor continuity"),
            "continuity.source_version"))
    if issues:
        return None, issues

    source_keys = [v.key for v in source.config.variants]
    target_keys = [v.key for v in config.variants]
    source_set, target_set = set(source_keys), set(target_keys)

    # Validate the rename mapping before touching order resolution.
    renames: dict[str, str] = {}
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    for i, entry in enumerate(declaration.renames or []):
        loc = f"continuity.renames[{i}]"
        if entry.source in seen_sources:
            issues.append(_issue(
                "duplicate_rename_source",
                f"variant {entry.source!r} appears in more than one rename mapping",
                f"{loc}.source"))
        if entry.target in seen_targets:
            issues.append(_issue(
                "duplicate_rename_target",
                f"rename target {entry.target!r} is mapped more than once",
                f"{loc}.target"))
        seen_sources.add(entry.source)
        seen_targets.add(entry.target)
        if entry.source not in source_set:
            issues.append(_issue(
                "rename_source_not_found",
                (f"rename source {entry.source!r} is not a variant of source "
                 f"version {source.version}"),
                f"{loc}.source"))
        if entry.target not in target_set:
            issues.append(_issue(
                "rename_target_not_found",
                (f"rename target {entry.target!r} is not a declared variant "
                 f"of the new version"),
                f"{loc}.target"))
        renames[entry.source] = entry.target

    if issues:
        return None, issues

    # Build the full forward map for every source variant and reject merges:
    # two source variants must never map to the same new key. This catches
    # both duplicate rename targets (already flagged) and a rename target
    # that collides with an unmapped surviving source variant.
    forward = {old: renames.get(old, old) for old in source_set}
    output_counts: dict[str, list[str]] = {}
    for old, new in forward.items():
        output_counts.setdefault(new, []).append(old)
    for new, olds in output_counts.items():
        if len(olds) > 1:
            issues.append(_issue(
                "rename_target_conflict",
                (f"rename target {new!r} would merge source variants "
                 f"{sorted(olds)}; each target variant may inherit only one "
                 f"source variant"),
                "continuity.renames"))

    if issues:
        return None, issues

    # Effective order: retained variants keep source order; added variants
    # are interspersed at their natural (sorted) position.
    effective_order = _merge_order(source_keys, target_set, renames)
    # The assignment seed follows the anchor chain: when the source itself
    # inherited an older seed, the new version inherits that same seed, so
    # buckets stay stable across a chain of continuity versions.
    source_block = source.continuity
    if source_block is not None and source_block.get("mode") == "inherit":
        assignment_seed = source_block["assignment_seed"]
        source_salt = source_block.get("source_salt", source.salt)
    else:
        assignment_seed = f"{source.salt}|{source.version}"
        source_salt = source.salt
    block = {
        "mode": "inherit",
        "source_version": source.version,
        "source_experiment": source.experiment_key,
        "assignment_seed": assignment_seed,
        "source_salt": source_salt,
        "variant_order": effective_order,
        "renames": renames,
        "source_variants": [
            {"key": v.key, "percentage": v.percentage}
            for v in source.config.variants],
        "source_variant_order": list(source_keys),
    }
    return block, []


def _merge_order(source_keys: list[str], target_keys: set[str],
                 renames: dict[str, str]) -> list[str]:
    """Stable variant order for a continuity version.

    A source key carries forward under its (possibly renamed) target key.
    Carried keys keep the source's original relative order; genuinely new
    variants are inserted at their natural sorted position relative to the
    carried keys (new-key-only ties break alphabetically).
    """
    carried: list[str] = []
    for old in source_keys:
        new = renames.get(old, old)
        if new in target_keys:
            carried.append(new)
    added = sorted(target_keys - set(carried))
    merged: list[str] = []
    ci = 0
    for new_key in added:
        while ci < len(carried) and carried[ci] < new_key:
            merged.append(carried[ci])
            ci += 1
        merged.append(new_key)
    merged.extend(carried[ci:])
    return merged


# ---------------------------------------------------------------------------
# Effective bucketing parameters (decision time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentParams:
    seed: str
    variant_order: list[str]
    inherited: bool


def effective_assignment(loaded: Any) -> AssignmentParams:
    """Resolve the assignment seed + variant ordering for a loaded version."""
    block = getattr(loaded, "continuity", None)
    if block is not None and block.get("mode") == "inherit":
        return AssignmentParams(
            seed=block["assignment_seed"],
            variant_order=list(block["variant_order"]),
            inherited=True,
        )
    # Ordinary versions: per-version salt|version seed, sorted-key order.
    ordered = sorted(v.key for v in loaded.config.variants)
    return AssignmentParams(
        seed=f"{loaded.salt}|{loaded.version}",
        variant_order=ordered,
        inherited=False,
    )


def variant_ranges(config: VersionConfigIn,
                   order: list[str]) -> list[dict[str, Any]]:
    """Contiguous [0, 10000) ranges laid out in the given variant order.

    The last range absorbs rounding so ranges cover the full bucket space.
    """
    by_key = {v.key: v for v in config.variants}
    ranges: list[dict[str, Any]] = []
    cursor = 0
    for i, key in enumerate(order):
        if i == len(order) - 1:
            width = BUCKET_SPACE - cursor
        else:
            width = round(BUCKET_SPACE * by_key[key].percentage / 100.0)
        ranges.append({"variant_key": key, "start": cursor,
                       "end": cursor + width, "percentage": by_key[key].percentage})
        cursor += width
    return ranges


def variant_for_bucket(ranges: list[dict[str, Any]], bucket: int) -> dict[str, Any]:
    for r in ranges:
        if r["start"] <= bucket < r["end"]:
            return r
    return ranges[-1]


# ---------------------------------------------------------------------------
# Continuity evaluation (decision time)
# ---------------------------------------------------------------------------


def source_ranges(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild the source version's variant ranges from the frozen block."""
    order = block["source_variant_order"]
    pct = {v["key"]: v["percentage"] for v in block["source_variants"]}
    ranges: list[dict[str, Any]] = []
    cursor = 0
    for i, key in enumerate(order):
        if i == len(order) - 1:
            width = BUCKET_SPACE - cursor
        else:
            width = round(BUCKET_SPACE * pct[key] / 100.0)
        ranges.append({"variant_key": key, "start": cursor,
                       "end": cursor + width})
        cursor += width
    return ranges


def evaluate_continuity(block: dict[str, Any],
                        target_ranges: list[dict[str, Any]],
                        bucket: int) -> dict[str, Any]:
    """Compare where ``bucket`` landed in the source vs. the new version.

    Returns a trace-ready dict with the source variant (pre-rename), its
    post-rename key, the current variant and a machine-readable change
    reason. The inherited seed guarantees the bucket position itself is
    identical in both versions; only the range layout can move.
    """
    renames: dict[str, str] = block["renames"]
    old_ranges = source_ranges(block)
    old = variant_for_bucket(old_ranges, bucket)["variant_key"]
    new_variant = variant_for_bucket(target_ranges, bucket)["variant_key"]
    old_after_rename = renames.get(old, old)
    renamed = old_after_rename != old

    # Forward image of every source variant under the rename map: a variant
    # outside this set can only exist because it was newly added.
    source_keys = {v["key"] for v in block["source_variants"]}
    carried_keys = {renames.get(k, k) for k in source_keys}
    target_keys = {r["variant_key"] for r in target_ranges}

    if new_variant == old_after_rename:
        status = "retained"
        reason = "renamed_variant" if renamed else "same_variant"
    elif new_variant not in carried_keys:
        status = "switched"
        reason = "variant_added"
    elif old_after_rename not in target_keys:
        status = "switched"
        reason = "variant_removed"
    else:
        status = "switched"
        reason = "weight_boundary_crossed"

    return {
        "mode": "inherit",
        "source_version": block["source_version"],
        "assignment_seed": block["assignment_seed"],
        "bucket": bucket,
        "source_variant": old,
        "source_variant_after_rename": old_after_rename,
        "variant_key": new_variant,
        "status": status,
        "change_reason": reason,
        "renamed": renamed,
    }


def migration_status(block: Optional[dict[str, Any]],
                     from_variant: Optional[str],
                     to_variant: Optional[str],
                     from_enrolled: bool, to_enrolled: bool) -> str:
    """Classify one user across two published versions for the preview."""
    if not from_enrolled and to_enrolled:
        return "entered"
    if from_enrolled and not to_enrolled:
        return "exited"
    if not from_enrolled and not to_enrolled:
        return "not_enrolled"
    if not block or block.get("mode") != "inherit":
        return "retained" if from_variant == to_variant else "switched"
    renames = block.get("renames", {})
    if to_variant == renames.get(from_variant, from_variant):
        return "retained"
    return "switched"


def switch_reason_between(from_loaded: Any, to_loaded: Any,
                          from_variant: str, to_variant: str,
                          user_key: str) -> str:
    """Explain why one enrolled user changed variant between two versions.

    Unlike the decision trace (which compares against the frozen anchor of
    the target version's continuity chain), this compares the two versions
    the caller explicitly named in a migration preview.
    """
    block = getattr(to_loaded, "continuity", None)
    if block is None or block.get("mode") != "inherit":
        return "reshuffled"
    renames = block.get("renames", {})
    if to_variant == renames.get(from_variant, from_variant):
        return "retained"

    from_params = effective_assignment(from_loaded)
    to_params = effective_assignment(to_loaded)
    to_ranges = variant_ranges(to_loaded.config, to_params.variant_order)

    # If the two versions place this user at different bucket positions,
    # their assignment seeds differ (the target's anchor chain does not
    # reach `from_version`) — that is a reshuffle, not a boundary move.
    from_bucket = assignment_bucket(
        from_loaded.experiment_key, from_params.seed, user_key)
    to_bucket = assignment_bucket(
        to_loaded.experiment_key, to_params.seed, user_key)
    if from_bucket != to_bucket:
        return "reshuffled"

    from_keys = {v.key for v in from_loaded.config.variants}
    from_carried = {renames.get(k, k) for k in from_keys}
    to_keys = {r["variant_key"] for r in to_ranges}

    if to_variant not in from_carried:
        return "variant_added"
    if renames.get(from_variant, from_variant) not in to_keys:
        return "variant_removed"
    # Same bucket position and both variants carried across: the range
    # boundary must have moved across the user's position.
    return "weight_boundary_crossed"
