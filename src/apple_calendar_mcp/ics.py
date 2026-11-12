"""Reading and writing iCalendar (.ics) text, without any backend.

Export writes what the server sees: one VEVENT per event, and for a repeating
event one VEVENT per occurrence in the exported range (so changed and removed
occurrences come out right). Timed events are written in UTC, all-day events
as dates.

Import reads the common subset of RFC 5545 that maps onto this server's
events: one-off and simply repeating events, with alarms and removed
occurrences (EXDATE). Anything it can't represent faithfully is reported as
skipped with the reason, never imported half-right.

Standard library only, so it works on every backend.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .models import WEEKDAYS, Availability, Event, Recurrence
from .store import split_event_id
from .timeutil import ONE_DAY

PRODID = "-//apple-calendar-mcp//Export//EN"
_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_FREQ = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")

Prop = tuple[str, dict[str, str], str]


@dataclass
class IcsEvent:
    """One importable event. ``end`` is exclusive; all-day times are local midnights."""

    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    alarms: list[int] = field(default_factory=list)
    recurrence: Recurrence | None = None
    availability: Availability | None = None
    exdates: list[datetime] = field(default_factory=list)


@dataclass
class IcsSkip:
    title: str
    reason: str


class _Unsupported(Exception):
    """This VEVENT can't be imported faithfully; the message says why."""


# ---------------------------------------------------------------- writing
def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold at 75 octets (RFC 5545 3.1) without splitting a UTF-8 character."""
    out: list[str] = []
    current, size = "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > 75:
            out.append(current)
            current, size = " ", 1
        current += ch
        size += width
    out.append(current)
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def export_ics(events: Iterable[Event], now: datetime) -> str:
    """iCalendar text for ``events`` (already expanded occurrences)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    for e in events:
        series, ts = split_event_id(e.id)
        uid = f"{series}-{ts}" if e.is_recurring and ts is not None else series
        lines += ["BEGIN:VEVENT", f"UID:{_escape(uid)}", f"DTSTAMP:{_utc(now)}"]
        if e.all_day:
            lines += [
                f"DTSTART;VALUE=DATE:{e.start.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{e.end.strftime('%Y%m%d')}",
            ]
        else:
            lines += [f"DTSTART:{_utc(e.start)}", f"DTEND:{_utc(e.end)}"]
        lines.append(f"SUMMARY:{_escape(e.title)}")
        if e.location:
            lines.append(f"LOCATION:{_escape(e.location)}")
        if e.notes:
            lines.append(f"DESCRIPTION:{_escape(e.notes)}")
        if e.url:
            lines.append("URL:" + e.url.replace("\r", "").replace("\n", ""))
        if e.availability == "free":
            lines.append("TRANSP:TRANSPARENT")
        if e.status in ("confirmed", "tentative"):
            lines.append(f"STATUS:{e.status.upper()}")
        elif e.status == "canceled":
            lines.append("STATUS:CANCELLED")
        for minutes in e.alarms_minutes_before:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "DESCRIPTION:Reminder",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# ---------------------------------------------------------------- reading
def _unescape(text: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def _content_line(line: str) -> Prop:
    """Split ``NAME;PARAM=value;...:value`` (parameter values may be quoted)."""
    n = len(line)
    i = 0
    while i < n and line[i] not in ";:":
        i += 1
    name = line[:i].upper()
    params: dict[str, str] = {}
    while i < n and line[i] == ";":
        eq = line.find("=", i)
        if eq < 0:
            raise ValueError(line)
        key = line[i + 1 : eq].upper()
        i = eq + 1
        if i < n and line[i] == '"':
            close = line.find('"', i + 1)
            if close < 0:
                raise ValueError(line)
            params[key] = line[i + 1 : close]
            i = close + 1
        else:
            j = i
            while j < n and line[j] not in ";:":
                j += 1
            params[key] = line[i:j]
            i = j
    if not name or i >= n or line[i] != ":":
        raise ValueError(line)
    return name, params, line[i + 1 :]


def _vevents(text: str) -> list[tuple[list[Prop], list[list[Prop]]]]:
    """Each VEVENT as (its properties, the properties of each VALARM)."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)

    found: list[tuple[list[Prop], list[list[Prop]]]] = []
    stack: list[str] = []
    saw_calendar = False
    for line in lines:
        try:
            name, params, value = _content_line(line)
        except ValueError:
            if stack and stack[0] == "VCALENDAR":
                continue  # a broken line inside the file: ignore it, keep the rest
            raise ValueError("This isn't iCalendar text: it should start with BEGIN:VCALENDAR.") from None
        kind = value.strip().upper()
        if name == "BEGIN":
            stack.append(kind)
            saw_calendar = saw_calendar or stack == ["VCALENDAR"]
            if stack == ["VCALENDAR", "VEVENT"]:
                found.append(([], []))
            elif stack == ["VCALENDAR", "VEVENT", "VALARM"]:
                found[-1][1].append([])
        elif name == "END":
            if not stack or stack[-1] != kind:
                raise ValueError("The iCalendar text is damaged: BEGIN and END lines don't match.")
            stack.pop()
        elif stack == ["VCALENDAR", "VEVENT"]:
            found[-1][0].append((name, params, value))
        elif stack == ["VCALENDAR", "VEVENT", "VALARM"]:
            found[-1][1][-1].append((name, params, value))
    if not saw_calendar:
        raise ValueError("This isn't iCalendar text: it should start with BEGIN:VCALENDAR.")
    if stack:
        raise ValueError("The iCalendar text is cut off: a BEGIN has no END.")
    return found


def _when(params: dict[str, str], value: str, tz: tzinfo) -> tuple[datetime, bool]:
    """A DATE or DATE-TIME value as an aware datetime in ``tz``, and whether it was a date."""
    v = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or len(v) == 8:
            return datetime.combine(datetime.strptime(v, "%Y%m%d").date(), time.min, tzinfo=tz), True
        naive = datetime.strptime(v.rstrip("Zz"), "%Y%m%dT%H%M%S")
    except ValueError:
        raise _Unsupported(f"it has a time that can't be read ({v[:30]!r})") from None
    if v[-1] in "Zz":
        return naive.replace(tzinfo=UTC).astimezone(tz), False
    tzid = params.get("TZID")
    if not tzid:
        return naive.replace(tzinfo=tz), False  # floating: the user's own zone
    try:
        zone = ZoneInfo(tzid.lstrip("/"))
    except (KeyError, ValueError, OSError):
        raise _Unsupported(f"its time zone {tzid[:40]!r} isn't a standard zone name") from None
    return naive.replace(tzinfo=zone).astimezone(tz), False


def _duration(value: str) -> timedelta | None:
    m = _DURATION.match(value.strip().upper())
    if not m or not any(m.groups()[1:]):
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups()[1:])
    span = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -span if m.group(1) == "-" else span


def _rule(value: str, start: datetime, tz: tzinfo) -> Recurrence:
    parts = dict(p.split("=", 1) for p in value.strip().upper().split(";") if "=" in p)
    freq = parts.pop("FREQ", "")
    if freq not in _FREQ:
        raise _Unsupported(f"it repeats {freq.lower() or 'in an unknown way'}, which isn't supported")
    parts.pop("WKST", None)
    try:
        interval = int(parts.pop("INTERVAL", "1"))
        count = int(parts.pop("COUNT")) if "COUNT" in parts else None
        until: date | None = None
        if "UNTIL" in parts:
            last, _ = _when({}, parts.pop("UNTIL"), tz)
            until = last.date()
    except ValueError:
        raise _Unsupported("its repeat rule can't be read") from None

    days = None
    if "BYDAY" in parts:
        codes = parts.pop("BYDAY").split(",")
        if freq != "WEEKLY" or any(c not in _BYDAY for c in codes):
            raise _Unsupported("it repeats on a pattern like 'the second Tuesday', which isn't supported")
        days = [WEEKDAYS[_BYDAY.index(c)] for c in codes]
    # Rules that only restate the start date add nothing.
    if freq in ("MONTHLY", "YEARLY") and parts.get("BYMONTHDAY") == str(start.day):
        del parts["BYMONTHDAY"]
    if freq == "YEARLY" and parts.get("BYMONTH") == str(start.month):
        del parts["BYMONTH"]
    if parts:
        raise _Unsupported(f"its repeat rule uses {', '.join(sorted(parts))}, which isn't supported")
    try:
        return Recurrence(
            frequency=_FREQ[freq],
            interval=interval,
            days_of_week=days,
            count=count,
            until=until,
        )
    except ValidationError as exc:
        raise _Unsupported(f"its repeat rule isn't valid ({exc.errors()[0]['msg']})") from None


def _event(props: list[Prop], alarms: list[list[Prop]], tz: tzinfo) -> IcsEvent:
    first: dict[str, tuple[dict[str, str], str]] = {}
    for name, params, value in props:
        first.setdefault(name, (params, value))

    def text(name: str) -> str | None:
        value = _unescape(first[name][1]).strip() if name in first else ""
        return value or None

    if "RECURRENCE-ID" in first:
        raise _Unsupported("it is a changed occurrence of a repeating event")
    if first.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
        raise _Unsupported("it is canceled")
    if "RDATE" in first:
        raise _Unsupported("it repeats on a list of extra dates, which isn't supported")
    if "DTSTART" not in first:
        raise _Unsupported("it has no start time")

    start, all_day = _when(*first["DTSTART"], tz)
    if "DTEND" in first:
        end, _ = _when(*first["DTEND"], tz)
    elif "DURATION" in first:
        span = _duration(first["DURATION"][1])
        if span is None:
            raise _Unsupported("its duration can't be read")
        end = start + span
    else:
        end = start + ONE_DAY if all_day else start
    if all_day and end <= start:
        end = start + ONE_DAY
    if end < start:
        raise _Unsupported("it ends before it starts")

    recurrence = _rule(first["RRULE"][1], start, tz) if "RRULE" in first else None
    exdates: list[datetime] = []
    if recurrence is not None:
        for name, params, value in props:
            if name == "EXDATE":
                exdates += [_when(params, v, tz)[0] for v in value.split(",") if v.strip()]
        if start in exdates:
            raise _Unsupported("its first occurrence was removed, which isn't supported")

    minutes: set[int] = set()
    for alarm in alarms:
        trigger = next(((p, v) for n, p, v in alarm if n == "TRIGGER"), None)
        if trigger is None or trigger[0].get("RELATED", "START").upper() != "START":
            continue
        before = _duration(trigger[1])
        if before is not None and before <= timedelta(0):
            minutes.add(int(-before.total_seconds() // 60))

    return IcsEvent(
        title=text("SUMMARY") or "",
        start=start,
        end=end,
        all_day=all_day,
        location=text("LOCATION"),
        notes=text("DESCRIPTION"),
        url=text("URL"),
        alarms=sorted(minutes, reverse=True),
        recurrence=recurrence,
        availability="free" if first.get("TRANSP", ({}, ""))[1].strip().upper() == "TRANSPARENT" else None,
        exdates=sorted(set(exdates)),
    )


def parse_ics(text: str, tz: tzinfo) -> tuple[list[IcsEvent], list[IcsSkip]]:
    """Importable events and the ones left out, in file order.

    Raises ``ValueError`` when ``text`` isn't iCalendar at all. Times without
    a zone (floating) are read in ``tz``.
    """
    events: list[IcsEvent] = []
    skipped: list[IcsSkip] = []
    for props, alarms in _vevents(text):
        title = next((_unescape(v).strip() for n, _, v in props if n == "SUMMARY"), "") or "(no title)"
        try:
            events.append(_event(props, alarms, tz))
        except _Unsupported as exc:
            skipped.append(IcsSkip(title=title[:200], reason=f"Skipped: {exc}."))
    return events, skipped
