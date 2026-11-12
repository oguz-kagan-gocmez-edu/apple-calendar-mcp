/** Answering invitations (port of tests/test_invitations.py and the CalDAV cases). */
import { beforeEach, describe, expect, it } from "vitest";

import { AccessControlledStore, AccessPolicy } from "../../src/access";
import { MemoryStore } from "../../src/memory-store";
import { CalendarService } from "../../src/service";
import { InvalidRequestError, ReadOnlyCalendarError } from "../../src/store";
import { IST, NOW, at, freshStore } from "./helpers";

describe("replies through the service", () => {
  let store: MemoryStore;
  let service: CalendarService;
  let invite: string;
  beforeEach(async () => {
    store = new MemoryStore();
    service = new CalendarService(store, IST, () => NOW);
    invite = (await service.createEvent({ title: "Design review", start: "2026-11-03T16:00" })).id;
    store.setMyStatus(invite, "pending");
  });

  it("accepts, declines and tentatively accepts", async () => {
    for (const reply of ["accepted", "tentative", "declined"] as const) {
      expect((await service.respondToInvitation(invite, reply)).my_status).toBe(reply);
    }
    expect((await service.getEvent(invite)).my_status).toBe("declined");
  });

  it("takes an answered invitation off the agenda", async () => {
    expect((await service.agenda({ day: "2026-11-03" })).invitations.map((e) => e.id)).toEqual([invite]);
    await service.respondToInvitation(invite, "accepted");
    expect((await service.agenda({ day: "2026-11-03" })).invitations).toEqual([]);
  });

  it("refuses events you weren't invited to, and span 'future'", async () => {
    const own = await service.createEvent({ title: "Mine", start: "2026-11-03T09:00" });
    await expect(service.respondToInvitation(own.id, "accepted")).rejects.toThrow(/isn't an invitation/);
    await expect(service.respondToInvitation(invite, "accepted", "future")).rejects.toThrow(InvalidRequestError);
  });

  it("stays out of the undo log", async () => {
    const before = (await service.listChanges()).length;
    await service.respondToInvitation(invite, "accepted");
    expect((await service.listChanges()).length).toBe(before);
  });

  it("is refused on a read-only calendar", async () => {
    const guarded = new CalendarService(
      new AccessControlledStore(store, AccessPolicy.parse("home")),
      IST,
      () => NOW,
    );
    await expect(guarded.respondToInvitation(invite, "accepted")).rejects.toThrow(ReadOnlyCalendarError);
    expect((await store.getEvent(invite)).my_status).toBe("pending");
  });
});

describe("replies over CalDAV", () => {
  async function invited(extra = "") {
    const user = `u${crypto.randomUUID().slice(0, 8)}@example.com`;
    const store = await freshStore(user);
    const href = `${new URL(store.client.baseUrl).origin}/${user}/home/inv.ics`;
    const auth = { Authorization: `Basic ${btoa(`${user}:x`)}` };
    const res = await fetch(href, {
      method: "PUT",
      headers: { ...auth, "Content-Type": "text/calendar" },
      body:
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n" +
        "UID:inv\r\nDTSTAMP:20261101T000000Z\r\n" +
        `DTSTART:20261103T060000Z\r\nDTEND:20261103T070000Z\r\nSUMMARY:Review\r\n${extra}` +
        "ORGANIZER:mailto:boss@example.com\r\nATTENDEE;PARTSTAT=ACCEPTED:mailto:boss@example.com\r\n" +
        `ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:${user}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n`,
    });
    expect(res.status).toBe(201);
    const raw = async () => (await (await fetch(href, { headers: auth })).text()).replace(/\r\n /g, "");
    return { store, user, raw };
  }

  it("sets only your own PARTSTAT and drops RSVP", async () => {
    const { store, user, raw } = await invited();
    const [inv] = await store.listEvents(at(3, 0), at(4, 0));
    const got = await store.respondToInvitation(inv!.id, "tentative", "all");
    expect(got.my_status).toBe("tentative");
    expect(Object.fromEntries(got.attendees.map((a) => [a.email, a.status]))).toEqual({
      "boss@example.com": "accepted",
      [user]: "tentative",
    });
    const text = await raw();
    expect(text).toContain(`ATTENDEE;PARTSTAT=TENTATIVE:mailto:${user}`);
    expect(text).not.toContain("RSVP");
    expect((await store.respondToInvitation(inv!.id, "declined", "this")).my_status).toBe("declined");
  });

  it("answers one occurrence, then the whole series", async () => {
    const { store } = await invited("RRULE:FREQ=WEEKLY;COUNT=3\r\n");
    const ids = (await store.listEvents(at(1, 0), at(30, 0))).map((e) => e.id);
    expect(ids).toHaveLength(3);
    const one = await store.respondToInvitation(ids[1]!, "declined", "this");
    expect([one.id, one.start]).toEqual([ids[1], at(10, 9)]);
    const statuses = async () => Promise.all(ids.map(async (i) => (await store.getEvent(i)).my_status));
    expect(await statuses()).toEqual(["pending", "declined", "pending"]);
    await store.respondToInvitation(ids[0]!, "accepted", "all");
    expect(await statuses()).toEqual(["accepted", "accepted", "accepted"]);
    expect(await store.listEvents(at(1, 0), at(30, 0))).toHaveLength(3);
  });

  it("needs an invitation and an email login", async () => {
    const { store } = await invited();
    const mine = await store.createEvent({
      title: "Mine", start: at(3, 12), end: at(3, 13), all_day: false, calendar_id: null, location: null,
      notes: null, url: null, alarms_minutes_before: [], recurrence: null, availability: null,
    });
    await expect(store.respondToInvitation(mine.id, "accepted", "all")).rejects.toThrow(/isn't an invitation/);
    const plain = await freshStore();
    const own = await plain.createEvent({
      title: "Mine", start: at(3, 12), end: at(3, 13), all_day: false, calendar_id: null, location: null,
      notes: null, url: null, alarms_minutes_before: [], recurrence: null, availability: null,
    });
    await expect(plain.respondToInvitation(own.id, "accepted", "all")).rejects.toThrow(/email address/);
  });
});
