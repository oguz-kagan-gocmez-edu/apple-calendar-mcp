"""Reminders: the storage interface, an in-memory store and the access wrapper.

Reminders are a separate, optional part of a backend. Only EventKit (macOS)
can reach them: iCloud stopped serving reminders over CalDAV when Apple moved
them to a newer format, so the CalDAV backend and the Worker don't offer the
reminder tools at all.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, tzinfo
from typing import Protocol, runtime_checkable

from .access import AccessPolicy, Level
from .models import Calendar, Reminder, ReminderDraft, ReminderList, ReminderPatch
from .store import NotFoundError, ReadOnlyCalendarError


@runtime_checkable
class ReminderStore(Protocol):
    def list_reminder_lists(self) -> list[ReminderList]: ...

    def list_reminders(self, list_ids: list[str] | None, *, completed: bool) -> list[Reminder]:
        """Open (``completed=False``) or completed reminders in the given lists (all if None)."""
        ...

    def get_reminder(self, reminder_id: str) -> Reminder: ...

    def create_reminder(self, draft: ReminderDraft) -> Reminder: ...

    def update_reminder(self, reminder_id: str, patch: ReminderPatch) -> Reminder: ...

    def delete_reminder(self, reminder_id: str) -> Reminder:
        """Delete and return the reminder as it was."""
        ...


def is_overdue(r: Reminder, now: datetime) -> bool:
    if r.completed or r.due is None:
        return False
    if r.due_all_day:
        return r.due.date() < now.astimezone(r.due.tzinfo).date()
    return r.due < now


class MemoryReminderStore:
    """In-memory reminders for tests and the ``--backend memory`` demo."""

    def __init__(self, tz: tzinfo, lists: list[ReminderList] | None = None) -> None:
        self.tz = tz
        self._lock = threading.RLock()
        self._lists = {
            rl.id: rl
            for rl in lists
            or [
                ReminderList(id="inbox", title="Reminders", source="Local", writable=True, is_default=True),
                ReminderList(id="groceries", title="Groceries", source="Local", writable=True),
                ReminderList(id="shared", title="Shared", source="iCloud", writable=False),
            ]
        }
        self._items: dict[str, Reminder] = {}

    def _list(self, list_id: str | None, *, for_write: bool) -> ReminderList:
        if list_id is None:
            rl = next((x for x in self._lists.values() if x.is_default), None)
            if rl is None:
                raise NotFoundError("No default reminder list; pass list_id.")
        else:
            rl = self._lists.get(list_id)
            if rl is None:
                raise NotFoundError(f"No reminder list with id {list_id!r}.")
        if for_write and not rl.writable:
            raise ReadOnlyCalendarError(f"Reminder list {rl.title!r} is read-only.")
        return rl

    def _get(self, reminder_id: str) -> Reminder:
        r = self._items.get(reminder_id)
        if r is None:
            raise NotFoundError(f"No reminder with id {reminder_id!r}.")
        return r

    def list_reminder_lists(self) -> list[ReminderList]:
        with self._lock:
            return list(self._lists.values())

    def list_reminders(self, list_ids: list[str] | None, *, completed: bool) -> list[Reminder]:
        with self._lock:
            for lid in list_ids or []:
                self._list(lid, for_write=False)
            return [
                r
                for r in self._items.values()
                if r.completed == completed and (not list_ids or r.list_id in list_ids)
            ]

    def get_reminder(self, reminder_id: str) -> Reminder:
        with self._lock:
            return self._get(reminder_id)

    def create_reminder(self, draft: ReminderDraft) -> Reminder:
        with self._lock:
            rl = self._list(draft.list_id, for_write=True)
            r = Reminder(
                id=uuid.uuid4().hex.upper(),
                list_id=rl.id,
                list_title=rl.title,
                title=draft.title,
                notes=draft.notes or None,
                due=draft.due,
                due_all_day=draft.due_all_day if draft.due else False,
                priority=draft.priority,
            )
            self._items[r.id] = r
            return r

    def update_reminder(self, reminder_id: str, patch: ReminderPatch) -> Reminder:
        with self._lock:
            r = self._get(reminder_id)
            self._list(r.list_id, for_write=True)
            changes: dict[str, object] = {}
            if patch.title is not None:
                changes["title"] = patch.title
            if patch.notes is not None:
                changes["notes"] = patch.notes or None
            if patch.clear_due:
                changes.update(due=None, due_all_day=False)
            elif patch.due is not None:
                changes.update(due=patch.due, due_all_day=patch.due_all_day)
            if patch.priority is not None:
                changes["priority"] = patch.priority
            if patch.completed is not None:
                changes.update(
                    completed=patch.completed,
                    completed_at=datetime.now(self.tz).replace(microsecond=0) if patch.completed else None,
                )
            if patch.list_id is not None:
                rl = self._list(patch.list_id, for_write=True)
                changes.update(list_id=rl.id, list_title=rl.title)
            r = r.model_copy(update=changes)
            self._items[r.id] = r
            return r

    def delete_reminder(self, reminder_id: str) -> Reminder:
        with self._lock:
            r = self._get(reminder_id)
            self._list(r.list_id, for_write=True)
            del self._items[reminder_id]
            return r


def _as_calendar(rl: ReminderList) -> Calendar:
    return Calendar(id=rl.id, title=rl.title, writable=rl.writable)


class AccessControlledReminders:
    """Applies the calendar access settings to reminder lists (matched by id or title)."""

    def __init__(self, inner: ReminderStore, policy: AccessPolicy) -> None:
        self.inner = inner
        self.policy = policy

    def _levels(self) -> tuple[dict[str, Level], str | None]:
        lists = self.inner.list_reminder_lists()
        default = next((x.id for x in lists if x.is_default), None)
        return {x.id: self.policy.level(_as_calendar(x)) for x in lists}, default

    def _visible(self, r: Reminder, levels: dict[str, Level], reminder_id: str) -> Reminder:
        if levels.get(r.list_id) == "hidden":
            raise NotFoundError(f"No reminder with id {reminder_id!r}.")
        return r

    def _check_writable(self, list_id: str | None, levels: dict[str, Level]) -> None:
        level = levels.get(list_id or "", "write")
        if level == "hidden":
            raise NotFoundError(f"No reminder list with id {list_id!r}.")
        if level == "read":
            raise ReadOnlyCalendarError(
                f"Reminder list {list_id!r} is read-only for this server (access settings)."
            )

    def list_reminder_lists(self) -> list[ReminderList]:
        out: list[ReminderList] = []
        for rl in self.inner.list_reminder_lists():
            level = self.policy.level(_as_calendar(rl))
            if level != "hidden":
                out.append(rl.model_copy(update={"writable": False}) if level == "read" else rl)
        return out

    def list_reminders(self, list_ids: list[str] | None, *, completed: bool) -> list[Reminder]:
        levels, _ = self._levels()
        if list_ids:
            wanted = [lid for lid in list_ids if levels.get(lid) != "hidden"]
        else:
            wanted = [lid for lid, lvl in levels.items() if lvl != "hidden"]
        if not wanted:
            return []
        found = self.inner.list_reminders(wanted, completed=completed)
        return [r for r in found if levels.get(r.list_id) != "hidden"]

    def get_reminder(self, reminder_id: str) -> Reminder:
        levels, _ = self._levels()
        return self._visible(self.inner.get_reminder(reminder_id), levels, reminder_id)

    def create_reminder(self, draft: ReminderDraft) -> Reminder:
        levels, default = self._levels()
        target = draft.list_id or default
        if draft.list_id is None and levels.get(target or "") in ("read", "hidden"):
            raise ReadOnlyCalendarError(
                "The default reminder list is read-only for this server; pass list_id "
                "(see list_reminder_lists)."
            )
        self._check_writable(target, levels)
        return self.inner.create_reminder(draft)

    def update_reminder(self, reminder_id: str, patch: ReminderPatch) -> Reminder:
        levels, _ = self._levels()
        current = self._visible(self.inner.get_reminder(reminder_id), levels, reminder_id)
        self._check_writable(current.list_id, levels)
        if patch.list_id is not None:
            self._check_writable(patch.list_id, levels)
        return self.inner.update_reminder(reminder_id, patch)

    def delete_reminder(self, reminder_id: str) -> Reminder:
        levels, _ = self._levels()
        current = self._visible(self.inner.get_reminder(reminder_id), levels, reminder_id)
        self._check_writable(current.list_id, levels)
        return self.inner.delete_reminder(reminder_id)
