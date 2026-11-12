"""End-to-end over the real MCP protocol, using the SDK's in-memory client."""

from typing import Any

import pytest
from mcp.client.client import Client

from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import CalendarService

pytestmark = pytest.mark.anyio

READ_TOOLS = {
    "get_time_context",
    "list_calendars",
    "list_events",
    "search_events",
    "get_event",
    "find_free_slots",
    "agenda",
    "find_conflicts",
}
WRITE_TOOLS = {
    "create_event",
    "update_event",
    "delete_event",
    "apply_changes",
    "undo_change",
    "list_changes",
    "respond_to_invitation",
}


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    res = await client.call_tool(name, args or {})
    assert not res.is_error, res.content
    data = res.structured_content
    assert data is not None
    return data.get("result", data)  # list outputs are wrapped as {"result": [...]}


async def test_tool_catalogue_and_annotations(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS | {"list_changes"}:
        assert tools[name].annotations is not None and tools[name].annotations.read_only_hint
    assert tools["delete_event"].annotations is not None
    assert tools["delete_event"].annotations.destructive_hint
    assert tools["list_events"].output_schema is not None


async def test_read_only_mode_hides_writes(service: CalendarService) -> None:
    async with Client(build_server(service, read_only=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == READ_TOOLS


async def test_full_lifecycle(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        created = await call(
            client,
            "create_event",
            {
                "title": "Project demo",
                "start": "2026-11-05T14:00",
                "duration_minutes": 45,
                "location": "Lab 3",
                "alarms_minutes_before": [15],
                "recurrence": {"frequency": "weekly", "count": 3},
            },
        )
        assert created["start"] == "2026-11-05T14:00:00+03:00" and created["is_recurring"]

        listed = await call(client, "list_events", {"start": "2026-11-01", "end": "2026-11-30"})
        assert listed["count"] == 3
        second = listed["events"][1]["id"]

        updated = await call(
            client, "update_event", {"event_id": second, "span": "this", "title": "Demo (moved)"}
        )
        assert updated["title"] == "Demo (moved)"

        found = await call(client, "search_events", {"query": "moved"})
        assert [e["id"] for e in found["events"]] == [second]

        deleted = await call(client, "delete_event", {"event_id": created["id"], "span": "all"})
        assert deleted["deleted"]
        listed = await call(client, "list_events", {"start": "2026-11-01", "end": "2026-11-30"})
        assert listed["count"] == 0


async def test_domain_errors_become_tool_errors(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        res = await client.call_tool("get_event", {"event_id": "missing|1"})
        assert res.is_error
        assert "No event" in res.content[0].text  # type: ignore[union-attr]

        res = await client.call_tool("create_event", {"title": "X", "start": "next tuesday"})
        assert res.is_error
        assert "ISO 8601" in res.content[0].text  # type: ignore[union-attr]


async def test_schema_validation_rejects_bad_span(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        res = await client.call_tool("delete_event", {"event_id": "x", "span": "everything"})
        assert res.is_error


async def test_calendars_and_free_slots(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        cals = await call(client, "list_calendars")
        assert {c["id"] for c in cals} == {"home", "work", "holidays"}
        slots = await call(
            client, "find_free_slots", {"start": "2026-11-03", "end": "2026-11-03", "duration_minutes": 120}
        )
        assert slots["slots"][0]["start"] == "2026-11-03T09:00:00+03:00"


async def test_agenda_and_conflicts(service: CalendarService) -> None:
    service.create_event("Lecture", "2026-11-02T10:00", end="2026-11-02T12:00")
    service.create_event("Lunch", "2026-11-02T11:30", duration_minutes=60)
    async with Client(build_server(service)) as client:
        a = await call(client, "agenda", {})
        assert a["up_next"]["title"] == "Lecture"
        assert a["busy_minutes"] == 150 and len(a["conflicts"]) == 1
        assert a["free_time"][0] == {
            "start": "2026-11-02T09:00:00+03:00",
            "end": "2026-11-02T10:00:00+03:00",
            "duration_minutes": 60,
        }
        c = await call(client, "find_conflicts", {"start": "2026-11-02", "end": "2026-11-08"})
        assert c["count"] == 1 and c["conflicts"][0]["overlap_minutes"] == 30
        slots = await call(
            client,
            "find_free_slots",
            {"start": "2026-11-02", "end": "2026-11-02", "duration_minutes": 60, "buffer_minutes": 30},
        )
        assert slots["slots"][0]["start"] == "2026-11-02T13:00:00+03:00"


async def test_batch_and_undo(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        r = await call(
            client,
            "apply_changes",
            {
                "changes": [
                    {"action": "create", "title": "A", "start": "2026-11-03T09:00"},
                    {"action": "create", "title": "B", "start": "2026-11-03T11:00"},
                ]
            },
        )
        assert [o["event"]["title"] for o in r["results"]] == ["A", "B"]
        changes = await call(client, "list_changes")
        assert changes[0]["change_id"] == r["change_id"] and changes[0]["can_undo"]
        undone = await call(client, "undo_change", {})
        assert len(undone["removed_ids"]) == 2
        listed = await call(client, "list_events", {"start": "2026-11-03", "end": "2026-11-03"})
        assert listed["count"] == 0
        res = await client.call_tool("apply_changes", {"changes": [{"action": "explode"}]})
        assert res.is_error
