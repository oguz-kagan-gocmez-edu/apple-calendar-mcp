/**
 * The service scenarios from tests/test_service.py, run against the CalDAV
 * store and a real Radicale server: the worker must answer exactly like the
 * Python backends.
 */
import { beforeEach, describe, expect, it } from "vitest";

import type { Recurrence } from "../../src/models";
import { RecurrenceSchema } from "../../src/models";
import type { CalendarService, CreateInput } from "../../src/service";
import { CalendarError, InvalidRequestError, NotFoundError } from "../../src/store";
import { IST, at, freshService } from "./helpers";

let service: CalendarService;
beforeEach(async () => {
  service = await freshService();
});

const rec = (r: Record<string, unknown>): Recurrence => RecurrenceSchema.parse(r);

async function titles(start = "2026-11-01", end = "2026-11-30"): Promise<string[]> {
  return (await service.listEvents(start, end)).events.map((e) => e.title);
}

const day = (ms: number): number => IST.date(ms).d;

// ------------------------------------------------------------------ create
describe("create", () => {
  it("defaults to one hour in the default calendar", async () => {
    const e = await service.createEvent({ title: "Study", start: "2026-11-03T14:00" });
    expect([e.start, e.end]).toEqual([at(3, 14), at(3, 15)]);
    expect(e.calendar_id).toBe("home");
  });

  it("takes duration_minutes", async () => {
    const e = await service.createEvent({ title: "Call", start: "2026-11-03T14:00", duration_minutes: 25 });
    expect(e.end).toBe(at(3, 14, 25));
  });

  it("turns an inclusive all-day end into an exclusive one", async () => {
    const e = await service.createEvent({ title: "Trip", start: "2026-11-05", end: "2026-11-07", all_day: true });
    expect([e.start, e.end]).toEqual([at(5, 0), at(8, 0)]);
  });

  it("normalises alarms", async () => {
    const e = await service.createEvent({ title: "X", start: "2026-11-03T14:00", alarms_minutes_before: [10, 60, 10] });
    expect(e.alarms_minutes_before).toEqual([60, 10]);
  });

  it.each<Partial<CreateInput>>([
    { title: "  " },
    { end: "2026-11-03T13:00" },
    { end: "2026-11-03T15:00", duration_minutes: 30 },
    { alarms_minutes_before: [-5] },
    { url: "javascript:alert(1)" },
    { recurrence: rec({ frequency: "daily", until: "2026-01-01" }) },
    { calendar_id: "nope" },
  ])("rejects invalid input %o", async (extra) => {
    await expect(service.createEvent({ title: "X", start: "2026-11-03T14:00", ...extra })).rejects.toBeInstanceOf(
      CalendarError,
    );
  });
});

// ------------------------------------------------------------------ queries
describe("queries", () => {
  it("treats a bare end date as inclusive", async () => {
    await service.createEvent({ title: "Late", start: "2026-11-03T23:00" });
    expect(await titles("2026-11-03", "2026-11-03")).toEqual(["Late"]);
  });

  it("limits ranges", async () => {
    await expect(service.listEvents("2026-11-05", "2026-11-04T00:00")).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(service.listEvents("2026-01-01", "2027-06-01")).rejects.toBeInstanceOf(InvalidRequestError);
  });

  it("truncates", async () => {
    for (let h = 9; h < 14; h++) await service.createEvent({ title: `E${h}`, start: `2026-11-03T${String(h).padStart(2, "0")}:00` });
    const res = await service.listEvents("2026-11-03", "2026-11-03", null, 3);
    expect(res.count).toBe(3);
    expect(res.truncated).toBe(true);
    expect(res.events.map((e) => e.title)).toEqual(["E9", "E10", "E11"]);
  });

  it("filters by calendar", async () => {
    await service.createEvent({ title: "A", start: "2026-11-03T09:00", calendar_id: "work" });
    await service.createEvent({ title: "B", start: "2026-11-03T10:00" });
    expect((await service.listEvents("2026-11-03", "2026-11-03", ["work"])).events.map((e) => e.title)).toEqual(["A"]);
  });

  it("searches all terms, case-insensitively", async () => {
    await service.createEvent({ title: "OS lecture", start: "2026-11-03T09:00", location: "AB1 245" });
    await service.createEvent({ title: "OS lab", start: "2026-11-04T09:00" });
    expect((await service.searchEvents("os ab1")).events.map((e) => e.title)).toEqual(["OS lecture"]);
    expect((await service.searchEvents("OS")).events).toHaveLength(2);
  });

  it("skips the past when finding free slots", async () => {
    await service.createEvent({ title: "Busy", start: "2026-11-02T09:00", end: "2026-11-02T12:00" });
    const res = await service.findFreeSlots({ start: "2026-11-02", end: "2026-11-02", duration_minutes: 60 });
    expect(res.slots.map((s) => [s.start, s.end])).toEqual([[at(2, 12), at(2, 18)]]);
  });

  it("reports the time context", () => {
    const ctx = service.timeContext();
    expect(ctx.weekday).toBe("Monday");
    expect(ctx.timezone).toBe("Europe/Istanbul");
    expect(ctx.iso_week).toBe(45);
  });
});

// ------------------------------------------------------------------ update
describe("update", () => {
  it("keeps the duration when moving", async () => {
    const e = await service.createEvent({ title: "M", start: "2026-11-03T14:00", duration_minutes: 30 });
    const moved = await service.updateEvent({ event_id: e.id, start: "2026-11-04T10:00" });
    expect([moved.start, moved.end]).toEqual([at(4, 10), at(4, 10, 30)]);
  });

  it("clears text with an empty string", async () => {
    const e = await service.createEvent({ title: "M", start: "2026-11-03T14:00", location: "Room 1", notes: "n" });
    const u = await service.updateEvent({ event_id: e.id, location: "" });
    expect(u.location).toBeNull();
    expect(u.notes).toBe("n");
  });

  it("turns a timed event into an all-day one", async () => {
    const e = await service.createEvent({ title: "M", start: "2026-11-03T14:00" });
    const u = await service.updateEvent({ event_id: e.id, all_day: true });
    expect(u.all_day).toBe(true);
    expect([u.start, u.end]).toEqual([at(3, 0), at(4, 0)]);
  });

  it("moves to another calendar", async () => {
    const e = await service.createEvent({ title: "M", start: "2026-11-03T14:00" });
    expect((await service.updateEvent({ event_id: e.id, calendar_id: "work" })).calendar_id).toBe("work");
  });

  it("refuses empty updates", async () => {
    const e = await service.createEvent({ title: "M", start: "2026-11-03T14:00" });
    await expect(service.updateEvent({ event_id: e.id })).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(service.updateEvent({ event_id: e.id, start: "2026-11-03T14:00" })).rejects.toBeInstanceOf(
      InvalidRequestError,
    );
  });
});

// ------------------------------------------------------------------ recurring
describe("recurring", () => {
  let weekly: string[];

  /** Mon+Wed lecture, 6 occurrences: 2,4,9,11,16,18 Nov. */
  beforeEach(async () => {
    await service.createEvent({
      title: "Lecture",
      start: "2026-11-02T10:40",
      duration_minutes: 110,
      recurrence: rec({ frequency: "weekly", days_of_week: ["mon", "wed"], count: 6 }),
    });
    weekly = (await service.listEvents("2026-11-01", "2026-11-30")).events.map((e) => e.id);
  });

  async function days(title = "Lecture"): Promise<number[]> {
    return (await service.listEvents("2026-11-01", "2026-11-30")).events.filter((e) => e.title === title).map((e) => day(e.start));
  }

  it("expands with unique ids", async () => {
    expect(await days()).toEqual([2, 4, 9, 11, 16, 18]);
    expect(new Set(weekly).size).toBe(6);
    expect((await service.getEvent(weekly[3]!)).start).toBe(at(11, 10, 40));
  });

  it("updates one occurrence", async () => {
    const u = await service.updateEvent({ event_id: weekly[2]!, span: "this", title: "Midterm", start: "2026-11-09T09:00" });
    expect(u.id).toBe(weekly[2]);
    expect([u.start, u.end]).toEqual([at(9, 9), at(9, 10, 50)]);
    expect(await days()).toEqual([2, 4, 11, 16, 18]);
    expect(await days("Midterm")).toEqual([9]);
  });

  it("splits the series for 'future'", async () => {
    await service.updateEvent({ event_id: weekly[2]!, span: "future", location: "New room" });
    const evs = (await service.listEvents("2026-11-01", "2026-11-30")).events;
    expect(evs.map((e) => e.location)).toEqual([null, null, "New room", "New room", "New room", "New room"]);
    expect(await days()).toEqual([2, 4, 9, 11, 16, 18]); // count preserved across the split
  });

  it("shifts the whole series for 'all'", async () => {
    await service.updateEvent({ event_id: weekly[3]!, span: "all", start: "2026-11-11T11:40" }); // +1h
    const evs = (await service.listEvents("2026-11-01", "2026-11-30")).events;
    expect(new Set(evs.map((e) => IST.iso(e.start).slice(11, 16)))).toEqual(new Set(["11:40"]));
    expect(evs).toHaveLength(6);
  });

  it("needs a series span to change the rule", async () => {
    await expect(service.updateEvent({ event_id: weekly[0]!, span: "this", remove_recurrence: true })).rejects.toBeInstanceOf(
      InvalidRequestError,
    );
  });

  it("deletes one occurrence", async () => {
    const r = await service.deleteEvent(weekly[1]!, "this");
    expect(r.deleted).toBe(true);
    expect(r.span).toBe("this");
    expect(await days()).toEqual([2, 9, 11, 16, 18]);
    await expect(service.getEvent(weekly[1]!)).rejects.toBeInstanceOf(NotFoundError);
  });

  it("deletes this and later occurrences", async () => {
    await service.deleteEvent(weekly[3]!, "future");
    expect(await days()).toEqual([2, 4, 9]);
  });

  it("deletes the whole series", async () => {
    await service.deleteEvent(weekly[4]!, "all");
    expect(await days()).toEqual([]);
  });

  it("finds a moved occurrence in its new window", async () => {
    await service.updateEvent({ event_id: weekly[5]!, span: "this", start: "2026-12-01T10:00" });
    const dec = (await service.listEvents("2026-12-01", "2026-12-01")).events;
    expect(dec.map((e) => e.id)).toEqual([weekly[5]]);
  });
});

it("ignores span for single events", async () => {
  const e = await service.createEvent({ title: "One", start: "2026-11-03T14:00" });
  expect((await service.deleteEvent(e.id, "future")).span).toBe("this");
});

it("reports unknown ids as not found", async () => {
  await expect(service.getEvent("does-not-exist|123")).rejects.toBeInstanceOf(NotFoundError);
  await expect(service.deleteEvent("does-not-exist", "this")).rejects.toBeInstanceOf(NotFoundError);
});

it("returns times with the user's offset", async () => {
  const e = await service.createEvent({ title: "X", start: "2026-11-03T14:00" });
  expect(IST.iso(e.start)).toBe("2026-11-03T14:00:00+03:00");
});
