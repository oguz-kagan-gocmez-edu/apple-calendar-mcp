"""Per-calendar access control, through the service and the MCP tools."""

from datetime import datetime

import pytest
from mcp.client.client import Client

from apple_calendar_mcp.access import AccessControlledStore, AccessPolicy
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.models import Calendar, Span
from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import NotFoundError, ReadOnlyCalendarError

from .conftest import IST, NOW


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def cal(id: str, title: str) -> Calendar:
    return Calendar(id=id, title=title, writable=True)


# ------------------------------------------------------------------ policy
@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (AccessPolicy.parse(), {"home": "write", "work": "write", "family": "write"}),
        (AccessPolicy.parse(read_only="Work"), {"home": "write", "work": "read", "family": "write"}),
        (AccessPolicy.parse(hidden=" family , "), {"home": "write", "work": "write", "family": "hidden"}),
        (AccessPolicy.parse(only="home,WORK"), {"home": "write", "work": "write", "family": "hidden"}),
        (
            AccessPolicy.parse(read_only="work", hidden="work"),
            {"home": "write", "work": "hidden", "family": "write"},
        ),
        (
            AccessPolicy.parse(read_only="work", only="work"),
            {"home": "hidden", "work": "read", "family": "hidden"},
        ),
    ],
)
def test_levels_by_id_or_title(policy: AccessPolicy, expected: dict[str, str]) -> None:
    cals = [cal("home", "Home"), cal("work", "Work"), cal("family", "Family")]
    assert {c.id: policy.level(c) for c in cals} == expected


def test_open_policy_and_typos() -> None:
    assert AccessPolicy.parse("", " , ", None).is_open
    p = AccessPolicy.parse(read_only="Wrok", hidden="home")
    assert not p.is_open
    assert p.unmatched([cal("home", "Home"), cal("work", "Work")]) == ["wrok"]


# ------------------------------------------------------------------ store
@pytest.fixture
def seeded(store: MemoryStore, service: CalendarService) -> dict[str, str]:
    """One event in each calendar; returns their ids by calendar."""
    return {
        "home": service.create_event("Gym", "2026-11-03T08:00", calendar_id="home").id,
        "work": service.create_event("Standup", "2026-11-03T09:00", calendar_id="work").id,
    }


def guarded(store: MemoryStore, **policy: str) -> CalendarService:
    return CalendarService(AccessControlledStore(store, AccessPolicy.parse(**policy)), IST, clock=lambda: NOW)


class TestHidden:
    def test_calendar_and_events_disappear(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, hidden="Work")
        assert [c.id for c in svc.list_calendars()] == ["home", "holidays"]
        assert [e.title for e in svc.list_events("2026-11-03", "2026-11-03").events] == ["Gym"]
        assert svc.list_events("2026-11-03", "2026-11-03", calendar_ids=["work"]).events == []
        assert svc.search_events("standup").events == []
        assert svc.agenda(day="2026-11-03").events[0].title == "Gym"

    def test_ids_behave_like_missing_ones(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, hidden="work")
        for call in (
            lambda: svc.get_event(seeded["work"]),
            lambda: svc.update_event(seeded["work"], title="X"),
            lambda: svc.delete_event(seeded["work"], Span.THIS),
            lambda: svc.create_event("X", "2026-11-03T10:00", calendar_id="work"),
            lambda: svc.update_event(seeded["home"], calendar_id="work"),
        ):
            with pytest.raises(NotFoundError):
                call()
        assert store.get_event(seeded["work"]).title == "Standup"  # untouched

    def test_hidden_time_is_free(self, store: MemoryStore, service: CalendarService) -> None:
        service.create_event("All day", "2026-11-03T09:00", end="2026-11-03T18:00", calendar_id="work")
        assert guarded(store, hidden="work").find_free_slots("2026-11-03", "2026-11-03", 60).slots

    def test_only_list(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, only="home")
        assert [c.id for c in svc.list_calendars()] == ["home"]
        assert [e.title for e in svc.list_events("2026-11-01", "2026-11-30").events] == ["Gym"]

    def test_nothing_visible(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, only="nope")
        assert svc.list_calendars() == []
        assert svc.list_events("2026-11-01", "2026-11-30").events == []


class TestReadOnly:
    def test_listed_as_not_writable(self, store: MemoryStore) -> None:
        cals = {c.id: c.writable for c in guarded(store, read_only="work").list_calendars()}
        assert cals == {"home": True, "work": False, "holidays": False}

    def test_reads_work_writes_refused(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, read_only="work")
        assert svc.get_event(seeded["work"]).title == "Standup"
        for call in (
            lambda: svc.update_event(seeded["work"], title="X"),
            lambda: svc.delete_event(seeded["work"], Span.THIS),
            lambda: svc.create_event("X", "2026-11-03T10:00", calendar_id="work"),
            lambda: svc.update_event(seeded["home"], calendar_id="work"),  # moving in
            lambda: svc.update_event(seeded["work"], calendar_id="home"),  # moving out
        ):
            with pytest.raises(ReadOnlyCalendarError, match="read-only"):
                call()
        assert store.get_event(seeded["work"]).title == "Standup"

    def test_other_calendars_still_writable(self, store: MemoryStore, seeded: dict[str, str]) -> None:
        svc = guarded(store, read_only="work")
        assert svc.update_event(seeded["home"], title="Run").title == "Run"
        assert svc.create_event("New", "2026-11-03T10:00").calendar_id == "home"
        svc.delete_event(seeded["home"], Span.THIS)

    def test_read_only_default_calendar_needs_calendar_id(self, store: MemoryStore) -> None:
        svc = guarded(store, read_only="home")
        with pytest.raises(ReadOnlyCalendarError, match="pass calendar_id"):
            svc.create_event("X", "2026-11-03T10:00")
        assert svc.create_event("X", "2026-11-03T10:00", calendar_id="work").calendar_id == "work"


@pytest.mark.anyio
async def test_tools_respect_the_policy(store: MemoryStore, seeded: dict[str, str]) -> None:
    async with Client(build_server(guarded(store, hidden="work", read_only="home"))) as client:
        cals = (await client.call_tool("list_calendars", {})).structured_content
        assert cals is not None and [c["id"] for c in cals["result"]] == ["home", "holidays"]
        res = await client.call_tool("delete_event", {"event_id": seeded["home"]})
        assert res.is_error and "read-only" in res.content[0].text  # type: ignore[union-attr]
        res = await client.call_tool("get_event", {"event_id": seeded["work"]})
        assert res.is_error and "No event" in res.content[0].text  # type: ignore[union-attr]
