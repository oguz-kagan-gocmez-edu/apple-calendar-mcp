/** Per-calendar access control (port of tests/test_access.py). */
import { beforeEach, describe, expect, it } from "vitest";

import { AccessControlledStore, AccessPolicy } from "../../src/access";
import { MemoryStore } from "../../src/memory-store";
import type { Calendar } from "../../src/models";
import { CalendarService } from "../../src/service";
import { NotFoundError, ReadOnlyCalendarError } from "../../src/store";
import { IST, NOW } from "./helpers";

const cal = (id: string, title: string): Calendar => ({
  id, title, source: null, color: null, writable: true, is_default: false,
});

it.each<[AccessPolicy, Record<string, string>]>([
  [AccessPolicy.parse(), { home: "write", work: "write", family: "write" }],
  [AccessPolicy.parse("Work"), { home: "write", work: "read", family: "write" }],
  [AccessPolicy.parse(null, " family , "), { home: "write", work: "write", family: "hidden" }],
  [AccessPolicy.parse(null, null, "home,WORK"), { home: "write", work: "write", family: "hidden" }],
  [AccessPolicy.parse("work", "work"), { home: "write", work: "hidden", family: "write" }],
  [AccessPolicy.parse("work", null, "work"), { home: "hidden", work: "read", family: "hidden" }],
])("levels by id or title %#", (policy, expected) => {
  const cals = [cal("home", "Home"), cal("work", "Work"), cal("family", "Family")];
  expect(Object.fromEntries(cals.map((c) => [c.id, policy.level(c)]))).toEqual(expected);
});

it("knows an empty policy", () => {
  expect(AccessPolicy.parse("", " , ", undefined).isOpen).toBe(true);
  expect(AccessPolicy.parse(null, "home").isOpen).toBe(false);
});

let store: MemoryStore;
let ids: { home: string; work: string };
beforeEach(async () => {
  store = new MemoryStore();
  const plain = new CalendarService(store, IST, () => NOW);
  ids = {
    home: (await plain.createEvent({ title: "Gym", start: "2026-11-03T08:00", calendar_id: "home" })).id,
    work: (await plain.createEvent({ title: "Standup", start: "2026-11-03T09:00", calendar_id: "work" })).id,
  };
});

const guarded = (readOnly?: string, hidden?: string, only?: string) =>
  new CalendarService(new AccessControlledStore(store, AccessPolicy.parse(readOnly, hidden, only)), IST, () => NOW);

describe("hidden", () => {
  it("removes the calendar and its events", async () => {
    const svc = guarded(undefined, "Work");
    expect((await svc.listCalendars()).map((c) => c.id)).toEqual(["home"]);
    expect((await svc.listEvents("2026-11-03", "2026-11-03")).events.map((e) => e.title)).toEqual(["Gym"]);
    expect((await svc.listEvents("2026-11-03", "2026-11-03", ["work"])).events).toEqual([]);
    expect((await svc.searchEvents("standup")).events).toEqual([]);
    expect((await svc.agenda({ day: "2026-11-03" })).events.map((e) => e.title)).toEqual(["Gym"]);
  });

  it("treats its ids as missing", async () => {
    const svc = guarded(undefined, "work");
    const calls = [
      () => svc.getEvent(ids.work),
      () => svc.updateEvent({ event_id: ids.work, title: "X" }),
      () => svc.deleteEvent(ids.work),
      () => svc.createEvent({ title: "X", start: "2026-11-03T10:00", calendar_id: "work" }),
      () => svc.updateEvent({ event_id: ids.home, calendar_id: "work" }),
    ];
    for (const call of calls) await expect(call()).rejects.toBeInstanceOf(NotFoundError);
    expect((await store.getEvent(ids.work)).title).toBe("Standup");
  });

  it("supports an allow-list", async () => {
    const svc = guarded(undefined, undefined, "home");
    expect((await svc.listCalendars()).map((c) => c.id)).toEqual(["home"]);
    expect((await svc.listEvents("2026-11-01", "2026-11-30")).events.map((e) => e.title)).toEqual(["Gym"]);
    expect((await guarded(undefined, undefined, "nope").listEvents("2026-11-01", "2026-11-30")).events).toEqual([]);
  });
});

describe("read-only", () => {
  it("lists the calendar as not writable and refuses writes", async () => {
    const svc = guarded("work");
    expect(Object.fromEntries((await svc.listCalendars()).map((c) => [c.id, c.writable]))).toEqual({
      home: true,
      work: false,
    });
    expect((await svc.getEvent(ids.work)).title).toBe("Standup");
    const calls = [
      () => svc.updateEvent({ event_id: ids.work, title: "X" }),
      () => svc.deleteEvent(ids.work),
      () => svc.createEvent({ title: "X", start: "2026-11-03T10:00", calendar_id: "work" }),
      () => svc.updateEvent({ event_id: ids.home, calendar_id: "work" }),
      () => svc.updateEvent({ event_id: ids.work, calendar_id: "home" }),
    ];
    for (const call of calls) await expect(call()).rejects.toBeInstanceOf(ReadOnlyCalendarError);
    expect((await svc.updateEvent({ event_id: ids.home, title: "Run" })).title).toBe("Run");
  });

  it("asks for a calendar when the default one is read-only", async () => {
    const svc = guarded("home");
    await expect(svc.createEvent({ title: "X", start: "2026-11-03T10:00" })).rejects.toThrow(/pass calendar_id/);
    expect((await svc.createEvent({ title: "X", start: "2026-11-03T10:00", calendar_id: "work" })).calendar_id).toBe(
      "work",
    );
  });
});
