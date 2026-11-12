"""Use-case layer between the MCP tools and a ``CalendarStore``.

Responsibilities:
  * parse and validate the loose input a language model sends;
  * resolve defaults (durations, all-day ranges, search windows);
  * implement search and free-slot finding on top of plain CRUD.

Everything here is synchronous and backend-agnostic, so it is unit-tested
against ``MemoryStore`` without macOS.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import date, datetime, time, timedelta, tzinfo
from urllib.parse import urlparse

from pydantic import ValidationError

from .changes import ChangeJournal, ChangeRecord, Step, check_unchanged, revert
from .ics import IcsEvent, export_ics, parse_ics
from .models import (
    Agenda,
    Availability,
    BatchChange,
    BatchResult,
    Calendar,
    CalendarStats,
    CalendarUsage,
    ChangeInfo,
    ChangeOutcome,
    Conflict,
    ConflictResult,
    CreateChange,
    DayStats,
    DeleteChange,
    DeleteResult,
    DuplicateGroup,
    DuplicateResult,
    Event,
    EventDraft,
    EventList,
    EventPatch,
    EventRef,
    FreeSlotResult,
    IcsExport,
    ImportResult,
    Recurrence,
    Reply,
    SkippedItem,
    Span,
    TimeContext,
    TitleUsage,
    UndoResult,
    WeekdayStats,
)
from .scheduling import (
    blocks_time,
    busy_time,
    duplicate_key,
    find_conflicts,
    find_duplicates,
    find_free_slots,
)
from .store import (
    CalendarError,
    CalendarStore,
    InvalidRequestError,
    NotFoundError,
    make_event_id,
    split_event_id,
)
from .timeutil import (
    ONE_DAY,
    is_date_only,
    overlaps,
    parse_clock,
    parse_datetime,
    start_of_day,
    tz_name,
)

MAX_RANGE = timedelta(days=366)
MAX_ALARM_MINUTES = 4 * 7 * 24 * 60  # four weeks
MAX_BUFFER_MINUTES = 240
MAX_AGENDA_DAYS = 14
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}
DEFAULT_DURATION = timedelta(hours=1)
MAX_BATCH = 25
MAX_EXPORT_EVENTS = 500
MAX_IMPORT_EVENTS = 100
MAX_ICS_CHARS = 1_000_000


def _conflict(pair: tuple[Event, Event, datetime, datetime]) -> Conflict:
    a, b, start, end = pair
    return Conflict(
        start=start,
        end=end,
        overlap_minutes=int((end - start).total_seconds() // 60),
        events=[EventRef.of(a), EventRef.of(b)],
    )


@contextmanager
def _as_invalid_request() -> Iterator[None]:
    """Turn parsing/validation errors into user-facing ``InvalidRequestError``."""
    try:
        yield
    except ValidationError as exc:
        msgs = "; ".join(err["msg"] for err in exc.errors())
        raise InvalidRequestError(msgs) from exc
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


class CalendarService:
    def __init__(
        self,
        store: CalendarStore,
        tz: tzinfo,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.tz = tz
        self._clock = clock or (lambda: datetime.now(tz))
        self.journal = ChangeJournal()

    # ------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self._clock().astimezone(self.tz)

    def _parse(self, value: str, field: str) -> datetime:
        try:
            return parse_datetime(value, self.tz)
        except ValueError as exc:
            raise InvalidRequestError(f"{field}: {exc}") from exc

    def _range(self, start: str, end: str) -> tuple[datetime, datetime]:
        s, e = self._parse(start, "start"), self._parse(end, "end")
        if is_date_only(end):
            e += ONE_DAY  # 'end=2026-11-07' means "through the 7th"
        if e <= s:
            raise InvalidRequestError("end must be after start.")
        if e - s > MAX_RANGE:
            raise InvalidRequestError("Range too large: query at most 366 days at a time.")
        return s, e

    @staticmethod
    def _limit(limit: int) -> int:
        if not 1 <= limit <= 1000:
            raise InvalidRequestError("limit must be between 1 and 1000.")
        return limit

    @staticmethod
    def _alarms(alarms: list[int] | None) -> list[int] | None:
        if alarms is None:
            return None
        for m in alarms:
            if not 0 <= m <= MAX_ALARM_MINUTES:
                raise InvalidRequestError(
                    f"Alarm offsets must be 0..{MAX_ALARM_MINUTES} minutes before the event."
                )
        return sorted(set(alarms), reverse=True)

    @staticmethod
    def _url(url: str | None) -> str | None:
        if not url:  # None keeps, '' clears
            return url
        scheme = urlparse(url).scheme.lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            raise InvalidRequestError(
                f"URL scheme {scheme or '(none)'!r} not allowed; use one of {sorted(ALLOWED_URL_SCHEMES)}."
            )
        return url

    @staticmethod
    def _check_recurrence(rec: Recurrence | None, start: datetime) -> None:
        if rec is not None and rec.until is not None and rec.until < start.date():
            raise InvalidRequestError("recurrence.until is before the event starts.")

    def _all_day_bounds(self, start: str, end: str | None) -> tuple[datetime, datetime]:
        """All-day input uses dates; ``end`` is the inclusive last day."""
        first: date = self._parse(start, "start").date()
        last: date = self._parse(end, "end").date() if end else first
        if last < first:
            raise InvalidRequestError("end date is before start date.")
        return start_of_day(first, self.tz), start_of_day(last + ONE_DAY, self.tz)

    def _event_list(self, events: list[Event], s: datetime, e: datetime, limit: int) -> EventList:
        events = sorted(events, key=lambda ev: (ev.start, ev.title))
        return EventList(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            count=min(len(events), limit),
            truncated=len(events) > limit,
            events=events[:limit],
        )

    # ------------------------------------------------------------ queries
    def time_context(self) -> TimeContext:
        now = self.now()
        return TimeContext(
            now=now.replace(microsecond=0),
            timezone=tz_name(self.tz),
            weekday=now.strftime("%A"),
            iso_week=now.isocalendar().week,
        )

    def list_calendars(self) -> list[Calendar]:
        return self.store.list_calendars()

    def list_events(
        self, start: str, end: str, calendar_ids: list[str] | None = None, limit: int = 200
    ) -> EventList:
        s, e = self._range(start, end)
        limit = self._limit(limit)
        return self._event_list(self.store.list_events(s, e, calendar_ids or None), s, e, limit)

    def search_events(
        self,
        query: str,
        start: str | None = None,
        end: str | None = None,
        calendar_ids: list[str] | None = None,
        limit: int = 50,
    ) -> EventList:
        terms = query.casefold().split()
        if not terms:
            raise InvalidRequestError("query must not be empty.")
        limit = self._limit(limit)
        today = start_of_day(self.now().date(), self.tz)
        s = self._parse(start, "start") if start else today - timedelta(days=30)
        if end:
            _, e = self._range(start or s.isoformat(), end)
        else:
            e = s + MAX_RANGE
        hits = []
        for ev in self.store.list_events(s, e, calendar_ids or None):
            haystack = " ".join(filter(None, (ev.title, ev.location, ev.notes))).casefold()
            if all(t in haystack for t in terms):
                hits.append(ev)
        return self._event_list(hits, s, e, limit)

    def get_event(self, event_id: str) -> Event:
        return self.store.get_event(event_id)

    def find_free_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int,
        calendar_ids: list[str] | None = None,
        working_hours_start: str = "09:00",
        working_hours_end: str = "18:00",
        include_weekends: bool = False,
        max_slots: int = 20,
        buffer_minutes: int = 0,
    ) -> FreeSlotResult:
        s, e = self._range(start, end)
        if not 5 <= duration_minutes <= 24 * 60:
            raise InvalidRequestError("duration_minutes must be between 5 and 1440.")
        if not 1 <= max_slots <= 200:
            raise InvalidRequestError("max_slots must be between 1 and 200.")
        if not 0 <= buffer_minutes <= MAX_BUFFER_MINUTES:
            raise InvalidRequestError(f"buffer_minutes must be between 0 and {MAX_BUFFER_MINUTES}.")
        day_start, day_end = self._working_hours(working_hours_start, working_hours_end)
        buffer = timedelta(minutes=buffer_minutes)
        s = max(s, self.now().replace(second=0, microsecond=0))  # no slots in the past
        # Events just outside the window still push the buffer into it.
        events = self.store.list_events(s - buffer, e + buffer, calendar_ids or None) if s < e else []
        slots, truncated = find_free_slots(
            events,
            window_start=s,
            window_end=e,
            duration=timedelta(minutes=duration_minutes),
            tz=self.tz,
            day_start=day_start,
            day_end=day_end,
            include_weekends=include_weekends,
            max_slots=max_slots,
            buffer=buffer,
        )
        return FreeSlotResult(timezone=tz_name(self.tz), slots=slots, truncated=truncated)

    @staticmethod
    def _working_hours(start: str, end: str) -> tuple[time, time]:
        with _as_invalid_request():
            return parse_clock(start), parse_clock(end)

    def find_conflicts(
        self, start: str, end: str, calendar_ids: list[str] | None = None, limit: int = 100
    ) -> ConflictResult:
        s, e = self._range(start, end)
        limit = self._limit(limit)
        pairs = [_conflict(p) for p in find_conflicts(self.store.list_events(s, e, calendar_ids or None))]
        return ConflictResult(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            count=min(len(pairs), limit),
            truncated=len(pairs) > limit,
            conflicts=pairs[:limit],
        )

    def find_duplicates(
        self, start: str, end: str, calendar_ids: list[str] | None = None, limit: int = 100
    ) -> DuplicateResult:
        s, e = self._range(start, end)
        limit = self._limit(limit)
        groups = [
            DuplicateGroup(
                title=copies[0].title,
                start=copies[0].start,
                end=copies[0].end,
                all_day=copies[0].all_day,
                repeats=repeats,
                events=[EventRef.of(c) for c in copies],
            )
            for copies, repeats in find_duplicates(self.store.list_events(s, e, calendar_ids or None))
        ]
        return DuplicateResult(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            count=min(len(groups), limit),
            truncated=len(groups) > limit,
            groups=groups[:limit],
        )

    def agenda(
        self,
        day: str | None = None,
        days: int = 1,
        calendar_ids: list[str] | None = None,
        working_hours_start: str = "09:00",
        working_hours_end: str = "18:00",
        min_free_minutes: int = 30,
    ) -> Agenda:
        if not 1 <= days <= MAX_AGENDA_DAYS:
            raise InvalidRequestError(f"days must be between 1 and {MAX_AGENDA_DAYS}.")
        if not 5 <= min_free_minutes <= 24 * 60:
            raise InvalidRequestError("min_free_minutes must be between 5 and 1440.")
        day_start, day_end = self._working_hours(working_hours_start, working_hours_end)
        now = self.now().replace(microsecond=0)
        first = self._parse(day, "day").date() if day else now.date()
        s, e = start_of_day(first, self.tz), start_of_day(first + timedelta(days=days), self.tz)

        events = [ev for ev in self.store.list_events(s, e, calendar_ids or None) if ev.status != "canceled"]
        events.sort(key=lambda ev: (ev.start, ev.title))
        timed = [ev for ev in events if not ev.all_day]
        attending = [ev for ev in timed if ev.my_status != "declined"]

        busy = timedelta(0)
        for i in range(days):
            d = first + timedelta(days=i)
            ws = datetime.combine(d, day_start, tzinfo=self.tz)
            we = datetime.combine(d if day_end > day_start else d + ONE_DAY, day_end, tzinfo=self.tz)
            busy += busy_time(timed, (ws, we))

        free, _ = find_free_slots(
            timed,
            window_start=max(s, now.replace(second=0)),
            window_end=e,
            duration=timedelta(minutes=min_free_minutes),
            tz=self.tz,
            day_start=day_start,
            day_end=day_end,
            include_weekends=True,
            max_slots=50,
        )
        return Agenda(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            now=now,
            happening_now=[EventRef.of(ev) for ev in attending if ev.start <= now < ev.end],
            up_next=next((EventRef.of(ev) for ev in attending if ev.start > now), None),
            all_day=[EventRef.of(ev) for ev in events if ev.all_day],
            events=[EventRef.of(ev) for ev in timed],
            busy_minutes=int(busy.total_seconds() // 60),
            free_time=free,
            conflicts=[_conflict(p) for p in find_conflicts(timed)],
            invitations=[EventRef.of(ev) for ev in events if ev.my_status == "pending"],
        )

    def calendar_stats(
        self,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
        working_hours_start: str = "09:00",
        working_hours_end: str = "18:00",
    ) -> CalendarStats:
        """Where the time goes in a range: busy time per day, weekday, calendar and title."""
        s, e = self._range(start, end)
        day_start, day_end = self._working_hours(working_hours_start, working_hours_end)
        events = [ev for ev in self.store.list_events(s, e, calendar_ids or None) if ev.status != "canceled"]
        timed = [ev for ev in events if not ev.all_day]
        taking = [ev for ev in timed if blocks_time(ev, ignore_all_day=True) and ev.end > ev.start]

        def minutes(span: timedelta) -> int:
            return int(span.total_seconds() // 60)

        def clipped(ev: Event) -> timedelta:
            return max(min(ev.end, e) - max(ev.start, s), timedelta(0))

        days: list[DayStats] = []
        in_hours = timedelta(0)
        d = s.astimezone(self.tz).date()
        while start_of_day(d, self.tz) < e:
            ds, de = max(start_of_day(d, self.tz), s), min(start_of_day(d + ONE_DAY, self.tz), e)
            days.append(
                DayStats(
                    date=d,
                    weekday=d.strftime("%A"),
                    events=sum(1 for ev in events if overlaps(ev.start, ev.end, ds, de)),
                    busy_minutes=minutes(busy_time(timed, (ds, de))),
                )
            )
            ws = datetime.combine(d, day_start, tzinfo=self.tz)
            we = datetime.combine(d if day_end > day_start else d + ONE_DAY, day_end, tzinfo=self.tz)
            if max(ws, s) < min(we, e):
                in_hours += busy_time(timed, (max(ws, s), min(we, e)))
            d += ONE_DAY

        weekdays: list[WeekdayStats] = []
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            same = [x for x in days if x.weekday == name]
            if same:
                total = sum(x.busy_minutes for x in same)
                weekdays.append(
                    WeekdayStats(
                        weekday=name,
                        days=len(same),
                        busy_minutes=total,
                        average_busy_minutes=total // len(same),
                    )
                )

        per_cal: dict[str, CalendarUsage] = {}
        for ev in events:
            use = per_cal.setdefault(
                ev.calendar_id,
                CalendarUsage(
                    calendar_id=ev.calendar_id, calendar_title=ev.calendar_title, events=0, minutes=0
                ),
            )
            use.events += 1
        for ev in taking:
            per_cal[ev.calendar_id].minutes += minutes(clipped(ev))

        per_title: dict[str, TitleUsage] = {}
        for ev in taking:
            key = " ".join(ev.title.casefold().split())
            row = per_title.setdefault(key, TitleUsage(title=ev.title, events=0, minutes=0))
            row.events += 1
            row.minutes += minutes(clipped(ev))

        busy = sum(x.busy_minutes for x in days)
        busiest = max(days, key=lambda x: x.busy_minutes, default=None)
        return CalendarStats(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            events=len(events),
            timed_events=len(timed),
            busy_minutes=busy,
            busy_minutes_in_working_hours=minutes(in_hours),
            busy_minutes_outside_working_hours=busy - minutes(in_hours),
            average_event_minutes=(
                minutes(sum((ev.end - ev.start for ev in taking), timedelta(0))) // len(taking)
                if taking
                else 0
            ),
            busiest_day=busiest if busiest and busiest.busy_minutes else None,
            days=days,
            by_weekday=weekdays,
            by_calendar=sorted(per_cal.values(), key=lambda u: (-u.minutes, -u.events, u.calendar_title)),
            top_titles=sorted(per_title.values(), key=lambda u: (-u.minutes, -u.events, u.title))[:10],
        )

    # ------------------------------------------------------------ commands
    def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
        calendar_id: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: Recurrence | None = None,
        availability: Availability | None = None,
    ) -> Event:
        draft = self._create_draft(
            title,
            start,
            end,
            duration_minutes,
            all_day,
            calendar_id,
            location,
            notes,
            url,
            alarms_minutes_before,
            recurrence,
            availability,
        )
        created = self.store.create_event(draft)
        self._record([Step("create", None, created)])
        return created

    def _create_draft(
        self,
        title: str,
        start: str,
        end: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
        calendar_id: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: Recurrence | None = None,
        availability: Availability | None = None,
    ) -> EventDraft:
        title = title.strip()
        if not title:
            raise InvalidRequestError("title must not be empty.")
        if end is not None and duration_minutes is not None:
            raise InvalidRequestError("Pass either end or duration_minutes, not both.")

        if all_day:
            if duration_minutes is not None:
                raise InvalidRequestError("duration_minutes is not used for all-day events.")
            s, e = self._all_day_bounds(start, end)
        else:
            s = self._parse(start, "start")
            if end is not None:
                e = self._parse(end, "end")
            elif duration_minutes is not None:
                if duration_minutes < 0:
                    raise InvalidRequestError("duration_minutes must be >= 0.")
                e = s + timedelta(minutes=duration_minutes)
            else:
                e = s + DEFAULT_DURATION
            if e < s:
                raise InvalidRequestError("end must not be before start.")

        self._check_recurrence(recurrence, s)
        with _as_invalid_request():
            draft = EventDraft(
                title=title,
                start=s,
                end=e,
                all_day=all_day,
                calendar_id=calendar_id,
                location=location,
                notes=notes,
                url=self._url(url),
                alarms_minutes_before=self._alarms(alarms_minutes_before) or [],
                recurrence=recurrence,
                availability=availability,
            )
        return draft

    def update_event(
        self,
        event_id: str,
        span: Span = Span.THIS,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        calendar_id: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: Recurrence | None = None,
        remove_recurrence: bool = False,
        availability: Availability | None = None,
    ) -> Event:
        current = self.store.get_event(event_id)
        patch, span = self._update_patch(
            current,
            span,
            title,
            start,
            end,
            all_day,
            calendar_id,
            location,
            notes,
            url,
            alarms_minutes_before,
            recurrence,
            remove_recurrence,
            availability,
        )
        updated = self.store.update_event(event_id, patch, span)
        self._record([Step("update", current, updated, span)])
        return updated

    def _update_patch(
        self,
        current: Event,
        span: Span = Span.THIS,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        calendar_id: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: Recurrence | None = None,
        remove_recurrence: bool = False,
        availability: Availability | None = None,
    ) -> tuple[EventPatch, Span]:
        if not current.is_recurring:
            span = Span.THIS  # spans only mean something for series
        if recurrence is not None and remove_recurrence:
            raise InvalidRequestError("Pass either recurrence or remove_recurrence, not both.")
        if (recurrence is not None or remove_recurrence) and current.is_recurring and span is Span.THIS:
            raise InvalidRequestError("Changing the repeat rule needs span 'future' or 'all', not 'this'.")
        if title is not None and not title.strip():
            raise InvalidRequestError("title must not be empty.")

        new_start, new_end = self._resolve_times(current, start, end, all_day)
        check_from = new_start or current.start
        self._check_recurrence(recurrence, check_from)

        with _as_invalid_request():
            patch = EventPatch(
                title=title.strip() if title is not None else None,
                start=new_start,
                end=new_end,
                all_day=all_day if all_day is not None and all_day != current.all_day else None,
                calendar_id=calendar_id if calendar_id not in (None, current.calendar_id) else None,
                location=location,
                notes=notes,
                url=self._url(url),
                alarms_minutes_before=self._alarms(alarms_minutes_before),
                recurrence=recurrence,
                remove_recurrence=remove_recurrence,
                availability=availability,
            )
        if patch.is_empty():
            raise InvalidRequestError("Nothing to update: pass at least one field to change.")
        return patch, span

    def _resolve_times(
        self, current: Event, start: str | None, end: str | None, all_day: bool | None
    ) -> tuple[datetime | None, datetime | None]:
        """Work out absolute new start/end, preserving duration when only one moves.

        Returns ``(None, None)`` when the timing does not change.
        """
        if start is None and end is None and (all_day is None or all_day == current.all_day):
            return None, None
        target_all_day = current.all_day if all_day is None else all_day

        if target_all_day:
            first = self._parse(start, "start").date() if start else current.start.astimezone(self.tz).date()
            if end is not None:
                last = self._parse(end, "end").date()
            elif current.all_day:
                days = max((current.end - current.start).days, 1)
                last = first + timedelta(days=days - 1)
            else:
                last = first
            if last < first:
                raise InvalidRequestError("end date is before start date.")
            s, e = start_of_day(first, self.tz), start_of_day(last + ONE_DAY, self.tz)
        else:
            s = self._parse(start, "start") if start else current.start
            if end is not None:
                e = self._parse(end, "end")
            elif start is not None:
                e = s + (DEFAULT_DURATION if current.all_day else current.end - current.start)
            else:
                e = current.end if not current.all_day else s + DEFAULT_DURATION
            if e < s:
                raise InvalidRequestError("end must not be before start.")
        if s == current.start and e == current.end:
            return None, None
        return s, e

    def delete_event(self, event_id: str, span: Span = Span.THIS) -> DeleteResult:
        before = self.store.delete_event(event_id, span)
        span = span if before.is_recurring else Span.THIS
        self._record([Step("delete", before, None, span)])
        return DeleteResult(
            deleted=True,
            event_id=event_id,
            span=span,
            title=before.title,
        )

    # ------------------------------------------------------------ iCalendar
    def export_ics(self, start: str, end: str, calendar_ids: list[str] | None = None) -> IcsExport:
        s, e = self._range(start, end)
        events = [ev for ev in self.store.list_events(s, e, calendar_ids or None) if ev.status != "canceled"]
        kept = events[:MAX_EXPORT_EVENTS]
        return IcsExport(
            timezone=tz_name(self.tz),
            range_start=s,
            range_end=e,
            count=len(kept),
            truncated=len(events) > len(kept),
            ics=export_ics(kept, self.now()),
        )

    def _ics_draft(self, item: IcsEvent, calendar_id: str | None) -> EventDraft:
        if item.all_day:
            start = item.start.date().isoformat()
            end = (item.end - ONE_DAY).date().isoformat()
        else:
            start, end = item.start.isoformat(), item.end.isoformat()
        return self._create_draft(
            title=item.title,
            start=start,
            end=end,
            all_day=item.all_day,
            calendar_id=calendar_id,
            location=item.location,
            notes=item.notes,
            url=item.url,
            alarms_minutes_before=item.alarms,
            recurrence=item.recurrence,
            availability=item.availability,
        )

    def _existing_keys(
        self, drafts: list[EventDraft], calendar_id: str | None
    ) -> set[tuple[str, datetime, datetime, bool]]:
        """Keys of events already in the calendar around the drafts' start times."""
        keys: set[tuple[str, datetime, datetime, bool]] = set()
        starts = sorted(d.start for d in drafts)
        while starts:
            lo = starts[0]
            hi = max(s for s in starts if s - lo < MAX_RANGE)
            found = self.store.list_events(lo, hi + ONE_DAY, [calendar_id] if calendar_id else None)
            keys |= {duplicate_key(e.title, e.start, e.end, e.all_day) for e in found}
            starts = [s for s in starts if s > hi]
        return keys

    def import_ics(
        self, ics: str, calendar_id: str | None = None, skip_existing: bool = True
    ) -> ImportResult:
        """Add the events in iCalendar text, as one undoable change."""
        if len(ics) > MAX_ICS_CHARS:
            raise InvalidRequestError("The iCalendar text is too large; split the file.")
        with _as_invalid_request():
            items, left_out = parse_ics(ics, self.tz)
        if len(items) > MAX_IMPORT_EVENTS:
            raise InvalidRequestError(
                f"The file has {len(items)} events; at most {MAX_IMPORT_EVENTS} can be imported "
                "at once. Split it into smaller files."
            )
        skipped = [SkippedItem(title=s.title, reason=s.reason) for s in left_out]

        plan: list[tuple[IcsEvent, EventDraft]] = []
        for item in items:
            try:
                plan.append((item, self._ics_draft(item, calendar_id)))
            except CalendarError as exc:
                skipped.append(SkippedItem(title=item.title[:200] or "(no title)", reason=f"Skipped: {exc}"))

        if skip_existing and plan:
            seen = self._existing_keys([d for _, d in plan], calendar_id)
            fresh: list[tuple[IcsEvent, EventDraft]] = []
            for item, draft in plan:
                key = duplicate_key(draft.title, draft.start, draft.end, draft.all_day)
                if key in seen:
                    skipped.append(
                        SkippedItem(title=draft.title, reason="Skipped: it is already in the calendar.")
                    )
                else:
                    seen.add(key)
                    fresh.append((item, draft))
            plan = fresh

        steps: list[Step] = []
        for item, draft in plan:
            try:
                created = self.store.create_event(draft)
                steps.append(Step("create", None, created))
                series = split_event_id(created.id)[0]
                for removed in item.exdates:
                    with suppress(NotFoundError):  # not one of the series' dates
                        self.store.delete_event(make_event_id(series, removed.timestamp()), Span.THIS)
            except CalendarError as exc:
                failed = self._rollback(steps)
                note = (
                    "Nothing was imported."
                    if not failed
                    else f"Removing the events already added also failed for: {', '.join(failed)}."
                )
                raise type(exc)(f'Importing "{draft.title}" failed: {exc} {note}') from exc

        rec = self._record(steps) if steps else None
        return ImportResult(
            change_id=rec.id if rec else None,
            imported=[EventRef.of(s.event) for s in steps],
            skipped=skipped,
        )

    # ------------------------------------------------------------ invitations
    @property
    def can_reply(self) -> bool:
        """Whether the backend can answer invitations (EventKit can't)."""
        return self.store.supports_replies

    def respond_to_invitation(self, event_id: str, reply: Reply, span: Span = Span.ALL) -> Event:
        """Answer an invitation. Not recorded for undo: the organizer has already been told."""
        if span is Span.FUTURE:
            raise InvalidRequestError("Answer 'this' occurrence or 'all' of the series.")
        return self.store.respond_to_invitation(event_id, reply, span)

    # ------------------------------------------------------------ change log
    def _record(self, steps: list[Step]) -> ChangeRecord:
        return self.journal.record(steps, self.now().replace(microsecond=0))

    def list_changes(self) -> list[ChangeInfo]:
        return [
            ChangeInfo(
                change_id=r.id, at=r.at, summary=r.summary, can_undo=r.blocker is None, why_not=r.blocker
            )
            for r in self.journal.recent()
        ]

    def undo_change(self, change_id: str | None = None) -> UndoResult:
        rec = self.journal.get(change_id)
        if rec.blocker:
            raise InvalidRequestError(f"Can't undo {rec.summary}: {rec.blocker}")
        for step in rec.steps:  # check everything before touching anything
            check_unchanged(self.store, step)
        restored: list[Event] = []
        removed: list[str] = []
        for step in reversed(rec.steps):
            event, gone = revert(self.store, step)
            if event is not None:
                restored.append(event)
            if gone is not None:
                removed.append(gone)
        self.journal.remove(rec.id)
        return UndoResult(change_id=rec.id, summary=rec.summary, restored=restored[::-1], removed_ids=removed)

    def apply_changes(self, changes: list[BatchChange]) -> BatchResult:
        """Apply creates, updates and deletes all-or-nothing, as one undoable change."""
        if not 1 <= len(changes) <= MAX_BATCH:
            raise InvalidRequestError(f"Pass between 1 and {MAX_BATCH} changes.")
        ids = [c.event_id for c in changes if not isinstance(c, CreateChange)]
        if len(ids) != len(set(ids)):
            raise InvalidRequestError("Each event may appear only once in a batch.")

        # 1. Validate everything (and read current state) before writing anything.
        plan: list[tuple[BatchChange, Event | None, EventDraft | EventPatch | None]] = []
        for n, c in enumerate(changes, 1):
            try:
                if isinstance(c, CreateChange):
                    args = c.model_dump(exclude={"action"})
                    plan.append((c, None, self._create_draft(**args)))
                    continue
                current = self.store.get_event(c.event_id)
                if isinstance(c, DeleteChange):
                    blocker = Step("delete", current, None).blocker
                    if blocker:
                        raise InvalidRequestError(f"{blocker} Delete it on its own with delete_event.")
                    plan.append((c, current, None))
                else:
                    args = c.model_dump(exclude={"action", "event_id"})
                    patch, _ = self._update_patch(current, Span.THIS, **args)
                    plan.append((c, current, patch))
            except CalendarError as exc:
                raise type(exc)(f"Change {n} ({c.action}): {exc} Nothing was changed.") from exc

        # 2. Apply in order; on failure, roll back what was done.
        steps: list[Step] = []
        for n, (c, before, payload) in enumerate(plan, 1):
            try:
                if isinstance(c, CreateChange):
                    assert isinstance(payload, EventDraft)
                    steps.append(Step("create", None, self.store.create_event(payload)))
                elif isinstance(c, DeleteChange):
                    steps.append(Step("delete", self.store.delete_event(c.event_id, Span.THIS), None))
                else:
                    assert isinstance(payload, EventPatch) and before is not None
                    steps.append(
                        Step("update", before, self.store.update_event(c.event_id, payload, Span.THIS))
                    )
            except CalendarError as exc:
                failed = self._rollback(steps)
                note = (
                    "The earlier changes were rolled back."
                    if not failed
                    else (f"Rolling back the earlier changes also failed for: {', '.join(failed)}.")
                )
                raise type(exc)(f"Change {n} ({c.action}): {exc} {note}") from exc

        rec = self._record(steps)
        outcomes = [ChangeOutcome(action=s.action, event=s.event) for s in steps]
        return BatchResult(change_id=rec.id, results=outcomes)

    def _rollback(self, steps: list[Step]) -> list[str]:
        failed: list[str] = []
        for step in reversed(steps):
            try:
                revert(self.store, step)
            except CalendarError:
                failed.append(f'"{step.title}"')
        return failed
