/**
 * Domain types, mirroring the Python package's ``models.py``.
 *
 * Inside the worker, times are epoch milliseconds. They become ISO 8601
 * strings (in the user's zone) only at the MCP boundary, in ``tools.ts``.
 * ``end`` is exclusive; an all-day event on 3 Nov is 3 Nov 00:00 → 4 Nov 00:00.
 */

import { z } from "zod";

export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Weekday = (typeof WEEKDAYS)[number];

export const AVAILABILITY = ["busy", "free", "tentative", "unavailable", "not_supported"] as const;
export type Availability = (typeof AVAILABILITY)[number];
export type EventStatus = "none" | "confirmed" | "tentative" | "canceled";
export type AttendeeStatus =
  | "unknown"
  | "pending"
  | "accepted"
  | "declined"
  | "tentative"
  | "delegated"
  | "completed"
  | "in_process";

export const SPANS = ["this", "future", "all"] as const;
/** Which occurrences of a recurring event a change applies to. */
export type Span = (typeof SPANS)[number];

export const RecurrenceSchema = z
  .object({
    frequency: z.enum(["daily", "weekly", "monthly", "yearly"]),
    interval: z.number().int().min(1).max(999).default(1).describe("Every N days/weeks/months/years."),
    days_of_week: z
      .array(z.enum(WEEKDAYS))
      .nullish()
      .describe("Weekly only: which weekdays, e.g. ['mon', 'wed']."),
    count: z.number().int().min(1).max(5000).nullish().describe("Stop after N occurrences."),
    until: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/)
      .nullish()
      .describe("Last date (inclusive) an occurrence may fall on, YYYY-MM-DD."),
  })
  .strict()
  .superRefine((r, ctx) => {
    if (r.count != null && r.until != null) {
      ctx.addIssue({ code: "custom", message: "Set at most one of 'count' and 'until'." });
    }
    if (r.days_of_week != null) {
      if (r.frequency !== "weekly") {
        ctx.addIssue({ code: "custom", message: "'days_of_week' is only valid for weekly recurrence." });
      } else if (r.days_of_week.length === 0) {
        ctx.addIssue({ code: "custom", message: "'days_of_week' must not be empty." });
      }
    }
  })
  .transform(
    (r): Recurrence => ({
      frequency: r.frequency,
      interval: r.interval,
      // canonical order, no duplicates
      days_of_week: r.days_of_week ? WEEKDAYS.filter((d) => r.days_of_week!.includes(d)) : null,
      count: r.count ?? null,
      until: r.until ?? null,
    }),
  );

export interface Recurrence {
  frequency: "daily" | "weekly" | "monthly" | "yearly";
  interval: number;
  days_of_week: Weekday[] | null;
  count: number | null;
  until: string | null; // YYYY-MM-DD
}

export interface Calendar {
  id: string;
  title: string;
  source: string | null;
  color: string | null;
  writable: boolean;
  is_default: boolean;
}

export interface Attendee {
  name: string | null;
  email: string | null;
  status: AttendeeStatus;
}

export interface Event {
  id: string;
  calendar_id: string;
  calendar_title: string;
  title: string;
  start: number;
  end: number;
  all_day: boolean;
  location: string | null;
  notes: string | null;
  url: string | null;
  is_recurring: boolean;
  recurrence: Recurrence | null;
  alarms_minutes_before: number[];
  availability: Availability;
  status: EventStatus;
  organizer: string | null;
  attendees: Attendee[];
}

export interface EventList {
  range_start: number;
  range_end: number;
  count: number;
  truncated: boolean;
  events: Event[];
}

export interface FreeSlot {
  start: number;
  end: number;
  duration_minutes: number;
}

export interface DeleteResult {
  deleted: boolean;
  event_id: string;
  span: Span;
  title: string;
}

/** Fully specified new event (validated by the service). */
export interface EventDraft {
  title: string;
  start: number;
  end: number;
  all_day: boolean;
  calendar_id: string | null;
  location: string | null;
  notes: string | null;
  url: string | null;
  alarms_minutes_before: number[];
  recurrence: Recurrence | null;
  availability: Availability | null;
}

/**
 * Partial update. ``undefined``/``null`` leaves a field unchanged; for text
 * fields an empty string clears it. ``start``/``end`` are absolute times for
 * the targeted occurrence.
 */
export interface EventPatch {
  title?: string | null;
  start?: number | null;
  end?: number | null;
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

export function patchIsEmpty(p: EventPatch): boolean {
  return Object.entries(p).every(([k, v]) => v == null || (k === "remove_recurrence" && v === false));
}

export function touchesRecurrence(p: EventPatch): boolean {
  return p.recurrence != null || p.remove_recurrence === true;
}
