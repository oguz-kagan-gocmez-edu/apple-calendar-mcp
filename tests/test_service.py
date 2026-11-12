"""Service-layer behaviour against the in-memory backend (mirrors EventKit semantics)."""

from datetime import datetime, timedelta

import pytest

from apple_calendar_mcp.memory_store import MemoryStore
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


# ------------------------------------------------------------------ agenda, conflicts, free time
@pytest.fixture
def monday(service: CalendarService, store: MemoryStore) -> CalendarService:
    """A busy Monday 2 Nov; the returned service's clock is 10:15 that day."""
    service.create_event("Standup", "2026-11-02T09:00", duration_minutes=30)
    service.create_event("Lecture", "2026-11-02T10:00", end="2026-11-02T12:00", location="B-101")
    service.create_event("Lunch", "2026-11-02T11:30", duration_minutes=60)
    declined = service.create_event("Optional sync", "2026-11-02T14:00")
    store.set_my_status(declined.id, "declined")
    invite = service.create_event("Design review", "2026-11-02T16:00")
    store.set_my_status(invite.id, "pending")
    service.create_event("Holiday", "2026-11-02", all_day=True)
    service.create_event("Tomorrow", "2026-11-03T09:00")
    return CalendarService(store, IST, clock=lambda: at(2, 10, 15))


class TestAgenda:
    def test_day_at_a_glance(self, monday: CalendarService) -> None:
        a = monday.agenda()
        assert (a.range_start, a.range_end) == (at(2, 0), at(3, 0))
        assert [e.title for e in a.happening_now] == ["Lecture"]
        assert a.up_next is not None and a.up_next.title == "Lunch"
        assert [e.title for e in a.all_day] == ["Holiday"]
        assert [e.title for e in a.events] == [
            "Standup",
            "Lecture",
            "Lunch",
            "Optional sync",
            "Design review",
        ]
        # 30 (standup) + 150 (lecture and lunch, overlap counted once) + 60 (pending invite)
        assert a.busy_minutes == 240
        assert [(s.start, s.end) for s in a.free_time] == [
            (at(2, 12, 30), at(2, 16)),  # the declined 14:00 sync doesn't block
            (at(2, 17), at(2, 18)),
        ]
        assert len(a.conflicts) == 1
        c = a.conflicts[0]
        assert (c.start, c.end, c.overlap_minutes) == (at(2, 11, 30), at(2, 12), 30)
        assert [e.title for e in c.events] == ["Lecture", "Lunch"]
        assert [e.title for e in a.invitations] == ["Design review"]

    def test_declined_event_is_never_next(self, monday: CalendarService) -> None:
        later = CalendarService(monday.store, IST, clock=lambda: at(2, 13))
        a = later.agenda()
        assert a.happening_now == [] and a.up_next is not None and a.up_next.title == "Design review"

    def test_other_day_and_multiple_days(self, monday: CalendarService) -> None:
        a = monday.agenda(day="2026-11-03")
        assert [e.title for e in a.events] == ["Tomorrow"] and a.up_next is not None
        assert a.free_time[0].start == at(3, 10)
        two = monday.agenda(days=2)
        assert two.range_end == at(4, 0) and len(two.events) == 6
        assert two.busy_minutes == 240 + 60

    @pytest.mark.parametrize(
        "kwargs", [{"days": 0}, {"days": 15}, {"min_free_minutes": 1}, {"working_hours_start": "25:00"}]
    )
    def test_rejects_invalid(self, service: CalendarService, kwargs: dict[str, object]) -> None:
        with pytest.raises(InvalidRequestError):
            service.agenda(**kwargs)  # type: ignore[arg-type]


def test_find_conflicts(monday: CalendarService) -> None:
    r = monday.find_conflicts("2026-11-02", "2026-11-03")
    assert r.count == 1 and not r.truncated
    assert [e.title for e in r.conflicts[0].events] == ["Lecture", "Lunch"]
    monday.create_event("Office hours", "2026-11-02T09:15")  # overlaps standup, lecture
    r = monday.find_conflicts("2026-11-02", "2026-11-02", limit=1)
    assert r.count == 1 and r.truncated


class TestFreeSlotsBufferAndDeclined:
    def test_buffer_around_events(self, service: CalendarService) -> None:
        service.create_event("Meeting", "2026-11-03T12:00")
        r = service.find_free_slots("2026-11-03", "2026-11-03", 60, buffer_minutes=15)
        assert [(s.start, s.end) for s in r.slots] == [(at(3, 9), at(3, 11, 45)), (at(3, 13, 15), at(3, 18))]

    def test_buffer_from_event_before_the_range(self, service: CalendarService) -> None:
        service.create_event("Early", "2026-11-03T09:00")
        r = service.find_free_slots("2026-11-03T10:00", "2026-11-03", 60, buffer_minutes=15)
        assert r.slots[0].start == at(3, 10, 15)

    def test_declined_invitation_is_free(self, service: CalendarService, store: MemoryStore) -> None:
        e = service.create_event("Big meeting", "2026-11-03T09:00", end="2026-11-03T18:00")
        assert service.find_free_slots("2026-11-03", "2026-11-03", 60).slots == []
        store.set_my_status(e.id, "declined")
        assert len(service.find_free_slots("2026-11-03", "2026-11-03", 60).slots) == 1

    def test_buffer_out_of_range(self, service: CalendarService) -> None:
        with pytest.raises(InvalidRequestError):
            service.find_free_slots("2026-11-03", "2026-11-03", 60, buffer_minutes=241)


class TestDuplicates:
    def test_groups_and_limit(self, service: CalendarService) -> None:
        service.create_event("Dentist", "2026-11-04T15:00")
        service.create_event("Dentist", "2026-11-04T15:00", calendar_id="work")
        service.create_event("Trip", "2026-11-06", all_day=True)
        service.create_event("trip", "2026-11-06", all_day=True)
        service.create_event("Trip", "2026-11-07", all_day=True)
        r = service.find_duplicates("2026-11-01", "2026-11-30")
        assert (r.count, r.truncated) == (2, False)
        assert [(g.title.lower(), g.all_day, g.repeats, len(g.events)) for g in r.groups] == [
            ("dentist", False, 1, 2),
            ("trip", True, 1, 2),
        ]
        assert [e.calendar_title for e in r.groups[0].events] == ["Home", "Work"]
        only_work = service.find_duplicates("2026-11-01", "2026-11-30", calendar_ids=["work"])
        assert only_work.count == 0
        r = service.find_duplicates("2026-11-01", "2026-11-30", limit=1)
        assert (r.count, r.truncated, len(r.groups)) == (1, True, 1)

    def test_copied_series(self, service: CalendarService) -> None:
        rule = Recurrence(frequency="weekly", count=3)
        a = service.create_event("Lecture", "2026-11-03T10:00", recurrence=rule)
        b = service.create_event("Lecture", "2026-11-03T10:00", recurrence=rule)
        (group,) = service.find_duplicates("2026-11-01", "2026-11-30").groups
        assert group.repeats == 3 and sorted(e.id for e in group.events) == sorted([a.id, b.id])
        # Deleting one copy clears it.
        service.delete_event(b.id, Span.ALL)
        assert service.find_duplicates("2026-11-01", "2026-11-30").count == 0
