"""CalDAV backend: iCloud (or any CalDAV server) for the remote connector.

Each calendar object is one iCalendar resource holding a *master* VEVENT
(with RRULE/EXDATE for recurring events) plus *override* VEVENTs that carry a
RECURRENCE-ID. Recurring-event edits follow the same rules as the other
backends (see memory_store.py):

* span THIS: edit = upsert an override; delete = add an EXDATE
* span FUTURE: end the master with UNTIL, continue as a new series (new UID)
* span ALL: edit the master and shift the series; delete the whole resource

Reading expands occurrences on the client with ``recurring-ical-events``
(iCloud's server-side expansion is unreliable). Writes are plain PUT/DELETE.
"""

from __future__ import annotations

import copy
import functools
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlparse

import icalendar
import recurring_ical_events
from caldav.davclient import DAVClient
from caldav.lib import error as dav_error

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
    Recurrence,
    Span,
)
from .store import (
    BackendError,
    CalendarError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ReadOnlyCalendarError,
    make_event_id,
    split_event_id,
)
from .timeutil import ONE_DAY, overlaps

DEFAULT_ICLOUD_URL = "https://caldav.icloud.com"
PRODID = "-//apple-calendar-mcp//CalDAV//EN"
_ICS_HEADERS = {"Content-Type": "text/calendar; charset=utf-8"}
_SERIES_ONLY = ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")
_PARTSTAT: dict[str, AttendeeStatus] = {
    "NEEDS-ACTION": "pending",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
    "COMPLETED": "completed",
    "IN-PROCESS": "in_process",
}
_STATUS: dict[str, EventStatus] = {
    "CONFIRMED": "confirmed",
    "TENTATIVE": "tentative",
    "CANCELLED": "canceled",
}
_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

T = TypeVar("T")

# caldav logs raw iCalendar data (attendee emails, notes) in some warnings.
# Event contents must never reach the server logs, so only keep its errors.
logging.getLogger("caldav").setLevel(logging.ERROR)
# icalendar's property types are a 30-way union; treat components as untyped here.
VEvent = Any
VCalendar = Any


def _translate_errors(fn: Callable[..., T]) -> Callable[..., T]:
    """Turn caldav/network errors into CalendarErrors that are safe to show."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except CalendarError:
            raise
        except dav_error.AuthorizationError as exc:
            raise PermissionDeniedError(
                "The CalDAV server rejected the login. Check the username and the "
                "app-specific password (appleid.apple.com > Sign-In and Security)."
            ) from exc
        except dav_error.NotFoundError as exc:
            raise NotFoundError("The calendar or event no longer exists on the server.") from exc
        except dav_error.DAVError as exc:
            raise BackendError(f"CalDAV server error ({type(exc).__name__}). Try again shortly.") from exc
        except OSError as exc:  # connection refused, DNS, TLS...
            raise BackendError(f"Could not reach the CalDAV server ({type(exc).__name__}).") from exc

    return wrapper


def _object_href(dav_cal: Any, uid: str) -> str:
    """URL for a new event object. UIDs can come from other people's invites,
    so quote everything: a UID like ``../x`` must not escape the calendar."""
    return str(dav_cal.url).rstrip("/") + "/" + quote(uid, safe="") + ".ics"


@dataclass
class _Resource:
    """One iCalendar resource (an event series) on the server."""

    calendar_id: str
    href: str
    ical: VCalendar

    @property
    def events(self) -> list[VEvent]:
        return [c for c in self.ical.walk("VEVENT")]

    @property
    def master(self) -> VEvent:
        for comp in self.events:
            if "RECURRENCE-ID" not in comp:
                return comp
        raise NotFoundError("Event has no master component.")

    def overrides(self) -> list[VEvent]:
        return [c for c in self.events if "RECURRENCE-ID" in c]

    def remove(self, comp: VEvent) -> None:
        self.ical.subcomponents.remove(comp)


class CaldavStore:
    """``CalendarStore`` backed by a CalDAV account (iCloud by default)."""

    def __init__(
        self,
        tz: tzinfo,
        *,
        url: str = DEFAULT_ICLOUD_URL,
        username: str,
        password: str,
        default_calendar: str | None = None,
        client: Any = None,
    ) -> None:
        self.tz = tz
        self.url = url
        self.default_calendar = default_calendar
        # iCloud uses HTTP Basic over TLS; sending it up front saves a 401 round trip.
        self._client = client or DAVClient(
            url=url, username=username, password=password, auth_type="basic", timeout=30
        )
        self._lock = threading.RLock()
        self._cals: dict[str, tuple[Any, Calendar]] | None = None
        self._uid_calendar: dict[str, str] = {}

    # ------------------------------------------------------------ time helpers
    def _local(self, value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=self.tz) if value.tzinfo is None else value.astimezone(self.tz)
        return datetime.combine(value, time(), self.tz)

    def _ts(self, value: date | datetime) -> int:
        return int(self._local(value).timestamp())

    def _like(self, master: VEvent, value: datetime) -> date | datetime:
        """``value`` in the same form as the master's DTSTART (date, floating or zoned)."""
        ref = master.decoded("DTSTART")
        if not isinstance(ref, datetime):
            return self._local(value).date()
        if ref.tzinfo is None:
            return self._local(value).replace(tzinfo=None)
        return value.astimezone(ref.tzinfo)

    @staticmethod
    def _is_all_day(comp: VEvent) -> bool:
        return not isinstance(comp.decoded("DTSTART"), datetime)

    def _times(self, comp: VEvent) -> tuple[datetime, datetime, bool]:
        raw = comp.decoded("DTSTART")
        all_day = not isinstance(raw, datetime)
        start = self._local(raw)
        if "DTEND" in comp:
            end = self._local(comp.decoded("DTEND"))
        elif "DURATION" in comp:
            end = start + comp.decoded("DURATION")
        else:
            end = start + (ONE_DAY if all_day else timedelta(0))
        return start, max(end, start), all_day

    def _set_times(self, comp: VEvent, start: datetime, end: datetime, all_day: bool) -> None:
        for name in ("DTSTART", "DTEND", "DURATION"):
            comp.pop(name, None)
        if all_day:
            first, after = self._local(start).date(), self._local(end).date()
            comp.add("DTSTART", first)
            comp.add("DTEND", max(after, first + ONE_DAY))
        else:
            comp.add("DTSTART", self._local(start))
            comp.add("DTEND", self._local(end))

    # ------------------------------------------------------------ rule mapping
    def _recurrence(self, master: VEvent) -> Recurrence | None:
        if "RRULE" not in master:
            return None
        rule = master["RRULE"]
        freq = str((rule.get("FREQ") or [""])[0]).lower()
        if freq not in ("daily", "weekly", "monthly", "yearly"):
            return None
        days = None
        if freq == "weekly" and rule.get("BYDAY"):
            days = sorted(
                {
                    WEEKDAYS[_BYDAY.index(str(d)[-2:].upper())]
                    for d in rule["BYDAY"]
                    if str(d)[-2:].upper() in _BYDAY
                },
                key=WEEKDAYS.index,
            )
        until = None
        if rule.get("UNTIL"):
            until = self._local(rule["UNTIL"][0]).date()
        count = int(rule["COUNT"][0]) if rule.get("COUNT") else None
        try:
            return Recurrence(
                frequency=freq,
                interval=int((rule.get("INTERVAL") or [1])[0]),
                days_of_week=days or None,
                count=count,
                until=None if count else until,
            )
        except ValueError:
            return None

    def _rrule(self, rec: Recurrence, master: VEvent) -> dict[str, Any]:
        rule: dict[str, Any] = {"FREQ": rec.frequency.upper()}
        if rec.interval != 1:
            rule["INTERVAL"] = rec.interval
        if rec.days_of_week:
            rule["BYDAY"] = [_BYDAY[WEEKDAYS.index(d)] for d in rec.days_of_week]
        if rec.count is not None:
            rule["COUNT"] = rec.count
        elif rec.until is not None:
            rule["UNTIL"] = self._until(master, rec.until)
        return rule

    def _until(self, master: VEvent, last_day: date) -> date | datetime:
        """UNTIL covering ``last_day`` inclusive, in the form RFC 5545 requires."""
        if self._is_all_day(master):
            return last_day
        return datetime.combine(last_day, time(23, 59, 59), self.tz).astimezone(UTC)

    # ------------------------------------------------------------ conversion
    def _event(self, res: _Resource, comp: VEvent, cal: Calendar) -> Event:
        master = res.master
        original = comp.decoded("RECURRENCE-ID") if "RECURRENCE-ID" in comp else comp.decoded("DTSTART")
        start, end, all_day = self._times(comp)
        organizer = comp.get("ORGANIZER")
        attendees = comp.get("ATTENDEE", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        status = _STATUS.get(str(comp.get("STATUS", "")).upper(), "none")
        availability: Availability = (
            "free" if str(comp.get("TRANSP", "")).upper() == "TRANSPARENT" else "busy"
        )
        return Event(
            id=make_event_id(str(master["UID"]), self._ts(original)),
            calendar_id=cal.id,
            calendar_title=cal.title,
            title=str(comp.get("SUMMARY", "")),
            start=start,
            end=end,
            all_day=all_day,
            location=str(comp["LOCATION"]) if comp.get("LOCATION") else None,
            notes=str(comp["DESCRIPTION"]) if comp.get("DESCRIPTION") else None,
            url=str(comp["URL"]) if comp.get("URL") else None,
            is_recurring="RRULE" in master,
            recurrence=self._recurrence(master),
            alarms_minutes_before=self._alarms(comp),
            availability=availability,
            status=status,
            organizer=(str(organizer.params.get("CN")) if organizer.params.get("CN") else _mailto(organizer))
            if organizer
            else None,
            attendees=[
                Attendee(
                    name=str(a.params["CN"]) if a.params.get("CN") else None,
                    email=_mailto(a),
                    status=_PARTSTAT.get(str(a.params.get("PARTSTAT", "")).upper(), "unknown"),
                )
                for a in attendees
            ],
        )

    @staticmethod
    def _alarms(comp: VEvent) -> list[int]:
        out: list[int] = []
        for alarm in comp.walk("VALARM"):
            trigger = alarm.decoded("TRIGGER") if "TRIGGER" in alarm else None
            if isinstance(trigger, timedelta) and trigger <= timedelta(0):
                out.append(int(-trigger.total_seconds() // 60))
        return sorted(set(out), reverse=True)  # same order the service normalises to

    @staticmethod
    def _set_alarms(comp: VEvent, minutes: list[int]) -> None:
        for alarm in list(comp.walk("VALARM")):
            comp.subcomponents.remove(alarm)
        for m in sorted(set(minutes)):
            alarm = icalendar.Alarm()
            alarm.add("ACTION", "DISPLAY")
            alarm.add("DESCRIPTION", "Reminder")
            alarm.add("TRIGGER", timedelta(minutes=-m))
            comp.add_component(alarm)

    def _apply_fields(self, comp: VEvent, patch: EventPatch | EventDraft) -> None:
        if patch.title is not None:
            comp.pop("SUMMARY", None)
            comp.add("SUMMARY", patch.title)
        for attr, prop in (("location", "LOCATION"), ("notes", "DESCRIPTION"), ("url", "URL")):
            value = getattr(patch, attr)
            if value is None:
                continue
            comp.pop(prop, None)
            if value:
                comp.add(prop, value)
        if patch.alarms_minutes_before is not None:
            self._set_alarms(comp, patch.alarms_minutes_before)
        if patch.availability is not None:
            comp.pop("TRANSP", None)
            comp.add("TRANSP", "TRANSPARENT" if patch.availability == "free" else "OPAQUE")
        comp.pop("DTSTAMP", None)
        comp.add("DTSTAMP", datetime.now(UTC))
        comp.pop("LAST-MODIFIED", None)
        comp.add("LAST-MODIFIED", datetime.now(UTC))
        seq = int(comp.get("SEQUENCE", 0))
        comp.pop("SEQUENCE", None)
        comp.add("SEQUENCE", seq + 1)

    # ------------------------------------------------------------ exdates/overrides
    def _exdates(self, master: VEvent) -> set[int]:
        prop = master.get("EXDATE")
        if prop is None:
            return set()
        props = prop if isinstance(prop, list) else [prop]
        return {self._ts(d.dt) for p in props for d in p.dts}

    def _set_exdates(self, master: VEvent, stamps: set[int]) -> None:
        master.pop("EXDATE", None)
        for ts in sorted(stamps):
            master.add("EXDATE", self._like(master, datetime.fromtimestamp(ts, self.tz)))

    def _override(self, res: _Resource, ts: int) -> VEvent | None:
        for comp in res.overrides():
            if self._ts(comp.decoded("RECURRENCE-ID")) == ts:
                return comp
        return None

    # ------------------------------------------------------------ server access
    def _calendars(self, refresh: bool = False) -> dict[str, tuple[Any, Calendar]]:
        if self._cals is not None and not refresh:
            return self._cals
        principal = self._client.principal()
        found: dict[str, tuple[Any, Calendar]] = {}
        host = urlparse(self.url).hostname or ""
        source = "iCloud" if host.endswith("icloud.com") else host
        for dav_cal in principal.calendars():
            try:
                comps = [str(c).upper() for c in dav_cal.get_supported_components()]
            except dav_error.DAVError:
                comps = ["VEVENT"]
            if "VEVENT" not in comps:
                continue  # iCloud Reminders lists are VTODO-only collections
            cal_id = unquote(str(dav_cal.url).rstrip("/").rsplit("/", 1)[-1])
            title = dav_cal.get_display_name() or cal_id
            found[cal_id] = (
                dav_cal,
                Calendar(id=cal_id, title=title, source=source, color=None, writable=True),
            )
        default = self.default_calendar if self.default_calendar in found else next(iter(found), None)
        if default is not None:
            dav_cal, cal = found[default]
            found[default] = (dav_cal, cal.model_copy(update={"is_default": True}))
        self._cals = found
        return found

    def _calendar(self, calendar_id: str | None, *, for_write: bool) -> tuple[Any, Calendar]:
        cals = self._calendars()
        if calendar_id is None:
            hit = next((v for v in cals.values() if v[1].is_default), None)
            if hit is None:
                raise InvalidRequestError("No calendars found on the CalDAV account.")
        else:
            hit = cals.get(calendar_id) or self._calendars(refresh=True).get(calendar_id)
            if hit is None:
                raise NotFoundError(f"No calendar with id {calendar_id!r}.")
        if for_write and not hit[1].writable:
            raise ReadOnlyCalendarError(f"Calendar {hit[1].title!r} is read-only.")
        return hit

    def _resource(self, calendar_id: str, obj: Any) -> _Resource:
        ical = icalendar.Calendar.from_ical(obj.data)
        res = _Resource(calendar_id=calendar_id, href=str(obj.url), ical=ical)
        self._uid_calendar[str(res.master["UID"])] = calendar_id
        return res

    def _find(self, uid: str) -> _Resource:
        cals = self._calendars()
        order = list(cals)
        if uid in self._uid_calendar and self._uid_calendar[uid] in cals:
            order.remove(self._uid_calendar[uid])
            order.insert(0, self._uid_calendar[uid])
        for cal_id in order:
            try:
                obj = cals[cal_id][0].object_by_uid(uid)
            except dav_error.NotFoundError:
                continue
            return self._resource(cal_id, obj)
        raise NotFoundError(f"No event with uid {uid!r}.")

    def _put(self, res: _Resource, *, new: bool = False) -> None:
        res.ical.add_missing_timezones()
        headers = dict(_ICS_HEADERS)
        if new:
            headers["If-None-Match"] = "*"
        self._client.put(res.href, res.ical.to_ical().decode("utf-8"), headers)
        self._uid_calendar[str(res.master["UID"])] = res.calendar_id

    def _new_resource(self, dav_cal: Any, cal: Calendar, master: VEvent) -> _Resource:
        ical = icalendar.Calendar()
        ical.add("PRODID", PRODID)
        ical.add("VERSION", "2.0")
        ical.add_component(master)
        href = _object_href(dav_cal, str(master["UID"]))
        return _Resource(calendar_id=cal.id, href=href, ical=ical)

    # ------------------------------------------------------------ occurrences
    def _occurrences(self, res: _Resource, start: datetime, end: datetime) -> Iterator[VEvent]:
        yield from recurring_ical_events.of(res.ical).between(start, end)

    def _locate(self, event_id: str) -> tuple[_Resource, int, VEvent]:
        """Resource, original-start stamp and the occurrence component for ``event_id``."""
        uid, ts = split_event_id(event_id)
        res = self._find(uid)
        master = res.master
        master_ts = self._ts(master.decoded("DTSTART"))
        if ts is None:
            ts = master_ts
        override = self._override(res, ts)
        if override is not None:
            return res, ts, override
        if ts in self._exdates(master):
            raise NotFoundError(f"Occurrence {event_id!r} was deleted.")
        if "RRULE" not in master and "RDATE" not in master:
            if ts != master_ts:
                raise NotFoundError(f"No event with id {event_id!r}.")
            return res, ts, master
        at = datetime.fromtimestamp(ts, self.tz)
        _, master_end, _ = self._times(master)
        span = max(master_end - self._local(master.decoded("DTSTART")), timedelta(seconds=1))
        for comp in self._occurrences(res, at - ONE_DAY, at + span + ONE_DAY):
            if self._ts(comp.decoded("RECURRENCE-ID")) == ts:
                return res, ts, comp
        raise NotFoundError(f"No event with id {event_id!r}.")

    def _materialize(self, res: _Resource, ts: int) -> Event:
        cal = self._calendars()[res.calendar_id][1]
        _, _, comp = self._locate(make_event_id(str(res.master["UID"]), ts))
        return self._event(res, comp, cal)

    # ------------------------------------------------------------ series edits
    def _shift_series(self, res: _Resource, delta: timedelta) -> None:
        master = res.master
        exdates = self._exdates(master)
        start, end, all_day = self._times(master)
        self._set_times(master, start + delta, end + delta, all_day)
        self._set_exdates(master, {ts + int(delta.total_seconds()) for ts in exdates})
        for ov in res.overrides():
            rid = self._local(ov.decoded("RECURRENCE-ID"))
            o_start, o_end, o_all_day = self._times(ov)
            if o_start == rid:  # not moved on its own: follows the series
                self._set_times(ov, o_start + delta, o_end + delta, o_all_day)
            ov.pop("RECURRENCE-ID")
            ov.add("RECURRENCE-ID", self._like(master, rid + delta))

    def _apply_to_series(
        self, res: _Resource, target: datetime, current: Event, patch: EventPatch
    ) -> datetime:
        """Apply ``patch`` to the whole series; return the target's new original start."""
        master = res.master
        new_target = target
        if patch.start is not None and patch.start != current.start:
            delta = patch.start - current.start
            self._shift_series(res, delta)
            new_target = target + delta
        start, end, all_day = self._times(master)
        if patch.end is not None:
            end = start + (patch.end - (patch.start or current.start))
        if patch.all_day is not None:
            all_day = patch.all_day
        self._set_times(master, start, end, all_day)
        self._apply_fields(master, patch)
        if patch.remove_recurrence:
            for name in _SERIES_ONLY:
                master.pop(name, None)
            for ov in res.overrides():
                res.remove(ov)
            s, e, ad = self._times(master)
            self._set_times(master, new_target, new_target + (e - s), ad)
        elif patch.recurrence is not None:
            master.pop("RRULE", None)
            master.add("RRULE", self._rrule(patch.recurrence, master))
        return new_target

    def _split(self, res: _Resource, at: datetime) -> _Resource:
        """End series ``res`` before ``at``; return the new tail series (not yet saved)."""
        master = res.master
        cut = int(at.timestamp())
        tail_master = copy.deepcopy(master)
        tail_uid = uuid.uuid4().hex.upper()
        tail_master.pop("UID")
        tail_master.add("UID", tail_uid)
        start, end, all_day = self._times(master)
        self._set_times(tail_master, at, at + (end - start), all_day)
        rule = dict(master["RRULE"])
        if rule.get("COUNT"):
            bare = icalendar.Calendar()
            plain = copy.deepcopy(master)
            plain.pop("EXDATE", None)
            bare.add_component(plain)
            before = sum(
                1
                for c in recurring_ical_events.of(bare).between(start, at)
                if self._ts(c.decoded("RECURRENCE-ID")) < cut
            )
            tail_rule = dict(rule)
            tail_rule["COUNT"] = [max(int(rule["COUNT"][0]) - before, 1)]
            tail_master.pop("RRULE")
            tail_master.add("RRULE", tail_rule)
        exdates = self._exdates(master)
        self._set_exdates(tail_master, {t for t in exdates if t >= cut})
        tail = _Resource(calendar_id=res.calendar_id, href="", ical=icalendar.Calendar())
        tail.ical.add("PRODID", PRODID)
        tail.ical.add("VERSION", "2.0")
        tail.ical.add_component(tail_master)
        for ov in res.overrides():
            if self._ts(ov.decoded("RECURRENCE-ID")) >= cut:
                res.remove(ov)
                ov.pop("UID")
                ov.add("UID", tail_uid)
                tail.ical.add_component(ov)
        self._truncate(res, at)
        dav_cal, _ = self._calendar(res.calendar_id, for_write=True)
        tail.href = _object_href(dav_cal, tail_uid)
        return tail

    def _truncate(self, res: _Resource, at: datetime) -> None:
        """Make series ``res`` end on the day before ``at``."""
        master = res.master
        cut = int(at.timestamp())
        rule = {k: v for k, v in dict(master["RRULE"]).items() if k not in ("COUNT", "UNTIL")}
        rule["UNTIL"] = self._until(master, self._local(at).date() - ONE_DAY)
        master.pop("RRULE")
        master.add("RRULE", rule)
        self._set_exdates(master, {t for t in self._exdates(master) if t < cut})
        for ov in res.overrides():
            if self._ts(ov.decoded("RECURRENCE-ID")) >= cut:
                res.remove(ov)

    def _move(self, res: _Resource, calendar_id: str) -> _Resource:
        dav_cal, cal = self._calendar(calendar_id, for_write=True)
        if cal.id == res.calendar_id:
            return res
        moved = _Resource(
            calendar_id=cal.id,
            href=_object_href(dav_cal, str(res.master["UID"])),
            ical=res.ical,
        )
        self._put(moved, new=True)
        self._client.delete(res.href)
        return moved

    # ------------------------------------------------------------ protocol
    @_translate_errors
    def list_calendars(self) -> list[Calendar]:
        with self._lock:
            return [cal for _, cal in self._calendars(refresh=True).values()]

    @_translate_errors
    def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        with self._lock:
            cals = self._calendars()
            wanted = calendar_ids or list(cals)
            out: list[Event] = []
            for cal_id in wanted:
                dav_cal, cal = self._calendar(cal_id, for_write=False)
                for obj in dav_cal.search(event=True, start=start, end=end):
                    res = self._resource(cal.id, obj)
                    for comp in self._occurrences(res, start, end):
                        ev = self._event(res, comp, cal)
                        if overlaps(ev.start, ev.end, start, end):
                            out.append(ev)
            out.sort(key=lambda e: (e.start, e.title))
            return out

    @_translate_errors
    def get_event(self, event_id: str) -> Event:
        with self._lock:
            res, _, comp = self._locate(event_id)
            return self._event(res, comp, self._calendars()[res.calendar_id][1])

    @_translate_errors
    def create_event(self, draft: EventDraft) -> Event:
        with self._lock:
            dav_cal, cal = self._calendar(draft.calendar_id, for_write=True)
            master = icalendar.Event()
            master.add("UID", uuid.uuid4().hex.upper())
            master.add("CREATED", datetime.now(UTC))
            self._set_times(master, draft.start, draft.end, draft.all_day)
            self._apply_fields(master, draft)
            if draft.recurrence is not None:
                master.add("RRULE", self._rrule(draft.recurrence, master))
            res = self._new_resource(dav_cal, cal, master)
            self._put(res, new=True)
            return self._materialize(res, self._ts(master.decoded("DTSTART")))

    @_translate_errors
    def update_event(self, event_id: str, patch: EventPatch, span: Span) -> Event:
        with self._lock:
            res, ts, comp = self._locate(event_id)
            self._calendar(res.calendar_id, for_write=True)
            cal = self._calendars()[res.calendar_id][1]
            current = self._event(res, comp, cal)
            master = res.master
            original = datetime.fromtimestamp(ts, self.tz)
            first = self._ts(master.decoded("DTSTART"))
            recurring = "RRULE" in master

            if not recurring or span is Span.ALL or (span is Span.FUTURE and ts == first):
                target = self._apply_to_series(res, original, current, patch)
                self._put(res)
                if patch.calendar_id is not None:
                    res = self._move(res, patch.calendar_id)
                return self._materialize(res, int(target.timestamp()))

            if span is Span.FUTURE:
                tail = self._split(res, original)
                target = self._apply_to_series(tail, original, current, patch)
                self._put(res)
                self._put(tail, new=True)
                if patch.calendar_id is not None:
                    tail = self._move(tail, patch.calendar_id)
                return self._materialize(tail, int(target.timestamp()))

            # Span.THIS on a recurring series -> override with RECURRENCE-ID
            if patch.touches_recurrence():
                raise InvalidRequestError("Recurrence can't be changed for a single occurrence.")
            if patch.calendar_id is not None and patch.calendar_id != res.calendar_id:
                raise InvalidRequestError(
                    "A single occurrence can't move to another calendar; use span 'all'."
                )
            override = self._override(res, ts)
            if override is None:
                override = copy.deepcopy(master)
                for name in (*_SERIES_ONLY, "DTSTART", "DTEND", "DURATION"):
                    override.pop(name, None)
                override.add("RECURRENCE-ID", self._like(master, original))
                self._set_times(override, current.start, current.end, current.all_day)
                res.ical.add_component(override)
            new_start = patch.start or current.start
            new_end = patch.end or new_start + (current.end - current.start)
            all_day = current.all_day if patch.all_day is None else patch.all_day
            self._set_times(override, new_start, new_end, all_day)
            self._apply_fields(override, patch)
            self._put(res)
            return self._materialize(res, ts)

    @_translate_errors
    def delete_event(self, event_id: str, span: Span) -> Event:
        with self._lock:
            res, ts, comp = self._locate(event_id)
            self._calendar(res.calendar_id, for_write=True)
            before = self._event(res, comp, self._calendars()[res.calendar_id][1])
            master = res.master
            first = self._ts(master.decoded("DTSTART"))
            if "RRULE" not in master or span is Span.ALL or (span is Span.FUTURE and ts == first):
                self._client.delete(res.href)
                self._uid_calendar.pop(str(master["UID"]), None)
            elif span is Span.THIS:
                override = self._override(res, ts)
                if override is not None:
                    res.remove(override)
                self._set_exdates(master, self._exdates(master) | {ts})
                self._put(res)
            else:
                self._truncate(res, datetime.fromtimestamp(ts, self.tz))
                self._put(res)
            return before


def _mailto(prop: Any) -> str | None:
    value = str(prop)
    return value[7:] if value.lower().startswith("mailto:") else value or None
