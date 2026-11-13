/** Change log, undo_change and apply_changes (port of tests/test_changes.py), on CalDAV/Radicale. */
import { beforeEach, describe, expect, it } from "vitest";

import { AccessControlledStore, AccessPolicy } from "../../src/access";
import { INVITE_DELETE, KvJournal, type KvLike, stepBlocker } from "../../src/changes";
import { MemoryStore } from "../../src/memory-store";
import type { Event, EventDraft, EventPatch, Span } from "../../src/models";
import { RecurrenceSchema } from "../../src/models";
import { type BatchChange, CalendarService } from "../../src/service";
import { BackendError, NotFoundError } from "../../src/store";
import { IST, NOW, at, freshService } from "./helpers";

let service: CalendarService;
beforeEach(async () => {
  service = await freshService();
});

const titles = async (svc = service) => (await svc.listEvents("2026-11-01", "2026-11-30")).events.map((e) => e.title);
const weekly = RecurrenceSchema.parse({ frequency: "weekly", count: 4 });
const daily = RecurrenceSchema.parse({ frequency: "daily", count: 3 });

describe("undo", () => {
  it("deletes a created event or series", async () => {
    await service.createEvent({ title: "Dentist", start: "2026-11-03T09:00" });
    const r = await service.undoChange();
    expect([r.summary, r.removed_ids.length, r.restored]).toEqual(['Created "Dentist"', 1, []]);
    await service.createEvent({ title: "Lecture", start: "2026-11-02T10:00", recurrence: weekly });
    await service.undoChange();
    expect(await titles()).toEqual([]);
    expect(await service.listChanges()).toEqual([]);
  });

  it("reports the rule of a created series", async () => {
    const e = await service.createEvent({ title: "Lecture", start: "2026-11-02T10:00", recurrence: weekly });
    expect(e.recurrence).toEqual(weekly);
    expect((await service.getEvent(e.id)).recurrence).toEqual(weekly);
  });

  it("restores edited fields", async () => {
    const e = await service.createEvent({
      title: "Dentist", start: "2026-11-03T09:00", calendar_id: "home", alarms_minutes_before: [10], notes: "keep",
    });
    await service.updateEvent({
      event_id: e.id, title: "Dentist (moved)", start: "2026-11-04T15:00", location: "Kadıköy",
      calendar_id: "work", alarms_minutes_before: [60], notes: "",
    });
    const back = (await service.undoChange()).restored[0]!;
    expect([back.title, back.start, back.end, back.location, back.notes]).toEqual(["Dentist", at(3, 9), at(3, 10), null, "keep"]);
    expect([back.calendar_id, back.alarms_minutes_before]).toEqual(["home", [10]]);
    expect((await service.listChanges()).map((c) => c.summary)).toEqual(['Created "Dentist"']);
  });

  it("restores one occurrence of a series", async () => {
    await service.createEvent({ title: "Daily", start: "2026-11-02T09:00", duration_minutes: 30, recurrence: daily });
    const ids = (await service.listEvents("2026-11-01", "2026-11-30")).events.map((e) => e.id);
    await service.updateEvent({ event_id: ids[1]!, span: "this", title: "Daily (moved)", start: "2026-11-03T15:00" });
    await service.undoChange();
    const evs = (await service.listEvents("2026-11-01", "2026-11-30")).events;
    expect(evs.map((e) => [e.title, e.start])).toEqual([["Daily", at(2, 9)], ["Daily", at(3, 9)], ["Daily", at(4, 9)]]);
  });

  it("logs series edits and deleted occurrences without undoing them", async () => {
    const e = await service.createEvent({ title: "Daily", start: "2026-11-02T09:00", recurrence: daily });
    await service.updateEvent({ event_id: e.id, span: "all", title: "Standup" });
    const [latest] = await service.listChanges();
    expect(latest!.can_undo).toBe(false);
    expect(latest!.why_not).toContain("several occurrences");
    await expect(service.undoChange()).rejects.toThrow(/Can't undo/);
    await service.deleteEvent(e.id, "this");
    expect((await service.listChanges())[0]!.can_undo).toBe(false);
  });

  it("re-creates a deleted event", async () => {
    const e = await service.createEvent({ title: "Dentist", start: "2026-11-03T09:00", location: "Kadıköy", calendar_id: "work" });
    await service.deleteEvent(e.id);
    const back = (await service.undoChange()).restored[0]!;
    expect(back.id).not.toBe(e.id);
    expect([back.title, back.start, back.location, back.calendar_id]).toEqual(["Dentist", at(3, 9), "Kadıköy", "work"]);
  });

  it("refuses over a later edit or a missing event", async () => {
    const e = await service.createEvent({ title: "Dentist", start: "2026-11-03T09:00" });
    await service.updateEvent({ event_id: e.id, title: "Dentist 2" });
    await service.store.updateEvent(e.id, { location: "Somewhere" }, "this");
    await expect(service.undoChange()).rejects.toThrow(/changed again/);
    await service.store.deleteEvent(e.id, "this");
    await expect(service.undoChange()).rejects.toThrow(/no longer exists/);
  });

  it("undoes an older change by id, once", async () => {
    await service.createEvent({ title: "A", start: "2026-11-03T09:00" });
    await service.createEvent({ title: "B", start: "2026-11-03T11:00" });
    const older = (await service.listChanges())[1]!;
    expect(older.summary).toBe('Created "A"');
    await service.undoChange(older.change_id);
    expect(await titles()).toEqual(["B"]);
    await expect(service.undoChange(older.change_id)).rejects.toBeInstanceOf(NotFoundError);
  });

  it("says when there is nothing to undo", async () => {
    await expect(service.undoChange()).rejects.toThrow(/no change/);
  });
});

describe("batch", () => {
  it("applies a mixed batch and undoes it in one step", async () => {
    const keep = await service.createEvent({ title: "Standup", start: "2026-11-03T09:00" });
    const drop = await service.createEvent({ title: "Old sync", start: "2026-11-03T11:00" });
    const r = await service.applyChanges([
      { action: "create", title: "Review", start: "2026-11-03T14:00", duration_minutes: 30 },
      { action: "update", event_id: keep.id, start: "2026-11-03T09:30" },
      { action: "delete", event_id: drop.id },
    ]);
    expect(r.results.map((o) => o.action)).toEqual(["create", "update", "delete"]);
    expect(await titles()).toEqual(["Standup", "Review"]);
    expect((await service.listChanges())[0]!.summary).toBe("3 changes in one batch");
    await service.undoChange(r.change_id);
    const evs = (await service.listEvents("2026-11-01", "2026-11-30")).events;
    expect(evs.map((e) => [e.title, e.start])).toEqual([["Standup", at(3, 9)], ["Old sync", at(3, 11)]]);
  });

  it("previews a batch without writing anything", async () => {
    const keep = await service.createEvent({ title: "Standup", start: "2026-11-03T09:00", location: "Room 1" });
    const drop = await service.createEvent({ title: "Old sync", start: "2026-11-03T11:00" });
    const before = await service.listChanges();
    const r = await service.applyChanges(
      [
        { action: "create", title: "Review", start: "2026-11-03T14:00", calendar_id: "work" },
        { action: "update", event_id: keep.id, start: "2026-11-03T09:30", location: "" },
        { action: "delete", event_id: drop.id },
      ],
      true,
    );
    expect([r.dry_run, r.change_id]).toEqual([true, null]);
    const [created, moved, deleted] = r.results.map((o) => o.event);
    expect([created!.id, created!.calendar_id, created!.end]).toEqual(["", "work", at(3, 15)]);
    expect([moved!.id, moved!.start, moved!.end, moved!.location]).toEqual([keep.id, at(3, 9, 30), at(3, 10, 30), null]);
    expect(deleted!.id).toBe(drop.id);
    expect(await titles()).toEqual(["Standup", "Old sync"]);
    expect(await service.listChanges()).toEqual(before);
    const first = (await service.listEvents("2026-11-01", "2026-11-30")).events[0]!;
    expect([first.start, first.location]).toEqual([at(3, 9), "Room 1"]);
  });

  it("refuses in a dry run what the write would refuse", async () => {
    const e = await service.createEvent({ title: "X", start: "2026-11-03T09:00" });
    await expect(
      service.applyChanges(
        [
          { action: "create", title: "A", start: "2026-11-03T10:00" },
          { action: "update", event_id: e.id, calendar_id: "nowhere" },
        ],
        true,
      ),
    ).rejects.toThrow(/Change 2 \(update\).*nowhere.*Nothing was changed/);
    const store = new AccessControlledStore(new MemoryStore(), AccessPolicy.parse("work", "", ""));
    const locked = new CalendarService(store, IST, () => NOW);
    const create = { action: "create", title: "A", start: "2026-11-03T10:00", calendar_id: "work" } as const;
    await expect(locked.applyChanges([create], true)).rejects.toThrow(/Change 1 \(create\).*read-only/);
    expect(await titles()).toEqual(["X"]);
  });

  it("changes nothing when an item is invalid", async () => {
    await expect(
      service.applyChanges([
        { action: "create", title: "A", start: "2026-11-03T09:00" },
        { action: "create", title: "B", start: "next tuesday" },
      ]),
    ).rejects.toThrow(/Change 2 \(create\).*Nothing was changed/);
    expect(await titles()).toEqual([]);
    expect(await service.listChanges()).toEqual([]);
  });

  it("refuses what it couldn't undo, duplicates and size limits", async () => {
    const e = await service.createEvent({ title: "Daily", start: "2026-11-02T09:00", recurrence: daily });
    await expect(service.applyChanges([{ action: "delete", event_id: e.id }])).rejects.toThrow(/delete_event/);
    await expect(service.applyChanges([{ action: "update", event_id: "missing|1", title: "X" }])).rejects.toThrow(
      /Change 1 \(update\)/,
    );
    await expect(
      service.applyChanges([
        { action: "update", event_id: e.id, title: "Y" },
        { action: "delete", event_id: e.id },
      ]),
    ).rejects.toThrow(/only once/);
    await expect(service.applyChanges([])).rejects.toThrow(/between 1 and 25/);
  });
});

// ------------------------------------------------------------------ memory-only details
class FailingStore extends MemoryStore {
  writes = 0;
  failOn = 0;
  private tick() {
    if (++this.writes === this.failOn) throw new BackendError("The server said no.");
  }
  override createEvent(d: EventDraft): Promise<Event> {
    this.tick();
    return super.createEvent(d);
  }
  override updateEvent(id: string, p: EventPatch, s: Span): Promise<Event> {
    this.tick();
    return super.updateEvent(id, p, s);
  }
}

it("rolls back a batch that fails half-way", async () => {
  const store = new FailingStore();
  const svc = new CalendarService(store, IST, () => NOW);
  const e = await svc.createEvent({ title: "Standup", start: "2026-11-03T09:00" });
  store.writes = 0;
  store.failOn = 3;
  const changes: BatchChange[] = [
    { action: "create", title: "A", start: "2026-11-03T10:00" },
    { action: "update", event_id: e.id, title: "Standup (moved)" },
    { action: "create", title: "B", start: "2026-11-03T12:00" },
  ];
  const err = await svc.applyChanges(changes).catch((x: unknown) => x);
  expect(err).toBeInstanceOf(BackendError);
  expect((err as Error).message).toMatch(/Change 3 \(create\).*rolled back/);
  expect(await titles(svc)).toEqual(["Standup"]);
  expect((await svc.listChanges()).map((c) => c.summary)).toEqual(['Created "Standup"']);
});

it("won't restore a deleted invitation", () => {
  const invite = { attendees: [{ name: null, email: "boss@example.com", status: "accepted" }], is_recurring: false } as unknown as Event;
  expect(stepBlocker({ action: "delete", before: invite, after: null, span: "this" })).toBe(INVITE_DELETE);
});

it("keeps the log in KV across service instances", async () => {
  const data = new Map<string, string>();
  const ttls: number[] = [];
  const kv: KvLike = {
    get: (k) => Promise.resolve(data.get(k) ?? null),
    put: (k, v, o) => (data.set(k, v), ttls.push(o?.expirationTtl ?? 0), Promise.resolve()),
    delete: (k) => (data.delete(k), Promise.resolve()),
  };
  const store = new MemoryStore();
  const first = new CalendarService(store, IST, () => NOW, new KvJournal(kv));
  await first.createEvent({ title: "A", start: "2026-11-03T09:00" });
  await first.createEvent({ title: "B", start: "2026-11-03T11:00" });
  expect(ttls.every((t) => t === 24 * 60 * 60)).toBe(true);

  const second = new CalendarService(store, IST, () => NOW, new KvJournal(kv)); // the next request
  expect((await second.listChanges()).map((c) => c.summary)).toEqual(['Created "B"', 'Created "A"']);
  await second.undoChange();
  expect(await titles(second)).toEqual(["A"]);
  await expect(second.undoChange("chg_../../x")).rejects.toBeInstanceOf(NotFoundError);
});
