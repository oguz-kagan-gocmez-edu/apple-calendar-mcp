/**
 * CalDAV backend (port of caldav_store.py). Each object holds a master VEVENT
 * (RRULE/EXDATE for series) plus RECURRENCE-ID overrides. Edits follow the
 * same rules as the Python backends:
 *
 * - span "this":   edit = upsert an override; delete = add an EXDATE
 * - span "future": end the master with UNTIL, continue as a new series (new UID)
 * - span "all":    edit the master and shift the series; delete the object
 */

import ICAL from "ical.js";

import type {
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
  Weekday,
} from "../models";
import { WEEKDAYS, touchesRecurrence } from "../models";
import { byStartThenTitle } from "../service";
import {
  type CalendarStore,
  InvalidRequestError,
  NotFoundError,
  ReadOnlyCalendarError,
  makeEventId,
  splitEventId,
} from "../store";
import { type Zone, addDays, overlaps, parseDate } from "../time";
import { type DavCalendar, DavClient } from "./client";
import {
  type Component,
  type Occurrence,
  type TimeValue,
  Expander,
  Resource,
  addTime,
  ensureVtimezone,
  readTime,
  untilValue,
  writeTime,
} from "./ical";

export const DEFAULT_ICLOUD_URL = "https://caldav.icloud.com";
const PRODID = "-//apple-calendar-mcp//CalDAV Worker//EN";
const SERIES_ONLY = ["rrule", "rdate", "exdate", "recurrence-id"];
const PARTSTAT: Record<string, AttendeeStatus> = {
  "NEEDS-ACTION": "pending",
  ACCEPTED: "accepted",
  DECLINED: "declined",
  TENTATIVE: "tentative",
  DELEGATED: "delegated",
  COMPLETED: "completed",
  "IN-PROCESS": "in_process",
};
const STATUS: Record<string, EventStatus> = { CONFIRMED: "confirmed", TENTATIVE: "tentative", CANCELLED: "canceled" };
const BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const CACHE_MS = 5 * 60_000;

export interface CaldavConfig {
  url?: string;
  username: string;
  password: string;
  defaultCalendar?: string | null;
  fetcher?: (input: string, init: RequestInit) => Promise<Response>;
}

interface Entry {
  dav: DavCalendar;
  cal: Calendar;
}

// Per isolate: discovery is three round trips, so remember it for a few minutes.
const discovery = new Map<string, { home: string; cals: Map<string, Entry>; at: number }>();
const uidCalendar = new Map<string, string>();

/** URL for a new object. UIDs can come from strangers' invites: quote everything. */
export function objectHref(calendarUrl: string, uid: string): string {
  return `${calendarUrl.replace(/\/+$/, "")}/${encodeURIComponent(uid)}.ics`;
}

function newUid(): string {
  return crypto.randomUUID().replace(/-/g, "").toUpperCase();
}

function clone(comp: Component): Component {
  return new ICAL.Component(structuredClone(comp.jCal));
}

function str(comp: Component, name: string): string | null {
  const v = comp.getFirstPropertyValue(name);
  return v == null || v === "" ? null : String(v);
}

function mailto(value: unknown): string | null {
  const s = value == null ? "" : String(value);
  return s.toLowerCase().startsWith("mailto:") ? s.slice(7) : s || null;
}

export class CaldavStore implements CalendarStore {
  readonly client: DavClient;
  private readonly key: string;

  constructor(
    readonly zone: Zone,
    private readonly config: CaldavConfig,
  ) {
    const url = config.url || DEFAULT_ICLOUD_URL;
    this.client = new DavClient(url, config.username, config.password, config.fetcher);
    this.key = `${url}\n${config.username}`;
  }

  // ------------------------------------------------------------ calendars
  private async calendars(refresh = false): Promise<Map<string, Entry>> {
    const cached = discovery.get(this.key);
    if (cached && !refresh && Date.now() - cached.at < CACHE_MS) return cached.cals;
    const home = cached?.home ?? (await this.client.home());
    const host = new URL(this.client.baseUrl).hostname;
    const source = host.endsWith("icloud.com") ? "iCloud" : host;
    const found = new Map<string, Entry>();
    for (const dav of await this.client.calendars(home)) {
      if (!dav.components.includes("VEVENT")) continue; // iCloud Reminders lists are VTODO-only
      found.set(dav.id, {
        dav,
        cal: { id: dav.id, title: dav.title, source, color: dav.color, writable: dav.writable, is_default: false },
      });
    }
    const wanted = this.config.defaultCalendar;
    const fallback = [...found.values()].find((e) => e.cal.writable) ?? found.values().next().value;
    const def = (wanted && found.get(wanted)) || fallback;
    if (def) def.cal = { ...def.cal, is_default: true };
    discovery.set(this.key, { home, cals: found, at: Date.now() });
    return found;
  }

  private async calendar(id: string | null | undefined, forWrite: boolean): Promise<Entry> {
    let hit: Entry | undefined;
    if (id == null) {
      hit = [...(await this.calendars()).values()].find((e) => e.cal.is_default);
      if (!hit) throw new InvalidRequestError("No calendars found on the CalDAV account.");
    } else {
      hit = (await this.calendars()).get(id) ?? (await this.calendars(true)).get(id);
      if (!hit) throw new NotFoundError(`No calendar with id '${id}'.`);
    }
    if (forWrite && !hit.cal.writable) throw new ReadOnlyCalendarError(`Calendar '${hit.cal.title}' is read-only.`);
    return hit;
  }

  private async find(uid: string): Promise<Resource> {
    const cals = await this.calendars();
    const hint = uidCalendar.get(uid);
    const order = [...cals.keys()].sort((a, b) => Number(b === hint) - Number(a === hint));
    if (hint && cals.has(hint)) {
      const obj = await this.client.byUid(cals.get(hint)!.dav.url, uid);
      if (obj) return this.resource(hint, obj.href, obj.etag, obj.data);
    }
    const rest = order.filter((id) => id !== hint);
    const hits = await Promise.all(rest.map((id) => this.client.byUid(cals.get(id)!.dav.url, uid)));
    const i = hits.findIndex(Boolean);
    if (i < 0) throw new NotFoundError(`No event with uid '${uid}'.`);
    return this.resource(rest[i]!, hits[i]!.href, hits[i]!.etag, hits[i]!.data);
  }

  private resource(calendarId: string, href: string, etag: string | null, data: string): Resource {
    const res = Resource.parse(calendarId, href, etag, data);
    if (uidCalendar.size > 5000) uidCalendar.clear();
    uidCalendar.set(res.uid, calendarId);
    return res;
  }

  private async put(res: Resource, create = false): Promise<void> {
    const text = res.toString();
    if (text.includes(`TZID=${this.zone.name}`)) {
      ensureVtimezone(res, this.zone, this.zone.date(Date.now()).y);
    }
    res.etag = await this.client.put(res.href, res.toString(), create ? { create: true } : { etag: res.etag });
    uidCalendar.set(res.uid, res.calendarId);
  }

  private newResource(calendarId: string, calendarUrl: string, master: Component): Resource {
    const cal = new ICAL.Component("vcalendar");
    cal.addPropertyWithValue("prodid", PRODID);
    cal.addPropertyWithValue("version", "2.0");
    cal.addSubcomponent(master);
    const uid = String(master.getFirstPropertyValue("uid"));
    return new Resource(calendarId, objectHref(calendarUrl, uid), null, cal);
  }

  // ------------------------------------------------------------ times
  /** Write start/end in the user's zone (dates for all-day), like the Python backend. */
  private setTimes(comp: Component, start: number, end: number, allDay: boolean): void {
    for (const name of ["dtstart", "dtend", "duration"]) comp.removeAllProperties(name);
    if (allDay) {
      const first = this.zone.date(start);
      let after = this.zone.date(end);
      if (after.y * 10000 + after.m * 100 + after.d <= first.y * 10000 + first.m * 100 + first.d) after = addDays(first, 1);
      addTime(comp, "dtstart", { kind: "date", date: first });
      addTime(comp, "dtend", { kind: "date", date: after });
    } else {
      addTime(comp, "dtstart", { kind: "zoned", tzid: this.zone.name, wall: this.zone.wall(start) });
      addTime(comp, "dtend", { kind: "zoned", tzid: this.zone.name, wall: this.zone.wall(end) });
    }
  }

  /** ``ms`` in the same form as the master's DTSTART. */
  private like(exp: Expander, ms: number): TimeValue {
    return exp.clock.like(readTime(exp.master, "dtstart")!, ms);
  }

  private setExdates(exp: Expander, stamps: Iterable<number>): void {
    exp.master.removeAllProperties("exdate");
    for (const ts of [...new Set(stamps)].sort((a, b) => a - b)) addTime(exp.master, "exdate", this.like(exp, ts));
  }

  /** UNTIL covering ``lastDay`` inclusive, in the form RFC 5545 requires. */
  private until(exp: Expander, lastDay: { y: number; m: number; d: number }): ICAL.Time {
    if (exp.allDay) return untilValue({ kind: "date", date: lastDay });
    const ms = this.zone.instant({ ...lastDay, h: 23, mi: 59, s: 59 });
    return untilValue({ kind: "utc", ms });
  }

  // ------------------------------------------------------------ rule mapping
  private recurrence(exp: Expander): Recurrence | null {
    const rule = exp.rule();
    if (!rule) return null;
    const freq = String(rule.freq ?? "").toLowerCase();
    if (!["daily", "weekly", "monthly", "yearly"].includes(freq)) return null;
    let days: Weekday[] | null = null;
    const byday = (rule.parts as Record<string, string[] | undefined>).BYDAY;
    if (freq === "weekly" && byday?.length) {
      const set = new Set(byday.map((d) => d.slice(-2).toUpperCase()));
      days = WEEKDAYS.filter((_, i) => set.has(BYDAY[i]!));
    }
    let until: string | null = null;
    if (rule.until) {
      const u = rule.until;
      const ms = u.isDate
        ? exp.clock.instant({ kind: "date", date: { y: u.year, m: u.month, d: u.day } })
        : exp.clock.instant(u.zone?.tzid === "UTC"
            ? { kind: "utc", ms: Date.UTC(u.year, u.month - 1, u.day, u.hour, u.minute, u.second) }
            : { kind: "floating", wall: { y: u.year, m: u.month, d: u.day, h: u.hour, mi: u.minute, s: u.second } });
      const d = this.zone.date(ms);
      until = `${String(d.y).padStart(4, "0")}-${String(d.m).padStart(2, "0")}-${String(d.d).padStart(2, "0")}`;
    }
    const count = rule.count ?? null;
    if (days !== null && days.length === 0) days = null;
    if (count !== null && (count < 1 || count > 5000)) return null;
    if (rule.interval < 1 || rule.interval > 999) return null;
    return {
      frequency: freq as Recurrence["frequency"],
      interval: rule.interval,
      days_of_week: days,
      count,
      until: count ? null : until,
    };
  }

  private rrule(rec: Recurrence, exp: Expander): ICAL.Recur {
    const data: Record<string, unknown> = { freq: rec.frequency.toUpperCase() };
    if (rec.interval !== 1) data.interval = rec.interval;
    if (rec.days_of_week?.length) data.parts = { BYDAY: rec.days_of_week.map((d) => BYDAY[WEEKDAYS.indexOf(d)]) };
    if (rec.count != null) data.count = rec.count;
    else if (rec.until != null) data.until = this.until(exp, parseDate(rec.until));
    return new ICAL.Recur(data as ConstructorParameters<typeof ICAL.Recur>[0]);
  }

  private setRule(comp: Component, rule: ICAL.Recur): void {
    comp.removeAllProperties("rrule");
    comp.addPropertyWithValue("rrule", rule);
  }

  // ------------------------------------------------------------ conversion
  private event(res: Resource, exp: Expander, occ: Occurrence, cal: Calendar): Event {
    const comp = occ.comp;
    const organizer = comp.getFirstProperty("organizer");
    const attendees: Attendee[] = comp.getAllProperties("attendee").map((a) => ({
      name: (a.getParameter("cn") as string | undefined) || null,
      email: mailto(a.getFirstValue()),
      status: PARTSTAT[String(a.getParameter("partstat") ?? "").toUpperCase()] ?? "unknown",
    }));
    const availability: Availability = String(comp.getFirstPropertyValue("transp") ?? "").toUpperCase() === "TRANSPARENT" ? "free" : "busy";
    return {
      id: makeEventId(res.uid, occ.original),
      calendar_id: cal.id,
      calendar_title: cal.title,
      title: str(comp, "summary") ?? "",
      start: occ.start,
      end: occ.end,
      all_day: occ.allDay,
      location: str(comp, "location"),
      notes: str(comp, "description"),
      url: str(comp, "url"),
      is_recurring: exp.master.hasProperty("rrule"),
      recurrence: this.recurrence(exp),
      alarms_minutes_before: this.alarms(comp),
      availability,
      status: STATUS[String(comp.getFirstPropertyValue("status") ?? "").toUpperCase()] ?? "none",
      organizer: organizer ? (organizer.getParameter("cn") as string | undefined) || mailto(organizer.getFirstValue()) : null,
      attendees,
    };
  }

  private alarms(comp: Component): number[] {
    const out = new Set<number>();
    for (const alarm of comp.getAllSubcomponents("valarm")) {
      const trigger = alarm.getFirstPropertyValue("trigger");
      if (trigger instanceof ICAL.Duration) {
        const secs = trigger.toSeconds();
        if (secs <= 0) out.add(Math.floor(-secs / 60));
      }
    }
    return [...out].sort((a, b) => b - a); // same order the service normalises to
  }

  private setAlarms(comp: Component, minutes: number[]): void {
    for (const alarm of comp.getAllSubcomponents("valarm")) comp.removeSubcomponent(alarm);
    for (const m of [...new Set(minutes)].sort((a, b) => a - b)) {
      const alarm = new ICAL.Component("valarm");
      alarm.addPropertyWithValue("action", "DISPLAY");
      alarm.addPropertyWithValue("description", "Reminder");
      alarm.addPropertyWithValue("trigger", ICAL.Duration.fromSeconds(-m * 60));
      comp.addSubcomponent(alarm);
    }
  }

  private applyFields(comp: Component, patch: EventPatch | EventDraft): void {
    if (patch.title != null) comp.updatePropertyWithValue("summary", patch.title);
    for (const [key, prop] of [["location", "location"], ["notes", "description"], ["url", "url"]] as const) {
      const value = patch[key];
      if (value == null) continue;
      comp.removeAllProperties(prop);
      if (value) comp.addPropertyWithValue(prop, value);
    }
    if (patch.alarms_minutes_before != null) this.setAlarms(comp, patch.alarms_minutes_before);
    if (patch.availability != null) comp.updatePropertyWithValue("transp", patch.availability === "free" ? "TRANSPARENT" : "OPAQUE");
    const now: TimeValue = { kind: "utc", ms: Math.floor(Date.now() / 1000) * 1000 };
    writeTime(comp, "dtstamp", now);
    writeTime(comp, "last-modified", now);
    const seq = Number(comp.getFirstPropertyValue("sequence") ?? 0);
    comp.updatePropertyWithValue("sequence", Number.isFinite(seq) ? seq + 1 : 1);
  }

  // ------------------------------------------------------------ locating
  private locateIn(res: Resource, ts: number | null, eventId: string): { exp: Expander; ts: number; occ: Occurrence } {
    const exp = new Expander(res, this.zone);
    const at = ts ?? exp.first;
    const override = exp.overrideAt(at);
    if (override) return { exp, ts: at, occ: exp.fromOverride(override) };
    if (exp.exdates().has(at)) throw new NotFoundError(`Occurrence '${eventId}' was deleted.`);
    if (!exp.isRecurring) {
      if (at !== exp.first) throw new NotFoundError(`No event with id '${eventId}'.`);
      return { exp, ts: at, occ: exp.between(-Infinity, Infinity)[0]! };
    }
    if (!exp.has(at)) throw new NotFoundError(`No event with id '${eventId}'.`);
    return { exp, ts: at, occ: exp.generated(at) };
  }

  private async locate(eventId: string): Promise<{ res: Resource; exp: Expander; ts: number; occ: Occurrence }> {
    const [uid, ts] = splitEventId(eventId);
    const res = await this.find(uid);
    return { res, ...this.locateIn(res, ts, eventId) };
  }

  private async materialize(res: Resource, ts: number): Promise<Event> {
    const cal = (await this.calendar(res.calendarId, false)).cal;
    const { exp, occ } = this.locateIn(res, ts, makeEventId(res.uid, ts));
    return this.event(res, exp, occ, cal);
  }

  // ------------------------------------------------------------ series edits
  private shiftSeries(res: Resource, delta: number): void {
    let exp = new Expander(res, this.zone);
    const exdates = [...exp.exdates()];
    const [start, end] = exp.span(exp.master);
    this.setTimes(exp.master, start + delta, end + delta, exp.allDay);
    exp = new Expander(res, this.zone); // master form may have changed
    this.setExdates(exp, exdates.map((t) => t + delta));
    for (const ov of res.overrides()) {
      const rid = exp.originalOf(ov);
      const [oStart, oEnd] = exp.span(ov);
      if (oStart === rid) this.setTimes(ov, oStart + delta, oEnd + delta, readTime(ov, "dtstart")!.kind === "date");
      writeTime(ov, "recurrence-id", this.like(exp, rid + delta));
    }
  }

  /** Apply ``patch`` to the whole series; return the target's new original start. */
  private applyToSeries(res: Resource, target: number, current: Event, patch: EventPatch): number {
    let newTarget = target;
    if (patch.start != null && patch.start !== current.start) {
      const delta = patch.start - current.start;
      this.shiftSeries(res, delta);
      newTarget = target + delta;
    }
    let exp = new Expander(res, this.zone);
    let [start, end] = exp.span(exp.master);
    let allDay = exp.allDay;
    if (patch.end != null) end = start + (patch.end - (patch.start ?? current.start));
    if (patch.all_day != null) allDay = patch.all_day;
    this.setTimes(exp.master, start, end, allDay);
    this.applyFields(exp.master, patch);
    exp = new Expander(res, this.zone);
    if (patch.remove_recurrence) {
      for (const name of SERIES_ONLY) exp.master.removeAllProperties(name);
      for (const ov of res.overrides()) res.cal.removeSubcomponent(ov);
      const [s, e] = exp.span(exp.master);
      this.setTimes(exp.master, newTarget, newTarget + (e - s), exp.allDay);
    } else if (patch.recurrence != null) {
      this.setRule(exp.master, this.rrule(patch.recurrence, exp));
    }
    return newTarget;
  }

  /** End series ``res`` before ``at``; return the new tail series (not saved yet). */
  private async split(res: Resource, at: number): Promise<Resource> {
    const exp = new Expander(res, this.zone);
    const tailUid = newUid();
    const tailMaster = clone(exp.master);
    tailMaster.updatePropertyWithValue("uid", tailUid);
    const [start, end] = exp.span(exp.master);
    this.setTimes(tailMaster, at, at + (end - start), exp.allDay);
    const rule = exp.rule()!;
    if (rule.count) {
      const tailRule = rule.clone();
      tailRule.count = Math.max(rule.count - exp.countBefore(at), 1);
      this.setRule(tailMaster, tailRule);
    }
    const exdates = [...exp.exdates()];
    const entry = await this.calendar(res.calendarId, true);
    const tail = this.newResource(res.calendarId, entry.dav.url, tailMaster);
    for (const vtz of res.cal.getAllSubcomponents("vtimezone")) tail.cal.addSubcomponent(clone(vtz));
    const tailExp = new Expander(tail, this.zone);
    this.setExdates(tailExp, exdates.filter((t) => t >= at));
    for (const ov of res.overrides()) {
      if (exp.originalOf(ov) >= at) {
        res.cal.removeSubcomponent(ov);
        ov.updatePropertyWithValue("uid", tailUid);
        tail.cal.addSubcomponent(ov);
      }
    }
    this.truncate(res, at);
    tail.href = objectHref(entry.dav.url, tailUid);
    return tail;
  }

  /** Make series ``res`` end on the day before ``at``. */
  private truncate(res: Resource, at: number): void {
    const exp = new Expander(res, this.zone);
    const rule = exp.rule()!.clone();
    rule.count = null;
    rule.until = this.until(exp, addDays(this.zone.date(at), -1));
    this.setRule(exp.master, rule);
    this.setExdates(exp, [...exp.exdates()].filter((t) => t < at));
    for (const ov of res.overrides()) {
      if (exp.originalOf(ov) >= at) res.cal.removeSubcomponent(ov);
    }
  }

  private async move(res: Resource, calendarId: string): Promise<Resource> {
    const entry = await this.calendar(calendarId, true);
    if (entry.cal.id === res.calendarId) return res;
    const moved = new Resource(entry.cal.id, objectHref(entry.dav.url, res.uid), null, res.cal);
    await this.put(moved, true);
    await this.client.delete(res.href, res.etag);
    return moved;
  }

  // ------------------------------------------------------------ CalendarStore
  async listCalendars(): Promise<Calendar[]> {
    return [...(await this.calendars(true)).values()].map((e) => e.cal);
  }

  async listEvents(start: number, end: number, calendarIds?: string[] | null): Promise<Event[]> {
    const cals = await this.calendars();
    const wanted = calendarIds?.length ? calendarIds : [...cals.keys()];
    const entries = await Promise.all(wanted.map((id) => this.calendar(id, false)));
    const batches = await Promise.all(entries.map((e) => this.client.query(e.dav.url, start, end)));
    const out: Event[] = [];
    entries.forEach((entry, i) => {
      for (const obj of batches[i]!) {
        const res = this.resource(entry.cal.id, obj.href, obj.etag, obj.data);
        let exp: Expander;
        try {
          exp = new Expander(res, this.zone);
        } catch {
          continue; // an object we can't read shouldn't hide the rest of the week
        }
        for (const occ of exp.between(start, end)) {
          if (overlaps(occ.start, occ.end, start, end)) out.push(this.event(res, exp, occ, entry.cal));
        }
      }
    });
    return out.sort(byStartThenTitle);
  }

  async getEvent(eventId: string): Promise<Event> {
    const { res, exp, occ } = await this.locate(eventId);
    return this.event(res, exp, occ, (await this.calendar(res.calendarId, false)).cal);
  }

  async createEvent(draft: EventDraft): Promise<Event> {
    const entry = await this.calendar(draft.calendar_id, true);
    const master = new ICAL.Component("vevent");
    master.addPropertyWithValue("uid", newUid());
    writeTime(master, "created", { kind: "utc", ms: Math.floor(Date.now() / 1000) * 1000 });
    this.setTimes(master, draft.start, draft.end, draft.all_day);
    this.applyFields(master, draft);
    const res = this.newResource(entry.cal.id, entry.dav.url, master);
    if (draft.recurrence) this.setRule(master, this.rrule(draft.recurrence, new Expander(res, this.zone)));
    await this.put(res, true);
    return this.materialize(res, new Expander(res, this.zone).first);
  }

  async updateEvent(eventId: string, patch: EventPatch, span: Span): Promise<Event> {
    let { res, exp, ts, occ } = await this.locate(eventId);
    const cal = (await this.calendar(res.calendarId, true)).cal;
    const current = this.event(res, exp, occ, cal);
    const recurring = exp.master.hasProperty("rrule");

    if (!recurring || span === "all" || (span === "future" && ts === exp.first)) {
      const target = this.applyToSeries(res, ts, current, patch);
      await this.put(res);
      if (patch.calendar_id != null) res = await this.move(res, patch.calendar_id);
      return this.materialize(res, target);
    }

    if (span === "future") {
      let tail = await this.split(res, ts);
      const target = this.applyToSeries(tail, ts, current, patch);
      await this.put(res);
      await this.put(tail, true);
      if (patch.calendar_id != null) tail = await this.move(tail, patch.calendar_id);
      return this.materialize(tail, target);
    }

    // "this" on a recurring series: an override with RECURRENCE-ID
    if (touchesRecurrence(patch)) throw new InvalidRequestError("Recurrence can't be changed for a single occurrence.");
    if (patch.calendar_id != null && patch.calendar_id !== res.calendarId) {
      throw new InvalidRequestError("A single occurrence can't move to another calendar; use span 'all'.");
    }
    let override = exp.overrideAt(ts);
    if (!override) {
      override = clone(exp.master);
      for (const name of [...SERIES_ONLY, "dtstart", "dtend", "duration"]) override.removeAllProperties(name);
      addTime(override, "recurrence-id", this.like(exp, ts));
      this.setTimes(override, current.start, current.end, current.all_day);
      res.cal.addSubcomponent(override);
    }
    const newStart = patch.start ?? current.start;
    const newEnd = patch.end ?? newStart + (current.end - current.start);
    this.setTimes(override, newStart, newEnd, patch.all_day ?? current.all_day);
    this.applyFields(override, patch);
    await this.put(res);
    return this.materialize(res, ts);
  }

  async deleteEvent(eventId: string, span: Span): Promise<Event> {
    const { res, exp, ts, occ } = await this.locate(eventId);
    const cal = (await this.calendar(res.calendarId, true)).cal;
    const before = this.event(res, exp, occ, cal);
    if (!exp.master.hasProperty("rrule") || span === "all" || (span === "future" && ts === exp.first)) {
      await this.client.delete(res.href, res.etag);
      uidCalendar.delete(res.uid);
    } else if (span === "this") {
      const override = exp.overrideAt(ts);
      if (override) res.cal.removeSubcomponent(override);
      this.setExdates(exp, [...exp.exdates(), ts]);
      await this.put(res);
    } else {
      this.truncate(res, ts);
      await this.put(res);
    }
    return before;
  }
}

/** For tests: forget cached discovery so each test account starts clean. */
export function resetCaches(): void {
  discovery.clear();
  uidCalendar.clear();
}

