/** Port of tests/test_scheduling.py. */
import { expect, it } from "vitest";

import type { Event } from "../../src/models";
import { findFreeSlots, mergeIntervals, subtract } from "../../src/scheduling";
import { MINUTE } from "../../src/time";
import { IST, at } from "./helpers";

const ev = (start: number, end: number, extra: Partial<Event> = {}): Event => ({
  id: "x|0", calendar_id: "c", calendar_title: "C", title: "t", start, end, all_day: false, location: null,
  notes: null, url: null, is_recurring: false, recurrence: null, alarms_minutes_before: [], availability: "busy",
  status: "none", organizer: null, attendees: [], ...extra,
});

const slots = (events: Event[], o: Partial<Parameters<typeof findFreeSlots>[1]> = {}): [number, number][] =>
  findFreeSlots(events, { windowStart: at(2, 0), windowEnd: at(3, 0), durationMs: 30 * MINUTE, zone: IST, ...o })[0].map((s) => [s.start, s.end]);

it("merges overlapping and touching intervals", () => {
  expect(mergeIntervals([[at(2, 11), at(2, 12)], [at(2, 9), at(2, 10)], [at(2, 10), at(2, 11, 30)]])).toEqual([[at(2, 9), at(2, 12)]]);
  expect(mergeIntervals([[at(2, 9), at(2, 9)]])).toEqual([]);
});

it("subtracts busy time", () => {
  expect(subtract([at(2, 9), at(2, 18)], [[at(2, 8), at(2, 10)], [at(2, 12), at(2, 13)]])[0]).toEqual([
    [at(2, 10), at(2, 12)],
    [at(2, 13), at(2, 18)],
  ]);
});

it("gives working hours for an empty day", () => expect(slots([])).toEqual([[at(2, 9), at(2, 18)]]));

it("splits the day around busy blocks", () => {
  expect(slots([ev(at(2, 10), at(2, 11)), ev(at(2, 10, 30), at(2, 12))])).toEqual([[at(2, 9), at(2, 10)], [at(2, 12), at(2, 18)]]);
});

it("drops short gaps", () => expect(slots([ev(at(2, 9, 20), at(2, 17, 50))])).toEqual([]));

it("ignores free, canceled and all-day events", () => {
  const events = [
    ev(at(2, 9), at(2, 18), { availability: "free" }),
    ev(at(2, 9), at(2, 18), { status: "canceled" }),
    ev(at(2, 0), at(3, 0), { all_day: true }),
  ];
  expect(slots(events)).toEqual([[at(2, 9), at(2, 18)]]);
  expect(slots([ev(at(2, 0), at(3, 0), { all_day: true })], { ignoreAllDay: false })).toEqual([]);
});

it("skips weekends unless asked", () => {
  expect(slots([], { windowStart: at(7, 0), windowEnd: at(9, 0) })).toEqual([]);
  expect(slots([], { windowStart: at(7, 0), windowEnd: at(9, 0), includeWeekends: true })).toHaveLength(2);
});

it("clips to the window and wraps overnight hours", () => {
  expect(slots([], { windowStart: at(2, 15) })).toEqual([[at(2, 15), at(2, 18)]]);
  expect(slots([], { windowEnd: at(3, 12), dayStart: 22 * 60, dayEnd: 2 * 60 })).toContainEqual([at(2, 22), at(3, 2)]);
});

it("truncates and validates", () => {
  const [found, truncated] = findFreeSlots([], { windowStart: at(2, 0), windowEnd: at(7, 0), durationMs: 30 * MINUTE, zone: IST, maxSlots: 2 });
  expect(found).toHaveLength(2);
  expect(truncated).toBe(true);
  expect(() => findFreeSlots([], { windowStart: at(2, 0), windowEnd: at(3, 0), durationMs: 0, zone: IST })).toThrow(RangeError);
});
