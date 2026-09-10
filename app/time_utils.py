"""Time helpers.

All datetimes crossing the API boundary are timezone aware and normalized
to UTC; schedule windows use the half-open interval [start, end).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def window_active(start: datetime, end: datetime, at: datetime) -> bool:
    """Half-open interval [start, end)."""
    at = at.astimezone(timezone.utc)
    return start.astimezone(timezone.utc) <= at < end.astimezone(timezone.utc)


def windows_overlap(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> bool:
    """Half-open windows overlap when neither is strictly before the other."""
    return (a_start.astimezone(timezone.utc) < b_end.astimezone(timezone.utc)
            and b_start.astimezone(timezone.utc) < a_end.astimezone(timezone.utc))


def any_overlap(windows: Iterable[tuple[datetime, datetime]],
                start: datetime, end: datetime) -> bool:
    return any(windows_overlap(start, end, ws, we) for ws, we in windows)
