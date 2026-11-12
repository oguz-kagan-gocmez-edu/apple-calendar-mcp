/** calendar_stats (port of TestStats in tests/test_service.py). */
import { beforeEach, expect, it } from "vitest";

import { MemoryStore } from "../../src/memory-store";
import { CalendarService } from "../../src/service";
import { InvalidRequestError } from "../../src/store";
import { IST, NOW } from "./helpers";

let store: MemoryStore;
let service: CalendarService;
beforeEach(() => {
  store = new MemoryStore();
  service = new CalendarService(store, IST, () => NOW);
});

it("shows where the time goes", async () => {
  await service.createEvent({ title: "Standup", start: "2026-11-02T09:00", duration_minutes: 60, calendar_id: "work" });
  await service.createEvent({ title: "Review", start: "2026-11-02T09:30", end: "2026-11-02T11:00", calendar_id: "work" });
  await service.createEvent({ title: "Call home", start: "2026-11-02T19:00", duration_minutes: 60 });
  await service.createEvent({ title: "standup", start: "2026-11-03T09:00", duration_minutes: 30, calendar_id: "work" });
  await service.createEvent({ title: "Focus", start: "2026-11-03T13:00", duration_minutes: 120, availability: "free" });
  store.setMyStatus((await service.createEvent({ title: "Optional", start: "2026-11-03T15:00" })).id, "declined");
  await service.createEvent({ title: "Holiday", start: "2026-11-04", all_day: true });

  const r = await service.calendarStats({ start: "2026-11-02", end: "2026-11-08" });
  expect([r.events, r.timed_events, r.busy_minutes]).toEqual([7, 6, 210]);
  expect([r.busy_minutes_in_working_hours, r.busy_minutes_outside_working_hours]).toEqual([150, 60]);
  expect(r.average_event_minutes).toBe(60);
  expect(r.days.map((d) => [d.date, d.weekday, d.events, d.busy_minutes])).toEqual([
    ["2026-11-02", "Monday", 3, 180],
    ["2026-11-03", "Tuesday", 3, 30],
    ["2026-11-04", "Wednesday", 1, 0],
    ["2026-11-05", "Thursday", 0, 0],
    ["2026-11-06", "Friday", 0, 0],
    ["2026-11-07", "Saturday", 0, 0],
    ["2026-11-08", "Sunday", 0, 0],
  ]);
  expect(r.busiest_day?.date).toBe("2026-11-02");
  expect(r.by_calendar.map((c) => [c.calendar_id, c.events, c.minutes])).toEqual([
    ["work", 3, 180],
    ["home", 4, 60],
  ]);
  expect(r.top_titles.map((t) => [t.title, t.events, t.minutes])).toEqual([
    ["Standup", 2, 90],
    ["Review", 1, 90],
    ["Call home", 1, 60],
  ]);
});

it("clips to the range and handles quiet ranges", async () => {
  await service.createEvent({ title: "Overnight", start: "2026-11-02T22:00", end: "2026-11-03T02:00" });
  const r = await service.calendarStats({ start: "2026-11-03", end: "2026-11-03" });
  expect([r.busy_minutes, r.top_titles[0]!.minutes, r.days.length]).toEqual([120, 120, 1]);
  const quiet = await service.calendarStats({ start: "2026-11-10", end: "2026-11-11" });
  expect([quiet.events, quiet.busy_minutes, quiet.busiest_day, quiet.average_event_minutes]).toEqual([0, 0, null, 0]);
  const two = await service.calendarStats({ start: "2026-11-02", end: "2026-11-15" });
  expect(two.by_weekday.find((w) => w.weekday === "Monday")).toEqual({
    weekday: "Monday",
    days: 2,
    busy_minutes: 120,
    average_busy_minutes: 60,
  });
});

it("rejects bad working hours", async () => {
  await expect(
    service.calendarStats({ start: "2026-11-02", end: "2026-11-08", working_hours_start: "9am" }),
  ).rejects.toThrow(InvalidRequestError);
});
