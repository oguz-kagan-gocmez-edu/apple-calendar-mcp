/** Agenda, conflicts and buffered free time (port of the matching tests in tests/test_service.py). */
import { beforeEach, describe, expect, it } from "vitest";

import { MemoryStore } from "../../src/memory-store";
import type { Event } from "../../src/models";
import { busyTime, findConflicts } from "../../src/scheduling";
import { CalendarService } from "../../src/service";
import { InvalidRequestError } from "../../src/store";
import { MINUTE } from "../../src/time";
import { IST, NOW, at } from "./helpers";

let store: MemoryStore;
let service: CalendarService;
beforeEach(() => {
  store = new MemoryStore();
  service = new CalendarService(store, IST, () => NOW);
});

/** A busy Monday 2 Nov; the returned service's clock is 10:15 that day. */
async function monday(): Promise<CalendarService> {
  await service.createEvent({ title: "Standup", start: "2026-11-02T09:00", duration_minutes: 30 });
  await service.createEvent({ title: "Lecture", start: "2026-11-02T10:00", end: "2026-11-02T12:00", location: "B-101" });
  await service.createEvent({ title: "Lunch", start: "2026-11-02T11:30", duration_minutes: 60 });
  store.setMyStatus((await service.createEvent({ title: "Optional sync", start: "2026-11-02T14:00" })).id, "declined");
  store.setMyStatus((await service.createEvent({ title: "Design review", start: "2026-11-02T16:00" })).id, "pending");
  await service.createEvent({ title: "Holiday", start: "2026-11-02", all_day: true });
  await service.createEvent({ title: "Tomorrow", start: "2026-11-03T09:00" });
  return new CalendarService(store, IST, () => at(2, 10, 15));
}

const titles = (refs: { title: string }[]) => refs.map((r) => r.title);

describe("agenda", () => {
  it("gives the day at a glance", async () => {
    const a = await (await monday()).agenda();
    expect([a.range_start, a.range_end]).toEqual([at(2, 0), at(3, 0)]);
    expect(titles(a.happening_now)).toEqual(["Lecture"]);
    expect(a.up_next?.title).toBe("Lunch");
    expect(titles(a.all_day)).toEqual(["Holiday"]);
    expect(titles(a.events)).toEqual(["Standup", "Lecture", "Lunch", "Optional sync", "Design review"]);
    // 30 (standup) + 150 (lecture and lunch, overlap counted once) + 60 (pending invite)
    expect(a.busy_minutes).toBe(240);
    expect(a.free_time.map((s) => [s.start, s.end])).toEqual([
      [at(2, 12, 30), at(2, 16)], // the declined 14:00 sync doesn't block
      [at(2, 17), at(2, 18)],
    ]);
    expect(a.conflicts).toHaveLength(1);
    const c = a.conflicts[0]!;
    expect([c.start, c.end, c.overlap_minutes]).toEqual([at(2, 11, 30), at(2, 12), 30]);
    expect(titles(c.events)).toEqual(["Lecture", "Lunch"]);
    expect(titles(a.invitations)).toEqual(["Design review"]);
  });

  it("never offers a declined event as next", async () => {
    await monday();
    const a = await new CalendarService(store, IST, () => at(2, 13)).agenda();
    expect(a.happening_now).toEqual([]);
    expect(a.up_next?.title).toBe("Design review");
  });

  it("covers another day or several", async () => {
    const svc = await monday();
    const a = await svc.agenda({ day: "2026-11-03" });
    expect(titles(a.events)).toEqual(["Tomorrow"]);
    expect(a.free_time[0]!.start).toBe(at(3, 10));
    const two = await svc.agenda({ days: 2 });
    expect(two.range_end).toBe(at(4, 0));
    expect(two.events).toHaveLength(6);
    expect(two.busy_minutes).toBe(240 + 60);
  });

  it.each([{ days: 0 }, { days: 15 }, { min_free_minutes: 1 }, { working_hours_start: "25:00" }])(
    "rejects %o",
    async (input) => {
      await expect(service.agenda(input)).rejects.toBeInstanceOf(InvalidRequestError);
    },
  );
});

it("finds conflicts and truncates", async () => {
  const svc = await monday();
  let r = await svc.findConflicts("2026-11-02", "2026-11-03");
  expect([r.count, r.truncated]).toEqual([1, false]);
  expect(titles(r.conflicts[0]!.events)).toEqual(["Lecture", "Lunch"]);
  await svc.createEvent({ title: "Office hours", start: "2026-11-02T09:15" }); // overlaps standup, lecture
  r = await svc.findConflicts("2026-11-02", "2026-11-02", null, 1);
  expect([r.count, r.truncated]).toEqual([1, true]);
});

describe("free slots with buffers and declined invitations", () => {
  const spans = (r: { slots: { start: number; end: number }[] }) => r.slots.map((s) => [s.start, s.end]);

  it("keeps a buffer around events", async () => {
    await service.createEvent({ title: "Meeting", start: "2026-11-03T12:00" });
    const r = await service.findFreeSlots({ start: "2026-11-03", end: "2026-11-03", duration_minutes: 60, buffer_minutes: 15 });
    expect(spans(r)).toEqual([
      [at(3, 9), at(3, 11, 45)],
      [at(3, 13, 15), at(3, 18)],
    ]);
  });

  it("applies the buffer of an event just before the range", async () => {
    await service.createEvent({ title: "Early", start: "2026-11-03T09:00" });
    const r = await service.findFreeSlots({ start: "2026-11-03T10:00", end: "2026-11-03", duration_minutes: 60, buffer_minutes: 15 });
    expect(r.slots[0]!.start).toBe(at(3, 10, 15));
  });

  it("treats a declined invitation as free", async () => {
    const e = await service.createEvent({ title: "Big meeting", start: "2026-11-03T09:00", end: "2026-11-03T18:00" });
    const q = { start: "2026-11-03", end: "2026-11-03", duration_minutes: 60 };
    expect((await service.findFreeSlots(q)).slots).toEqual([]);
    store.setMyStatus(e.id, "declined");
    expect((await service.findFreeSlots(q)).slots).toHaveLength(1);
  });

  it("rejects a buffer over four hours", async () => {
    await expect(
      service.findFreeSlots({ start: "2026-11-03", end: "2026-11-03", duration_minutes: 60, buffer_minutes: 241 }),
    ).rejects.toBeInstanceOf(InvalidRequestError);
  });
});

// ------------------------------------------------------------------ pure helpers
const ev = (title: string, start: number, end: number, extra: Partial<Event> = {}): Event => ({
  id: `${title}|0`, calendar_id: "c", calendar_title: "C", title, start, end, all_day: false, location: null,
  notes: null, url: null, is_recurring: false, recurrence: null, alarms_minutes_before: [], availability: "busy",
  status: "none", organizer: null, attendees: [], my_status: null, ...extra,
});

it("pairs every overlap, not touching events", () => {
  const found = findConflicts([
    ev("a", at(2, 9), at(2, 11)),
    ev("b", at(2, 10), at(2, 12)),
    ev("c", at(2, 10, 30), at(2, 10, 45)),
    ev("d", at(2, 12), at(2, 13)),
  ]).map(([a, b, s, e]) => [a.title, b.title, s, e]);
  expect(found).toEqual([
    ["a", "b", at(2, 10), at(2, 11)],
    ["a", "c", at(2, 10, 30), at(2, 10, 45)],
    ["b", "c", at(2, 10, 30), at(2, 10, 45)],
  ]);
});

it.each<Partial<Event>>([{ availability: "free" }, { status: "canceled" }, { my_status: "declined" }, { all_day: true }])(
  "skips %o in conflicts",
  (extra) => {
    expect(findConflicts([ev("a", at(2, 9), at(2, 11)), ev("b", at(2, 10), at(2, 12), extra)])).toEqual([]);
  },
);

it("counts busy time once, inside the window", () => {
  const events = [
    ev("a", at(2, 8), at(2, 10)),
    ev("b", at(2, 9, 30), at(2, 10, 30)),
    ev("free", at(2, 14), at(2, 15), { availability: "free" }),
  ];
  expect(busyTime(events, [at(2, 9), at(2, 18)])).toBe(90 * MINUTE);
});
