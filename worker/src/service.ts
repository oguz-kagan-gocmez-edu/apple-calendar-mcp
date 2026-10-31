/**
 * Use-case layer between the MCP tools and a ``CalendarStore`` (port of service.py):
 * parse the loose input a model sends, resolve defaults, and build search and
 * free-slot finding on top of plain CRUD.
 */

import type {
  Availability,
  Calendar,
  DeleteResult,
  Event,
  EventDraft,
  EventList,
  EventPatch,
  FreeSlot,
  Recurrence,
  Span,
} from "./models";
import { patchIsEmpty } from "./models";
import { findFreeSlots } from "./scheduling";
import { type CalendarStore, InvalidRequestError } from "./store";
import {
  type CivilDate,
  type Parsed,
  type Zone,
  DAY,
  HOUR,
  MINUTE,
  addDays,
  compareDates,
  dayNumber,
  isDateOnly,
  isoWeek,
  parseClock,
  parseDate,
  parseDateTime,
} from "./time";

const MAX_RANGE_DAYS = 366;
const MAX_ALARM_MINUTES = 4 * 7 * 24 * 60; // four weeks
const ALLOWED_URL_SCHEMES = new Set(["http", "https", "mailto", "tel"]);
const DEFAULT_DURATION = HOUR;

export interface TimeContext {
  now: number;
  timezone: string;
  weekday: string;
  iso_week: number;
}

export interface CreateInput {
  title: string;
  start: string;
  end?: string | null;
  duration_minutes?: number | null;
  all_day?: boolean;
  calendar_id?: string | null;
  location?: string | null;
  notes?: string | null;
  url?: string | null;
  alarms_minutes_before?: number[] | null;
  recurrence?: Recurrence | null;
  availability?: Availability | null;
}

export interface UpdateInput {
  event_id: string;
  span?: Span;
  title?: string | null;
  start?: string | null;
  end?: string | null;
  all_day?: boolean | null;
  calendar_id?: string | null;
  location?: string | null;
  notes?: string | null;
  url?: string | null;
  alarms_minutes_before?: number[] | null;
  recurrence?: Recurrence | null;
  remove_recurrence?: boolean;
  availability?: Availability | null;
}

export interface FreeSlotInput {
  start: string;
  end: string;
  duration_minutes: number;
  calendar_ids?: string[] | null;
  working_hours_start?: string;
  working_hours_end?: string;
  include_weekends?: boolean;
  max_slots?: number;
}

export class CalendarService {
  constructor(
    readonly store: CalendarStore,
    readonly zone: Zone,
    private readonly clock: () => number = Date.now,
  ) {}

  // ------------------------------------------------------------ helpers
  now(): number {
    return this.clock();
  }

  private parse(value: string, field: string): Parsed {
    try {
      return parseDateTime(value, this.zone);
    } catch (err) {
      throw new InvalidRequestError(`${field}: ${(err as Error).message}`);
    }
  }

  private range(start: string, end: string): [number, number] {
    const s = this.parse(start, "start").ms;
    let e = this.parse(end, "end").ms;
    if (isDateOnly(end)) e = this.zone.startOfDay(addDays(parseDate(end), 1)); // inclusive end date
    if (e <= s) throw new InvalidRequestError("end must be after start.");
    if (e - s > MAX_RANGE_DAYS * DAY + HOUR) {
      throw new InvalidRequestError("Range too large: query at most 366 days at a time.");
    }
    return [s, e];
  }

  private static limit(limit: number): number {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new InvalidRequestError("limit must be between 1 and 1000.");
    }
    return limit;
  }

  private static alarms(alarms: number[] | null | undefined): number[] | null {
    if (alarms == null) return null;
    for (const m of alarms) {
      if (!Number.isInteger(m) || m < 0 || m > MAX_ALARM_MINUTES) {
        throw new InvalidRequestError(`Alarm offsets must be 0..${MAX_ALARM_MINUTES} minutes before the event.`);
      }
    }
    return [...new Set(alarms)].sort((a, b) => b - a);
  }

  private static url(url: string | null | undefined): string | null | undefined {
    if (!url) return url; // null/undefined keeps, '' clears
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url)?.[1]?.toLowerCase() ?? "";
    if (!ALLOWED_URL_SCHEMES.has(scheme)) {
      throw new InvalidRequestError(
        `URL scheme '${scheme || "(none)"}' not allowed; use one of ${JSON.stringify([...ALLOWED_URL_SCHEMES].sort())}.`,
      );
    }
    return url;
  }

  private checkRecurrence(rec: Recurrence | null | undefined, startDate: CivilDate): void {
    if (rec?.until != null && compareDates(parseDate(rec.until), startDate) < 0) {
      throw new InvalidRequestError("recurrence.until is before the event starts.");
    }
  }

  /** All-day input uses dates; ``end`` is the inclusive last day. */
  private allDayBounds(start: string, end: string | null | undefined): [number, number] {
    const first = this.parse(start, "start").date;
    const last = end ? this.parse(end, "end").date : first;
    if (compareDates(last, first) < 0) throw new InvalidRequestError("end date is before start date.");
    return [this.zone.startOfDay(first), this.zone.startOfDay(addDays(last, 1))];
  }

  private eventList(events: Event[], s: number, e: number, limit: number): EventList {
    const sorted = [...events].sort(byStartThenTitle);
    return {
      range_start: s,
      range_end: e,
      count: Math.min(sorted.length, limit),
      truncated: sorted.length > limit,
      events: sorted.slice(0, limit),
    };
  }

  // ------------------------------------------------------------ queries
  timeContext(): TimeContext {
    const now = Math.floor(this.now() / 1000) * 1000;
    return {
      now,
      timezone: this.zone.name,
      weekday: this.zone.weekdayName(now),
      iso_week: isoWeek(this.zone.date(now)),
    };
  }

  listCalendars(): Promise<Calendar[]> {
    return this.store.listCalendars();
  }

  async listEvents(start: string, end: string, calendarIds?: string[] | null, limit = 200): Promise<EventList> {
    const [s, e] = this.range(start, end);
    const lim = CalendarService.limit(limit);
    return this.eventList(await this.store.listEvents(s, e, calendarIds?.length ? calendarIds : null), s, e, lim);
  }

  async searchEvents(
    query: string,
    start?: string | null,
    end?: string | null,
    calendarIds?: string[] | null,
    limit = 50,
  ): Promise<EventList> {
    const terms = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) throw new InvalidRequestError("query must not be empty.");
    const lim = CalendarService.limit(limit);
    const today = this.zone.date(this.now());
    const s = start ? this.parse(start, "start").ms : this.zone.startOfDay(addDays(today, -30));
    let e: number;
    if (end) [, e] = this.range(start ?? this.zone.iso(s), end);
    else e = s + MAX_RANGE_DAYS * DAY;
    const hits = (await this.store.listEvents(s, e, calendarIds?.length ? calendarIds : null)).filter((ev) => {
      const haystack = [ev.title, ev.location, ev.notes].filter(Boolean).join(" ").toLocaleLowerCase();
      return terms.every((t) => haystack.includes(t));
    });
    return this.eventList(hits, s, e, lim);
  }

  getEvent(eventId: string): Promise<Event> {
    return this.store.getEvent(eventId);
  }

  async findFreeSlots(input: FreeSlotInput): Promise<{ slots: FreeSlot[]; truncated: boolean }> {
    let [s, e] = this.range(input.start, input.end);
    const duration = input.duration_minutes;
    if (!Number.isInteger(duration) || duration < 5 || duration > 24 * 60) {
      throw new InvalidRequestError("duration_minutes must be between 5 and 1440.");
    }
    const maxSlots = input.max_slots ?? 20;
    if (!Number.isInteger(maxSlots) || maxSlots < 1 || maxSlots > 200) {
      throw new InvalidRequestError("max_slots must be between 1 and 200.");
    }
    let dayStart: number, dayEnd: number;
    try {
      dayStart = parseClock(input.working_hours_start ?? "09:00");
      dayEnd = parseClock(input.working_hours_end ?? "18:00");
    } catch (err) {
      throw new InvalidRequestError((err as Error).message);
    }
    s = Math.max(s, Math.floor(this.now() / MINUTE) * MINUTE); // no slots in the past
    const events =
      s < e ? await this.store.listEvents(s, e, input.calendar_ids?.length ? input.calendar_ids : null) : [];
    const [slots, truncated] = findFreeSlots(events, {
      windowStart: s,
      windowEnd: e,
      durationMs: duration * MINUTE,
      zone: this.zone,
      dayStart,
      dayEnd,
      includeWeekends: input.include_weekends ?? false,
      maxSlots,
    });
    return { slots, truncated };
  }

  // ------------------------------------------------------------ commands
  async createEvent(input: CreateInput): Promise<Event> {
    const title = input.title.trim();
    if (!title) throw new InvalidRequestError("title must not be empty.");
    if (input.end != null && input.duration_minutes != null) {
      throw new InvalidRequestError("Pass either end or duration_minutes, not both.");
    }
    const allDay = input.all_day ?? false;
    let s: number, e: number, startDate: CivilDate;
    if (allDay) {
      if (input.duration_minutes != null) {
        throw new InvalidRequestError("duration_minutes is not used for all-day events.");
      }
      [s, e] = this.allDayBounds(input.start, input.end);
      startDate = this.zone.date(s);
    } else {
      const parsed = this.parse(input.start, "start");
      s = parsed.ms;
      startDate = this.zone.date(s);
      if (input.end != null) e = this.parse(input.end, "end").ms;
      else if (input.duration_minutes != null) {
        if (input.duration_minutes < 0) throw new InvalidRequestError("duration_minutes must be >= 0.");
        e = s + input.duration_minutes * MINUTE;
      } else e = s + DEFAULT_DURATION;
      if (e < s) throw new InvalidRequestError("end must not be before start.");
    }
    this.checkRecurrence(input.recurrence, startDate);
    const draft: EventDraft = {
      title,
      start: s,
      end: e,
      all_day: allDay,
      calendar_id: input.calendar_id ?? null,
      location: input.location ?? null,
      notes: input.notes ?? null,
      url: CalendarService.url(input.url) ?? null,
      alarms_minutes_before: CalendarService.alarms(input.alarms_minutes_before) ?? [],
      recurrence: input.recurrence ?? null,
      availability: input.availability ?? null,
    };
    return this.store.createEvent(draft);
  }

  async updateEvent(input: UpdateInput): Promise<Event> {
    const current = await this.store.getEvent(input.event_id);
    let span: Span = input.span ?? "this";
    if (!current.is_recurring) span = "this"; // spans only mean something for series
    const removeRecurrence = input.remove_recurrence ?? false;
    if (input.recurrence != null && removeRecurrence) {
      throw new InvalidRequestError("Pass either recurrence or remove_recurrence, not both.");
    }
    if ((input.recurrence != null || removeRecurrence) && current.is_recurring && span === "this") {
      throw new InvalidRequestError("Changing the repeat rule needs span 'future' or 'all', not 'this'.");
    }
    if (input.title != null && !input.title.trim()) throw new InvalidRequestError("title must not be empty.");

    const [newStart, newEnd] = this.resolveTimes(current, input.start, input.end, input.all_day);
    this.checkRecurrence(input.recurrence, this.zone.date(newStart ?? current.start));

    const patch: EventPatch = {
      title: input.title != null ? input.title.trim() : null,
      start: newStart,
      end: newEnd,
      all_day: input.all_day != null && input.all_day !== current.all_day ? input.all_day : null,
      calendar_id: input.calendar_id != null && input.calendar_id !== current.calendar_id ? input.calendar_id : null,
      location: input.location,
      notes: input.notes,
      url: CalendarService.url(input.url),
      alarms_minutes_before: CalendarService.alarms(input.alarms_minutes_before),
      recurrence: input.recurrence ?? null,
      remove_recurrence: removeRecurrence,
      availability: input.availability ?? null,
    };
    if (patchIsEmpty(patch)) {
      throw new InvalidRequestError("Nothing to update: pass at least one field to change.");
    }
    return this.store.updateEvent(input.event_id, patch, span);
  }

  /** Absolute new start/end, keeping the duration when only one moves. [null, null] = no change. */
  private resolveTimes(
    current: Event,
    start: string | null | undefined,
    end: string | null | undefined,
    allDay: boolean | null | undefined,
  ): [number | null, number | null] {
    if (start == null && end == null && (allDay == null || allDay === current.all_day)) return [null, null];
    const targetAllDay = allDay ?? current.all_day;
    let s: number, e: number;
    if (targetAllDay) {
      const first = start ? this.parse(start, "start").date : this.zone.date(current.start);
      let last: CivilDate;
      if (end != null) last = this.parse(end, "end").date;
      else if (current.all_day) {
        const days = Math.max(dayNumber(this.zone.date(current.end)) - dayNumber(this.zone.date(current.start)), 1);
        last = addDays(first, days - 1);
      } else last = first;
      if (compareDates(last, first) < 0) throw new InvalidRequestError("end date is before start date.");
      s = this.zone.startOfDay(first);
      e = this.zone.startOfDay(addDays(last, 1));
    } else {
      s = start ? this.parse(start, "start").ms : current.start;
      if (end != null) e = this.parse(end, "end").ms;
      else if (start != null) e = s + (current.all_day ? DEFAULT_DURATION : current.end - current.start);
      else e = current.all_day ? s + DEFAULT_DURATION : current.end;
      if (e < s) throw new InvalidRequestError("end must not be before start.");
    }
    if (s === current.start && e === current.end) return [null, null];
    return [s, e];
  }

  async deleteEvent(eventId: string, span: Span = "this"): Promise<DeleteResult> {
    const before = await this.store.deleteEvent(eventId, span);
    return { deleted: true, event_id: eventId, span: before.is_recurring ? span : "this", title: before.title };
  }
}

export function byStartThenTitle(a: Event, b: Event): number {
  return a.start - b.start || (a.title < b.title ? -1 : a.title > b.title ? 1 : 0);
}

