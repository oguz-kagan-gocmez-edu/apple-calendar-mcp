"""In-memory ``CalendarStore`` used by the test-suite and the ``--backend memory`` demo.

It deliberately mirrors EventKit's model of recurring events:

* a *series* has one identifier and a recurrence rule;
* each *occurrence* is addressed by (series id, original start);
* editing one occurrence (span THIS) creates a detached override;
* deleting one occurrence (span THIS) adds an exception date;
* span FUTURE splits the series at the targeted occurrence.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .models import Availability, Calendar, Event, EventDraft, EventPatch, Recurrence, Span
from .recurrence import count_before, occurrence_starts
from .store import (
    InvalidRequestError,
    NotFoundError,
    ReadOnlyCalendarError,
    make_event_id,
    split_event_id,
)
from .timeutil import ONE_DAY, overlaps

_OVERRIDABLE = ("title", "start", "end", "all_day", "location", "notes", "url", "alarms", "availability")


@dataclass
class _Series:
    uid: str
    calendar_id: str
    title: str
    start: datetime
    duration: timedelta
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    alarms: list[int] = field(default_factory=list)
    recurrence: Recurrence | None = None
    availability: Availability = "busy"
    exdates: set[int] = field(default_factory=set)
    overrides: dict[int, dict[str, Any]] = field(default_factory=dict)


def _clean(text: str | None, current: str | None) -> str | None:
    """Patch semantics for text: None keeps, '' clears, anything else sets."""
    if text is None:
        return current
    return text or None


class MemoryStore:
    def __init__(self, tz: tzinfo, calendars: list[Calendar] | None = None) -> None:
        self.tz = tz
        self._lock = threading.RLock()
        self._calendars: dict[str, Calendar] = {}
        for cal in calendars or [
            Calendar(
                id="home", title="Home", source="Local", color="#34C759", writable=True, is_default=True
            ),
            Calendar(id="work", title="Work", source="Local", color="#007AFF", writable=True),
            Calendar(id="holidays", title="Holidays", source="Subscribed", color="#8E8E93", writable=False),
        ]:
            self._calendars[cal.id] = cal
        self._series: dict[str, _Series] = {}

    # ------------------------------------------------------------ helpers
    def _calendar(self, calendar_id: str | None, *, for_write: bool) -> Calendar:
        if calendar_id is None:
            cal = next((c for c in self._calendars.values() if c.is_default), None)
            if cal is None:
                raise InvalidRequestError("No default calendar; pass calendar_id.")
        else:
            cal = self._calendars.get(calendar_id)
            if cal is None:
                raise NotFoundError(f"No calendar with id {calendar_id!r}.")
        if for_write and not cal.writable:
            raise ReadOnlyCalendarError(f"Calendar {cal.title!r} is read-only.")
        return cal

    def _locate(self, event_id: str) -> tuple[_Series, datetime]:
        uid, ts = split_event_id(event_id)
        series = self._series.get(uid)
        if series is None:
            raise NotFoundError(f"No event with id {event_id!r}.")
        if ts is None:
            return series, series.start
        if ts in series.exdates:
            raise NotFoundError(f"Occurrence {event_id!r} was deleted.")
        for start in occurrence_starts(series.start, series.recurrence):
            st = int(start.timestamp())
            if st == ts:
                return series, start
            if st > ts:
                break
        raise NotFoundError(f"No event with id {event_id!r}.")

    def _materialize(self, s: _Series, original: datetime) -> Event:
        ts = int(original.timestamp())
        ov = s.overrides.get(ts, {})
        start = ov.get("start", original)
        cal = self._calendars[s.calendar_id]
        return Event(
            id=make_event_id(s.uid, ts),
            calendar_id=cal.id,
            calendar_title=cal.title,
            title=ov.get("title", s.title),
            start=start,
            end=ov.get("end", start + s.duration),
            all_day=ov.get("all_day", s.all_day),
            location=ov.get("location", s.location),
            notes=ov.get("notes", s.notes),
            url=ov.get("url", s.url),
            is_recurring=s.recurrence is not None,
            recurrence=s.recurrence,
            alarms_minutes_before=list(ov.get("alarms", s.alarms)),
            availability=ov.get("availability", s.availability),
        )

    @staticmethod
    def _shift_keys(s: _Series, delta: timedelta) -> None:
        secs = int(delta.total_seconds())
        if secs == 0:
            return
        s.exdates = {t + secs for t in s.exdates}
        s.overrides = {t + secs: v for t, v in s.overrides.items()}

    def _apply_to_series(self, s: _Series, target: datetime, patch: EventPatch) -> datetime:
        """Apply ``patch`` to the whole series ``s``.

        Time changes are expressed relative to the targeted occurrence and
        applied as a shift to the series. Returns the target's new original start.
        """
        current = self._materialize(s, target)
        new_target = target
        if patch.start is not None:
            delta = patch.start - current.start
            s.start = s.start + delta
            new_target = target + delta
            self._shift_keys(s, delta)
        if patch.end is not None:
            new_start = patch.start or current.start
            s.duration = patch.end - new_start
        if patch.all_day is not None:
            s.all_day = patch.all_day
        if patch.title is not None:
            s.title = patch.title
        s.location = _clean(patch.location, s.location)
        s.notes = _clean(patch.notes, s.notes)
        s.url = _clean(patch.url, s.url)
        if patch.alarms_minutes_before is not None:
            s.alarms = list(patch.alarms_minutes_before)
        if patch.availability is not None:
            s.availability = patch.availability
        if patch.calendar_id is not None:
            s.calendar_id = self._calendar(patch.calendar_id, for_write=True).id
        if patch.remove_recurrence:
            s.recurrence = None
            s.exdates.clear()
            s.overrides.clear()
            s.start = new_target
        elif patch.recurrence is not None:
            s.recurrence = patch.recurrence
        return new_target

    def _split(self, s: _Series, at: datetime) -> _Series:
        """Cut series ``s`` so it ends before ``at``; return the new tail series."""
        assert s.recurrence is not None
        rule = s.recurrence
        tail_rule = rule
        if rule.count is not None:
            remaining = rule.count - count_before(s.start, rule, at)
            tail_rule = rule.model_copy(update={"count": max(remaining, 1)})
        cut = int(at.timestamp())
        tail = _Series(
            uid=uuid.uuid4().hex.upper(),
            calendar_id=s.calendar_id,
            title=s.title,
            start=at,
            duration=s.duration,
            all_day=s.all_day,
            location=s.location,
            notes=s.notes,
            url=s.url,
            alarms=list(s.alarms),
            recurrence=tail_rule,
            availability=s.availability,
            exdates={t for t in s.exdates if t >= cut},
            overrides={t: v for t, v in s.overrides.items() if t >= cut},
        )
        # One occurrence per day at most for our rule subset, so "until the day
        # before" cuts exactly at ``at``.
        last_day: date = at.date() - ONE_DAY
        s.recurrence = rule.model_copy(update={"until": last_day, "count": None})
        s.exdates = {t for t in s.exdates if t < cut}
        s.overrides = {t: v for t, v in s.overrides.items() if t < cut}
        self._series[tail.uid] = tail
        return tail

    # ------------------------------------------------------------ protocol
    def list_calendars(self) -> list[Calendar]:
        with self._lock:
            return list(self._calendars.values())

    def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        with self._lock:
            wanted = set(calendar_ids) if calendar_ids else None
            out: list[Event] = []
            for s in self._series.values():
                if wanted is not None and s.calendar_id not in wanted:
                    continue
                seen: set[int] = set()
                for original in occurrence_starts(s.start, s.recurrence):
                    if original >= end:
                        break
                    ts = int(original.timestamp())
                    seen.add(ts)
                    if ts in s.exdates:
                        continue
                    ev = self._materialize(s, original)
                    if overlaps(ev.start, ev.end, start, end):
                        out.append(ev)
                # detached occurrences moved into the window from later dates
                for ts in s.overrides.keys() - seen - s.exdates:
                    ev = self._materialize(s, datetime.fromtimestamp(ts, tz=s.start.tzinfo))
                    if overlaps(ev.start, ev.end, start, end):
                        out.append(ev)
            out.sort(key=lambda e: (e.start, e.title))
            return out

    def get_event(self, event_id: str) -> Event:
        with self._lock:
            s, original = self._locate(event_id)
            return self._materialize(s, original)

    def create_event(self, draft: EventDraft) -> Event:
        with self._lock:
            cal = self._calendar(draft.calendar_id, for_write=True)
            s = _Series(
                uid=uuid.uuid4().hex.upper(),
                calendar_id=cal.id,
                title=draft.title,
                start=draft.start,
                duration=draft.end - draft.start,
                all_day=draft.all_day,
                location=draft.location or None,
                notes=draft.notes or None,
                url=draft.url or None,
                alarms=list(draft.alarms_minutes_before),
                recurrence=draft.recurrence,
                availability=draft.availability or "busy",
            )
            self._series[s.uid] = s
            return self._materialize(s, s.start)

    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        with self._lock:
            s, original = self._locate(event_id)
            self._calendar(s.calendar_id, for_write=True)

            if s.recurrence is None or span is Span.ALL or (span is Span.FUTURE and original == s.start):
                target = self._apply_to_series(s, original, patch)
                return self._materialize(s, target)

            if span is Span.FUTURE:
                tail = self._split(s, original)
                target = self._apply_to_series(tail, tail.start, patch)
                return self._materialize(tail, target)

            # Span.THIS on a recurring series -> detached override
            if patch.touches_recurrence():
                raise InvalidRequestError("Recurrence can't be changed for a single occurrence.")
            if patch.calendar_id is not None and patch.calendar_id != s.calendar_id:
                raise InvalidRequestError(
                    "A single occurrence can't move to another calendar; use span 'all'."
                )
            ts = int(original.timestamp())
            ov = s.overrides.setdefault(ts, {})
            for name, value in (
                ("title", patch.title),
                ("start", patch.start),
                ("end", patch.end),
                ("all_day", patch.all_day),
                ("availability", patch.availability),
            ):
                if value is not None:
                    ov[name] = value
            for name in ("location", "notes", "url"):
                value = getattr(patch, name)
                if value is not None:
                    ov[name] = value or None
            if patch.alarms_minutes_before is not None:
                ov["alarms"] = list(patch.alarms_minutes_before)
            assert set(ov) <= set(_OVERRIDABLE)
            return self._materialize(s, original)

    def delete_event(self, event_id: str, span: Span) -> Event:
        with self._lock:
            s, original = self._locate(event_id)
            self._calendar(s.calendar_id, for_write=True)
            before = self._materialize(s, original)
            ts = int(original.timestamp())
            if s.recurrence is None or span is Span.ALL or (span is Span.FUTURE and original == s.start):
                del self._series[s.uid]
            elif span is Span.THIS:
                s.exdates.add(ts)
                s.overrides.pop(ts, None)
            else:
                assert s.recurrence is not None
                s.recurrence = s.recurrence.model_copy(
                    update={"until": original.date() - ONE_DAY, "count": None}
                )
                s.exdates = {t for t in s.exdates if t < ts}
                s.overrides = {t: v for t, v in s.overrides.items() if t < ts}
            return before


def seed_demo(store: MemoryStore, today: date | None = None) -> None:
    """Populate a believable student week, for demos and the MCP Inspector."""
    tz = store.tz
    today = today or datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())

    def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
        return datetime.combine(monday + timedelta(days=day_offset), datetime.min.time(), tzinfo=tz).replace(
            hour=hh, minute=mm
        )

    def add(title: str, start: datetime, minutes: int, cal: str = "home", **kw: Any) -> None:
        store.create_event(
            EventDraft(
                title=title, start=start, end=start + timedelta(minutes=minutes), calendar_id=cal, **kw
            )
        )

    add(
        "Operating Systems lecture",
        at(0, 10, 40),
        110,
        "work",
        location="AB1 245",
        recurrence=Recurrence(frequency="weekly", days_of_week=["mon", "wed"], count=28),
    )
    add("Robotics team meeting", at(1, 18), 90, "work", recurrence=Recurrence(frequency="weekly"))
    add("Gym", at(2, 7, 30), 60, alarms_minutes_before=[15])
    add("Coffee with Ali", at(3, 15), 45, location="Kampüs Kafe")
    add("Lab report deadline", at(4, 23, 59), 0, "work", alarms_minutes_before=[1440, 60])
    store.create_event(
        EventDraft(
            title="Hiking trip",
            start=datetime.combine(monday + timedelta(days=5), datetime.min.time(), tzinfo=tz),
            end=datetime.combine(monday + timedelta(days=6), datetime.min.time(), tzinfo=tz),
            all_day=True,
            calendar_id="home",
            availability="free",
        )
    )
