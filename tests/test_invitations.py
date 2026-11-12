"""Answering invitations, through the service, the access wrapper and the MCP tools."""

import pytest
from mcp.client.client import Client

from apple_calendar_mcp.access import AccessControlledStore, AccessPolicy
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.models import Recurrence, Span
from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import InvalidRequestError, ReadOnlyCalendarError

from .conftest import IST, NOW


@pytest.fixture
def invite(service: CalendarService, store: MemoryStore) -> str:
    e = service.create_event("Design review", "2026-11-03T16:00")
    store.set_my_status(e.id, "pending")
    return e.id


@pytest.fixture
def weekly_invite(service: CalendarService, store: MemoryStore) -> list[str]:
    service.create_event("Team sync", "2026-11-03T10:00", recurrence=Recurrence(frequency="weekly", count=3))
    ids = [e.id for e in service.list_events("2026-11-01", "2026-11-30").events]
    store.set_my_status(ids[0], "pending")
    return ids


def test_accept_decline_tentative(service: CalendarService, invite: str) -> None:
    for reply in ("accepted", "tentative", "declined"):
        assert service.respond_to_invitation(invite, reply).my_status == reply  # type: ignore[arg-type]
    assert service.get_event(invite).my_status == "declined"


def test_answered_invitation_leaves_the_agenda(service: CalendarService, invite: str) -> None:
    assert [e.id for e in service.agenda("2026-11-03").invitations] == [invite]
    service.respond_to_invitation(invite, "accepted")
    assert service.agenda("2026-11-03").invitations == []


def test_own_event_is_not_an_invitation(service: CalendarService) -> None:
    own = service.create_event("Mine", "2026-11-03T09:00")
    with pytest.raises(InvalidRequestError, match="isn't an invitation"):
        service.respond_to_invitation(own.id, "accepted")


def test_whole_series_by_default(service: CalendarService, weekly_invite: list[str]) -> None:
    service.respond_to_invitation(weekly_invite[1], "accepted")
    assert [service.get_event(i).my_status for i in weekly_invite] == ["accepted"] * 3


def test_one_occurrence(service: CalendarService, weekly_invite: list[str]) -> None:
    service.respond_to_invitation(weekly_invite[0], "accepted")
    got = service.respond_to_invitation(weekly_invite[1], "declined", Span.THIS)
    assert got.id == weekly_invite[1] and got.my_status == "declined"
    assert [service.get_event(i).my_status for i in weekly_invite] == ["accepted", "declined", "accepted"]
    # Answering the series again replaces the single answer.
    service.respond_to_invitation(weekly_invite[0], "tentative", Span.ALL)
    assert [service.get_event(i).my_status for i in weekly_invite] == ["tentative"] * 3


def test_future_span_is_refused(service: CalendarService, weekly_invite: list[str]) -> None:
    with pytest.raises(InvalidRequestError, match="'this' occurrence or 'all'"):
        service.respond_to_invitation(weekly_invite[1], "accepted", Span.FUTURE)


def test_replies_are_not_in_the_undo_log(service: CalendarService, invite: str) -> None:
    before = len(service.list_changes())
    service.respond_to_invitation(invite, "accepted")
    assert len(service.list_changes()) == before


def test_access_levels(store: MemoryStore, service: CalendarService) -> None:
    work = service.create_event("Review", "2026-11-03T16:00", calendar_id="work")
    store.set_my_status(work.id, "pending")
    guarded = CalendarService(
        AccessControlledStore(store, AccessPolicy.parse(read_only="work")), IST, clock=lambda: NOW
    )
    assert guarded.can_reply
    with pytest.raises(ReadOnlyCalendarError):
        guarded.respond_to_invitation(work.id, "accepted")
    assert store.get_event(work.id).my_status == "pending"


pytestmark_anyio = pytest.mark.anyio


@pytestmark_anyio
async def test_tool(service: CalendarService, invite: str) -> None:
    async with Client(build_server(service)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        ann = tools["respond_to_invitation"].annotations
        assert ann is not None and ann.open_world_hint and not ann.destructive_hint
        res = await client.call_tool("respond_to_invitation", {"event_id": invite, "reply": "accepted"})
        assert not res.is_error and res.structured_content is not None
        assert res.structured_content["my_status"] == "accepted"
        bad = await client.call_tool("respond_to_invitation", {"event_id": invite, "reply": "maybe"})
        assert bad.is_error
        bad = await client.call_tool(
            "respond_to_invitation", {"event_id": invite, "reply": "accepted", "span": "future"}
        )
        assert bad.is_error


@pytestmark_anyio
async def test_tool_is_absent_when_the_backend_cannot_reply(store: MemoryStore) -> None:
    class NoReplies(MemoryStore):
        supports_replies = False

    service = CalendarService(NoReplies(IST), IST, clock=lambda: NOW)
    assert not service.can_reply
    async with Client(build_server(service)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "respond_to_invitation" not in names and "create_event" in names
