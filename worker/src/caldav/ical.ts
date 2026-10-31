/**
 * iCalendar plumbing on top of ical.js: time values in all four RFC 5545
 * forms, recurrence expansion, and VTIMEZONE generation.
 *
 * ical.js only knows time zones it has a VTIMEZONE for, so conversions here
 * go through ``Zone`` (Intl) for IANA names and fall back to the object's own
 * VTIMEZONE for anything else (e.g. Outlook's "W. Europe Standard Time").
 */

import ICAL from "ical.js";

import { BackendError, NotFoundError } from "../store";
import {
  type CivilDate,
  type Wall,
  DAY,
  MINUTE,
  Zone,
  addDays,
  dayNumber,
  fromDayNumber,
  weekday,
} from "../time";

export type Component = ICAL.Component;

/** A DTSTART-like value as written in the file. */
export type TimeValue =
  | { kind: "date"; date: CivilDate }
  | { kind: "utc"; ms: number }
  | { kind: "zoned"; tzid: string; wall: Wall }
  | { kind: "floating"; wall: Wall };

const zones = new Map<string, Zone | null>();

/** An IANA zone for ``tzid``, also accepting prefixed forms like "/mozilla.org/.../Europe/Berlin". */
export function ianaZone(tzid: string): Zone | null {
  let zone = zones.get(tzid);
  if (zone === undefined) {
    zone = null;
    const parts = tzid.split("/").filter(Boolean);
    for (let i = 0; i < parts.length && !zone; i++) {
      const name = parts.slice(i).join("/");
      if (Zone.isValid(name)) zone = new Zone(name);
    }
    if (zones.size > 256) zones.clear();
    zones.set(tzid, zone);
  }
  return zone;
}

// ---------------------------------------------------------------- resources
/** One iCalendar object on the server: a master VEVENT plus RECURRENCE-ID overrides. */
export class Resource {
  constructor(
    public calendarId: string,
    public href: string,
    public etag: string | null,
    readonly cal: Component,
  ) {}

  static parse(calendarId: string, href: string, etag: string | null, text: string): Resource {
    try {
      return new Resource(calendarId, href, etag, new ICAL.Component(ICAL.parse(text)));
    } catch {
      throw new BackendError("The server returned an event that could not be read.");
    }
  }

  get events(): Component[] {
    return this.cal.getAllSubcomponents("vevent");
  }

  get master(): Component {
    const master = this.events.find((c) => !c.hasProperty("recurrence-id"));
    if (!master) throw new NotFoundError("Event has no master component.");
    return master;
  }

  overrides(): Component[] {
    return this.events.filter((c) => c.hasProperty("recurrence-id"));
  }

  get uid(): string {
    return String(this.master.getFirstPropertyValue("uid") ?? "");
  }

  toString(): string {
    return this.cal.toString();
  }
}

// ---------------------------------------------------------------- reading times
export function readTime(comp: Component, name: string): TimeValue | null {
  const prop = comp.getFirstProperty(name);
  return prop ? timeOf(prop.getFirstValue() as ICAL.Time, prop.getParameter("tzid") as string | undefined) : null;
}

function timeOf(t: ICAL.Time, tzid?: string): TimeValue {
  if (t.isDate) return { kind: "date", date: { y: t.year, m: t.month, d: t.day } };
  const wall: Wall = { y: t.year, m: t.month, d: t.day, h: t.hour, mi: t.minute, s: t.second };
  if (t.zone?.tzid === "UTC") return { kind: "utc", ms: Date.UTC(wall.y, wall.m - 1, wall.d, wall.h, wall.mi, wall.s) };
  if (tzid) return { kind: "zoned", tzid, wall };
  return { kind: "floating", wall };
}

/** Every value of every ``name`` property (EXDATE and RDATE can hold several). */
export function readTimes(comp: Component, name: string): TimeValue[] {
  const out: TimeValue[] = [];
  for (const prop of comp.getAllProperties(name)) {
    const tzid = prop.getParameter("tzid") as string | undefined;
    for (const v of prop.getValues()) {
      if (v instanceof ICAL.Time) out.push(timeOf(v, tzid));
    }
  }
  return out;
}

/** Converts time values to instants for one resource; ``user`` reads floating times. */
export class Clock {
  private readonly vtz = new Map<string, ICAL.Timezone | null>();

  constructor(
    readonly user: Zone,
    private readonly cal: Component,
  ) {}

  private vtimezone(tzid: string): ICAL.Timezone | null {
    let tz = this.vtz.get(tzid);
    if (tz === undefined) {
      const comp = this.cal.getAllSubcomponents("vtimezone").find((c) => c.getFirstPropertyValue("tzid") === tzid);
      tz = comp ? new ICAL.Timezone(comp) : null;
      this.vtz.set(tzid, tz);
    }
    return tz;
  }

  instant(v: TimeValue): number {
    switch (v.kind) {
      case "utc":
        return v.ms;
      case "date":
        return this.user.startOfDay(v.date);
      case "floating":
        return this.user.instant(v.wall);
      case "zoned": {
        const iana = ianaZone(v.tzid);
        if (iana) return iana.instant(v.wall);
        const tz = this.vtimezone(v.tzid);
        if (!tz) return this.user.instant(v.wall); // unknown zone: best effort, read as local
        const t = ICAL.Time.fromData({ year: v.wall.y, month: v.wall.m, day: v.wall.d, hour: v.wall.h, minute: v.wall.mi, second: v.wall.s });
        const asUtc = Date.UTC(v.wall.y, v.wall.m - 1, v.wall.d, v.wall.h, v.wall.mi, v.wall.s);
        return asUtc - tz.utcOffset(t) * 1000;
      }
    }
  }

  /** ``ms`` written in the same form as ``ref`` (date, floating, zoned or UTC). */
  like(ref: TimeValue, ms: number): TimeValue {
    switch (ref.kind) {
      case "utc":
        return { kind: "utc", ms };
      case "date":
        return { kind: "date", date: this.user.date(ms) };
      case "floating":
        return { kind: "floating", wall: this.user.wall(ms) };
      case "zoned": {
        const iana = ianaZone(ref.tzid);
        if (iana) return { kind: "zoned", tzid: ref.tzid, wall: iana.wall(ms) };
        const tz = this.vtimezone(ref.tzid);
        if (!tz) return { kind: "zoned", tzid: ref.tzid, wall: this.user.wall(ms) };
        const t = ICAL.Time.fromJSDate(new Date(ms), true).convertToZone(tz);
        return { kind: "zoned", tzid: ref.tzid, wall: { y: t.year, m: t.month, d: t.day, h: t.hour, mi: t.minute, s: t.second } };
      }
    }
  }
}

// ---------------------------------------------------------------- writing times
function icalTime(v: TimeValue): ICAL.Time {
  switch (v.kind) {
    case "date":
      return ICAL.Time.fromData({ year: v.date.y, month: v.date.m, day: v.date.d, isDate: true });
    case "utc": {
      const t = new Date(v.ms);
      return ICAL.Time.fromData(
        { year: t.getUTCFullYear(), month: t.getUTCMonth() + 1, day: t.getUTCDate(), hour: t.getUTCHours(), minute: t.getUTCMinutes(), second: t.getUTCSeconds() },
        ICAL.Timezone.utcTimezone,
      );
    }
    case "zoned":
    case "floating":
      return ICAL.Time.fromData({ year: v.wall.y, month: v.wall.m, day: v.wall.d, hour: v.wall.h, minute: v.wall.mi, second: v.wall.s });
  }
}

export function writeTime(comp: Component, name: string, v: TimeValue): void {
  comp.removeAllProperties(name);
  addTime(comp, name, v);
}

export function addTime(comp: Component, name: string, v: TimeValue): void {
  const prop = comp.addPropertyWithValue(name, icalTime(v));
  if (v.kind === "zoned") prop.setParameter("tzid", v.tzid);
}

export function untilValue(v: TimeValue): ICAL.Time {
  return icalTime(v);
}

// ---------------------------------------------------------------- recurrence
export interface Occurrence {
  comp: Component;
  /** Original start (RECURRENCE-ID) as an instant: the occurrence's identity. */
  original: number;
  start: number;
  end: number;
  allDay: boolean;
}

const MAX_STEPS = 20_000; // hard stop for pathological rules (CPU budget)

export class Expander {
  readonly clock: Clock;
  readonly master: Component;
  readonly masterStart: TimeValue;
  readonly allDay: boolean;
  private readonly startMs: number;
  private readonly durationMs: number;
  private readonly durationDays: number;

  constructor(
    readonly res: Resource,
    user: Zone,
  ) {
    this.clock = new Clock(user, res.cal);
    this.master = res.master;
    const start = readTime(this.master, "dtstart");
    if (!start) throw new BackendError("The server returned an event without a start time.");
    this.masterStart = start;
    this.allDay = start.kind === "date";
    this.startMs = this.clock.instant(start);
    const [s, e] = this.span(this.master);
    this.durationMs = e - s;
    this.durationDays = this.allDay ? Math.max(Math.round((e - s) / DAY), 1) : 0;
  }

  get isRecurring(): boolean {
    return this.master.hasProperty("rrule") || this.master.hasProperty("rdate");
  }

  get first(): number {
    return this.startMs;
  }

  /** [start, end] instants of a component (end >= start). */
  span(comp: Component): [number, number] {
    const startV = readTime(comp, "dtstart");
    if (!startV) throw new BackendError("The server returned an event without a start time.");
    const start = this.clock.instant(startV);
    const endV = readTime(comp, "dtend");
    let end: number;
    if (endV) end = this.clock.instant(endV);
    else if (comp.hasProperty("duration")) {
      end = start + (comp.getFirstPropertyValue("duration") as ICAL.Duration).toSeconds() * 1000;
    } else end = start + (startV.kind === "date" ? DAY : 0);
    return [start, Math.max(start, end)];
  }

  exdates(): Set<number> {
    return new Set(readTimes(this.master, "exdate").map((v) => this.clock.instant(v)));
  }

  overrideAt(original: number): Component | null {
    return this.res.overrides().find((o) => this.originalOf(o) === original) ?? null;
  }

  originalOf(comp: Component): number {
    const rid = readTime(comp, "recurrence-id");
    return rid ? this.clock.instant(rid) : this.clock.instant(readTime(comp, "dtstart")!);
  }

  /** The master's RRULE, or null. */
  rule(): ICAL.Recur | null {
    return (this.master.getFirstPropertyValue("rrule") as ICAL.Recur | null) ?? null;
  }

  /** Original starts from RRULE/RDATE (EXDATEs not removed) with ``from <= start < to``. */
  originals(from: number, to: number): number[] {
    const out: number[] = [];
    const rule = this.rule();
    if (rule) out.push(...this.ruleStarts(rule, from, to));
    for (const v of readTimes(this.master, "rdate")) {
      const ms = this.clock.instant(v);
      if (from <= ms && ms < to) out.push(ms);
    }
    if (!rule && !this.master.hasProperty("rdate") && from <= this.startMs && this.startMs < to) out.push(this.startMs);
    return [...new Set(out)].sort((a, b) => a - b);
  }

  private ruleStarts(rule: ICAL.Recur, from: number, to: number): number[] {
    const until = this.untilMs(rule);
    const plain = rule.clone();
    plain.until = null; // handled here, as an instant (ical.js can't compare unknown zones)
    const dt = this.dtstartFor(plain, from);
    const it = plain.iterator(dt);
    const out: number[] = [];
    for (let steps = 0; steps < MAX_STEPS; steps++) {
      const next = it.next();
      if (!next) break;
      const ms = this.instantOfWall(next);
      if (until !== null && ms > until) break;
      if (ms >= to) break;
      if (ms >= from) out.push(ms);
    }
    return out;
  }

  /** How many rule instances (with COUNT, ignoring EXDATE) start before ``cut``. */
  countBefore(cut: number): number {
    const rule = this.rule();
    if (!rule) return 0;
    return this.ruleStarts(rule, -Infinity, cut).length;
  }

  private instantOfWall(t: ICAL.Time): number {
    const v = this.masterStart;
    if (t.isDate) return this.clock.instant({ kind: "date", date: { y: t.year, m: t.month, d: t.day } });
    const wall = { y: t.year, m: t.month, d: t.day, h: t.hour, mi: t.minute, s: t.second };
    if (v.kind === "utc") return Date.UTC(wall.y, wall.m - 1, wall.d, wall.h, wall.mi, wall.s);
    if (v.kind === "zoned") return this.clock.instant({ kind: "zoned", tzid: v.tzid, wall });
    return this.clock.instant({ kind: "floating", wall });
  }

  private untilMs(rule: ICAL.Recur): number | null {
    const until = rule.until;
    if (!until) return null;
    if (until.isDate) {
      // A DATE until covers that whole day.
      const day = { y: until.year, m: until.month, d: until.day };
      return this.allDay ? this.clock.instant({ kind: "date", date: day }) : this.clock.user.startOfDay(addDays(day, 1)) - 1;
    }
    return this.clock.instant(timeOf(until)); // UTC, or floating (read in the user's zone)
  }

  /**
   * DTSTART for iterating ``rule``. Rules without COUNT are moved forward by
   * whole periods to just before ``from``, so a series that started years
   * ago costs a handful of steps instead of hundreds or thousands.
   */
  private dtstartFor(rule: ICAL.Recur, from: number): ICAL.Time {
    const v = this.masterStart;
    const base: CivilDate = v.kind === "date" ? v.date : v.kind === "utc" ? fromDayNumber(Math.floor(v.ms / DAY)) : v.wall;
    let date = base;
    const parts = rule.parts as Record<string, unknown>;
    const simple = !rule.count && !parts.BYSETPOS && !parts.BYWEEKNO && !parts.BYYEARDAY;
    if (simple && Number.isFinite(from)) {
      // Two days of slack for time-zone differences; the event's own length on top.
      const target = fromDayNumber(dayNumber(this.clock.user.date(from - this.durationMs)) - 2);
      date = skipAhead(rule.freq, rule.interval, base, target);
    }
    if (v.kind === "date") return ICAL.Time.fromData({ year: date.y, month: date.m, day: date.d, isDate: true });
    const clock = v.kind === "utc" ? new Date(v.ms) : null;
    const h = clock ? clock.getUTCHours() : (v as { wall: Wall }).wall.h;
    const mi = clock ? clock.getUTCMinutes() : (v as { wall: Wall }).wall.mi;
    const s = clock ? clock.getUTCSeconds() : (v as { wall: Wall }).wall.s;
    return ICAL.Time.fromData({ year: date.y, month: date.m, day: date.d, hour: h, minute: mi, second: s });
  }

  /** The generated occurrence starting at ``original`` (no override applied). */
  generated(original: number): Occurrence {
    let end: number;
    if (this.allDay) end = this.clock.user.startOfDay(addDays(this.clock.user.date(original), this.durationDays));
    else end = original + this.durationMs;
    return { comp: this.master, original, start: original, end, allDay: this.allDay };
  }

  fromOverride(comp: Component): Occurrence {
    const [start, end] = this.span(comp);
    return { comp, original: this.originalOf(comp), start, end, allDay: readTime(comp, "dtstart")!.kind === "date" };
  }

  /** Occurrences overlapping [from, to): generated ones minus EXDATEs, with overrides applied. */
  between(from: number, to: number): Occurrence[] {
    if (!this.isRecurring) {
      const [start, end] = this.span(this.master);
      return [{ comp: this.master, original: start, start, end, allDay: this.allDay }];
    }
    const ex = this.exdates();
    const overrides = new Map(this.res.overrides().map((o) => [this.originalOf(o), o]));
    const out: Occurrence[] = [];
    const lead = Math.max(this.durationMs, this.durationDays * DAY) + DAY;
    for (const original of this.originals(from - lead, to)) {
      if (ex.has(original) || overrides.has(original)) continue;
      out.push(this.generated(original));
    }
    for (const [original, comp] of overrides) {
      if (!ex.has(original)) out.push(this.fromOverride(comp));
    }
    return out;
  }

  /** True if ``original`` is an instance of the series (not deleted). */
  has(original: number): boolean {
    if (this.exdates().has(original)) return false;
    return this.originals(original, original + 1).includes(original);
  }
}

/**
 * The latest date ``base + k * period`` (k >= 0) that is not after ``target``.
 * Months and years only step to dates that exist (no 31 April, no 29 Feb in
 * common years), so the moved DTSTART is itself a real instance.
 */
export function skipAhead(freq: string, interval: number, base: CivilDate, target: CivilDate): CivilDate {
  const days = dayNumber(target) - dayNumber(base);
  if (days <= 0) return base;
  if (freq === "DAILY" || freq === "WEEKLY") {
    const period = freq === "DAILY" ? interval : 7 * interval;
    return fromDayNumber(dayNumber(base) + Math.floor(days / period) * period);
  }
  if (freq !== "MONTHLY" && freq !== "YEARLY") return base;
  const step = freq === "MONTHLY" ? interval : 12 * interval;
  const months = (target.y - base.y) * 12 + (target.m - base.m);
  for (let k = Math.floor(months / step); k > 0; k--) {
    const total = base.m - 1 + k * step;
    const candidate = { y: base.y + Math.floor(total / 12), m: (total % 12) + 1, d: base.d };
    const exists = fromDayNumber(dayNumber(candidate)).d === candidate.d;
    if (exists && dayNumber(candidate) <= dayNumber(target)) return candidate;
  }
  return base;
}

// ---------------------------------------------------------------- VTIMEZONE
const vtzCache = new Map<string, string>();

/**
 * A VTIMEZONE for an IANA zone, so files we write are self-contained (RFC 5545
 * requires one per TZID). Yearly DST rules are written as RRULEs; zones
 * without DST get a single STANDARD block.
 */
export function vtimezone(zone: Zone, year: number): Component {
  const key = `${zone.name}:${year}`;
  let text = vtzCache.get(key);
  if (!text) {
    text = buildVtimezone(zone, year).toString();
    if (vtzCache.size > 64) vtzCache.clear();
    vtzCache.set(key, text);
  }
  return new ICAL.Component(ICAL.parse(text));
}

interface Transition {
  at: number;
  from: number; // minutes
  to: number;
}

function transitions(zone: Zone, year: number): Transition[] {
  const out: Transition[] = [];
  let t = Date.UTC(year, 0, 1);
  const end = Date.UTC(year + 1, 0, 1);
  let prev = zone.offset(t);
  for (; t < end; t += DAY) {
    const next = zone.offset(t + DAY);
    if (next !== prev) {
      let lo = t;
      let hi = t + DAY;
      while (hi - lo > MINUTE) {
        const mid = lo + Math.floor((hi - lo) / 2 / MINUTE) * MINUTE;
        if (zone.offset(mid) === prev) lo = mid;
        else hi = mid;
      }
      out.push({ at: hi, from: prev, to: next });
      prev = next;
    }
  }
  return out;
}

const BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

function yearlyRule(local: Wall): string {
  const daysInMonth = new Date(Date.UTC(local.y, local.m, 0)).getUTCDate();
  const nth = local.d + 7 > daysInMonth ? -1 : Math.ceil(local.d / 7);
  return `FREQ=YEARLY;BYMONTH=${local.m};BYDAY=${nth}${BYDAY[weekday(local)]}`;
}

function localWall(at: number, offsetMin: number): Wall {
  const t = new Date(at + offsetMin * MINUTE);
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate(), h: t.getUTCHours(), mi: t.getUTCMinutes(), s: 0 };
}

function observance(kind: "standard" | "daylight", tr: Transition, rrule: string | null): Component {
  const c = new ICAL.Component(kind);
  const w = localWall(tr.at, tr.from);
  c.addPropertyWithValue("dtstart", ICAL.Time.fromData({ year: w.y, month: w.m, day: w.d, hour: w.h, minute: w.mi, second: 0 }));
  c.addPropertyWithValue("tzoffsetfrom", ICAL.UtcOffset.fromSeconds(tr.from * 60));
  c.addPropertyWithValue("tzoffsetto", ICAL.UtcOffset.fromSeconds(tr.to * 60));
  if (rrule) c.addPropertyWithValue("rrule", ICAL.Recur.fromString(rrule));
  return c;
}

function buildVtimezone(zone: Zone, year: number): Component {
  const vtz = new ICAL.Component("vtimezone");
  vtz.addPropertyWithValue("tzid", zone.name);
  const now = transitions(zone, year);
  const later = transitions(zone, year + 1);
  if (now.length === 0) {
    const off = zone.offset(Date.UTC(year, 6, 1));
    vtz.addSubcomponent(observance("standard", { at: Date.UTC(1970, 0, 1) - off * MINUTE, from: off, to: off }, null));
    return vtz;
  }
  const rules = now.map((tr) => yearlyRule(localWall(tr.at, tr.from)));
  const repeats =
    now.length === 2 &&
    later.length === 2 &&
    later.every((tr, i) => yearlyRule(localWall(tr.at, tr.from)) === rules[i] && tr.to === now[i]!.to);
  if (repeats) {
    now.forEach((tr, i) => vtz.addSubcomponent(observance(tr.to > tr.from ? "daylight" : "standard", tr, rules[i]!)));
    return vtz;
  }
  // Irregular zone: list this and the next few years' transitions explicitly.
  for (let y = year; y <= year + 5; y++) {
    for (const tr of transitions(zone, y)) {
      vtz.addSubcomponent(observance(tr.to > tr.from ? "daylight" : "standard", tr, null));
    }
  }
  return vtz;
}

export function ensureVtimezone(res: Resource, zone: Zone, year: number): void {
  const has = res.cal.getAllSubcomponents("vtimezone").some((c) => c.getFirstPropertyValue("tzid") === zone.name);
  if (has) return;
  const vtz = vtimezone(zone, year);
  // VTIMEZONEs go before the events, as most clients write them.
  const events = res.cal.getAllSubcomponents("vevent");
  for (const e of events) res.cal.removeSubcomponent(e);
  res.cal.addSubcomponent(vtz);
  for (const e of events) res.cal.addSubcomponent(e);
}

