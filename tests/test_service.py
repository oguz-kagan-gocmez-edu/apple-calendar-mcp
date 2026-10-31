"""Service-layer behaviour against the in-memory backend (mirrors EventKit semantics)."""

from datetime import datetime, timedelta

import pytest

from apple_calendar_mcp.models import Recurrence, Span
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import InvalidRequestError, NotFoundError, ReadOnlyCalendarError

from .conftest import IST


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def titles(service: CalendarService, start: str = "2026-11-01", end: str = "2026-11-30") -> list[str]:
    return [e.title for e in service.list_events(start, end).events]


# ------------------------------------------------------------------ create
class TestCreate:
    def test_default_one_hour_in_default_calendar(self, service: CalendarService) -> None:
        e = service.create_event("Study", "2026-11-03T14:00")
        assert (e.start, e.end) == (at(3, 14), at(3, 15))
        assert e.calendar_id == "home"

    def test_duration_minutes(self, service: CalendarService) -> None:
        e = service.create_event("Call", "2026-11-03T14:00", duration_minutes=25)
        assert e.end == at(3, 14, 25)

    def test_all_day_inclusive_end_becomes_exclusive(self, service: CalendarService) -> None:
        e = service.create_event("Trip", "2026-11-05", end="2026-11-07", all_day=True)
        assert (e.start, e.end) == (at(5, 0), at(8, 0))

    def test_alarms_normalised(self, service: CalendarService) -> None:
        e = service.create_event("X", "2026-11-03T14:00", alarms_minutes_before=[10, 60, 10])
        assert e.alarms_minutes_before == [60, 10]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": "  "},
            {"end": "2026-11-03T13:00"},
            {"end": "2026-11-03T15:00", "duration_minutes": 30},
            {"alarms_minutes_before": [-5]},
            {"url": "javascript:alert(1)"},
            {"recurrence": Recurrence(frequency="daily", until=datetime(2026, 1, 1).date())},
            {"calendar_id": "nope"},
        ],
    )
    def test_rejects_invalid(self, service: CalendarService, kwargs: dict[str, object]) -> None:
        args: dict[str, object] = {"title": "X", "start": "2026-11-03T14:00"} | kwargs
        with pytest.raises((InvalidRequestError, NotFoundError)):
            service.create_event(**args)  # type: ignore[arg-type]

    def test_read_only_calendar(self, service: CalendarService) -> None:
        with pytest.raises(ReadOnlyCalendarError):
            service.create_event("X", "2026-11-03T14:00", calendar_id="holidays")


# ------------------------------------------------------------------ queries
class TestQueries:
    def test_bare_end_date_is_inclusive(self, service: CalendarService) -> None:
        service.create_event("Late", "2026-11-03T23:00")
        assert titles(service, "2026-11-03", "2026-11-03") == ["Late"]

    def test_range_limits(self, service: CalendarService) -> None:
        with pytest.raises(InvalidRequestError):
            service.list_events("2026-11-05", "2026-11-04T00:00")
        with pytest.raises(InvalidRequestError):
            service.list_events("2026-01-01", "2027-06-01")

    def test_truncation(self, service: CalendarService) -> None:
        for h in range(9, 14):
            service.create_event(f"E{h}", f"2026-11-03T{h:02d}:00")
        res = service.list_events("2026-11-03", "2026-11-03", limit=3)
        assert res.count == 3 and res.truncated and [e.title for e in res.events] == ["E9", "E10", "E11"]

    def test_calendar_filter(self, service: CalendarService) -> None:
        service.create_event("A", "2026-11-03T09:00", calendar_id="work")
        service.create_event("B", "2026-11-03T10:00")
        assert [e.title for e in service.list_events("2026-11-03", "2026-11-03", ["work"]).events] == ["A"]

    def test_search_all_terms_case_insensitive(self, service: CalendarService) -> None:
        service.create_event("OS lecture", "2026-11-03T09:00", location="AB1 245")
        service.create_event("OS lab", "2026-11-04T09:00")
        assert [e.title for e in service.search_events("os ab1").events] == ["OS lecture"]
        assert len(service.search_events("OS").events) == 2

    def test_free_slots_skip_past(self, service: CalendarService) -> None:
        service.create_event("Busy", "2026-11-02T09:00", end="2026-11-02T12:00")
        res = service.find_free_slots("2026-11-02", "2026-11-02", 60)
        assert [(s.start, s.end) for s in res.slots] == [(at(2, 12), at(2, 18))]

    def test_time_context(self, service: CalendarService) -> None:
        ctx = service.time_context()
        assert ctx.weekday == "Monday" and ctx.timezone == "Europe/Istanbul"


# ------------------------------------------------------------------ update
class TestUpdate:
    def test_move_keeps_duration(self, service: CalendarService) -> None:
        e = service.create_event("M", "2026-11-03T14:00", duration_minutes=30)
        moved = service.update_event(e.id, start="2026-11-04T10:00")
        assert (moved.start, moved.end) == (at(4, 10), at(4, 10, 30))

    def test_clear_text_with_empty_string(self, service: CalendarService) -> None:
        e = service.create_event("M", "2026-11-03T14:00", location="Room 1", notes="n")
        u = service.update_event(e.id, location="")
        assert u.location is None and u.notes == "n"

    def test_timed_to_all_day(self, service: CalendarService) -> None:
        e = service.create_event("M", "2026-11-03T14:00")
        u = service.update_event(e.id, all_day=True)
        assert u.all_day and (u.start, u.end) == (at(3, 0), at(4, 0))

    def test_move_calendar(self, service: CalendarService) -> None:
        e = service.create_event("M", "2026-11-03T14:00")
        assert service.update_event(e.id, calendar_id="work").calendar_id == "work"

    def test_nothing_to_update(self, service: CalendarService) -> None:
        e = service.create_event("M", "2026-11-03T14:00")
        with pytest.raises(InvalidRequestError):
            service.update_event(e.id)
        with pytest.raises(InvalidRequestError):
            service.update_event(e.id, start="2026-11-03T14:00")  # same time = no change


# ------------------------------------------------------------------ recurring
@pytest.fixture
def weekly(service: CalendarService) -> list[str]:
    """Mon+Wed lecture, 6 occurrences: 2,4,9,11,16,18 Nov. Returns occurrence ids."""
    service.create_event(
        "Lecture",
        "2026-11-02T10:40",
        duration_minutes=110,
        recurrence=Recurrence(frequency="weekly", days_of_week=["mon", "wed"], count=6),
    )
    return [e.id for e in service.list_events("2026-11-01", "2026-11-30").events]


def days(service: CalendarService, title: str = "Lecture") -> list[int]:
    return [e.start.day for e in service.list_events("2026-11-01", "2026-11-30").events if e.title == title]


class TestRecurring:
    def test_expansion_and_unique_ids(self, service: CalendarService, weekly: list[str]) -> None:
        assert days(service) == [2, 4, 9, 11, 16, 18]
        assert len(set(weekly)) == 6
        assert service.get_event(weekly[3]).start == at(11, 10, 40)

    def test_update_this_occurrence(self, service: CalendarService, weekly: list[str]) -> None:
        u = service.update_event(weekly[2], span=Span.THIS, title="Midterm", start="2026-11-09T09:00")
        assert u.id == weekly[2] and u.start == at(9, 9) and u.end == at(9, 10, 50)
        assert days(service) == [2, 4, 11, 16, 18] and days(service, "Midterm") == [9]

    def test_update_future_splits_series(self, service: CalendarService, weekly: list[str]) -> None:
        service.update_event(weekly[2], span=Span.FUTURE, location="New room")
        evs = service.list_events("2026-11-01", "2026-11-30").events
        assert [e.location for e in evs] == [None, None, "New room", "New room", "New room", "New room"]
        assert days(service) == [2, 4, 9, 11, 16, 18]  # count preserved across the split

    def test_update_all_shifts_series(self, service: CalendarService, weekly: list[str]) -> None:
        service.update_event(weekly[3], span=Span.ALL, start="2026-11-11T11:40")  # +1h
        evs = service.list_events("2026-11-01", "2026-11-30").events
        assert {(e.start.hour, e.start.minute) for e in evs} == {(11, 40)}
        assert len(evs) == 6

    def test_recurrence_change_needs_series_span(self, service: CalendarService, weekly: list[str]) -> None:
        with pytest.raises(InvalidRequestError):
            service.update_event(weekly[0], span=Span.THIS, remove_recurrence=True)

    def test_delete_this(self, service: CalendarService, weekly: list[str]) -> None:
        r = service.delete_event(weekly[1], Span.THIS)
        assert r.deleted and r.span is Span.THIS
        assert days(service) == [2, 9, 11, 16, 18]
        with pytest.raises(NotFoundError):
            service.get_event(weekly[1])

    def test_delete_future(self, service: CalendarService, weekly: list[str]) -> None:
        service.delete_event(weekly[3], Span.FUTURE)
        assert days(service) == [2, 4, 9]

    def test_delete_all(self, service: CalendarService, weekly: list[str]) -> None:
        service.delete_event(weekly[4], Span.ALL)
        assert days(service) == []

    def test_moved_occurrence_found_in_new_window(self, service: CalendarService, weekly: list[str]) -> None:
        service.update_event(weekly[5], span=Span.THIS, start="2026-12-01T10:00")
        dec = service.list_events("2026-12-01", "2026-12-01").events
        assert [e.id for e in dec] == [weekly[5]]

    def test_span_ignored_for_single_events(self, service: CalendarService) -> None:
        e = service.create_event("One", "2026-11-03T14:00")
        assert service.delete_event(e.id, Span.FUTURE).span is Span.THIS


def test_unknown_id(service: CalendarService) -> None:
    with pytest.raises(NotFoundError):
        service.get_event("does-not-exist|123")
    with pytest.raises(NotFoundError):
        service.delete_event("does-not-exist", Span.THIS)


def test_timestamps_are_aware(service: CalendarService) -> None:
    e = service.create_event("X", "2026-11-03T14:00")
    assert e.start.utcoffset() == timedelta(hours=3)
