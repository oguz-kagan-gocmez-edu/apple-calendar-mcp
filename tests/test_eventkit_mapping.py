"""EventKit backend logic, exercised against the fake PyObjC layer."""

import importlib
import sys
from collections.abc import Iterator
from datetime import datetime
from types import ModuleType

import pytest

from apple_calendar_mcp import macos_app
from apple_calendar_mcp.models import EventDraft, EventPatch, Recurrence, ReminderDraft, ReminderPatch, Span
from apple_calendar_mcp.store import NotFoundError, PermissionDeniedError, ReadOnlyCalendarError

from . import fake_eventkit as fk
from .conftest import IST


@pytest.fixture
def ek_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    fk.install(monkeypatch)
    # Whatever is installed on this machine, and wherever this checkout is.
    monkeypatch.setattr(macos_app, "unusable_reason", lambda: None)
    fk.EKEventStore.status, fk.EKEventStore.grant_on_request, fk.EKEventStore.requests = 0, True, 0
    fk.EKEventStore.prompt_shown = True
    fk.EKEventStore.reminder_status, fk.EKEventStore.reminder_grant = 0, True
    fk.EKEventStore.reminder_requests = 0
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

    def test_refused_without_prompt_points_to_helper_app(self, store) -> None:  # type: ignore[no-untyped-def]
        fk.EKEventStore.grant_on_request, fk.EKEventStore.prompt_shown = False, False
        with pytest.raises(PermissionDeniedError, match=r"without showing a prompt.*install-app"):
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


def test_my_status_comes_from_the_current_user_attendee(store) -> None:  # type: ignore[no-untyped-def]
    e = store.create_event(EventDraft(title="Invite", start=at(3, 9), end=at(3, 10)))
    raw = store._store.events[e.id.split("|")[0]]
    assert store.get_event(e.id).my_status is None  # own event, no attendees
    raw._attendees = [
        fk.EKParticipant("Ayşe", "ayse@x.com", 2),
        fk.EKParticipant("Me", "me@x.com", 1, me=True),
    ]
    got = store.get_event(e.id)
    assert got.my_status == "pending"
    assert [(a.email, a.status) for a in got.attendees] == [
        ("ayse@x.com", "accepted"),
        ("me@x.com", "pending"),
    ]
    raw._attendees = [fk.EKParticipant("Ayşe", "ayse@x.com", 3)]
    assert store.get_event(e.id).my_status is None  # someone else declined, not me


# ------------------------------------------------------------------ reminders
class TestReminders:
    def test_asked_separately_and_only_when_used(self, store) -> None:  # type: ignore[no-untyped-def]
        store.list_calendars()
        assert fk.EKEventStore.reminder_requests == 0
        store.list_reminder_lists()
        store.list_reminder_lists()
        assert fk.EKEventStore.reminder_requests == 1

    def test_denied(self, store) -> None:  # type: ignore[no-untyped-def]
        fk.EKEventStore.reminder_grant = False
        with pytest.raises(PermissionDeniedError, match="Reminders access was not granted"):
            store.list_reminder_lists()
        fk.EKEventStore.reminder_status = 2
        with pytest.raises(PermissionDeniedError, match="Privacy & Security > Reminders"):
            store.list_reminders(None, completed=False)

    def test_lists(self, store) -> None:  # type: ignore[no-untyped-def]
        lists = store.list_reminder_lists()
        assert [(x.id, x.writable, x.is_default) for x in lists] == [
            ("LIST-TODO", True, True),
            ("LIST-SHARED", False, False),
        ]

    def test_round_trip(self, store) -> None:  # type: ignore[no-untyped-def]
        timed = store.create_reminder(
            ReminderDraft(title="Call bank", notes="ask about fees", due=at(5, 17), priority="high")
        )
        assert (timed.list_id, timed.due, timed.due_all_day, timed.priority) == (
            "LIST-TODO",
            at(5, 17),
            False,
            "high",
        )
        native = store._store.reminders[timed.id]
        assert native.priority() == 1 and native.dueDateComponents().timeZone().name() == "Europe/Istanbul"
        assert [a.absoluteDate().timeIntervalSince1970() for a in native.alarms] == [at(5, 17).timestamp()]

        dated = store.create_reminder(ReminderDraft(title="Pay rent", due=at(6, 0), due_all_day=True))
        assert (dated.due, dated.due_all_day) == (at(6, 0), True)
        assert store._store.reminders[dated.id].dueDateComponents().hour() == fk.UNDEFINED
        assert store._store.reminders[dated.id].alarms == []

        bare = store.create_reminder(ReminderDraft(title="Someday", priority="low"))
        assert (bare.due, bare.priority) == (None, "low")
        assert {r.title for r in store.list_reminders(None, completed=False)} == {
            "Call bank",
            "Pay rent",
            "Someday",
        }

    def test_update_complete_move_delete(self, store) -> None:  # type: ignore[no-untyped-def]
        r = store.create_reminder(ReminderDraft(title="Milk", due=at(5, 17)))
        r = store.update_reminder(r.id, ReminderPatch(clear_due=True, notes="2 l", priority="medium"))
        assert (r.due, r.notes, r.priority) == (None, "2 l", "medium")
        assert store._store.reminders[r.id].alarms == []
        done = store.update_reminder(r.id, ReminderPatch(completed=True))
        assert done.completed and done.completed_at is not None
        assert store.list_reminders(None, completed=False) == []
        assert [x.id for x in store.list_reminders(["LIST-TODO"], completed=True)] == [r.id]
        with pytest.raises(ReadOnlyCalendarError):
            store.update_reminder(r.id, ReminderPatch(list_id="LIST-SHARED"))
        with pytest.raises(NotFoundError):
            store.list_reminders(["CAL-HOME"], completed=False)  # a calendar, not a reminder list
        assert store.delete_reminder(r.id).title == "Milk"
        with pytest.raises(NotFoundError):
            store.get_reminder(r.id)

    def test_event_ids_are_not_reminders(self, store) -> None:  # type: ignore[no-untyped-def]
        ev = store.create_event(EventDraft(title="X", start=at(3, 9), end=at(3, 10)))
        with pytest.raises(NotFoundError, match="No reminder"):
            store.get_reminder(ev.id.split("|")[0])


def test_refusal_from_a_protected_folder_says_to_move(
    store,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(macos_app, "unusable_reason", lambda: "The helper app can't be used: Desktop.")
    fk.EKEventStore.grant_on_request, fk.EKEventStore.prompt_shown = False, False
    with pytest.raises(PermissionDeniedError) as err:
        store.list_calendars()
    assert "can't be used: Desktop" in str(err.value) and "install-app" not in str(err.value)
