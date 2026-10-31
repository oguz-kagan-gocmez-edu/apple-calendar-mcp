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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Availability = Literal["busy", "free", "tentative", "unavailable", "not_supported"]
EventStatus = Literal["none", "confirmed", "tentative", "canceled"]
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


class DeleteResult(_Model):
    deleted: bool
    event_id: str
    span: Span
    title: str


class TimeContext(_Model):
    now: datetime
    timezone: str
    weekday: str
    iso_week: int


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
