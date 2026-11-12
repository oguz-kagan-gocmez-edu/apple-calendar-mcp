"""Change log, undo_change and apply_changes.

The ``TestUndo`` and ``TestBatch`` scenarios only use the service, so
test_caldav_store.py re-runs them against a real CalDAV server.
"""

from datetime import datetime
from typing import Any

import pytest
from pydantic import TypeAdapter

from apple_calendar_mcp.changes import INVITE_DELETE, ChangeJournal, Step
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.models import Attendee, BatchChange, Event, EventDraft, EventPatch, Recurrence, Span
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import BackendError, InvalidRequestError, NotFoundError

from .conftest import IST, NOW


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def titles(service: CalendarService) -> list[str]:
    return [e.title for e in service.list_events("2026-11-01", "2026-11-30").events]


def batch(*items: dict[str, Any]) -> list[BatchChange]:
    return TypeAdapter(list[BatchChange]).validate_python(list(items))


class TestUndo:
    def test_undo_create(self, service: CalendarService) -> None:
        service.create_event("Dentist", "2026-11-03T09:00")
        r = service.undo_change()
        assert r.summary == 'Created "Dentist"' and len(r.removed_ids) == 1 and r.restored == []
        assert titles(service) == []
        assert service.list_changes() == []

    def test_undo_create_series(self, service: CalendarService) -> None:
        service.create_event(
            "Lecture", "2026-11-02T10:00", recurrence=Recurrence(frequency="weekly", count=4)
        )
        service.undo_change()
        assert titles(service) == []

    def test_undo_update_restores_fields(self, service: CalendarService) -> None:
        e = service.create_event(
            "Dentist", "2026-11-03T09:00", calendar_id="home", alarms_minutes_before=[10]
        )
        service.update_event(
            e.id,
            title="Dentist (moved)",
            start="2026-11-04T15:00",
            location="Kadıköy",
            calendar_id="work",
            alarms_minutes_before=[60],
        )
        r = service.undo_change()
        back = r.restored[0]
        assert (back.title, back.start, back.end, back.location) == ("Dentist", at(3, 9), at(3, 10), None)
        assert (back.calendar_id, back.alarms_minutes_before) == ("home", [10])
        assert [c.summary for c in service.list_changes()] == ['Created "Dentist"']

    def test_undo_clears_text_that_was_empty(self, service: CalendarService) -> None:
        e = service.create_event("X", "2026-11-03T09:00", notes="keep")
        service.update_event(e.id, notes="", location="Room 1")
        back = service.undo_change().restored[0]
        assert (back.notes, back.location) == ("keep", None)

    def test_undo_one_occurrence(self, service: CalendarService) -> None:
        first = service.create_event(
            "Daily",
            "2026-11-02T09:00",
            duration_minutes=30,
            recurrence=Recurrence(frequency="daily", count=3),
        )
        ids = [e.id for e in service.list_events("2026-11-01", "2026-11-30").events]
        service.update_event(ids[1], span=Span.THIS, title="Daily (moved)", start="2026-11-03T15:00")
        service.undo_change()
        evs = service.list_events("2026-11-01", "2026-11-30").events
        assert [(e.title, e.start) for e in evs] == [
            ("Daily", at(2, 9)),
            ("Daily", at(3, 9)),
            ("Daily", at(4, 9)),
        ]
        assert service.get_event(first.id).is_recurring

    def test_series_edits_are_logged_but_not_undoable(self, service: CalendarService) -> None:
        e = service.create_event(
            "Daily", "2026-11-02T09:00", recurrence=Recurrence(frequency="daily", count=3)
        )
        service.update_event(e.id, span=Span.ALL, title="Standup")
        latest = service.list_changes()[0]
        assert not latest.can_undo and latest.why_not and "several occurrences" in latest.why_not
        with pytest.raises(InvalidRequestError, match="Can't undo"):
            service.undo_change()
        assert titles(service) == ["Standup"] * 3

    def test_undo_delete_recreates(self, service: CalendarService) -> None:
        e = service.create_event("Dentist", "2026-11-03T09:00", location="Kadıköy", calendar_id="work")
        service.delete_event(e.id)
        back = service.undo_change().restored[0]
        assert back.id != e.id
        assert (back.title, back.start, back.location, back.calendar_id) == (
            "Dentist",
            at(3, 9),
            "Kadıköy",
            "work",
        )

    def test_deleted_occurrence_is_not_undoable(self, service: CalendarService) -> None:
        e = service.create_event(
            "Daily", "2026-11-02T09:00", recurrence=Recurrence(frequency="daily", count=3)
        )
        service.delete_event(e.id, Span.THIS)
        assert not service.list_changes()[0].can_undo

    def test_refuses_over_a_later_edit(self, service: CalendarService) -> None:
        e = service.create_event("Dentist", "2026-11-03T09:00")
        service.update_event(e.id, title="Dentist 2")
        change = service.list_changes()[0].change_id
        service.store.update_event(e.id, EventPatch(location="Somewhere"), Span.THIS)  # edited elsewhere
        with pytest.raises(InvalidRequestError, match="changed again"):
            service.undo_change(change)
        assert service.get_event(e.id).title == "Dentist 2"

    def test_refuses_when_the_event_is_gone(self, service: CalendarService) -> None:
        e = service.create_event("Dentist", "2026-11-03T09:00")
        service.store.delete_event(e.id, Span.THIS)
        with pytest.raises(InvalidRequestError, match="no longer exists"):
            service.undo_change()

    def test_undo_an_older_change_by_id(self, service: CalendarService) -> None:
        a = service.create_event("A", "2026-11-03T09:00")
        service.create_event("B", "2026-11-03T11:00")
        older = service.list_changes()[1]
        assert older.summary == 'Created "A"'
        service.undo_change(older.change_id)
        assert titles(service) == ["B"]
        with pytest.raises(NotFoundError):
            service.get_event(a.id)
        with pytest.raises(NotFoundError, match="No change"):
            service.undo_change(older.change_id)  # already undone

    def test_nothing_to_undo(self, service: CalendarService) -> None:
        with pytest.raises(NotFoundError, match="no change"):
            service.undo_change()


class TestBatch:
    def test_mixed_batch_and_undo(self, service: CalendarService) -> None:
        keep = service.create_event("Standup", "2026-11-03T09:00")
        drop = service.create_event("Old sync", "2026-11-03T11:00")
        r = service.apply_changes(
            batch(
                {"action": "create", "title": "Review", "start": "2026-11-03T14:00", "duration_minutes": 30},
                {"action": "update", "event_id": keep.id, "start": "2026-11-03T09:30"},
                {"action": "delete", "event_id": drop.id},
            )
        )
        assert [o.action for o in r.results] == ["create", "update", "delete"]
        assert titles(service) == ["Standup", "Review"]
        assert service.list_changes()[0].summary == "3 changes in one batch"

        service.undo_change(r.change_id)
        evs = service.list_events("2026-11-01", "2026-11-30").events
        assert [(e.title, e.start) for e in evs] == [("Standup", at(3, 9)), ("Old sync", at(3, 11))]

    def test_invalid_item_changes_nothing(self, service: CalendarService) -> None:
        with pytest.raises(InvalidRequestError, match=r"Change 2 \(create\).*Nothing was changed"):
            service.apply_changes(
                batch(
                    {"action": "create", "title": "A", "start": "2026-11-03T09:00"},
                    {"action": "create", "title": "B", "start": "next tuesday"},
                )
            )
        assert titles(service) == [] and service.list_changes() == []

    def test_rejects_what_it_could_not_undo(self, service: CalendarService) -> None:
        e = service.create_event(
            "Daily", "2026-11-02T09:00", recurrence=Recurrence(frequency="daily", count=3)
        )
        with pytest.raises(InvalidRequestError, match="delete_event"):
            service.apply_changes(batch({"action": "delete", "event_id": e.id}))
        with pytest.raises(NotFoundError, match=r"Change 1 \(update\)"):
            service.apply_changes(batch({"action": "update", "event_id": "missing|1", "title": "X"}))

    def test_limits(self, service: CalendarService) -> None:
        e = service.create_event("X", "2026-11-03T09:00")
        with pytest.raises(InvalidRequestError, match="only once"):
            service.apply_changes(
                batch(
                    {"action": "update", "event_id": e.id, "title": "Y"},
                    {"action": "delete", "event_id": e.id},
                )
            )
        with pytest.raises(InvalidRequestError, match="between 1 and 25"):
            service.apply_changes([])


# ------------------------------------------------------------------ memory-only details
class FailingStore(MemoryStore):
    """Fails the n-th write from now on."""

    def __init__(self, fail_on: int) -> None:
        super().__init__(IST)
        self.writes, self.fail_on = 0, fail_on

    def _tick(self) -> None:
        self.writes += 1
        if self.writes == self.fail_on:
            raise BackendError("The server said no.")

    def create_event(self, draft: EventDraft) -> Event:
        self._tick()
        return super().create_event(draft)

    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        self._tick()
        return super().update_event(event_id, patch, span)


def test_failed_batch_rolls_back() -> None:
    store = FailingStore(fail_on=0)
    service = CalendarService(store, IST, clock=lambda: NOW)
    e = service.create_event("Standup", "2026-11-03T09:00")
    store.writes, store.fail_on = 0, 3  # the third write of the batch fails
    with pytest.raises(BackendError, match=r"Change 3 \(create\).*rolled back"):
        service.apply_changes(
            batch(
                {"action": "create", "title": "A", "start": "2026-11-03T10:00"},
                {"action": "update", "event_id": e.id, "title": "Standup (moved)"},
                {"action": "create", "title": "B", "start": "2026-11-03T12:00"},
            )
        )
    assert titles(service) == ["Standup"]
    assert [c.summary for c in service.list_changes()] == ['Created "Standup"']


def test_deleted_invitation_is_not_undoable() -> None:
    invite = Event(
        id="x|0",
        calendar_id="c",
        calendar_title="C",
        title="Sync",
        start=at(3, 9),
        end=at(3, 10),
        attendees=[Attendee(email="boss@example.com")],
    )
    assert Step("delete", invite, None).blocker == INVITE_DELETE


def test_journal_keeps_the_latest() -> None:
    journal = ChangeJournal(limit=3)
    e = Event(id="x|0", calendar_id="c", calendar_title="C", title="t", start=at(3, 9), end=at(3, 10))
    ids = [journal.record([Step("create", None, e)], NOW).id for _ in range(5)]
    assert [r.id for r in journal.recent()] == ids[:1:-1]
