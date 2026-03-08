"""Shared datetime helpers for electricity_price_suite."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def parse_iso_aware(value: object) -> datetime | None:
    """Parse an ISO datetime string and require timezone information."""

    if value is None:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt


def parse_iso_in_tz(value: object, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO datetime string and normalize it into the given timezone."""

    dt = parse_iso_aware(value)
    if dt is None:
        return None
    return dt.astimezone(tz)


def format_iso(dt: datetime, *, timespec: str = "seconds") -> str:
    """Format a datetime consistently for persisted payloads and attributes."""

    return dt.isoformat(timespec=timespec)
