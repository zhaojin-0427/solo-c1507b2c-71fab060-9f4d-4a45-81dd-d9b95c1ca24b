"""Audience condition tree evaluation.

Produces a nested trace alongside the boolean result so every decision can
explain *why* a user matched or missed the audience.
"""

from __future__ import annotations

from typing import Any

from .schemas import AudienceNode, ConditionSpec

_MISSING = object()


def get_path(attrs: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path (``user.tier``) against a nested dict."""
    cur: Any = attrs
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _compare(op: str, actual: Any, expected: Any) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "in":
            return isinstance(expected, (list, tuple)) and actual in expected
        if op == "nin":
            return isinstance(expected, (list, tuple)) and actual not in expected
        if op in ("gt", "gte", "lt", "lte"):
            if actual is None or expected is None:
                return False
            if type(actual) is not type(expected):
                return False
            return {
                "gt": actual > expected,
                "gte": actual >= expected,
                "lt": actual < expected,
                "lte": actual <= expected,
            }[op]
        if op == "contains":
            if isinstance(actual, (str, list, tuple, dict)):
                return expected in actual
            return False
        if op == "not_contains":
            if isinstance(actual, (str, list, tuple, dict)):
                return expected not in actual
            return False
        if op == "starts_with":
            return isinstance(actual, str) and actual.startswith(str(expected))
        if op == "ends_with":
            return isinstance(actual, str) and actual.endswith(str(expected))
    except TypeError:
        return False
    return False


def _eval_condition(cond: ConditionSpec, attrs: dict[str, Any]) -> dict[str, Any]:
    actual = get_path(attrs, cond.field)
    if cond.op == "exists":
        matched = actual is not _MISSING
    elif actual is _MISSING:
        matched = False
    else:
        matched = _compare(cond.op, actual, cond.value)
    return {
        "type": "condition",
        "field": cond.field,
        "op": cond.op,
        "expected": cond.value,
        "actual": None if actual is _MISSING else actual,
        "matched": matched,
    }


def evaluate(node: AudienceNode, attrs: dict[str, Any]) -> dict[str, Any]:
    if node.condition is not None:
        return _eval_condition(node.condition, attrs)

    if node.all is not None:
        children = [evaluate(child, attrs) for child in node.all]
        return {"type": "all", "matched": all(c["matched"] for c in children),
                "children": children}

    if node.any is not None:
        children = [evaluate(child, attrs) for child in node.any]
        return {"type": "any", "matched": any(c["matched"] for c in children),
                "children": children}

    # not node
    assert node.not_ is not None
    child = evaluate(node.not_, attrs)
    return {"type": "not", "matched": not child["matched"], "child": child}


def matches(node: AudienceNode, attrs: dict[str, Any]) -> bool:
    return bool(evaluate(node, attrs)["matched"])
