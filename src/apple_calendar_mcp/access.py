"""Per-calendar access control, enforced in front of any backend.

Each calendar gets one of three levels:

* ``write``: the default; Claude can read and change it.
* ``read``: Claude can read it, but every write is refused.
* ``hidden``: Claude can't see it at all. It is missing from
  ``list_calendars``, its events never appear, and its ids behave like ids
  that don't exist.

Calendars are named by id or by title (case-insensitive). A title matches
every calendar with that title, so use the id when two accounts share a name.
``hidden`` wins over ``read``. With an ``only`` list, every calendar not on it
is hidden.

The wrapper sits between the service and the backend, so every tool (and
anything added later) goes through the same checks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .models import Calendar, Event, EventDraft, EventPatch, Reply, Span
from .store import CalendarStore, NotFoundError, ReadOnlyCalendarError

Level = Literal["write", "read", "hidden"]


def _names(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    items = value.split(",") if isinstance(value, str) else value
    return frozenset(n.strip().casefold() for n in items if n.strip())


@dataclass(frozen=True)
class AccessPolicy:
    read_only: frozenset[str] = frozenset()
    hidden: frozenset[str] = frozenset()
    only: frozenset[str] | None = None  # None: no allow-list

    @classmethod
    def parse(
        cls,
        read_only: str | Iterable[str] | None = None,
        hidden: str | Iterable[str] | None = None,
        only: str | Iterable[str] | None = None,
    ) -> AccessPolicy:
        """Build from comma-separated lists (or iterables) of calendar ids or titles."""
        allow = _names(only)
        return cls(_names(read_only), _names(hidden), allow or None)

    @property
    def is_open(self) -> bool:
        """True when the policy changes nothing."""
        return not self.read_only and not self.hidden and self.only is None

    @staticmethod
    def _matches(cal: Calendar, names: frozenset[str]) -> bool:
        return cal.id.casefold() in names or cal.title.strip().casefold() in names

    def level(self, cal: Calendar) -> Level:
        if self._matches(cal, self.hidden):
            return "hidden"
        if self.only is not None and not self._matches(cal, self.only):
            return "hidden"
        if self._matches(cal, self.read_only):
            return "read"
        return "write"

    def unmatched(self, calendars: list[Calendar]) -> list[str]:
        """Names in the policy that match no calendar (likely typos)."""
        known = {c.id.casefold() for c in calendars} | {c.title.strip().casefold() for c in calendars}
        names = self.read_only | self.hidden | (self.only or frozenset())
        return sorted(n for n in names if n not in known)


class AccessControlledStore:
    """A ``CalendarStore`` that applies an :class:`AccessPolicy` to another one."""

    def __init__(self, inner: CalendarStore, policy: AccessPolicy) -> None:
        self.inner = inner
        self.policy = policy

    # ------------------------------------------------------------ helpers
    def _levels(self) -> tuple[dict[str, Level], str | None]:
        """Level per calendar id, and the backend's default calendar id."""
        cals = self.inner.list_calendars()
        default = next((c.id for c in cals if c.is_default), None)
        return {c.id: self.policy.level(c) for c in cals}, default

    def _check_writable(self, calendar_id: str | None, levels: dict[str, Level], title: str = "") -> None:
        level = levels.get(calendar_id or "", "write")
        if level == "hidden":
            raise NotFoundError(f"No calendar with id {calendar_id!r}.")
        if level == "read":
            raise ReadOnlyCalendarError(
                f"Calendar {title or calendar_id!r} is read-only for this server (access settings)."
            )

    def _visible(self, event: Event, levels: dict[str, Level], event_id: str) -> Event:
        if levels.get(event.calendar_id) == "hidden":
            raise NotFoundError(f"No event with id {event_id!r}.")
        return event

    # ------------------------------------------------------------ reads
    def list_calendars(self) -> list[Calendar]:
        out: list[Calendar] = []
        for cal in self.inner.list_calendars():
            level = self.policy.level(cal)
            if level == "hidden":
                continue
            out.append(cal.model_copy(update={"writable": False}) if level == "read" else cal)
        return out

    def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        levels, _ = self._levels()
        wanted: list[str] | None
        if calendar_ids:
            # Hidden ids are dropped quietly, the same as ids that don't exist.
            wanted = [cid for cid in calendar_ids if levels.get(cid) != "hidden"]
        elif "hidden" in levels.values():
            wanted = [cid for cid, lvl in levels.items() if lvl != "hidden"]
        else:
            wanted = None
        if wanted == []:
            return []
        events = self.inner.list_events(start, end, wanted)
        return [e for e in events if levels.get(e.calendar_id) != "hidden"]

    def get_event(self, event_id: str) -> Event:
        levels, _ = self._levels()
        return self._visible(self.inner.get_event(event_id), levels, event_id)

    # ------------------------------------------------------------ writes
    def create_event(self, draft: EventDraft) -> Event:
        levels, default = self._levels()
        target = draft.calendar_id or default
        if draft.calendar_id is None and levels.get(target or "") in ("read", "hidden"):
            raise ReadOnlyCalendarError(
                "The default calendar is read-only for this server; pass calendar_id (see list_calendars)."
            )
        self._check_writable(target, levels)
        return self.inner.create_event(draft)

    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        levels, _ = self._levels()
        current = self._visible(self.inner.get_event(event_id), levels, event_id)
        self._check_writable(current.calendar_id, levels, current.calendar_title)
        if patch.calendar_id is not None:
            self._check_writable(patch.calendar_id, levels)
        return self.inner.update_event(event_id, patch, span)

    def delete_event(self, event_id: str, span: Span) -> Event:
        levels, _ = self._levels()
        current = self._visible(self.inner.get_event(event_id), levels, event_id)
        self._check_writable(current.calendar_id, levels, current.calendar_title)
        return self.inner.delete_event(event_id, span)

    @property
    def supports_replies(self) -> bool:
        return self.inner.supports_replies

    def respond_to_invitation(self, event_id: str, reply: Reply, span: Span) -> Event:
        levels, _ = self._levels()
        current = self._visible(self.inner.get_event(event_id), levels, event_id)
        self._check_writable(current.calendar_id, levels, current.calendar_title)
        return self.inner.respond_to_invitation(event_id, reply, span)
