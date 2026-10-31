"""The storage interface every backend implements.

The server never talks to EventKit directly; it talks to a ``CalendarStore``.
That keeps the MCP layer testable on any OS (with ``MemoryStore``) and leaves
room for other backends later (e.g. iCloud CalDAV for a remote connector).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import Calendar, Event, EventDraft, EventPatch, Span


class CalendarError(Exception):
    """Base class for errors that are safe to show to the model/user."""


class NotFoundError(CalendarError):
    pass


class PermissionDeniedError(CalendarError):
    pass


class ReadOnlyCalendarError(CalendarError):
    pass


class InvalidRequestError(CalendarError):
    pass


class BackendError(CalendarError):
    pass


@runtime_checkable
class CalendarStore(Protocol):
    """Minimal CRUD surface. Search and free/busy are built on top of it."""

    def list_calendars(self) -> list[Calendar]: ...

    def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        """Occurrences overlapping [start, end), recurring events expanded."""
        ...

    def get_event(self, event_id: str) -> Event: ...

    def create_event(self, draft: EventDraft) -> Event: ...

    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        """Apply ``patch``; return the updated targeted occurrence (or series head)."""
        ...

    def delete_event(self, event_id: str, span: Span) -> Event:
        """Delete and return the event as it was before deletion."""
        ...


def make_event_id(series_id: str, occurrence_ts: float) -> str:
    """Compose an occurrence id: '<series id>|<original start, unix seconds>'."""
    return f"{series_id}|{int(occurrence_ts)}"


def split_event_id(event_id: str) -> tuple[str, int | None]:
    """Inverse of :func:`make_event_id`. Plain series ids are accepted too."""
    series, sep, ts = event_id.rpartition("|")
    if not sep:
        return event_id, None
    try:
        return series, int(ts)
    except ValueError:
        return event_id, None
