"""Reminders through the service and the MCP tools (memory backend)."""

from datetime import datetime
from typing import Any

import pytest
from mcp.client.client import Client

from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.reminders import MemoryReminderStore
from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import InvalidRequestError, NotFoundError, ReadOnlyCalendarError

from .conftest import IST, NOW

REMINDER_READS = {"list_reminder_lists", "list_reminders"}
REMINDER_WRITES = {"create_reminder", "update_reminder", "complete_reminder", "delete_reminder"}


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


@pytest.fixture
def todo() -> MemoryReminderStore:
    return MemoryReminderStore(IST)


@pytest.fixture
def rs(store: MemoryStore, todo: MemoryReminderStore) -> CalendarService:
    """Clock: Monday 2 Nov 2026, 08:00."""
    return CalendarService(store, IST, clock=lambda: NOW, reminders=todo)


class TestService:
    def test_create_with_and_without_a_time(self, rs: CalendarService) -> None:
        timed = rs.create_reminder("Call bank", due="2026-11-05T17:00", priority="high", notes="fees")
        assert (timed.due, timed.due_all_day, timed.list_id, timed.overdue) == (
            at(5, 17),
            False,
            "inbox",
            False,
        )
        dated = rs.create_reminder("Pay rent", due="2026-11-06")
        assert (dated.due, dated.due_all_day) == (at(6, 0), True)
        undated = rs.create_reminder("  Someday  ", list_id="groceries")
        assert (undated.title, undated.due, undated.list_title) == ("Someday", None, "Groceries")

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"title": "  "}, "title"),
            ({"title": "x", "due": "friday"}, "ISO 8601"),
            ({"title": "x", "list_id": "nope"}, "No reminder list"),
        ],
    )
    def test_create_rejects(self, rs: CalendarService, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises((InvalidRequestError, NotFoundError), match=match):
            rs.create_reminder(**kwargs)

    def test_read_only_list(self, rs: CalendarService) -> None:
        with pytest.raises(ReadOnlyCalendarError):
            rs.create_reminder("x", list_id="shared")

    def test_order_overdue_and_due_before(self, rs: CalendarService) -> None:
        rs.create_reminder("Undated", priority="high")
        rs.create_reminder("Yesterday", due="2026-11-01")
        rs.create_reminder("Today, no time", due="2026-11-02")  # not overdue until the day is over
        rs.create_reminder("Earlier today", due="2026-11-02T07:00")
        rs.create_reminder("Later today", due="2026-11-02T18:00")
        rs.create_reminder("Next week", due="2026-11-09")
        r = rs.list_reminders()
        assert [(x.title, x.overdue) for x in r.reminders] == [
            ("Yesterday", True),
            ("Today, no time", False),
            ("Earlier today", True),
            ("Later today", False),
            ("Next week", False),
            ("Undated", False),
        ]
        today = rs.list_reminders(due_before="2026-11-02")
        assert [x.title for x in today.reminders] == [
            "Yesterday",
            "Today, no time",
            "Earlier today",
            "Later today",
        ]
        assert rs.list_reminders(limit=2).truncated

    def test_query_and_lists(self, rs: CalendarService) -> None:
        rs.create_reminder("Buy oat milk", list_id="groceries")
        rs.create_reminder("Email Prof. Kaya", notes="about the thesis draft")
        assert [x.title for x in rs.list_reminders(query="THESIS email").reminders] == ["Email Prof. Kaya"]
        assert [x.title for x in rs.list_reminders(list_ids=["groceries"]).reminders] == ["Buy oat milk"]
        assert [x.title for x in rs.list_reminder_lists()] == ["Reminders", "Groceries", "Shared"]

    def test_update_complete_and_delete(self, rs: CalendarService) -> None:
        r = rs.create_reminder("Milk", due="2026-11-03T09:00")
        r = rs.update_reminder(r.id, title="Oat milk", notes="2 l", priority="low", list_id="groceries")
        assert (r.title, r.notes, r.priority, r.list_id) == ("Oat milk", "2 l", "low", "groceries")
        r = rs.update_reminder(r.id, due="2026-11-04")
        assert (r.due, r.due_all_day) == (at(4, 0), True)
        r = rs.update_reminder(r.id, clear_due=True, notes="")
        assert (r.due, r.notes) == (None, None)
        done = rs.complete_reminder(r.id)
        assert done.completed and done.completed_at is not None
        assert rs.list_reminders().count == 0
        assert [x.id for x in rs.list_reminders(status="completed").reminders] == [r.id]
        assert rs.list_reminders(status="all").count == 1
        assert not rs.complete_reminder(r.id, completed=False).completed
        gone = rs.delete_reminder(r.id)
        assert (gone.deleted, gone.title) == (True, "Oat milk")
        with pytest.raises(NotFoundError):
            rs.get_reminder(r.id)

    def test_update_rejects(self, rs: CalendarService) -> None:
        r = rs.create_reminder("x")
        with pytest.raises(InvalidRequestError, match="Nothing to update"):
            rs.update_reminder(r.id)
        with pytest.raises(InvalidRequestError, match="not both"):
            rs.update_reminder(r.id, due="2026-11-04", clear_due=True)

    def test_not_available_without_a_reminder_store(self, service: CalendarService) -> None:
        assert not service.has_reminders
        with pytest.raises(InvalidRequestError, match="only the macOS"):
            service.list_reminders()


pytestmark_anyio = pytest.mark.anyio


async def _call(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    res = await client.call_tool(name, args or {})
    assert not res.is_error, res.content
    assert res.structured_content is not None
    return res.structured_content.get("result", res.structured_content)


@pytestmark_anyio
async def test_tools(rs: CalendarService) -> None:
    async with Client(build_server(rs)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) >= REMINDER_READS | REMINDER_WRITES
        delete = tools["delete_reminder"].annotations
        assert delete is not None and delete.destructive_hint
        lists = await _call(client, "list_reminder_lists")
        assert lists[0]["is_default"]
        made = await _call(client, "create_reminder", {"title": "Renew passport", "due": "2026-11-20"})
        assert made["due_all_day"] and made["due"] == "2026-11-20T00:00:00+03:00"
        open_ = await _call(client, "list_reminders", {"due_before": "2026-11-30"})
        assert [x["title"] for x in open_["reminders"]] == ["Renew passport"]
        done = await _call(client, "complete_reminder", {"reminder_id": made["id"]})
        assert done["completed"]
        bad = await client.call_tool("update_reminder", {"reminder_id": made["id"], "priority": "urgent"})
        assert bad.is_error
        assert (await _call(client, "delete_reminder", {"reminder_id": made["id"]}))["deleted"]


@pytestmark_anyio
async def test_tools_depend_on_backend_and_mode(rs: CalendarService, service: CalendarService) -> None:
    async with Client(build_server(rs, read_only=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names >= REMINDER_READS and not REMINDER_WRITES & names
    async with Client(build_server(service)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not (REMINDER_READS | REMINDER_WRITES) & names
