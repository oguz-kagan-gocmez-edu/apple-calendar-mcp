/**
 * Reading and writing iCalendar (.ics) text without a backend (port of ics.py).
 *
 * Export writes one VEVENT per event, and for a repeating event one VEVENT
 * per occurrence in the exported range. Timed events are written in UTC,
 * all-day events as dates.
 *
 * Import reads the subset of RFC 5545 that maps onto this server's events.
 * Anything it can't represent faithfully is reported as skipped with the
 * reason, never imported half-right.
 */

import { type Availability, type Event, type Recurrence, RecurrenceSchema, WEEKDAYS } from "./models";
import { splitEventId } from "./store";
import { DAY, HOUR, MINUTE, Zone, formatDate } from "./time";

const PRODID = "-//apple-calendar-mcp//Export//EN";
const BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const FREQ: Record<string, Recurrence["frequency"]> = {
  DAILY: "daily",
  WEEKLY: "weekly",
  MONTHLY: "monthly",
  YEARLY: "yearly",
};
const DURATION = /^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/;
const NOT_ICS = "This isn't iCalendar text: it should start with BEGIN:VCALENDAR.";

type Params = Record<string, string>;
type Prop = [name: string, params: Params, value: string];

/** One importable event. ``end`` is exclusive; all-day times are local midnights. */
export interface IcsEvent {
  title: string;
  start: number;
  end: number;
  all_day: boolean;
  location: string | null;
  notes: string | null;
  url: string | null;
  alarms: number[];
  recurrence: Recurrence | null;
  availability: Availability | null;
  exdates: number[];
}

export interface IcsSkip {
  title: string;
  reason: string;
}

/** This VEVENT can't be imported faithfully; the message says why. */
class Unsupported extends Error {}

// ---------------------------------------------------------------- writing
function escapeText(text: string): string {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\r\n|\n|\r/g, "\\n");
}

const encoder = new TextEncoder();

/** Fold at 75 octets (RFC 5545 3.1) without splitting a UTF-8 character. */
function fold(line: string): string {
  const out: string[] = [];
  let current = "";
  let size = 0;
  for (const ch of line) {
    const width = encoder.encode(ch).length;
    if (size + width > 75) {
      out.push(current);
      current = " ";
      size = 1;
    }
    current += ch;
    size += width;
  }
  out.push(current);
  return out.join("\r\n");
}

const utc = (ms: number) => new Date(ms).toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
const day = (ms: number, zone: Zone) => formatDate(zone.date(ms)).replace(/-/g, "");

/** iCalendar text for ``events`` (already expanded occurrences). */
export function exportIcs(events: Iterable<Event>, now: number, zone: Zone): string {
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", `PRODID:${PRODID}`, "CALSCALE:GREGORIAN"];
  for (const e of events) {
    const [series, ts] = splitEventId(e.id);
    const uid = e.is_recurring && ts !== null ? `${series}-${ts / 1000}` : series;
    lines.push("BEGIN:VEVENT", `UID:${escapeText(uid)}`, `DTSTAMP:${utc(now)}`);
    if (e.all_day) {
      lines.push(`DTSTART;VALUE=DATE:${day(e.start, zone)}`, `DTEND;VALUE=DATE:${day(e.end, zone)}`);
    } else {
      lines.push(`DTSTART:${utc(e.start)}`, `DTEND:${utc(e.end)}`);
    }
    lines.push(`SUMMARY:${escapeText(e.title)}`);
    if (e.location) lines.push(`LOCATION:${escapeText(e.location)}`);
    if (e.notes) lines.push(`DESCRIPTION:${escapeText(e.notes)}`);
    if (e.url) lines.push(`URL:${e.url.replace(/[\r\n]/g, "")}`);
    if (e.availability === "free") lines.push("TRANSP:TRANSPARENT");
    if (e.status === "confirmed" || e.status === "tentative") lines.push(`STATUS:${e.status.toUpperCase()}`);
    else if (e.status === "canceled") lines.push("STATUS:CANCELLED");
    for (const minutes of e.alarms_minutes_before) {
      lines.push("BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", `TRIGGER:-PT${minutes}M`, "END:VALARM");
    }
    lines.push("END:VEVENT");
  }
  lines.push("END:VCALENDAR");
  return `${lines.map(fold).join("\r\n")}\r\n`;
}

// ---------------------------------------------------------------- reading
function unescapeText(text: string): string {
  return text.replace(/\\([\\;,nN])/g, (_, c: string) => (c === "n" || c === "N" ? "\n" : c));
}

/** Split ``NAME;PARAM=value;...:value`` (parameter values may be quoted). Null if it isn't one. */
function contentLine(line: string): Prop | null {
  const n = line.length;
  let i = 0;
  while (i < n && line[i] !== ";" && line[i] !== ":") i++;
  const name = line.slice(0, i).toUpperCase();
  const params: Params = {};
  while (i < n && line[i] === ";") {
    const eq = line.indexOf("=", i);
    if (eq < 0) return null;
    const key = line.slice(i + 1, eq).toUpperCase();
    i = eq + 1;
    if (line[i] === '"') {
      const close = line.indexOf('"', i + 1);
      if (close < 0) return null;
      params[key] = line.slice(i + 1, close);
      i = close + 1;
    } else {
      let j = i;
      while (j < n && line[j] !== ";" && line[j] !== ":") j++;
      params[key] = line.slice(i, j);
      i = j;
    }
  }
  if (!name || i >= n || line[i] !== ":") return null;
  return [name, params, line.slice(i + 1)];
}

interface RawEvent {
  props: Prop[];
  alarms: Prop[][];
}

function vevents(text: string): RawEvent[] {
  const lines: string[] = [];
  for (const raw of text.split(/\r\n|\n|\r/)) {
    if ((raw.startsWith(" ") || raw.startsWith("\t")) && lines.length) lines[lines.length - 1] += raw.slice(1);
    else if (raw.trim()) lines.push(raw);
  }
  const found: RawEvent[] = [];
  const stack: string[] = [];
  const path = () => stack.join("/");
  let sawCalendar = false;
  for (const line of lines) {
    const parsed = contentLine(line);
    if (!parsed) {
      if (stack[0] === "VCALENDAR") continue; // a broken line inside the file: keep the rest
      throw new RangeError(NOT_ICS);
    }
    const [name, , value] = parsed;
    const kind = value.trim().toUpperCase();
    if (name === "BEGIN") {
      stack.push(kind);
      sawCalendar ||= path() === "VCALENDAR";
      if (path() === "VCALENDAR/VEVENT") found.push({ props: [], alarms: [] });
      else if (path() === "VCALENDAR/VEVENT/VALARM") found[found.length - 1]!.alarms.push([]);
    } else if (name === "END") {
      if (stack[stack.length - 1] !== kind) {
        throw new RangeError("The iCalendar text is damaged: BEGIN and END lines don't match.");
      }
      stack.pop();
    } else if (path() === "VCALENDAR/VEVENT") {
      found[found.length - 1]!.props.push(parsed);
    } else if (path() === "VCALENDAR/VEVENT/VALARM") {
      const alarms = found[found.length - 1]!.alarms;
      alarms[alarms.length - 1]!.push(parsed);
    }
  }
  if (!sawCalendar) throw new RangeError(NOT_ICS);
  if (stack.length) throw new RangeError("The iCalendar text is cut off: a BEGIN has no END.");
  return found;
}

/** A DATE or DATE-TIME value as an instant, and whether it was a date. */
function when(params: Params, value: string, zone: Zone): [number, boolean] {
  const v = value.trim();
  const unreadable = () => new Unsupported(`it has a time that can't be read ('${v.slice(0, 30)}')`);
  const m = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})([Zz])?)?$/.exec(v);
  if (!m) throw unreadable();
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map((p) => Number(p ?? 0)) as [number, number, number, number, number, number];
  const check = new Date(Date.UTC(y, mo - 1, d, h, mi, s));
  if (check.getUTCMonth() !== mo - 1 || check.getUTCDate() !== d || h > 23 || mi > 59 || s > 60) throw unreadable();
  if (m[4] === undefined) {
    return [zone.startOfDay({ y, m: mo, d }), true];
  }
  if ((params.VALUE ?? "").toUpperCase() === "DATE") throw unreadable();
  const wall = { y, m: mo, d, h, mi, s };
  if (m[7]) return [check.getTime(), false];
  const tzid = params.TZID;
  if (!tzid) return [zone.instant(wall), false]; // floating: the user's own zone
  const name = tzid.replace(/^\/+/, "");
  if (!Zone.isValid(name)) {
    throw new Unsupported(`its time zone '${tzid.slice(0, 40)}' isn't a standard zone name`);
  }
  return [new Zone(name).instant(wall), false];
}

/** Milliseconds, or null if the value isn't a duration. */
function duration(value: string): number | null {
  const m = DURATION.exec(value.trim().toUpperCase());
  if (!m || m.slice(2).every((g) => g === undefined)) return null;
  const [weeks, days, hours, minutes, seconds] = m.slice(2).map((g) => Number(g ?? 0)) as [number, number, number, number, number];
  const span = (weeks * 7 + days) * DAY + hours * HOUR + minutes * MINUTE + seconds * 1000;
  return m[1] === "-" ? -span : span;
}

function rule(value: string, start: number, zone: Zone): Recurrence {
  const parts = new Map<string, string>();
  for (const p of value.trim().toUpperCase().split(";")) {
    const eq = p.indexOf("=");
    if (eq > 0) parts.set(p.slice(0, eq), p.slice(eq + 1));
  }
  const take = (key: string) => {
    const v = parts.get(key);
    parts.delete(key);
    return v;
  };
  const freq = take("FREQ") ?? "";
  if (!(freq in FREQ)) {
    throw new Unsupported(`it repeats ${freq.toLowerCase() || "in an unknown way"}, which isn't supported`);
  }
  take("WKST");
  const interval = Number(take("INTERVAL") ?? "1");
  const countText = take("COUNT");
  const count = countText === undefined ? null : Number(countText);
  const untilText = take("UNTIL");
  const until = untilText === undefined ? null : formatDate(zone.date(when({}, untilText, zone)[0]));
  if (!Number.isInteger(interval) || (count !== null && !Number.isInteger(count))) {
    throw new Unsupported("its repeat rule can't be read");
  }

  let days: Recurrence["days_of_week"] = null;
  const byday = take("BYDAY");
  if (byday !== undefined) {
    const codes = byday.split(",");
    if (freq !== "WEEKLY" || codes.some((c) => !BYDAY.includes(c))) {
      throw new Unsupported("it repeats on a pattern like 'the second Tuesday', which isn't supported");
    }
    days = codes.map((c) => WEEKDAYS[BYDAY.indexOf(c)]!);
  }
  // Rules that only restate the start date add nothing.
  const wall = zone.wall(start);
  if ((freq === "MONTHLY" || freq === "YEARLY") && parts.get("BYMONTHDAY") === String(wall.d)) take("BYMONTHDAY");
  if (freq === "YEARLY" && parts.get("BYMONTH") === String(wall.m)) take("BYMONTH");
  if (parts.size) {
    throw new Unsupported(`its repeat rule uses ${[...parts.keys()].sort().join(", ")}, which isn't supported`);
  }
  const checked = RecurrenceSchema.safeParse({ frequency: FREQ[freq], interval, days_of_week: days, count, until });
  if (!checked.success) {
    throw new Unsupported(`its repeat rule isn't valid (${checked.error.issues[0]?.message ?? "unknown"})`);
  }
  return checked.data as Recurrence;
}

function event({ props, alarms }: RawEvent, zone: Zone): IcsEvent {
  const first = new Map<string, [Params, string]>();
  for (const [name, params, value] of props) if (!first.has(name)) first.set(name, [params, value]);
  const text = (name: string) => unescapeText(first.get(name)?.[1] ?? "").trim() || null;
  const upper = (name: string) => (first.get(name)?.[1] ?? "").trim().toUpperCase();

  if (first.has("RECURRENCE-ID")) throw new Unsupported("it is a changed occurrence of a repeating event");
  if (upper("STATUS") === "CANCELLED") throw new Unsupported("it is canceled");
  if (first.has("RDATE")) throw new Unsupported("it repeats on a list of extra dates, which isn't supported");
  const dtstart = first.get("DTSTART");
  if (!dtstart) throw new Unsupported("it has no start time");

  const [start, allDay] = when(dtstart[0], dtstart[1], zone);
  const nextDay = () => zone.startOfDay({ ...zone.date(start + 36 * HOUR) });
  let end: number;
  const dtend = first.get("DTEND");
  if (dtend) {
    end = when(dtend[0], dtend[1], zone)[0];
  } else if (first.has("DURATION")) {
    const span = duration(first.get("DURATION")![1]);
    if (span === null) throw new Unsupported("its duration can't be read");
    end = start + span;
  } else {
    end = allDay ? nextDay() : start;
  }
  if (allDay && end <= start) end = nextDay();
  if (end < start) throw new Unsupported("it ends before it starts");

  const rrule = first.get("RRULE");
  const recurrence = rrule ? rule(rrule[1], start, zone) : null;
  const exdates = new Set<number>();
  if (recurrence) {
    for (const [name, params, value] of props) {
      if (name !== "EXDATE") continue;
      for (const v of value.split(",")) if (v.trim()) exdates.add(when(params, v, zone)[0]);
    }
    if (exdates.has(start)) throw new Unsupported("its first occurrence was removed, which isn't supported");
  }

  const minutes = new Set<number>();
  for (const alarm of alarms) {
    const trigger = alarm.find(([name]) => name === "TRIGGER");
    if (!trigger || (trigger[1].RELATED ?? "START").toUpperCase() !== "START") continue;
    const before = duration(trigger[2]);
    if (before !== null && before <= 0) minutes.add(Math.floor(-before / MINUTE));
  }

  return {
    title: text("SUMMARY") ?? "",
    start,
    end,
    all_day: allDay,
    location: text("LOCATION"),
    notes: text("DESCRIPTION"),
    url: text("URL"),
    alarms: [...minutes].sort((a, b) => b - a),
    recurrence,
    availability: upper("TRANSP") === "TRANSPARENT" ? "free" : null,
    exdates: [...exdates].sort((a, b) => a - b),
  };
}

/**
 * Importable events and the ones left out, in file order. Throws
 * ``RangeError`` when ``text`` isn't iCalendar at all. Times without a zone
 * (floating) are read in ``zone``.
 */
export function parseIcs(text: string, zone: Zone): [IcsEvent[], IcsSkip[]] {
  const events: IcsEvent[] = [];
  const skipped: IcsSkip[] = [];
  for (const raw of vevents(text)) {
    const summary = raw.props.find(([name]) => name === "SUMMARY");
    const title = (summary ? unescapeText(summary[2]).trim() : "") || "(no title)";
    try {
      events.push(event(raw, zone));
    } catch (err) {
      if (!(err instanceof Unsupported)) throw err;
      skipped.push({ title: title.slice(0, 200), reason: `Skipped: ${err.message}.` });
    }
  }
  return [events, skipped];
}
