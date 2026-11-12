/**
 * Use-case layer between the MCP tools and a ``CalendarStore`` (port of service.py):
 * parse the loose input a model sends, resolve defaults, and build search and
 * free-slot finding on top of plain CRUD.
 */

import type {
  Agenda,
  Availability,
  Calendar,
  Conflict,
  ConflictResult,
  DeleteResult,
  Event,
  EventDraft,
  EventList,
  EventPatch,
  FreeSlot,
  Recurrence,
  Reply,
  Span,
} from "./models";
import {
  type ChangeJournal,
  MemoryJournal,
  type Step,
  checkUnchanged,
  recordBlocker,
  revert,
  stepBlocker,
  stepEvent,
} from "./changes";
import { eventRef, patchIsEmpty } from "./models";
import { type ConflictPair, busyTime, findConflicts, findFreeSlots } from "./scheduling";
import { type CalendarStore, CalendarError, InvalidRequestError } from "./store";
import {
  type CivilDate,
  type Parsed,
  type Zone,
  DAY,
  HOUR,
  MINUTE,
  addDays,
  at,
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
const MAX_BUFFER_MINUTES = 240;
const MAX_AGENDA_DAYS = 14;
const MAX_BATCH = 25;

function conflict([a, b, start, end]: ConflictPair): Conflict {
  return { start, end, overlap_minutes: Math.floor((end - start) / MINUTE), events: [eventRef(a), eventRef(b)] };
}

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
  buffer_minutes?: number;
}

export interface AgendaInput {
  day?: string | null;
  days?: number;
  calendar_ids?: string[] | null;
  working_hours_start?: string;
  working_hours_end?: string;
  min_free_minutes?: number;
}

export interface ChangeInfo {
  change_id: string;
  at: number;
  summary: string;
  can_undo: boolean;
  why_not: string | null;
}

export interface UndoResult {
  change_id: string;
  summary: string;
  restored: Event[];
  removed_ids: string[];
}

export type BatchChange =
  | ({ action: "create" } & CreateInput)
  | ({ action: "update" } & Omit<UpdateInput, "span" | "recurrence" | "remove_recurrence">)
  | { action: "delete"; event_id: string };

export interface BatchResult {
  change_id: string;
  results: { action: Step["action"]; event: Event }[];
}

/** Rethrow a domain error with context, keeping its type. */
function withContext(err: unknown, prefix: string, suffix: string): unknown {
  if (!(err instanceof CalendarError)) return err;
  const out = new (err.constructor as new (m: string) => CalendarError)(`${prefix}${err.message} ${suffix}`);
  return out;
}

export class CalendarService {
  constructor(
    readonly store: CalendarStore,
    readonly zone: Zone,
    private readonly clock: () => number = Date.now,
    readonly journal: ChangeJournal = new MemoryJournal(),
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
    const bufferMinutes = input.buffer_minutes ?? 0;
    if (!Number.isInteger(bufferMinutes) || bufferMinutes < 0 || bufferMinutes > MAX_BUFFER_MINUTES) {
      throw new InvalidRequestError(`buffer_minutes must be between 0 and ${MAX_BUFFER_MINUTES}.`);
    }
    const [dayStart, dayEnd] = CalendarService.workingHours(input.working_hours_start, input.working_hours_end);
    const buffer = bufferMinutes * MINUTE;
    s = Math.max(s, Math.floor(this.now() / MINUTE) * MINUTE); // no slots in the past
    // Events just outside the window still push the buffer into it.
    const events =
      s < e
        ? await this.store.listEvents(s - buffer, e + buffer, input.calendar_ids?.length ? input.calendar_ids : null)
        : [];
    const [slots, truncated] = findFreeSlots(events, {
      windowStart: s,
      windowEnd: e,
      durationMs: duration * MINUTE,
      zone: this.zone,
      dayStart,
      dayEnd,
      includeWeekends: input.include_weekends ?? false,
      maxSlots,
      bufferMs: buffer,
    });
    return { slots, truncated };
  }

  private static workingHours(start = "09:00", end = "18:00"): [number, number] {
    try {
      return [parseClock(start), parseClock(end)];
    } catch (err) {
      throw new InvalidRequestError((err as Error).message);
    }
  }

  async findConflicts(start: string, end: string, calendarIds?: string[] | null, limit = 100): Promise<ConflictResult> {
    const [s, e] = this.range(start, end);
    const lim = CalendarService.limit(limit);
    const pairs = findConflicts(await this.store.listEvents(s, e, calendarIds?.length ? calendarIds : null)).map(
      conflict,
    );
    return {
      range_start: s,
      range_end: e,
      count: Math.min(pairs.length, lim),
      truncated: pairs.length > lim,
      conflicts: pairs.slice(0, lim),
    };
  }

  async agenda(input: AgendaInput = {}): Promise<Agenda> {
    const days = input.days ?? 1;
    if (!Number.isInteger(days) || days < 1 || days > MAX_AGENDA_DAYS) {
      throw new InvalidRequestError(`days must be between 1 and ${MAX_AGENDA_DAYS}.`);
    }
    const minFree = input.min_free_minutes ?? 30;
    if (!Number.isInteger(minFree) || minFree < 5 || minFree > 24 * 60) {
      throw new InvalidRequestError("min_free_minutes must be between 5 and 1440.");
    }
    const [dayStart, dayEnd] = CalendarService.workingHours(input.working_hours_start, input.working_hours_end);
    const now = Math.floor(this.now() / 1000) * 1000;
    const first = input.day ? this.parse(input.day, "day").date : this.zone.date(now);
    const s = this.zone.startOfDay(first);
    const e = this.zone.startOfDay(addDays(first, days));

    const events = (await this.store.listEvents(s, e, input.calendar_ids?.length ? input.calendar_ids : null))
      .filter((ev) => ev.status !== "canceled")
      .sort(byStartThenTitle);
    const timed = events.filter((ev) => !ev.all_day);
    const attending = timed.filter((ev) => ev.my_status !== "declined");

    let busy = 0;
    for (let i = 0; i < days; i++) {
      const d = addDays(first, i);
      const ws = this.zone.instant(at(d, dayStart));
      const we = this.zone.instant(at(dayEnd > dayStart ? d : addDays(d, 1), dayEnd));
      busy += busyTime(timed, [ws, we]);
    }

    const [free] = findFreeSlots(timed, {
      windowStart: Math.max(s, Math.floor(now / MINUTE) * MINUTE),
      windowEnd: e,
      durationMs: minFree * MINUTE,
      zone: this.zone,
      dayStart,
      dayEnd,
      includeWeekends: true,
      maxSlots: 50,
    });
    const upNext = attending.find((ev) => ev.start > now);
    return {
      range_start: s,
      range_end: e,
      now,
      happening_now: attending.filter((ev) => ev.start <= now && now < ev.end).map(eventRef),
      up_next: upNext ? eventRef(upNext) : null,
      all_day: events.filter((ev) => ev.all_day).map(eventRef),
      events: timed.map(eventRef),
      busy_minutes: Math.floor(busy / MINUTE),
      free_time: free,
      conflicts: findConflicts(timed).map(conflict),
      invitations: events.filter((ev) => ev.my_status === "pending").map(eventRef),
    };
  }

  // ------------------------------------------------------------ commands
  async createEvent(input: CreateInput): Promise<Event> {
    const created = await this.store.createEvent(this.createDraft(input));
    await this.recordSteps([{ action: "create", before: null, after: created, span: "this" }]);
    return created;
  }

  private createDraft(input: CreateInput): EventDraft {
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
    return draft;
  }

  async updateEvent(input: UpdateInput): Promise<Event> {
    const current = await this.store.getEvent(input.event_id);
    const [patch, span] = this.updatePatch(current, input);
    const updated = await this.store.updateEvent(input.event_id, patch, span);
    await this.recordSteps([{ action: "update", before: current, after: updated, span }]);
    return updated;
  }

  private updatePatch(current: Event, input: Omit<UpdateInput, "event_id">): [EventPatch, Span] {
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
    return [patch, span];
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
    const used: Span = before.is_recurring ? span : "this";
    await this.recordSteps([{ action: "delete", before, after: null, span: used }]);
    return { deleted: true, event_id: eventId, span: used, title: before.title };
  }

  // ------------------------------------------------------------ invitations
  /** Answer an invitation. Not recorded for undo: the organizer has already been told. */
  async respondToInvitation(eventId: string, reply: Reply, span: Span = "all"): Promise<Event> {
    if (span === "future") throw new InvalidRequestError("Answer 'this' occurrence or 'all' of the series.");
    return this.store.respondToInvitation(eventId, reply, span);
  }

  // ------------------------------------------------------------ change log
  private recordSteps(steps: Step[]) {
    return this.journal.record(steps, Math.floor(this.now() / 1000) * 1000);
  }

  async listChanges(): Promise<ChangeInfo[]> {
    return (await this.journal.recent()).map((r) => {
      const why = recordBlocker(r);
      return { change_id: r.id, at: r.at, summary: r.summary, can_undo: why === null, why_not: why };
    });
  }

  async undoChange(changeId?: string | null): Promise<UndoResult> {
    const rec = await this.journal.get(changeId ?? null);
    const why = recordBlocker(rec);
    if (why) throw new InvalidRequestError(`Can't undo ${rec.summary}: ${why}`);
    for (const step of rec.steps) await checkUnchanged(this.store, step); // check before touching anything
    const restored: Event[] = [];
    const removed: string[] = [];
    for (const step of [...rec.steps].reverse()) {
      const [event, gone] = await revert(this.store, step);
      if (event) restored.unshift(event);
      if (gone) removed.push(gone);
    }
    await this.journal.remove(rec.id);
    return { change_id: rec.id, summary: rec.summary, restored, removed_ids: removed };
  }

  /** Apply creates, updates and deletes all-or-nothing, as one undoable change. */
  async applyChanges(changes: BatchChange[]): Promise<BatchResult> {
    if (changes.length < 1 || changes.length > MAX_BATCH) {
      throw new InvalidRequestError(`Pass between 1 and ${MAX_BATCH} changes.`);
    }
    const ids = changes.flatMap((c) => (c.action === "create" ? [] : [c.event_id]));
    if (new Set(ids).size !== ids.length) throw new InvalidRequestError("Each event may appear only once in a batch.");

    // 1. Validate everything (and read current state) before writing anything.
    type Planned =
      | { action: "create"; draft: EventDraft }
      | { action: "update"; before: Event; patch: EventPatch }
      | { action: "delete"; before: Event };
    const plan: Planned[] = [];
    for (const [i, c] of changes.entries()) {
      try {
        if (c.action === "create") {
          plan.push({ action: "create", draft: this.createDraft(c) });
          continue;
        }
        const before = await this.store.getEvent(c.event_id);
        if (c.action === "delete") {
          const why = stepBlocker({ action: "delete", before, after: null, span: "this" });
          if (why) throw new InvalidRequestError(`${why} Delete it on its own with delete_event.`);
          plan.push({ action: "delete", before });
        } else {
          const [patch] = this.updatePatch(before, { ...c, span: "this" });
          plan.push({ action: "update", before, patch });
        }
      } catch (err) {
        throw withContext(err, `Change ${i + 1} (${c.action}): `, "Nothing was changed.");
      }
    }

    // 2. Apply in order; on failure, roll back what was done.
    const steps: Step[] = [];
    for (const [i, p] of plan.entries()) {
      try {
        if (p.action === "create") {
          steps.push({ action: "create", before: null, after: await this.store.createEvent(p.draft), span: "this" });
        } else if (p.action === "delete") {
          const before = await this.store.deleteEvent(p.before.id, "this");
          steps.push({ action: "delete", before, after: null, span: "this" });
        } else {
          const after = await this.store.updateEvent(p.before.id, p.patch, "this");
          steps.push({ action: "update", before: p.before, after, span: "this" });
        }
      } catch (err) {
        const failed = await this.rollback(steps);
        const note = failed.length
          ? `Rolling back the earlier changes also failed for: ${failed.join(", ")}.`
          : "The earlier changes were rolled back.";
        throw withContext(err, `Change ${i + 1} (${p.action}): `, note);
      }
    }
    const rec = await this.recordSteps(steps);
    return { change_id: rec.id, results: steps.map((s) => ({ action: s.action, event: stepEvent(s) })) };
  }

  private async rollback(steps: Step[]): Promise<string[]> {
    const failed: string[] = [];
    for (const step of [...steps].reverse()) {
      try {
        await revert(this.store, step);
      } catch (err) {
        if (!(err instanceof CalendarError)) throw err;
        failed.push(`"${stepEvent(step).title}"`);
      }
    }
    return failed;
  }
}

export function byStartThenTitle(a: Event, b: Event): number {
  return a.start - b.start || (a.title < b.title ? -1 : a.title > b.title ? 1 : 0);
}

