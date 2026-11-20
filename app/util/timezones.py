"""Game timezone helpers.

Every game has a reference timezone (an IANA name such as ``Asia/Kolkata``,
default ``UTC``). Calendar logic for a game -- which day an award falls on,
streaks of consecutive days, time-of-day slots -- uses it, so a "day" means
the game's local day rather than the UTC day.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_GAME_TIMEZONE = "UTC"


def validate_timezone_name(value: str | None) -> str | None:
    """
    Return ``value`` unchanged if it is a valid IANA timezone name.

    ``None`` passes through so optional fields keep their default.

    Raises:
        ValueError: If ``value`` is not a known IANA timezone name.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a non-empty IANA name, e.g. 'UTC'")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Unknown timezone {value!r}; use an IANA name such as "
            "'UTC', 'Europe/Madrid' or 'Asia/Kolkata'"
        ) from exc
    return value


def zone_of(name: str | None) -> ZoneInfo:
    """
    Resolve a game's timezone name to a ``ZoneInfo``, falling back to UTC.

    Names are validated on input, so the fallback only covers rows written
    before the column existed or a timezone later removed from the system
    database; scoring keeps working in UTC rather than failing.
    """
    try:
        return ZoneInfo(name or DEFAULT_GAME_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_GAME_TIMEZONE)


def local_datetime(timestamp: datetime.datetime, zone: ZoneInfo) -> datetime.datetime:
    """``timestamp`` in ``zone``; naive values are taken as UTC (as stored)."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return timestamp.astimezone(zone)


def local_now(zone: ZoneInfo) -> datetime.datetime:
    """The current time in ``zone``."""
    return datetime.datetime.now(zone)
