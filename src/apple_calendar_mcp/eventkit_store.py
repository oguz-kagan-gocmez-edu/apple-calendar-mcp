"""``CalendarStore`` backed by Apple's EventKit framework (macOS only).

EventKit is an Objective-C framework; PyObjC bridges it to Python. Selector
names map mechanically: ``-[EKEventStore eventsMatchingPredicate:]`` becomes
``store.eventsMatchingPredicate_(pred)``, and an ``NSError **`` out-parameter
becomes an extra tuple element in the return value.

EventKit reads the same database as Calendar.app, so every account synced on
the Mac (iCloud, Google, Exchange, ...) is visible through one API.

Thread-safety: MCPServer runs sync tools on AnyIO worker threads. We serialise
every EventKit call through one re-entrant lock and wrap each public method in
an autorelease pool so Objective-C temporaries are freed promptly.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

if sys.platform != "darwin":  # pragma: no cover - import guard
    raise ImportError("The EventKit backend only runs on macOS.")

import EventKit as EK
import objc
from Foundation import NSURL, NSDate, NSDateComponents, NSTimeZone

from .models import (
    WEEKDAYS,
    Attendee,
    AttendeeStatus,
    Availability,
    Calendar,
    Event,
    EventDraft,
    EventPatch,
    EventStatus,
    Priority,
    Recurrence,
    Reminder,
    ReminderDraft,
    ReminderList,
    ReminderPatch,
    Reply,
    Span,
)
from .store import (
    BackendError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ReadOnlyCalendarError,
    make_event_id,
    split_event_id,
)
from .timeutil import ONE_DAY, tz_name

log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# --- EventKit enum values (with literal fallbacks for older PyObjC builds) ----
_ENTITY_EVENT = getattr(EK, "EKEntityTypeEvent", 0)
_ENTITY_REMINDER = getattr(EK, "EKEntityTypeReminder", 1)
# NSDateComponents reports unset fields as NSIntegerMax.
_UNDEFINED = 2**63 - 1
_AUTH_NOT_DETERMINED = getattr(EK, "EKAuthorizationStatusNotDetermined", 0)
_AUTH_RESTRICTED = getattr(EK, "EKAuthorizationStatusRestricted", 1)
_AUTH_DENIED = getattr(EK, "EKAuthorizationStatusDenied", 2)
_AUTH_FULL = getattr(EK, "EKAuthorizationStatusFullAccess", 3)  # == legacy "Authorized"
_AUTH_WRITE_ONLY = getattr(EK, "EKAuthorizationStatusWriteOnly", 4)

_SPAN = {
    Span.THIS: getattr(EK, "EKSpanThisEvent", 0),
    Span.FUTURE: getattr(EK, "EKSpanFutureEvents", 1),
}
_FREQ = {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}  # EKRecurrenceFrequency
_FREQ_REV = {v: k for k, v in _FREQ.items()}
# EKWeekday: Sunday = 1 ... Saturday = 7
_WEEKDAY_TO_EK = {d: (i + 1) % 7 + 1 for i, d in enumerate(WEEKDAYS)}
_EK_TO_WEEKDAY = {v: k for k, v in _WEEKDAY_TO_EK.items()}

_AVAILABILITY: dict[int, Availability] = {
    -1: "not_supported",
    0: "busy",
    1: "free",
    2: "tentative",
    3: "unavailable",
}
_AVAILABILITY_REV = {v: k for k, v in _AVAILABILITY.items()}
_STATUS: dict[int, EventStatus] = {0: "none", 1: "confirmed", 2: "tentative", 3: "canceled"}
# EKReminder priority: 0 none, 1-4 high, 5 medium, 6-9 low.
_PRIORITY_TO_EK: dict[Priority, int] = {"none": 0, "high": 1, "medium": 5, "low": 9}


def _priority(value: int) -> Priority:
    if value <= 0:
        return "none"
    return "high" if value < 5 else "medium" if value == 5 else "low"


_PARTICIPANT: dict[int, AttendeeStatus] = {
    0: "unknown",
    1: "pending",
    2: "accepted",
    3: "declined",
    4: "tentative",
    5: "delegated",
    6: "completed",
    7: "in_process",
}


def _helper_unusable() -> str | None:
    from . import macos_app

    return macos_app.unusable_reason()


def _eventkit_call(fn: Callable[P, R]) -> Callable[P, R]:
    """Decorator: lock + autorelease pool + access check for public methods."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self = args[0]
        assert isinstance(self, EventKitStore)
        with self._lock, objc.autorelease_pool():
            self.ensure_access()
            return fn(*args, **kwargs)

    return wrapper


class EventKitStore:
    # EventKit gives apps no way to answer an invitation.
    supports_replies = False

    def __init__(self, tz: tzinfo, access_timeout: float = 30.0) -> None:
        self.tz = tz
        self.access_timeout = access_timeout
        self._lock = threading.RLock()
        self._store = EK.EKEventStore.alloc().init()
        self._has_access = False
        self._has_reminder_access = False

    # ------------------------------------------------------------ permission
    @staticmethod
    def authorization_status(entity: str = "events") -> str:
        kind = _ENTITY_REMINDER if entity == "reminders" else _ENTITY_EVENT
        status = EK.EKEventStore.authorizationStatusForEntityType_(kind)
        return {
            _AUTH_NOT_DETERMINED: "not_determined",
            _AUTH_RESTRICTED: "restricted",
            _AUTH_DENIED: "denied",
            _AUTH_FULL: "full_access",
            _AUTH_WRITE_ONLY: "write_only",
        }.get(status, f"unknown({status})")

    def ensure_access(self) -> None:
        """Make sure we have *full* calendar access, prompting once if needed.

        macOS shows the consent dialog on behalf of the app that launched this
        process (e.g. Claude Desktop or Terminal). If no GUI session exists
        the prompt can never appear, so we time out instead of hanging.
        """
        if self._has_access:
            return
        status = self.authorization_status()
        if status == "full_access":
            self._has_access = True
            return
        if status == "write_only":
            raise PermissionDeniedError(
                "Calendar access is 'Add Events Only'. Enable Full Access under "
                "System Settings > Privacy & Security > Calendars."
            )
        if status in ("denied", "restricted"):
            raise PermissionDeniedError(
                "Calendar access was denied. Enable it for the app running this server under "
                "System Settings > Privacy & Security > Calendars, then restart that app."
            )

        done = threading.Event()
        result: dict[str, Any] = {}

        def handler(granted: bool, error: Any) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        if hasattr(self._store, "requestFullAccessToEventsWithCompletion_"):  # macOS 14+
            self._store.requestFullAccessToEventsWithCompletion_(handler)
        else:  # macOS 13 and older
            self._store.requestAccessToEntityType_completion_(_ENTITY_EVENT, handler)

        if not done.wait(self.access_timeout):
            raise PermissionDeniedError(
                "Timed out waiting for the macOS calendar permission prompt. Run "
                "`apple-calendar-mcp doctor` in Terminal, or grant access under "
                "System Settings > Privacy & Security > Calendars."
            )
        if not result.get("granted"):
            err = result.get("error")
            detail = f" ({err.localizedDescription()})" if err is not None else ""
            if self.authorization_status() == "not_determined":
                # Refused without ever asking: the app that started us can't
                # show the calendar prompt (it lacks the usage description).
                raise PermissionDeniedError(
                    f"macOS refused calendar access without showing a prompt{detail}. "
                    + (
                        _helper_unusable()
                        or "The app that started this server cannot ask for calendar access. Run "
                        "`apple-calendar-mcp install-app`, then `apple-calendar-mcp doctor`."
                    )
                )
            raise PermissionDeniedError(f"Calendar access was not granted{detail}.")
        # A store created before the grant may hold an empty snapshot.
        self._store.reset()
        self._has_access = True

    # ------------------------------------------------------------ conversion
    def _ns(self, dt: datetime) -> Any:
        return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())

    def _py(self, ns: Any) -> datetime:
        return datetime.fromtimestamp(ns.timeIntervalSince1970(), tz=self.tz)

    @staticmethod
    def _hex_color(cal: Any) -> str | None:
        try:
            from AppKit import NSColorSpace

            c = cal.color().colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
            r, g, b = (round(x * 255) for x in (c.redComponent(), c.greenComponent(), c.blueComponent()))
            return f"#{r:02X}{g:02X}{b:02X}"
        except Exception:  # colour is cosmetic; never fail a request over it
            return None

    def _to_calendar(self, cal: Any, default_id: str | None) -> Calendar:
        source = cal.source()
        return Calendar(
            id=str(cal.calendarIdentifier()),
            title=str(cal.title()),
            source=str(source.title()) if source is not None else None,
            color=self._hex_color(cal),
            writable=bool(cal.allowsContentModifications()),
            is_default=str(cal.calendarIdentifier()) == default_id,
        )

    def _to_recurrence(self, ev: Any) -> Recurrence | None:
        rules = ev.recurrenceRules() or []
        if not rules:
            return None
        rule = rules[0]  # Calendar.app only ever creates one rule per event
        days = None
        if rule.daysOfTheWeek():
            days = [
                _EK_TO_WEEKDAY[d.dayOfTheWeek()]
                for d in rule.daysOfTheWeek()
                if d.dayOfTheWeek() in _EK_TO_WEEKDAY
            ] or None
        frequency = _FREQ_REV.get(int(rule.frequency()), "daily")
        count = until = None
        end = rule.recurrenceEnd()
        if end is not None:
            if end.occurrenceCount():
                count = int(end.occurrenceCount())
            elif end.endDate() is not None:
                until = self._py(end.endDate()).date()
        try:
            return Recurrence(
                frequency=frequency,  # type: ignore[arg-type]
                interval=max(int(rule.interval()), 1),
                days_of_week=days if frequency == "weekly" else None,
                count=count,
                until=until,
            )
        except ValueError:
            log.debug("recurrence rule outside supported subset: %s", rule)
            return Recurrence(frequency=frequency, interval=max(int(rule.interval()), 1))  # type: ignore[arg-type]

    def _to_event(self, ev: Any) -> Event:
        start = self._py(ev.startDate())
        end = self._py(ev.endDate())
        all_day = bool(ev.isAllDay())
        if all_day:
            # EventKit reports all-day ends as 23:59:59 on the last day; we use
            # an exclusive midnight end like iCalendar's DTEND.
            last: date = end.date() if end.time() != time.min else end.date() - ONE_DAY
            start = datetime.combine(start.date(), time.min, tzinfo=self.tz)
            end = datetime.combine(max(last, start.date()) + ONE_DAY, time.min, tzinfo=self.tz)

        occurrence = ev.occurrenceDate() or ev.startDate()
        participants = list(ev.attendees() or [])
        attendees = [
            Attendee(
                name=str(p.name()) if p.name() else None,
                email=_mailto(p.URL()),
                status=_PARTICIPANT.get(int(p.participantStatus()), "unknown"),
            )
            for p in participants
        ]
        me = next((i for i, p in enumerate(participants) if p.isCurrentUser()), None)
        organizer = ev.organizer()
        alarms = [
            round(-a.relativeOffset() / 60)
            for a in (ev.alarms() or [])
            if a.absoluteDate() is None and a.relativeOffset() <= 0
        ]
        cal = ev.calendar()
        return Event(
            id=make_event_id(str(ev.eventIdentifier()), occurrence.timeIntervalSince1970()),
            calendar_id=str(cal.calendarIdentifier()),
            calendar_title=str(cal.title()),
            title=str(ev.title() or "(No title)"),
            start=start,
            end=end,
            all_day=all_day,
            location=str(ev.location()) if ev.location() else None,
            notes=str(ev.notes()) if ev.notes() else None,
            url=str(ev.URL().absoluteString()) if ev.URL() is not None else None,
            is_recurring=bool(ev.hasRecurrenceRules()),
            recurrence=self._to_recurrence(ev),
            alarms_minutes_before=sorted(set(alarms), reverse=True),
            availability=_AVAILABILITY.get(int(ev.availability()), "busy"),
            status=_STATUS.get(int(ev.status()), "none"),
            organizer=str(organizer.name()) if organizer is not None and organizer.name() else None,
            attendees=attendees,
            my_status=attendees[me].status if me is not None else None,
        )

    def _make_rule(self, rec: Recurrence) -> Any:
        end = None
        if rec.count is not None:
            end = EK.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(rec.count)
        elif rec.until is not None:
            last_moment = datetime.combine(rec.until, time(23, 59, 59), tzinfo=self.tz)
            end = EK.EKRecurrenceEnd.recurrenceEndWithEndDate_(self._ns(last_moment))
        freq = _FREQ[rec.frequency]
        if rec.days_of_week:
            days = [EK.EKRecurrenceDayOfWeek.dayOfWeek_(_WEEKDAY_TO_EK[d]) for d in rec.days_of_week]
            return EK.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(  # noqa: E501
                freq, rec.interval, days, None, None, None, None, None, end
            )
        return EK.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(freq, rec.interval, end)

    # ------------------------------------------------------------ lookup
    def _calendar(self, calendar_id: str | None, *, for_write: bool) -> Any:
        if calendar_id is None:
            cal = self._store.defaultCalendarForNewEvents()
            if cal is None:
                raise InvalidRequestError("No default calendar is set; pass calendar_id.")
        else:
            cal = self._store.calendarWithIdentifier_(calendar_id)
            if cal is None:
                raise NotFoundError(f"No calendar with id {calendar_id!r}.")
        if for_write and not cal.allowsContentModifications():
            raise ReadOnlyCalendarError(f"Calendar {cal.title()!r} is read-only.")
        return cal

    def _find(self, event_id: str) -> Any:
        """Resolve an occurrence id to the concrete EKEvent occurrence."""
        ident, ts = split_event_id(event_id)
        base = self._store.eventWithIdentifier_(ident)  # first occurrence for series
        if base is None:
            raise NotFoundError(f"No event with id {event_id!r}.")
        if ts is None or not base.hasRecurrenceRules():
            return base
        if abs(base.occurrenceDate().timeIntervalSince1970() - ts) < 1:
            return base
        # occurrenceDate is the *original* start; a detached occurrence may have
        # been moved, so search a widening window around it.
        original = datetime.fromtimestamp(ts, tz=self.tz)
        for radius in (timedelta(days=2), timedelta(days=60), timedelta(days=366)):
            pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                self._ns(original - radius), self._ns(original + radius), [base.calendar()]
            )
            for ev in self._store.eventsMatchingPredicate_(pred) or []:
                if (
                    ev.eventIdentifier() == ident
                    and abs(ev.occurrenceDate().timeIntervalSince1970() - ts) < 1
                ):
                    return ev
        raise NotFoundError(f"Occurrence {event_id!r} not found (deleted or moved).")

    def _save(self, ev: Any, span: Span) -> None:
        ok, err = self._store.saveEvent_span_commit_error_(ev, _SPAN[span], True, None)
        if not ok:
            raise BackendError(f"Saving failed: {err.localizedDescription() if err else 'unknown error'}")

    # ------------------------------------------------------------ protocol
    @_eventkit_call
    def list_calendars(self) -> list[Calendar]:
        default = self._store.defaultCalendarForNewEvents()
        default_id = str(default.calendarIdentifier()) if default is not None else None
        cals = self._store.calendarsForEntityType_(_ENTITY_EVENT) or []
        return sorted(
            (self._to_calendar(c, default_id) for c in cals), key=lambda c: (c.source or "", c.title)
        )

    @_eventkit_call
    def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        self._store.refreshSourcesIfNecessary()
        calendars = None
        if calendar_ids:
            calendars = [self._calendar(cid, for_write=False) for cid in calendar_ids]
        pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            self._ns(start), self._ns(end), calendars
        )
        events = [self._to_event(ev) for ev in (self._store.eventsMatchingPredicate_(pred) or [])]
        events.sort(key=lambda e: (e.start, e.title))
        return events

    @_eventkit_call
    def get_event(self, event_id: str) -> Event:
        return self._to_event(self._find(event_id))

    @_eventkit_call
    def create_event(self, draft: EventDraft) -> Event:
        cal = self._calendar(draft.calendar_id, for_write=True)
        ev = EK.EKEvent.eventWithEventStore_(self._store)
        ev.setCalendar_(cal)
        ev.setTitle_(draft.title)
        self._set_times(ev, draft.start, draft.end, draft.all_day)
        if draft.location:
            ev.setLocation_(draft.location)
        if draft.notes:
            ev.setNotes_(draft.notes)
        if draft.url:
            ev.setURL_(NSURL.URLWithString_(draft.url))
        ev.setAlarms_([EK.EKAlarm.alarmWithRelativeOffset_(-60 * m) for m in draft.alarms_minutes_before])
        if draft.recurrence is not None:
            ev.setRecurrenceRules_([self._make_rule(draft.recurrence)])
        if draft.availability is not None:
            ev.setAvailability_(_AVAILABILITY_REV[draft.availability])
        self._save(ev, Span.THIS)
        return self._to_event(ev)

    def _set_times(self, ev: Any, start: datetime, end: datetime, all_day: bool) -> None:
        ev.setAllDay_(all_day)
        ev.setStartDate_(self._ns(start))
        # For all-day events EventKit expects an end *inside* the last day.
        ev.setEndDate_(self._ns(end - timedelta(seconds=1) if all_day and end > start else end))

    @_eventkit_call
    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        target = self._find(event_id)
        self._calendar(str(target.calendar().calendarIdentifier()), for_write=True)
        ev, ek_span = target, span

        if target.hasRecurrenceRules() and span is Span.ALL:
            # EventKit has no "whole series" span: edit the first occurrence with
            # FUTURE, translating the target's time change into the same shift.
            ident, _ = split_event_id(event_id)
            ev, ek_span = self._store.eventWithIdentifier_(ident), Span.FUTURE
            if patch.start is not None or patch.end is not None:
                current = self._to_event(target)
                head = self._to_event(ev)
                shift = (patch.start or current.start) - current.start
                new_len = (patch.end or current.end) - (patch.start or current.start)
                patch = patch.model_copy(
                    update={"start": head.start + shift, "end": head.start + shift + new_len}
                )
        elif not target.hasRecurrenceRules():
            ek_span = Span.THIS  # spans are meaningless for single events
        elif span is Span.THIS and patch.calendar_id is not None:
            raise InvalidRequestError("A single occurrence can't move to another calendar; use span 'all'.")

        if patch.title is not None:
            ev.setTitle_(patch.title)
        if patch.start is not None or patch.end is not None or patch.all_day is not None:
            cur = self._to_event(ev)
            self._set_times(
                ev,
                patch.start or cur.start,
                patch.end or cur.end,
                cur.all_day if patch.all_day is None else patch.all_day,
            )
        if patch.location is not None:
            ev.setLocation_(patch.location or None)
        if patch.notes is not None:
            ev.setNotes_(patch.notes or None)
        if patch.url is not None:
            ev.setURL_(NSURL.URLWithString_(patch.url) if patch.url else None)
        if patch.alarms_minutes_before is not None:
            ev.setAlarms_([EK.EKAlarm.alarmWithRelativeOffset_(-60 * m) for m in patch.alarms_minutes_before])
        if patch.availability is not None:
            ev.setAvailability_(_AVAILABILITY_REV[patch.availability])
        if patch.calendar_id is not None:
            ev.setCalendar_(self._calendar(patch.calendar_id, for_write=True))
        if patch.remove_recurrence:
            ev.setRecurrenceRules_(None)
        elif patch.recurrence is not None:
            ev.setRecurrenceRules_([self._make_rule(patch.recurrence)])

        self._save(ev, ek_span)
        return self._to_event(ev)

    @_eventkit_call
    def delete_event(self, event_id: str, span: Span) -> Event:
        ev = self._find(event_id)
        self._calendar(str(ev.calendar().calendarIdentifier()), for_write=True)
        before = self._to_event(ev)
        ek_span = span
        if span is Span.ALL:
            ident, _ = split_event_id(event_id)
            ev, ek_span = self._store.eventWithIdentifier_(ident), Span.FUTURE
        ok, err = self._store.removeEvent_span_commit_error_(ev, _SPAN[ek_span], True, None)
        if not ok:
            raise BackendError(f"Deleting failed: {err.localizedDescription() if err else 'unknown error'}")
        return before

    def respond_to_invitation(self, event_id: str, reply: Reply, span: Span) -> Event:
        raise InvalidRequestError(
            "macOS doesn't let apps answer invitations. Answer it in the Calendar app, "
            "or use the iCloud (CalDAV) backend."
        )

    # ------------------------------------------------------------ reminders
    def ensure_reminder_access(self) -> None:
        """Full access to Reminders, asked for separately and only when a reminder tool is used."""
        if self._has_reminder_access:
            return
        status = self.authorization_status("reminders")
        if status == "full_access":
            self._has_reminder_access = True
            return
        if status in ("denied", "restricted", "write_only"):
            raise PermissionDeniedError(
                "Reminders access is off. Enable Full Access for the app running this server under "
                "System Settings > Privacy & Security > Reminders, then restart that app."
            )
        done = threading.Event()
        result: dict[str, Any] = {}

        def handler(granted: bool, error: Any) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        if hasattr(self._store, "requestFullAccessToRemindersWithCompletion_"):  # macOS 14+
            self._store.requestFullAccessToRemindersWithCompletion_(handler)
        else:
            self._store.requestAccessToEntityType_completion_(_ENTITY_REMINDER, handler)
        if not done.wait(self.access_timeout):
            raise PermissionDeniedError(
                "Timed out waiting for the macOS Reminders permission prompt. Run "
                "`apple-calendar-mcp doctor` in Terminal, or grant access under "
                "System Settings > Privacy & Security > Reminders."
            )
        if not result.get("granted"):
            err = result.get("error")
            detail = f" ({err.localizedDescription()})" if err is not None else ""
            if self.authorization_status("reminders") == "not_determined":
                raise PermissionDeniedError(
                    f"macOS refused Reminders access without showing a prompt{detail}. "
                    + (
                        _helper_unusable()
                        or "Reinstall the helper app so it can ask (`apple-calendar-mcp install-app`), "
                        "then run `apple-calendar-mcp doctor`."
                    )
                )
            raise PermissionDeniedError(f"Reminders access was not granted{detail}.")
        self._store.reset()
        self._has_reminder_access = True

    def _components(self, when: datetime, all_day: bool) -> Any:
        local = when.astimezone(self.tz)
        comps = NSDateComponents.alloc().init()
        comps.setYear_(local.year)
        comps.setMonth_(local.month)
        comps.setDay_(local.day)
        if not all_day:
            comps.setHour_(local.hour)
            comps.setMinute_(local.minute)
            comps.setTimeZone_(NSTimeZone.timeZoneWithName_(tz_name(self.tz)))
        return comps

    def _from_components(self, comps: Any) -> tuple[datetime | None, bool]:
        if comps is None or comps.year() in (None, _UNDEFINED):
            return None, False
        day = date(int(comps.year()), int(comps.month()), int(comps.day()))
        hour = comps.hour()
        if hour in (None, _UNDEFINED):
            return datetime.combine(day, time.min, tzinfo=self.tz), True
        minute = comps.minute()
        zone_name = comps.timeZone().name() if comps.timeZone() is not None else None
        zone: tzinfo = self.tz
        if zone_name:
            with contextlib.suppress(KeyError, ValueError):
                zone = ZoneInfo(str(zone_name))
        moment = datetime.combine(
            day, time(int(hour), 0 if minute in (None, _UNDEFINED) else int(minute)), tzinfo=zone
        )
        return moment.astimezone(self.tz), False

    def _to_list(self, cal: Any, default_id: str | None) -> ReminderList:
        c = self._to_calendar(cal, default_id)
        return ReminderList(
            id=c.id,
            title=c.title,
            source=c.source,
            color=c.color,
            writable=c.writable,
            is_default=c.is_default,
        )

    def _to_reminder(self, rem: Any) -> Reminder:
        due, all_day = self._from_components(rem.dueDateComponents())
        done_at = rem.completionDate()
        cal = rem.calendar()
        return Reminder(
            id=str(rem.calendarItemIdentifier()),
            list_id=str(cal.calendarIdentifier()),
            list_title=str(cal.title()),
            title=str(rem.title() or "(No title)"),
            notes=str(rem.notes()) if rem.notes() else None,
            due=due,
            due_all_day=all_day,
            priority=_priority(int(rem.priority())),
            completed=bool(rem.isCompleted()),
            completed_at=self._py(done_at).replace(microsecond=0) if done_at is not None else None,
            is_recurring=bool(rem.hasRecurrenceRules()),
        )

    def _reminder_list(self, list_id: str | None, *, for_write: bool) -> Any:
        if list_id is None:
            cal = self._store.defaultCalendarForNewReminders()
            if cal is None:
                raise InvalidRequestError("No default reminder list is set; pass list_id.")
        else:
            cal = self._store.calendarWithIdentifier_(list_id)
            if cal is None or list_id not in {
                str(c.calendarIdentifier())
                for c in self._store.calendarsForEntityType_(_ENTITY_REMINDER) or []
            }:
                raise NotFoundError(f"No reminder list with id {list_id!r}.")
        if for_write and not cal.allowsContentModifications():
            raise ReadOnlyCalendarError(f"Reminder list {cal.title()!r} is read-only.")
        return cal

    def _find_reminder(self, reminder_id: str) -> Any:
        item = self._store.calendarItemWithIdentifier_(reminder_id)
        if item is None or not hasattr(item, "isCompleted"):
            raise NotFoundError(f"No reminder with id {reminder_id!r}.")
        return item

    def _fetch(self, predicate: Any) -> list[Any]:
        done = threading.Event()
        found: list[Any] = []

        def handler(reminders: Any) -> None:
            found.extend(reminders or [])
            done.set()

        self._store.fetchRemindersMatchingPredicate_completion_(predicate, handler)
        if not done.wait(30):
            raise BackendError("Reading reminders timed out.")
        return found

    def _set_due(self, rem: Any, due: datetime | None, all_day: bool) -> None:
        rem.setDueDateComponents_(self._components(due, all_day) if due is not None else None)
        # Like the Reminders app: a timed due gets an alert at that time.
        rem.setAlarms_([EK.EKAlarm.alarmWithAbsoluteDate_(self._ns(due))] if due and not all_day else [])

    def _save_reminder(self, rem: Any) -> None:
        ok, err = self._store.saveReminder_commit_error_(rem, True, None)
        if not ok:
            raise BackendError(f"Saving failed: {err.localizedDescription() if err else 'unknown error'}")

    def list_reminder_lists(self) -> list[ReminderList]:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            default = self._store.defaultCalendarForNewReminders()
            default_id = str(default.calendarIdentifier()) if default is not None else None
            cals = self._store.calendarsForEntityType_(_ENTITY_REMINDER) or []
            return sorted(
                (self._to_list(c, default_id) for c in cals), key=lambda x: (x.source or "", x.title)
            )

    def list_reminders(self, list_ids: list[str] | None, *, completed: bool) -> list[Reminder]:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            cals = [self._reminder_list(lid, for_write=False) for lid in list_ids] if list_ids else None
            if completed:
                pred = self._store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
                    None, None, cals
                )
            else:
                pred = self._store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                    None, None, cals
                )
            return [self._to_reminder(r) for r in self._fetch(pred)]

    def get_reminder(self, reminder_id: str) -> Reminder:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            return self._to_reminder(self._find_reminder(reminder_id))

    def create_reminder(self, draft: ReminderDraft) -> Reminder:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            cal = self._reminder_list(draft.list_id, for_write=True)
            rem = EK.EKReminder.reminderWithEventStore_(self._store)
            rem.setCalendar_(cal)
            rem.setTitle_(draft.title)
            if draft.notes:
                rem.setNotes_(draft.notes)
            rem.setPriority_(_PRIORITY_TO_EK[draft.priority])
            self._set_due(rem, draft.due, draft.due_all_day)
            self._save_reminder(rem)
            return self._to_reminder(rem)

    def update_reminder(self, reminder_id: str, patch: ReminderPatch) -> Reminder:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            rem = self._find_reminder(reminder_id)
            self._reminder_list(str(rem.calendar().calendarIdentifier()), for_write=True)
            if patch.title is not None:
                rem.setTitle_(patch.title)
            if patch.notes is not None:
                rem.setNotes_(patch.notes or None)
            if patch.clear_due:
                self._set_due(rem, None, False)
            elif patch.due is not None:
                self._set_due(rem, patch.due, patch.due_all_day)
            if patch.priority is not None:
                rem.setPriority_(_PRIORITY_TO_EK[patch.priority])
            if patch.completed is not None:
                rem.setCompleted_(patch.completed)
            if patch.list_id is not None:
                rem.setCalendar_(self._reminder_list(patch.list_id, for_write=True))
            self._save_reminder(rem)
            return self._to_reminder(rem)

    def delete_reminder(self, reminder_id: str) -> Reminder:
        with self._lock, objc.autorelease_pool():
            self.ensure_reminder_access()
            rem = self._find_reminder(reminder_id)
            self._reminder_list(str(rem.calendar().calendarIdentifier()), for_write=True)
            before = self._to_reminder(rem)
            ok, err = self._store.removeReminder_commit_error_(rem, True, None)
            if not ok:
                raise BackendError(
                    f"Deleting failed: {err.localizedDescription() if err else 'unknown error'}"
                )
            return before


def _mailto(url: Any) -> str | None:
    if url is None:
        return None
    s = str(url.absoluteString())
    return s[len("mailto:") :] if s.lower().startswith("mailto:") else None
