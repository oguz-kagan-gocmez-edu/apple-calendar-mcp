"""EventKit backend logic, exercised against the fake PyObjC layer."""

import importlib
import sys
from collections.abc import Iterator
from datetime import datetime
from types import ModuleType

import pytest

from apple_calendar_mcp.models import EventDraft, EventPatch, Recurrence, Span
from apple_calendar_mcp.store import NotFoundError, PermissionDeniedError, ReadOnlyCalendarError

from . import fake_eventkit as fk
from .conftest import IST


@pytest.fixture
def ek_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    fk.install(monkeypatch)
    fk.EKEventStore.status, fk.EKEventStore.grant_on_request, fk.EKEventStore.requests = 0, True, 0
    monkeypatch.delitem(sys.modules, "apple_calendar_mcp.eventkit_store", raising=False)
    module = importlib.import_module("apple_calendar_mcp.eventkit_store")
    yield module
    sys.modules.pop("apple_calendar_mcp.eventkit_store", None)


@pytest.fixture
def store(ek_module: ModuleType):  # type: ignore[no-untyped-def]
    return ek_module.EventKitStore(IST, access_timeout=0.2)


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


class TestPermission:
    def test_prompts_once_then_caches(self, store) -> None:  # type: ignore[no-untyped-def]
        store.list_calendars()
        store.list_calendars()
        assert fk.EKEventStore.requests == 1

    def test_denied(self, store) -> None:  # type: ignore[no-untyped-def]
        fk.EKEventStore.grant_on_request = False
        with pytest.raises(PermissionDeniedError, match="User denied"):
            store.list_calendars()

    def test_no_answer_times_out(self, store) -> None:  # type: ignore[no-untyped-def]
        fk.EKEventStore.grant_on_request = None
        with pytest.raises(PermissionDeniedError, match="Timed out"):
            store.list_calendars()

    def test_write_only_is_rejected(self, store) -> None:  # type: ignore[no-untyped-def]
        fk.EKEventStore.status = 4
        with pytest.raises(PermissionDeniedError, match="Full Access"):
            store.list_calendars()


def test_calendars(store) -> None:  # type: ignore[no-untyped-def]
    cals = {c.id: c for c in store.list_calendars()}
    assert cals["CAL-HOME"].is_default and cals["CAL-HOME"].writable
    assert not cals["CAL-HOL"].writable and cals["CAL-HOL"].color is None


def test_create_timed_round_trip(store) -> None:  # type: ignore[no-untyped-def]
    e = store.create_event(
        EventDraft(
            title="OS",
            start=at(3, 10, 40),
            end=at(3, 12, 30),
            location="AB1",
            url="https://ozu.edu.tr",
            alarms_minutes_before=[60, 10],
            availability="tentative",
        )
    )
    assert (e.start, e.end) == (at(3, 10, 40), at(3, 12, 30))
    assert e.calendar_id == "CAL-HOME" and e.alarms_minutes_before == [60, 10]
    assert e.url == "https://ozu.edu.tr" and e.availability == "tentative"
    assert store.get_event(e.id) == e
    assert [x.id for x in store.list_events(at(3, 0), at(4, 0))] == [e.id]


def test_all_day_end_convention(store) -> None:  # type: ignore[no-untyped-def]
    e = store.create_event(EventDraft(title="Trip", start=at(5, 0), end=at(8, 0), all_day=True))
    raw = next(iter(store._store.events.values()))
    # stored inside the last day, as EventKit expects ...
    assert datetime.fromtimestamp(raw.endDate().timeIntervalSince1970(), IST) == at(7, 23, 59).replace(
        second=59
    )
    # ... and reported back with an exclusive midnight end
    assert (e.start, e.end) == (at(5, 0), at(8, 0))


def test_recurrence_rule_mapping(ek_module, store) -> None:  # type: ignore[no-untyped-def]
    rec = Recurrence(frequency="weekly", interval=2, days_of_week=["mon", "sun"], count=10)
    e = store.create_event(EventDraft(title="R", start=at(2, 9), end=at(2, 10), recurrence=rec))
    rule = store._store.events[e.id.split("|")[0]].recurrenceRules()[0]
    assert rule.frequency() == 1 and rule.interval() == 2
    assert [d.dayOfTheWeek() for d in rule.daysOfTheWeek()] == [2, 1]  # EKWeekday: Mon=2, Sun=1
    assert e.recurrence == rec and e.is_recurring


def test_update_and_clear(store) -> None:  # type: ignore[no-untyped-def]
    e = store.create_event(EventDraft(title="A", start=at(3, 9), end=at(3, 10), location="Room", notes="n"))
    u = store.update_event(
        e.id, EventPatch(title="B", location="", start=at(3, 11), end=at(3, 12)), Span.THIS
    )
    assert (u.title, u.location, u.notes) == ("B", None, "n")
    assert (u.start, u.end) == (at(3, 11), at(3, 12))
    assert store._store.saves[-1][1] == 0  # EKSpanThisEvent for a single event


def test_read_only_calendar_rejected(store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReadOnlyCalendarError):
        store.create_event(EventDraft(title="X", start=at(3, 9), end=at(3, 10), calendar_id="CAL-HOL"))


def test_delete(store) -> None:  # type: ignore[no-untyped-def]
    e = store.create_event(EventDraft(title="X", start=at(3, 9), end=at(3, 10)))
    assert store.delete_event(e.id, Span.THIS).title == "X"
    with pytest.raises(NotFoundError):
        store.get_event(e.id)


def test_save_error_surfaces(store) -> None:  # type: ignore[no-untyped-def]
    from apple_calendar_mcp.store import BackendError

    with pytest.raises(BackendError, match="start date"):
        store.create_event(EventDraft(title="X", start=at(3, 10), end=at(3, 9)))
