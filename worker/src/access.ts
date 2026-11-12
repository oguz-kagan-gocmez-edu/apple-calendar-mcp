/**
 * Per-calendar access control in front of any store (port of access.py).
 *
 * Levels: "write" (default), "read" (writes refused) and "hidden" (the
 * calendar, its events and its ids behave as if they didn't exist).
 * Calendars are named by id or title, case-insensitively; "hidden" wins over
 * "read"; with an ``only`` list every calendar not on it is hidden.
 */

import type { Calendar, Event, EventDraft, EventPatch, Span } from "./models";
import { type CalendarStore, NotFoundError, ReadOnlyCalendarError } from "./store";

export type Level = "write" | "read" | "hidden";

function names(value: string | undefined | null): Set<string> {
  return new Set(
    (value ?? "")
      .split(",")
      .map((n) => n.trim().toLowerCase())
      .filter(Boolean),
  );
}

export class AccessPolicy {
  private constructor(
    readonly readOnly: Set<string>,
    readonly hidden: Set<string>,
    readonly only: Set<string> | null,
  ) {}

  /** From comma-separated lists of calendar ids or titles. */
  static parse(readOnly?: string | null, hidden?: string | null, only?: string | null): AccessPolicy {
    const allow = names(only);
    return new AccessPolicy(names(readOnly), names(hidden), allow.size ? allow : null);
  }

  get isOpen(): boolean {
    return !this.readOnly.size && !this.hidden.size && this.only === null;
  }

  private static matches(cal: Calendar, set: Set<string>): boolean {
    return set.has(cal.id.toLowerCase()) || set.has(cal.title.trim().toLowerCase());
  }

  level(cal: Calendar): Level {
    if (AccessPolicy.matches(cal, this.hidden)) return "hidden";
    if (this.only && !AccessPolicy.matches(cal, this.only)) return "hidden";
    if (AccessPolicy.matches(cal, this.readOnly)) return "read";
    return "write";
  }
}

export class AccessControlledStore implements CalendarStore {
  constructor(
    readonly inner: CalendarStore,
    readonly policy: AccessPolicy,
  ) {}

  private async levels(): Promise<[Map<string, Level>, string | null]> {
    const cals = await this.inner.listCalendars();
    return [new Map(cals.map((c) => [c.id, this.policy.level(c)])), cals.find((c) => c.is_default)?.id ?? null];
  }

  private static checkWritable(calendarId: string | null, levels: Map<string, Level>, title = ""): void {
    const level = levels.get(calendarId ?? "") ?? "write";
    if (level === "hidden") throw new NotFoundError(`No calendar with id '${calendarId}'.`);
    if (level === "read") {
      throw new ReadOnlyCalendarError(
        `Calendar '${title || calendarId}' is read-only for this server (access settings).`,
      );
    }
  }

  private static visible(event: Event, levels: Map<string, Level>, eventId: string): Event {
    if (levels.get(event.calendar_id) === "hidden") throw new NotFoundError(`No event with id '${eventId}'.`);
    return event;
  }

  // ------------------------------------------------------------ reads
  async listCalendars(): Promise<Calendar[]> {
    const out: Calendar[] = [];
    for (const cal of await this.inner.listCalendars()) {
      const level = this.policy.level(cal);
      if (level === "hidden") continue;
      out.push(level === "read" ? { ...cal, writable: false } : cal);
    }
    return out;
  }

  async listEvents(start: number, end: number, calendarIds?: string[] | null): Promise<Event[]> {
    const [levels] = await this.levels();
    let wanted: string[] | null;
    if (calendarIds?.length) {
      // Hidden ids are dropped quietly, the same as ids that don't exist.
      wanted = calendarIds.filter((id) => levels.get(id) !== "hidden");
    } else if ([...levels.values()].includes("hidden")) {
      wanted = [...levels].filter(([, l]) => l !== "hidden").map(([id]) => id);
    } else wanted = null;
    if (wanted && !wanted.length) return [];
    const events = await this.inner.listEvents(start, end, wanted);
    return events.filter((e) => levels.get(e.calendar_id) !== "hidden");
  }

  async getEvent(eventId: string): Promise<Event> {
    const [levels] = await this.levels();
    return AccessControlledStore.visible(await this.inner.getEvent(eventId), levels, eventId);
  }

  // ------------------------------------------------------------ writes
  async createEvent(draft: EventDraft): Promise<Event> {
    const [levels, fallback] = await this.levels();
    const target = draft.calendar_id ?? fallback;
    if (draft.calendar_id == null && (levels.get(target ?? "") ?? "write") !== "write") {
      throw new ReadOnlyCalendarError(
        "The default calendar is read-only for this server; pass calendar_id (see list_calendars).",
      );
    }
    AccessControlledStore.checkWritable(target, levels);
    return this.inner.createEvent(draft);
  }

  async updateEvent(eventId: string, patch: EventPatch, span: Span): Promise<Event> {
    const [levels] = await this.levels();
    const current = AccessControlledStore.visible(await this.inner.getEvent(eventId), levels, eventId);
    AccessControlledStore.checkWritable(current.calendar_id, levels, current.calendar_title);
    if (patch.calendar_id != null) AccessControlledStore.checkWritable(patch.calendar_id, levels);
    return this.inner.updateEvent(eventId, patch, span);
  }

  async deleteEvent(eventId: string, span: Span): Promise<Event> {
    const [levels] = await this.levels();
    const current = AccessControlledStore.visible(await this.inner.getEvent(eventId), levels, eventId);
    AccessControlledStore.checkWritable(current.calendar_id, levels, current.calendar_title);
    return this.inner.deleteEvent(eventId, span);
  }
}
