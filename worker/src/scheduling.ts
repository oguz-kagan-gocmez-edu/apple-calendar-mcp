/**
 * Free-slot and conflict search: pure interval arithmetic, no I/O (port of scheduling.py).
 *
 * Free slots:
 * 1. Keep events that block time, pad them with the buffer, sort. O(n log n)
 * 2. Merge overlapping busy intervals.                            O(n)
 * 3. Per day, intersect working hours with the window and
 *    subtract busy intervals with a two-pointer sweep.             O(n + d)
 *
 * Conflicts: sort by start and sweep, keeping the events still running;
 * each new event overlaps exactly those.                 O(n log n + k)
 */

import type { Event, FreeSlot } from "./models";
import { splitEventId } from "./store";
import { type Zone, addDays, at, compareDates, weekday, MINUTE } from "./time";

export type Interval = [number, number];

/** Whether the event takes the time: not 'free', not canceled, not declined by you. */
export function blocksTime(event: Event, ignoreAllDay: boolean): boolean {
  if (event.status === "canceled" || event.availability === "free" || event.my_status === "declined") return false;
  return !(ignoreAllDay && event.all_day);
}

export type ConflictPair = [a: Event, b: Event, start: number, end: number];

/**
 * Pairs of timed events that both block time and overlap. ``a`` starts first
 * (ties broken by end, then title). Touching events don't conflict.
 */
export function findConflicts(events: Iterable<Event>): ConflictPair[] {
  const timed = [...events]
    .filter((e) => blocksTime(e, true) && e.end > e.start)
    .sort((a, b) => a.start - b.start || a.end - b.end || (a.title < b.title ? -1 : a.title > b.title ? 1 : 0));
  let active: Event[] = [];
  const found: ConflictPair[] = [];
  for (const e of timed) {
    active = active.filter((a) => a.end > e.start);
    for (const a of active) found.push([a, e, e.start, Math.min(a.end, e.end)]);
    active.push(e);
  }
  return found;
}

/**
 * Events that look like copies of each other: ``[copies, repeats]`` per group.
 *
 * Copies share the title (case and spacing ignored), start, end and all-day
 * flag, and belong to different series. Canceled events are skipped. Two
 * repeating series that are copies give one group (their first shared
 * occurrence), and ``repeats`` counts the shared occurrences.
 */
export function findDuplicates(events: Iterable<Event>): [Event[], number][] {
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
  const sorted = [...events].sort(
    (a, b) => a.start - b.start || a.end - b.end || cmp(a.calendar_title, b.calendar_title) || cmp(a.id, b.id),
  );
  const buckets = new Map<string, Map<string, Event>>();
  for (const e of sorted) {
    if (e.status === "canceled") continue;
    const title = e.title.toLowerCase().split(/\s+/).filter(Boolean).join(" ");
    const key = JSON.stringify([title, e.start, e.end, e.all_day]);
    let bySeries = buckets.get(key);
    if (!bySeries) buckets.set(key, (bySeries = new Map()));
    const series = splitEventId(e.id)[0];
    if (!bySeries.has(series)) bySeries.set(series, e);
  }
  const groups: [Event[], number][] = [];
  const seriesGroup = new Map<string, number>();
  for (const bySeries of buckets.values()) {
    if (bySeries.size < 2) continue;
    const copies = [...bySeries.values()];
    if (copies.every((e) => e.is_recurring)) {
      const pair = JSON.stringify([...bySeries.keys()].sort());
      const seen = seriesGroup.get(pair);
      if (seen !== undefined) {
        groups[seen]![1] += 1;
        continue;
      }
      seriesGroup.set(pair, groups.length);
    }
    groups.push([copies, 1]);
  }
  return groups;
}

/** Milliseconds inside ``window`` covered by blocking timed events, overlaps counted once. */
export function busyTime(events: Iterable<Event>, window: Interval): number {
  const [wStart, wEnd] = window;
  const busy = mergeIntervals(
    [...events].filter((e) => blocksTime(e, true)).map((e): Interval => [Math.max(e.start, wStart), Math.min(e.end, wEnd)]),
  );
  return busy.reduce((sum, [s, e]) => sum + (e - s), 0);
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
  /** Keep this much time clear before and after every busy event. */
  bufferMs?: number;
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
  const buffer = o.bufferMs ?? 0;
  if (o.durationMs <= 0) throw new RangeError("duration must be positive");
  if (buffer < 0) throw new RangeError("buffer must not be negative");
  if (o.windowEnd <= o.windowStart) return [[], false];

  // Zero-length markers (e.g. a 23:59 deadline) take no time, buffer or not.
  const busy = mergeIntervals(
    [...events]
      .filter((e) => e.end > e.start && blocksTime(e, ignoreAllDay))
      .map((e): Interval => [e.start - buffer, e.end + buffer]),
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
