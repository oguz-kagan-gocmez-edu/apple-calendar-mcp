"""Time-zone and ISO-8601 helpers.

Every datetime that crosses a module boundary in this package is *aware*
(has a tzinfo). Naive input from the model is interpreted in the server's
local time zone, which is resolved once at startup.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ_ENV_VAR = "APPLE_CALENDAR_MCP_TZ"


def resolve_local_tz(override: str | None = None) -> tzinfo:
    """Return the IANA zone to use for naive times.

    Order: explicit override -> $APPLE_CALENDAR_MCP_TZ -> $TZ ->
    /etc/localtime symlink (macOS and most Linux) -> fixed system offset.
    An IANA zone is preferred over a fixed offset because it handles DST.
    """
    for candidate in (override, os.environ.get(TZ_ENV_VAR), os.environ.get("TZ")):
        if candidate:
            try:
                return ZoneInfo(candidate.lstrip(":"))
            except (ZoneInfoNotFoundError, ValueError):
                continue

    localtime = Path("/etc/localtime")
    try:
        target = str(localtime.resolve())
        marker = "zoneinfo/"
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass

    fallback = datetime.now().astimezone().tzinfo
    assert fallback is not None
    return fallback


def tz_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def is_date_only(value: str) -> bool:
    """True for strings like '2026-11-03' (no time component)."""
    return len(value.strip()) == 10 and "T" not in value and " " not in value.strip()


def parse_datetime(value: str, tz: tzinfo) -> datetime:
    """Parse ISO 8601 into an aware datetime.

    Accepts '2026-11-03', '2026-11-03T14:00', '2026-11-03 14:00',
    '2026-11-03T14:00:00+03:00' and a trailing 'Z'. Date-only values map
    to local midnight. Naive values are interpreted in ``tz``.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("empty datetime string")
    try:
        if is_date_only(raw):
            return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=tz)
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError as exc:
        raise ValueError(
            f"Invalid date/time {value!r}. Use ISO 8601, e.g. "
            "'2026-11-03', '2026-11-03T14:00' or '2026-11-03T14:00:00+03:00'."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_clock(value: str) -> time:
    """Parse 'HH:MM' (24h)."""
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid clock time {value!r}; expected 'HH:MM'.") from exc


def start_of_day(d: date, tz: tzinfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


def to_local(dt: datetime, tz: tzinfo) -> datetime:
    return dt.astimezone(tz)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap: [a_start, a_end) ∩ [b_start, b_end) ≠ ∅.

    A zero-length interval (a_start == a_end) counts as overlapping when it
    lies inside the other interval, so instantaneous events are not lost.
    """
    if a_start == a_end:
        return b_start <= a_start < b_end
    return a_start < b_end and b_start < a_end


ONE_DAY = timedelta(days=1)
