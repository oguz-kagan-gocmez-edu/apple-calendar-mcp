/**
 * Free-slot search: pure interval arithmetic, no I/O (port of scheduling.py).
 *
 * 1. Keep events that block time and sort them.               O(n log n)
 * 2. Merge overlapping busy intervals.                        O(n)
 * 3. Per day, intersect working hours with the window and
 *    subtract busy intervals with a two-pointer sweep.         O(n + d)
 */

import type { Event, FreeSlot } from "./models";
import { type Zone, addDays, at, compareDates, weekday, MINUTE } from "./time";

export type Interval = [number, number];

export function blocksTime(event: Event, ignoreAllDay: boolean): boolean {
  if (event.status === "canceled" || event.availability === "free") return false;
  return !(ignoreAllDay && event.all_day);
}

/** Merge overlapping or touching intervals. Input order does not matter. */
export function mergeIntervals(intervals: Iterable<Interval>): Interval[] {
  const sorted = [...intervals].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: Interval[] = [];
  for (const [start, end] of sorted) {
    if (end <= start) continue;
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/**
 * Free parts of ``window`` not covered by sorted, disjoint ``busy``. ``first``
 * is where to start scanning; the returned index lets the caller resume.
 */
export function subtract(window: Interval, busy: Interval[], first = 0): [Interval[], number] {
  const [wStart, wEnd] = window;
  let i = first;
  while (i < busy.length && busy[i]![1] <= wStart) i++;
  const resume = i;
  const free: Interval[] = [];
  let cursor = wStart;
  while (i < busy.length && busy[i]![0] < wEnd) {
    const [bStart, bEnd] = busy[i]!;
    if (bStart > cursor) free.push([cursor, bStart]);
    cursor = Math.max(cursor, bEnd);
    i++;
  }
  if (cursor < wEnd) free.push([cursor, wEnd]);
  return [free, resume];
}

export interface SlotOptions {
  windowStart: number;
  windowEnd: number;
  durationMs: number;
  zone: Zone;
  dayStart?: number; // minutes after midnight
  dayEnd?: number;
  includeWeekends?: boolean;
  ignoreAllDay?: boolean;
  maxSlots?: number;
}

/**
 * Gaps of at least ``durationMs`` inside daily working hours. Returns
 * ``[slots, truncated]``. If dayEnd <= dayStart the window wraps past midnight.
 */
export function findFreeSlots(events: Iterable<Event>, o: SlotOptions): [FreeSlot[], boolean] {
  const dayStart = o.dayStart ?? 9 * 60;
  const dayEnd = o.dayEnd ?? 18 * 60;
  const ignoreAllDay = o.ignoreAllDay ?? true;
  const maxSlots = o.maxSlots ?? 20;
  if (o.durationMs <= 0) throw new RangeError("duration must be positive");
  if (o.windowEnd <= o.windowStart) return [[], false];

  const busy = mergeIntervals(
    [...events].filter((e) => blocksTime(e, ignoreAllDay)).map((e): Interval => [e.start, e.end]),
  );
  const slots: FreeSlot[] = [];
  let cursor = 0;
  // Start a day early so a wrap-around window from "yesterday" is included.
  let day = addDays(o.zone.date(o.windowStart), -1);
  const lastDay = o.zone.date(o.windowEnd);
  while (compareDates(day, lastDay) <= 0) {
    if (o.includeWeekends || weekday(day) < 5) {
      let ws = o.zone.instant(at(day, dayStart));
      let we = o.zone.instant(at(day, dayEnd));
      if (we <= ws) we = o.zone.instant(at(addDays(day, 1), dayEnd));
      ws = Math.max(ws, o.windowStart);
      we = Math.min(we, o.windowEnd);
      if (ws < we) {
        const [gaps, next] = subtract([ws, we], busy, cursor);
        cursor = next;
        for (const [gStart, gEnd] of gaps) {
          if (gEnd - gStart >= o.durationMs) {
            if (slots.length >= maxSlots) return [slots, true];
            slots.push({ start: gStart, end: gEnd, duration_minutes: Math.floor((gEnd - gStart) / MINUTE) });
          }
        }
      }
    }
    day = addDays(day, 1);
  }
  return [slots, false];
}
