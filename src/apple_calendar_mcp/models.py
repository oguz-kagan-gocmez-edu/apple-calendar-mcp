"""Backend-agnostic domain models.

These pydantic models are both the internal data types and the MCP tool
output schemas, so the JSON Claude sees is exactly what is defined here.

Conventions
-----------
* All datetimes are timezone-aware and serialised as ISO 8601 with offset.
* ``end`` is **exclusive**. For an all-day event on 3 Nov, ``start`` is
  2026-11-03T00:00 and ``end`` is 2026-11-04T00:00 (same as iCalendar DTEND).
* Event ids are opaque strings. For recurring events each occurrence has its
  own id, so an id always points at one concrete occurrence.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Availability = Literal["busy", "free", "tentative", "unavailable", "not_supported"]
EventStatus = Literal["none", "confirmed", "tentative", "canceled"]
Reply = Literal["accepted", "declined", "tentative"]
AttendeeStatus = Literal[
    "unknown", "pending", "accepted", "declined", "tentative", "delegated", "completed", "in_process"
]


class Span(StrEnum):
    """Which occurrences of a recurring event a change applies to."""

    THIS = "this"  # only the targeted occurrence
    FUTURE = "future"  # the targeted occurrence and all later ones
    ALL = "all"  # the whole series


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Calendar(_Model):
    id: str
    title: str
    source: str | None = Field(None, description="Account the calendar belongs to, e.g. 'iCloud'.")
    color: str | None = Field(None, description="Hex colour, e.g. '#1BADF8'.")
    writable: bool = Field(description="False for subscribed/holiday/read-only calendars.")
    is_default: bool = Field(False, description="Default calendar for new events.")


class Attendee(_Model):
    name: str | None = None
    email: str | None = None
    status: AttendeeStatus = "unknown"


class Recurrence(_Model):
    """A simple RRULE subset: FREQ, INTERVAL, BYDAY (weekly), COUNT/UNTIL."""

    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    interval: int = Field(1, ge=1, le=999, description="Every N days/weeks/months/years.")
    days_of_week: list[Weekday] | None = Field(
        None, description="Weekly only: which weekdays, e.g. ['mon', 'wed']."
    )
    count: int | None = Field(None, ge=1, le=5000, description="Stop after N occurrences.")
    until: date | None = Field(None, description="Last date (inclusive) an occurrence may fall on.")

    @model_validator(mode="after")
    def _check(self) -> Recurrence:
        if self.count is not None and self.until is not None:
            raise ValueError("Set at most one of 'count' and 'until'.")
        if self.days_of_week is not None:
            if self.frequency != "weekly":
                raise ValueError("'days_of_week' is only valid for weekly recurrence.")
            if not self.days_of_week:
                raise ValueError("'days_of_week' must not be empty.")
            # canonical order, no duplicates
            self.days_of_week = [d for d in WEEKDAYS if d in set(self.days_of_week)]
        return self


class Event(_Model):
    id: str = Field(description="Opaque id of this occurrence. Pass it to get/update/delete.")
    calendar_id: str
    calendar_title: str
    title: str
    start: datetime
    end: datetime = Field(description="Exclusive end.")
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    is_recurring: bool = False
    recurrence: Recurrence | None = None
    alarms_minutes_before: list[int] = Field(default_factory=list)
    availability: Availability = "busy"
    status: EventStatus = "none"
    organizer: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    my_status: AttendeeStatus | None = Field(
        None,
        description="Your own reply when you're invited ('pending' = not answered yet). "
        "None when you aren't an attendee, e.g. your own events.",
    )


class EventRef(_Model):
    """A compact pointer to one occurrence, for summaries."""

    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    calendar_title: str
    location: str | None = None

    @classmethod
    def of(cls, e: Event) -> EventRef:
        return cls(
            id=e.id,
            title=e.title,
            start=e.start,
            end=e.end,
            all_day=e.all_day,
            calendar_title=e.calendar_title,
            location=e.location,
        )


class Conflict(_Model):
    start: datetime = Field(description="Start of the overlap.")
    end: datetime = Field(description="End of the overlap (exclusive).")
    overlap_minutes: int
    events: list[EventRef] = Field(description="The two events that overlap.")


class ConflictResult(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    count: int
    truncated: bool
    conflicts: list[Conflict]


class DayStats(_Model):
    date: date
    weekday: str
    events: int = Field(description="Events on the day (all-day ones included, canceled ones not).")
    busy_minutes: int = Field(description="Time blocked by timed events, overlaps counted once.")


class WeekdayStats(_Model):
    weekday: str
    days: int = Field(description="How many of this weekday fall in the range.")
    busy_minutes: int
    average_busy_minutes: int


class CalendarUsage(_Model):
    calendar_id: str
    calendar_title: str
    events: int
    minutes: int = Field(description="Sum of the durations of its timed events that take time.")


class TitleUsage(_Model):
    title: str
    events: int
    minutes: int


class CalendarStats(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    events: int = Field(description="Events in the range (all-day included, canceled left out).")
    timed_events: int
    busy_minutes: int = Field(
        description="Time blocked by timed events, overlaps counted once. Skips events marked 'free' "
        "and invitations you declined."
    )
    busy_minutes_in_working_hours: int
    busy_minutes_outside_working_hours: int
    average_event_minutes: int = Field(description="Average length of timed events that take time.")
    busiest_day: DayStats | None
    days: list[DayStats]
    by_weekday: list[WeekdayStats]
    by_calendar: list[CalendarUsage] = Field(description="Most minutes first.")
    top_titles: list[TitleUsage] = Field(
        description="Up to 10 titles that take the most time (case ignored), e.g. recurring meetings."
    )


class DuplicateGroup(_Model):
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    repeats: int = Field(
        description="1 for one-off copies. More: the copies are repeating series that share this "
        "many occurrences in the range; 'events' holds the first one of each."
    )
    events: list[EventRef] = Field(description="The copies. Keep one; the others are the extras.")


class DuplicateResult(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    count: int
    truncated: bool
    groups: list[DuplicateGroup]


class EventList(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    count: int
    truncated: bool = Field(description="True if more events matched than 'limit'.")
    events: list[Event]


class FreeSlot(_Model):
    start: datetime
    end: datetime
    duration_minutes: int


class FreeSlotResult(_Model):
    timezone: str
    slots: list[FreeSlot]
    truncated: bool


class Agenda(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    now: datetime
    happening_now: list[EventRef] = Field(description="Timed events in progress right now.")
    up_next: EventRef | None = Field(description="The next timed event that hasn't started yet.")
    all_day: list[EventRef]
    events: list[EventRef] = Field(
        description="Timed events in the range, by start (canceled ones left out)."
    )
    busy_minutes: int = Field(
        description="Time blocked by events within working hours, overlaps counted once."
    )
    free_time: list[FreeSlot] = Field(description="Free gaps left within working hours, from now on.")
    conflicts: list[Conflict]
    invitations: list[EventRef] = Field(description="Events you're invited to and haven't answered.")


class DeleteResult(_Model):
    deleted: bool
    event_id: str
    span: Span
    title: str


class ChangeInfo(_Model):
    change_id: str
    at: datetime
    summary: str
    can_undo: bool
    why_not: str | None = Field(None, description="Why this change can't be undone, if it can't.")


class UndoResult(_Model):
    change_id: str
    summary: str = Field(description="The change that was undone.")
    restored: list[Event] = Field(description="Events as they are again (re-created ones have new ids).")
    removed_ids: list[str] = Field(description="Events the undo deleted (ones the change had created).")


class ChangeOutcome(_Model):
    action: Literal["create", "update", "delete"]
    event: Event = Field(description="The event after the change (for delete: as it was).")


class BatchResult(_Model):
    change_id: str | None = Field(
        description="Pass to undo_change to undo the whole batch. None for a dry run."
    )
    dry_run: bool = Field(False, description="True if nothing was written: results are a preview.")
    results: list[ChangeOutcome]


class IcsExport(_Model):
    timezone: str
    range_start: datetime
    range_end: datetime
    count: int
    truncated: bool = Field(description="True if the range held more events than were exported.")
    ics: str = Field(description="iCalendar text; save it as a .ics file.")


class SkippedItem(_Model):
    title: str
    reason: str


class ImportResult(_Model):
    change_id: str | None = Field(
        description="Pass to undo_change to remove everything this import added. None if nothing was added."
    )
    dry_run: bool = Field(
        False, description="True if nothing was written: 'imported' lists what would be added (no ids)."
    )
    imported: list[EventRef]
    skipped: list[SkippedItem] = Field(description="Events left out, each with the reason.")


class TimeContext(_Model):
    now: datetime
    timezone: str
    weekday: str
    iso_week: int


# ---------------------------------------------------------------- reminders
Priority = Literal["none", "high", "medium", "low"]
ReminderStatus = Literal["open", "completed", "all"]


class ReminderList(_Model):
    id: str
    title: str
    source: str | None = None
    color: str | None = None
    writable: bool
    is_default: bool = False


class Reminder(_Model):
    id: str = Field(description="Opaque id. Pass it to update/complete/delete.")
    list_id: str
    list_title: str
    title: str
    notes: str | None = None
    due: datetime | None = Field(None, description="When it is due; for a date-only due, local midnight.")
    due_all_day: bool = Field(False, description="True when the due has a date but no time.")
    overdue: bool = False
    priority: Priority = "none"
    completed: bool = False
    completed_at: datetime | None = None
    is_recurring: bool = Field(False, description="Repeats (read-only here; edit the rule in Reminders).")


class ReminderResult(_Model):
    timezone: str
    count: int
    truncated: bool
    reminders: list[Reminder]


class ReminderDeleted(_Model):
    deleted: bool
    reminder_id: str
    title: str


class ReminderDraft(_Model):
    title: str
    list_id: str | None = None
    notes: str | None = None
    due: datetime | None = None
    due_all_day: bool = False
    priority: Priority = "none"


class ReminderPatch(_Model):
    """Partial update. ``None`` means 'leave unchanged'; for notes '' clears."""

    title: str | None = None
    list_id: str | None = None
    notes: str | None = None
    due: datetime | None = None
    due_all_day: bool = False
    clear_due: bool = False
    priority: Priority | None = None
    completed: bool | None = None

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_defaults=True)


# ---------------------------------------------------------------- write models
class EventDraft(_Model):
    """Fully-specified new event (validated by the service layer)."""

    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    calendar_id: str | None = None
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    alarms_minutes_before: list[int] = Field(default_factory=list)
    recurrence: Recurrence | None = None
    availability: Availability | None = None


class EventPatch(_Model):
    """Partial update. ``None`` means 'leave unchanged'.

    For text fields an empty string clears the value. ``start``/``end`` are
    absolute times for the *targeted occurrence*; for FUTURE/ALL spans the
    backend applies the same shift to the rest of the series.
    """

    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool | None = None
    calendar_id: str | None = None
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    alarms_minutes_before: list[int] | None = None
    recurrence: Recurrence | None = None
    remove_recurrence: bool = False
    availability: Availability | None = None

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_defaults=True)

    def touches_recurrence(self) -> bool:
        return self.recurrence is not None or self.remove_recurrence


# ---------------------------------------------------------------- batch input
class CreateChange(_Model):
    action: Literal["create"]
    title: str
    start: str = Field(description="Start time, or the date for all-day events.")
    end: str | None = Field(None, description="End time; all-day: last day (inclusive).")
    duration_minutes: int | None = Field(None, description="Alternative to end.")
    all_day: bool = False
    calendar_id: str | None = None
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    alarms_minutes_before: list[int] | None = None
    recurrence: Recurrence | None = None
    availability: Availability | None = None


class UpdateChange(_Model):
    """Edits one event, or one occurrence of a series (span 'this')."""

    action: Literal["update"]
    event_id: str
    title: str | None = None
    start: str | None = Field(None, description="New start; end moves too, keeping the duration.")
    end: str | None = None
    all_day: bool | None = None
    calendar_id: str | None = None
    location: str | None = Field(None, description="Omit to keep; '' clears.")
    notes: str | None = Field(None, description="Omit to keep; '' clears.")
    url: str | None = Field(None, description="Omit to keep; '' clears.")
    alarms_minutes_before: list[int] | None = None
    availability: Availability | None = None


class DeleteChange(_Model):
    """Deletes a one-off event without attendees (so the batch can be undone)."""

    action: Literal["delete"]
    event_id: str


BatchChange = Annotated[CreateChange | UpdateChange | DeleteChange, Field(discriminator="action")]
