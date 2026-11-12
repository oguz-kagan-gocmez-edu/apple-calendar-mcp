"""A tiny pure-Python stand-in for the PyObjC EventKit/Foundation/objc modules.

It implements only the selectors ``eventkit_store`` uses, with the same names
and argument conventions as PyObjC (trailing underscores, error out-params
returned as tuples). This lets the EventKit backend's *mapping logic* run on
Linux CI. It does not prove EventKit itself behaves this way; the manual
macOS checklist in the README covers that.
"""

from __future__ import annotations

import contextlib
import itertools
import types
from collections.abc import Callable
from typing import Any

_ids = itertools.count(1)


# ------------------------------------------------------------------ Foundation
class NSDate:
    def __init__(self, ts: float) -> None:
        self._ts = ts

    @classmethod
    def dateWithTimeIntervalSince1970_(cls, ts: float) -> NSDate:
        return cls(ts)

    def timeIntervalSince1970(self) -> float:
        return self._ts


class NSURL:
    def __init__(self, s: str) -> None:
        self._s = s

    @classmethod
    def URLWithString_(cls, s: str) -> NSURL:
        return cls(s)

    def absoluteString(self) -> str:
        return self._s


UNDEFINED = 2**63 - 1


class NSTimeZone:
    def __init__(self, name: str) -> None:
        self._name = name

    @classmethod
    def timeZoneWithName_(cls, name: str) -> NSTimeZone:
        return cls(name)

    def name(self) -> str:
        return self._name


class NSDateComponents:
    @classmethod
    def alloc(cls) -> NSDateComponents:
        return cls()

    def init(self) -> NSDateComponents:
        self._v: dict[str, Any] = {}
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("set") and name.endswith("_"):
            key = name[3].lower() + name[4:-1]
            return lambda value: self._v.__setitem__(key, value)
        if name in ("year", "month", "day", "hour", "minute"):
            return lambda: self._v.get(name, UNDEFINED)
        if name == "timeZone":
            return lambda: self._v.get("timeZone")
        raise AttributeError(name)


class NSError:
    def __init__(self, msg: str) -> None:
        self._msg = msg

    def localizedDescription(self) -> str:
        return self._msg


# ------------------------------------------------------------------ EventKit
class EKSource:
    def __init__(self, title: str) -> None:
        self._title = title

    def title(self) -> str:
        return self._title


class EKCalendar:
    def __init__(self, ident: str, title: str, writable: bool = True) -> None:
        self._id, self._title, self._writable = ident, title, writable

    def calendarIdentifier(self) -> str:
        return self._id

    def title(self) -> str:
        return self._title

    def source(self) -> EKSource:
        return EKSource("iCloud")

    def allowsContentModifications(self) -> bool:
        return self._writable

    def color(self) -> None:
        return None  # _hex_color must tolerate this


class EKAlarm:
    def __init__(self, offset: float, absolute: NSDate | None = None) -> None:
        self._offset, self._absolute = offset, absolute

    @classmethod
    def alarmWithRelativeOffset_(cls, offset: float) -> EKAlarm:
        return cls(offset)

    @classmethod
    def alarmWithAbsoluteDate_(cls, d: NSDate) -> EKAlarm:
        return cls(0, d)

    def relativeOffset(self) -> float:
        return self._offset

    def absoluteDate(self) -> NSDate | None:
        return self._absolute


class EKRecurrenceEnd:
    def __init__(self, count: int = 0, end: NSDate | None = None) -> None:
        self._count, self._end = count, end

    @classmethod
    def recurrenceEndWithOccurrenceCount_(cls, n: int) -> EKRecurrenceEnd:
        return cls(count=n)

    @classmethod
    def recurrenceEndWithEndDate_(cls, d: NSDate) -> EKRecurrenceEnd:
        return cls(end=d)

    def occurrenceCount(self) -> int:
        return self._count

    def endDate(self) -> NSDate | None:
        return self._end


class EKRecurrenceDayOfWeek:
    def __init__(self, day: int) -> None:
        self._day = day

    @classmethod
    def dayOfWeek_(cls, day: int) -> EKRecurrenceDayOfWeek:
        return cls(day)

    def dayOfTheWeek(self) -> int:
        return self._day


class EKRecurrenceRule:
    @classmethod
    def alloc(cls) -> EKRecurrenceRule:
        return cls()

    def initRecurrenceWithFrequency_interval_end_(self, f: int, i: int, end: Any) -> EKRecurrenceRule:
        self._f, self._i, self._days, self._end = f, i, None, end
        return self

    def initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(  # noqa: E501
        self, f: int, i: int, days: Any, *rest: Any
    ) -> EKRecurrenceRule:
        self._f, self._i, self._days, self._end = f, i, days, rest[-1]
        return self

    def frequency(self) -> int:
        return self._f

    def interval(self) -> int:
        return self._i

    def daysOfTheWeek(self) -> Any:
        return self._days

    def recurrenceEnd(self) -> Any:
        return self._end


class EKParticipant:
    def __init__(self, name: str, email: str, status: int, *, me: bool = False) -> None:
        self._name, self._url, self._status, self._me = name, NSURL(f"mailto:{email}"), status, me

    def name(self) -> str:
        return self._name

    def URL(self) -> NSURL:
        return self._url

    def participantStatus(self) -> int:
        return self._status

    def isCurrentUser(self) -> bool:
        return self._me


class EKEvent:
    def __init__(self, store: EKEventStore) -> None:
        self._store = store
        self._ident: str | None = None
        self._title = ""
        self._start = self._end = NSDate(0)
        self._all_day = False
        self._location = self._notes = None
        self._url: NSURL | None = None
        self._alarms: list[EKAlarm] = []
        self._rules: list[EKRecurrenceRule] | None = None
        self._calendar: EKCalendar | None = None
        self._availability = 0
        self._attendees: list[EKParticipant] | None = None

    @classmethod
    def eventWithEventStore_(cls, store: EKEventStore) -> EKEvent:
        return cls(store)

    # getters
    def eventIdentifier(self) -> str | None:
        return self._ident

    def title(self) -> str:
        return self._title

    def startDate(self) -> NSDate:
        return self._start

    def endDate(self) -> NSDate:
        return self._end

    def occurrenceDate(self) -> NSDate:
        return self._start

    def isAllDay(self) -> bool:
        return self._all_day

    def location(self) -> Any:
        return self._location

    def notes(self) -> Any:
        return self._notes

    def URL(self) -> NSURL | None:
        return self._url

    def alarms(self) -> list[EKAlarm]:
        return self._alarms

    def recurrenceRules(self) -> Any:
        return self._rules

    def hasRecurrenceRules(self) -> bool:
        return bool(self._rules)

    def calendar(self) -> EKCalendar | None:
        return self._calendar

    def availability(self) -> int:
        return self._availability

    def status(self) -> int:
        return 1

    def organizer(self) -> None:
        return None

    def attendees(self) -> list[EKParticipant] | None:
        return self._attendees

    # setters
    def setTitle_(self, v: str) -> None:
        self._title = v

    def setStartDate_(self, v: NSDate) -> None:
        self._start = v

    def setEndDate_(self, v: NSDate) -> None:
        self._end = v

    def setAllDay_(self, v: bool) -> None:
        self._all_day = v

    def setLocation_(self, v: Any) -> None:
        self._location = v

    def setNotes_(self, v: Any) -> None:
        self._notes = v

    def setURL_(self, v: Any) -> None:
        self._url = v

    def setAlarms_(self, v: Any) -> None:
        self._alarms = list(v or [])

    def setRecurrenceRules_(self, v: Any) -> None:
        self._rules = v

    def setCalendar_(self, v: EKCalendar) -> None:
        self._calendar = v

    def setAvailability_(self, v: int) -> None:
        self._availability = v


class EKReminder:
    def __init__(self, store: EKEventStore) -> None:
        self._store = store
        self._ident: str | None = None
        self._title = ""
        self._notes: Any = None
        self._due: NSDateComponents | None = None
        self._priority = 0
        self._completed = False
        self._completion: NSDate | None = None
        self._calendar: EKCalendar | None = None
        self.alarms: list[EKAlarm] = []

    @classmethod
    def reminderWithEventStore_(cls, store: EKEventStore) -> EKReminder:
        return cls(store)

    def calendarItemIdentifier(self) -> str | None:
        return self._ident

    def title(self) -> str:
        return self._title

    def notes(self) -> Any:
        return self._notes

    def dueDateComponents(self) -> NSDateComponents | None:
        return self._due

    def priority(self) -> int:
        return self._priority

    def isCompleted(self) -> bool:
        return self._completed

    def completionDate(self) -> NSDate | None:
        return self._completion

    def calendar(self) -> EKCalendar | None:
        return self._calendar

    def hasRecurrenceRules(self) -> bool:
        return False

    def setTitle_(self, v: str) -> None:
        self._title = v

    def setNotes_(self, v: Any) -> None:
        self._notes = v

    def setDueDateComponents_(self, v: Any) -> None:
        self._due = v

    def setPriority_(self, v: int) -> None:
        self._priority = v

    def setCompleted_(self, v: bool) -> None:
        self._completed = v
        self._completion = NSDate(1_800_000_000) if v else None

    def setCalendar_(self, v: EKCalendar) -> None:
        self._calendar = v

    def setAlarms_(self, v: Any) -> None:
        self.alarms = list(v or [])


class EKEventStore:
    # class-level knobs the tests flip
    status = 0  # not determined
    grant_on_request: bool | None = True  # None = never answer (simulates no GUI)
    requests = 0
    prompt_shown = True  # False = refused without a prompt (host app can't ask)
    reminder_status = 0
    reminder_grant: bool | None = True
    reminder_requests = 0

    def __init__(self) -> None:
        self.calendars = [EKCalendar("CAL-HOME", "Home"), EKCalendar("CAL-HOL", "Holidays", writable=False)]
        self.lists = [
            EKCalendar("LIST-TODO", "Reminders"),
            EKCalendar("LIST-SHARED", "Shared", writable=False),
        ]
        self.reminders: dict[str, EKReminder] = {}
        self.events: dict[str, EKEvent] = {}
        self.saves: list[tuple[str, int]] = []

    @classmethod
    def alloc(cls) -> EKEventStore:
        return cls.__new__(cls)

    def init(self) -> EKEventStore:
        self.__init__()  # type: ignore[misc]
        return self

    @classmethod
    def authorizationStatusForEntityType_(cls, entity: int) -> int:
        return cls.reminder_status if entity == 1 else cls.status

    def requestFullAccessToRemindersWithCompletion_(self, handler: Callable[[bool, Any], None]) -> None:
        type(self).reminder_requests += 1
        if self.reminder_grant is None:
            return
        type(self).reminder_status = 3 if self.reminder_grant else 2
        handler(self.reminder_grant, None if self.reminder_grant else NSError("User denied"))

    def requestFullAccessToEventsWithCompletion_(self, handler: Callable[[bool, Any], None]) -> None:
        type(self).requests += 1
        if self.grant_on_request is None:
            return
        if self.prompt_shown:
            type(self).status = 3 if self.grant_on_request else 2
        handler(self.grant_on_request, None if self.grant_on_request else NSError("User denied"))

    def reset(self) -> None:
        pass

    def refreshSourcesIfNecessary(self) -> None:
        pass

    def calendarsForEntityType_(self, entity: int) -> list[EKCalendar]:
        return self.lists if entity == 1 else self.calendars

    def calendarWithIdentifier_(self, ident: str) -> EKCalendar | None:
        return next((c for c in self.calendars + self.lists if c.calendarIdentifier() == ident), None)

    def defaultCalendarForNewReminders(self) -> EKCalendar:
        return self.lists[0]

    def calendarItemWithIdentifier_(self, ident: str) -> Any:
        return self.reminders.get(ident) or self.events.get(ident)

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        self, _s: Any, _e: Any, cals: Any
    ) -> Any:
        return (False, cals)

    def predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
        self, _s: Any, _e: Any, cals: Any
    ) -> Any:
        return (True, cals)

    def fetchRemindersMatchingPredicate_completion_(self, pred: Any, handler: Callable[[Any], None]) -> str:
        done, cals = pred
        ids = {c.calendarIdentifier() for c in cals} if cals else None
        handler(
            [
                r
                for r in self.reminders.values()
                if r._completed == done and (ids is None or r._calendar.calendarIdentifier() in ids)  # type: ignore[union-attr]
            ]
        )
        return "fetch"

    def saveReminder_commit_error_(self, rem: EKReminder, commit: bool, _err: None) -> tuple[bool, Any]:
        if rem._ident is None:
            rem._ident = f"REM{next(_ids)}"
        self.reminders[rem._ident] = rem
        return True, None

    def removeReminder_commit_error_(self, rem: EKReminder, commit: bool, _err: None) -> tuple[bool, Any]:
        self.reminders.pop(rem._ident or "", None)
        return True, None

    def defaultCalendarForNewEvents(self) -> EKCalendar:
        return self.calendars[0]

    def predicateForEventsWithStartDate_endDate_calendars_(self, s: NSDate, e: NSDate, cals: Any) -> Any:
        return (s.timeIntervalSince1970(), e.timeIntervalSince1970(), cals)

    def eventsMatchingPredicate_(self, pred: Any) -> list[EKEvent]:
        s, e, cals = pred
        ids = {c.calendarIdentifier() for c in cals} if cals else None
        return [
            ev
            for ev in self.events.values()
            if ev._start.timeIntervalSince1970() < e
            and ev._end.timeIntervalSince1970() > s
            and (ids is None or ev._calendar.calendarIdentifier() in ids)  # type: ignore[union-attr]
        ]

    def eventWithIdentifier_(self, ident: str) -> EKEvent | None:
        return self.events.get(ident)

    def saveEvent_span_commit_error_(
        self, ev: EKEvent, span: int, commit: bool, _err: None
    ) -> tuple[bool, Any]:
        if ev._end.timeIntervalSince1970() < ev._start.timeIntervalSince1970():
            return False, NSError("The start date must be before the end date.")
        if ev._ident is None:
            ev._ident = f"EV{next(_ids)}:SERIES"
        self.events[ev._ident] = ev
        self.saves.append((ev._ident, span))
        return True, None

    def removeEvent_span_commit_error_(
        self, ev: EKEvent, span: int, commit: bool, _err: None
    ) -> tuple[bool, Any]:
        self.events.pop(ev._ident or "", None)
        return True, None


def install(monkeypatch: Any) -> None:
    """Register the fakes as EventKit / Foundation / objc and pretend to be macOS."""
    ek = types.ModuleType("EventKit")
    for name, obj in {
        "EKEventStore": EKEventStore,
        "EKEvent": EKEvent,
        "EKReminder": EKReminder,
        "EKAlarm": EKAlarm,
        "EKRecurrenceRule": EKRecurrenceRule,
        "EKRecurrenceEnd": EKRecurrenceEnd,
        "EKRecurrenceDayOfWeek": EKRecurrenceDayOfWeek,
    }.items():
        setattr(ek, name, obj)
    foundation = types.ModuleType("Foundation")
    foundation.NSDate = NSDate  # type: ignore[attr-defined]
    foundation.NSURL = NSURL  # type: ignore[attr-defined]
    foundation.NSDateComponents = NSDateComponents  # type: ignore[attr-defined]
    foundation.NSTimeZone = NSTimeZone  # type: ignore[attr-defined]
    objc = types.ModuleType("objc")
    objc.autorelease_pool = contextlib.nullcontext  # type: ignore[attr-defined]

    monkeypatch.setitem(__import__("sys").modules, "EventKit", ek)
    monkeypatch.setitem(__import__("sys").modules, "Foundation", foundation)
    monkeypatch.setitem(__import__("sys").modules, "objc", objc)
    monkeypatch.setattr("sys.platform", "darwin")
