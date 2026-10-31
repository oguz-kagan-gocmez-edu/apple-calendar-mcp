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
from contextlib import contextmanager
from datetime import date, datetime, timedelta, tzinfo
from urllib.parse import urlparse

from pydantic import ValidationError

from .models import (
    Availability,
    Calendar,
    DeleteResult,
    Event,
    EventDraft,
    EventList,
    EventPatch,
    FreeSlotResult,
    Recurrence,
    Span,
    TimeContext,
)
from .scheduling import find_free_slots
from .store import CalendarStore, InvalidRequestError
from .timeutil import ONE_DAY, is_date_only, parse_clock, parse_datetime, start_of_day, tz_name

MAX_RANGE = timedelta(days=366)
MAX_ALARM_MINUTES = 4 * 7 * 24 * 60  # four weeks
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}
DEFAULT_DURATION = timedelta(hours=1)


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
    ) -> FreeSlotResult:
        s, e = self._range(start, end)
        if not 5 <= duration_minutes <= 24 * 60:
            raise InvalidRequestError("duration_minutes must be between 5 and 1440.")
        if not 1 <= max_slots <= 200:
            raise InvalidRequestError("max_slots must be between 1 and 200.")
        with _as_invalid_request():
            day_start, day_end = parse_clock(working_hours_start), parse_clock(working_hours_end)
        s = max(s, self.now().replace(second=0, microsecond=0))  # no slots in the past
        slots, truncated = find_free_slots(
            self.store.list_events(s, e, calendar_ids or None) if s < e else [],
            window_start=s,
            window_end=e,
            duration=timedelta(minutes=duration_minutes),
            tz=self.tz,
            day_start=day_start,
            day_end=day_end,
            include_weekends=include_weekends,
            max_slots=max_slots,
        )
        return FreeSlotResult(timezone=tz_name(self.tz), slots=slots, truncated=truncated)

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
        return self.store.create_event(draft)

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
        return self.store.update_event(event_id, patch, span)

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
        return DeleteResult(
            deleted=True,
            event_id=event_id,
            span=span if before.is_recurring else Span.THIS,
            title=before.title,
        )
