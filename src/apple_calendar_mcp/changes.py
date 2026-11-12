"""The change log behind ``undo_change``.

Every write the service makes is recorded as a :class:`ChangeRecord`: one or
more :class:`Step`\\ s, each holding the event before and after. Undo works
from those snapshots, and only where it can put things back exactly:

* a created event (or series) is deleted again;
* an edited one-off event, or one occurrence of a series (span 'this'), gets
  its previous fields back;
* a deleted one-off event without attendees is created again (with a new id).

Edits to several occurrences of a series, and deleted occurrences or
invitations, are recorded with the reason undo isn't possible. Before undoing,
the event must still look exactly as the change left it; otherwise undo would
overwrite a later edit, so it refuses.

The log lives in memory, newest last, and keeps the last ``MAX_CHANGES``.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .models import Event, EventDraft, EventPatch, Span
from .store import CalendarStore, InvalidRequestError, NotFoundError

MAX_CHANGES = 50

# Fields undo restores and compares. Attendees, organizer and status aren't
# writable through this server, so they can't drift because of it either.
FIELDS = (
    "title",
    "start",
    "end",
    "all_day",
    "calendar_id",
    "location",
    "notes",
    "url",
    "alarms_minutes_before",
    "availability",
    "recurrence",
)
_TEXT = {"location", "notes", "url"}

SERIES_EDIT = "Changes to several occurrences of a repeating event can't be undone automatically."
SERIES_DELETE = "Deleted occurrences of a repeating event can't be restored automatically."
INVITE_DELETE = "A deleted event with attendees can't be restored: the invitations can't be recreated."


@dataclass(frozen=True)
class Step:
    action: Literal["create", "update", "delete"]
    before: Event | None  # None for create
    after: Event | None  # None for delete
    span: Span = Span.THIS

    @property
    def blocker(self) -> str | None:
        """Why this step can't be undone, or None if it can."""
        if self.action == "update":
            assert self.before is not None and self.after is not None
            if self.before.is_recurring and self.span is not Span.THIS:
                return SERIES_EDIT
            if self.before.recurrence != self.after.recurrence:
                return SERIES_EDIT
        if self.action == "delete":
            assert self.before is not None
            if self.before.is_recurring:
                return SERIES_DELETE
            if self.before.attendees:
                return INVITE_DELETE
        return None

    @property
    def event(self) -> Event:
        """The event after the step (for a delete: as it was)."""
        e = self.after or self.before
        assert e is not None
        return e

    @property
    def title(self) -> str:
        return self.event.title


@dataclass
class ChangeRecord:
    id: str
    at: datetime
    summary: str
    steps: list[Step] = field(default_factory=list)

    @property
    def blocker(self) -> str | None:
        return next((s.blocker for s in self.steps if s.blocker), None)


def summarize(steps: list[Step]) -> str:
    if len(steps) == 1:
        verb = {"create": "Created", "update": "Changed", "delete": "Deleted"}[steps[0].action]
        return f'{verb} "{steps[0].title}"'
    return f"{len(steps)} changes in one batch"


class ChangeJournal:
    """Recent changes, newest last. Thread-safe."""

    def __init__(self, limit: int = MAX_CHANGES) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._records: OrderedDict[str, ChangeRecord] = OrderedDict()

    def record(self, steps: list[Step], at: datetime) -> ChangeRecord:
        rec = ChangeRecord(id=f"chg_{secrets.token_hex(4)}", at=at, summary=summarize(steps), steps=steps)
        with self._lock:
            self._records[rec.id] = rec
            while len(self._records) > self._limit:
                self._records.popitem(last=False)
        return rec

    def get(self, change_id: str | None) -> ChangeRecord:
        with self._lock:
            if change_id is None:
                if not self._records:
                    raise NotFoundError("There is no change to undo.")
                return next(reversed(self._records.values()))
            rec = self._records.get(change_id)
        if rec is None:
            raise NotFoundError(
                f"No change with id {change_id!r}. It may be too old, already undone, "
                "or from before the server restarted; see list_changes."
            )
        return rec

    def remove(self, change_id: str) -> None:
        with self._lock:
            self._records.pop(change_id, None)

    def recent(self) -> list[ChangeRecord]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._records.values()))


# ---------------------------------------------------------------- undo mechanics
def _values(e: Event) -> dict[str, Any]:
    return {f: getattr(e, f) for f in FIELDS}


def inverse_patch(before: Event, after: Event) -> EventPatch:
    """A patch that turns ``after`` back into ``before``."""
    old, new = _values(before), _values(after)
    changes: dict[str, Any] = {}
    for f in FIELDS:
        if f == "recurrence" or old[f] == new[f]:
            continue
        changes[f] = "" if f in _TEXT and old[f] is None else old[f]
    if "start" in changes or "end" in changes:  # times move together
        changes["start"], changes["end"] = old["start"], old["end"]
    return EventPatch(**changes)


def draft_of(e: Event) -> EventDraft:
    return EventDraft(
        title=e.title,
        start=e.start,
        end=e.end,
        all_day=e.all_day,
        calendar_id=e.calendar_id,
        location=e.location,
        notes=e.notes,
        url=e.url,
        alarms_minutes_before=e.alarms_minutes_before,
        recurrence=e.recurrence,
        availability=e.availability if e.availability != "not_supported" else None,
    )


def check_unchanged(store: CalendarStore, step: Step) -> None:
    """Refuse to undo over a later edit (or a deleted event)."""
    if step.after is None:
        return
    try:
        now = store.get_event(step.after.id)
    except NotFoundError as exc:
        raise InvalidRequestError(
            f'"{step.title}" no longer exists, so this change can\'t be undone.'
        ) from exc
    if _values(now) != _values(step.after):
        raise InvalidRequestError(
            f'"{step.title}" was changed again after this change; undoing would overwrite that. '
            "Edit it directly instead."
        )


def revert(store: CalendarStore, step: Step) -> tuple[Event | None, str | None]:
    """Undo one step. Returns (restored event, removed event id)."""
    if step.action == "create":
        assert step.after is not None
        store.delete_event(step.after.id, Span.ALL if step.after.is_recurring else Span.THIS)
        return None, step.after.id
    if step.action == "update":
        assert step.before is not None and step.after is not None
        patch = inverse_patch(step.before, step.after)
        if patch.is_empty():
            return step.after, None
        return store.update_event(step.after.id, patch, Span.THIS), None
    assert step.before is not None
    return store.create_event(draft_of(step.before)), None
