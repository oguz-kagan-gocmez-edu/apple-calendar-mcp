/** find_duplicates (port of the matching tests in tests/test_scheduling.py and tests/test_service.py). */
import { describe, expect, it } from "vitest";

import { MemoryStore } from "../../src/memory-store";
import type { Event } from "../../src/models";
import { findDuplicates } from "../../src/scheduling";
import { CalendarService } from "../../src/service";
import { IST, NOW, at, freshService } from "./helpers";

const copy = (uid: string, title: string, start: number, end: number, extra: Partial<Event> = {}): Event => ({
  id: `${uid}|${start / 1000}`, calendar_id: "c", calendar_title: "C", title, start, end, all_day: false,
  location: null, notes: null, url: null, is_recurring: false, recurrence: null, alarms_minutes_before: [],
  availability: "busy", status: "none", organizer: null, attendees: [], my_status: null, ...extra,
});
const ids = (groups: [Event[], number][]) => groups.map(([copies, n]) => [copies.map((e) => e.id.split("|")[0]), n]);

describe("findDuplicates", () => {
  it("matches title and times, ignoring case and spacing", () => {
    const events = [
      copy("a", "Dentist", at(4, 15), at(4, 16)),
      copy("b", "  dentist ", at(4, 15), at(4, 16)),
      copy("c", "Dentist", at(4, 15), at(4, 17)),
      copy("d", "Dentist", at(5, 15), at(5, 16)),
      copy("e", "Dentists", at(4, 15), at(4, 16)),
      copy("f", "Dentist", at(4, 15), at(4, 16), { status: "canceled" }),
      copy("g", "Dentist", at(4, 15), at(4, 16), { all_day: true }),
    ];
    expect(ids(findDuplicates(events))).toEqual([[["a", "b"], 1]]);
  });

  it("never pairs occurrences of one series", () => {
    expect(findDuplicates([copy("a", "Sync", at(4, 15), at(4, 16)), copy("a", "Sync", at(4, 15), at(4, 16))])).toEqual([]);
  });

  it("gives one group for copied series", () => {
    const events = [3, 10, 17].flatMap((day) =>
      ["a", "b"].map((uid) => copy(uid, "Lecture", at(day, 10), at(day, 12), { is_recurring: true })),
    );
    events.push(copy("c", "Lecture", at(10, 10), at(10, 12)));
    const found = findDuplicates(events);
    expect(ids(found)).toEqual([[["a", "b"], 2], [["a", "b", "c"], 1]]);
    expect(found[0]![0][0]!.start).toBe(at(3, 10));
  });
});

describe("find_duplicates through the service", () => {
  it("groups copies across calendars and honours the limit", async () => {
    const service = new CalendarService(new MemoryStore(), IST, () => NOW);
    await service.createEvent({ title: "Dentist", start: "2026-11-04T15:00" });
    await service.createEvent({ title: "Dentist", start: "2026-11-04T15:00", calendar_id: "work" });
    await service.createEvent({ title: "Trip", start: "2026-11-06", all_day: true });
    await service.createEvent({ title: "trip", start: "2026-11-06", all_day: true });
    await service.createEvent({ title: "Trip", start: "2026-11-07", all_day: true });
    const r = await service.findDuplicates("2026-11-01", "2026-11-30");
    expect([r.count, r.truncated]).toEqual([2, false]);
    expect(r.groups.map((g) => [g.title.toLowerCase(), g.all_day, g.repeats, g.events.length])).toEqual([
      ["dentist", false, 1, 2],
      ["trip", true, 1, 2],
    ]);
    expect(r.groups[0]!.events.map((e) => e.calendar_title)).toEqual(["Home", "Work"]);
    expect((await service.findDuplicates("2026-11-01", "2026-11-30", ["work"])).count).toBe(0);
    const one = await service.findDuplicates("2026-11-01", "2026-11-30", null, 1);
    expect([one.count, one.truncated, one.groups.length]).toEqual([1, true, 1]);
  });

  it("finds a copied series over CalDAV", async () => {
    const service = await freshService();
    const rule = { frequency: "weekly" as const, interval: 1, days_of_week: null, count: 3, until: null };
    const a = await service.createEvent({ title: "Lecture", start: "2026-11-03T10:00", recurrence: rule });
    const b = await service.createEvent({ title: "Lecture", start: "2026-11-03T10:00", recurrence: rule });
    const { groups } = await service.findDuplicates("2026-11-01", "2026-11-30");
    expect(groups).toHaveLength(1);
    expect(groups[0]!.repeats).toBe(3);
    expect(groups[0]!.events.map((e) => e.id).sort()).toEqual([a.id, b.id].sort());
    await service.deleteEvent(b.id, "all");
    expect((await service.findDuplicates("2026-11-01", "2026-11-30")).count).toBe(0);
  });
});
