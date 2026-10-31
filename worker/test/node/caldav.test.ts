/** CalDAV backend specifics (mirrors tests/test_caldav_store.py) and the client's safety rules. */
import { describe, expect, it } from "vitest";

import { DavClient } from "../../src/caldav/client";
import { CaldavStore, objectHref } from "../../src/caldav/store";
import type { EventDraft } from "../../src/models";
import { BackendError, NotFoundError, PermissionDeniedError } from "../../src/store";
import { IST, at, freshStore } from "./helpers";

const draft = (d: Partial<EventDraft>): EventDraft => ({
  title: "X",
  start: at(3, 9),
  end: at(3, 10),
  all_day: false,
  calendar_id: null,
  location: null,
  notes: null,
  url: null,
  alarms_minutes_before: [],
  recurrence: null,
  availability: null,
  ...d,
});

async function raw(store: CaldavStore, eventId: string): Promise<string> {
  const cals = await store.listCalendars();
  const home = `${new URL(store.client.baseUrl).origin}`;
  void cals;
  const uid = eventId.split("|")[0]!;
  for (const cal of ["home", "work"]) {
    const user = (store as unknown as { config: { username: string } }).config.username;
    const obj = await store.client.byUid(`${home}/${user}/${cal}/`, uid);
    if (obj) return obj.data;
  }
  throw new Error("not found");
}

describe("CalDAV store", () => {
  it("lists calendars with the configured default", async () => {
    const store = await freshStore();
    const cals = Object.fromEntries((await store.listCalendars()).map((c) => [c.id, c]));
    expect(Object.keys(cals).sort()).toEqual(["home", "work"]);
    expect(cals.home!.is_default).toBe(true);
    expect(cals.home!.title).toBe("Home");
    expect(cals.home!.source).toBe("127.0.0.1");
  });

  it("round-trips every field", async () => {
    const store = await freshStore();
    const e = await store.createEvent(
      draft({ title: "Dentist", location: "Kadıköy", notes: "Bring card", url: "https://example.com", alarms_minutes_before: [60, 10], availability: "free" }),
    );
    const got = await store.getEvent(e.id);
    expect([got.title, got.location, got.notes, got.url]).toEqual(["Dentist", "Kadıköy", "Bring card", "https://example.com"]);
    expect(got.alarms_minutes_before).toEqual([60, 10]);
    expect(got.availability).toBe("free");
    expect([got.start, got.end]).toEqual([at(3, 9), at(3, 10)]);
  });

  it("stores all-day events as dates", async () => {
    const store = await freshStore();
    const e = await store.createEvent(draft({ title: "Trip", start: at(5, 0), end: at(8, 0), all_day: true }));
    const text = await raw(store, e.id);
    expect(text).toContain("DTSTART;VALUE=DATE:20261105");
    expect(text).toContain("DTEND;VALUE=DATE:20261108");
    expect((await store.getEvent(e.id)).all_day).toBe(true);
  });

  it("writes the RRULE in RFC form and reads it back", async () => {
    const store = await freshStore();
    const recurrence = { frequency: "weekly" as const, interval: 1, days_of_week: ["mon" as const, "thu" as const], count: null, until: "2026-11-30" };
    const e = await store.createEvent(draft({ title: "Gym", start: at(2, 7), end: at(2, 8), recurrence }));
    expect((await store.getEvent(e.id)).recurrence).toEqual(recurrence);
    expect(await store.listEvents(at(1, 0), at(30, 23))).toHaveLength(9);
    const rule = /^RRULE:(.*)$/m.exec(await raw(store, e.id))![1]!.split(";").sort();
    expect(rule).toEqual(["BYDAY=MO,TH", "FREQ=WEEKLY", "UNTIL=20261130T205959Z"]); // 23:59:59 local, in UTC
  });

  it("writes a VTIMEZONE for the zone it uses", async () => {
    const store = await freshStore();
    const e = await store.createEvent(draft({}));
    const text = await raw(store, e.id);
    expect(text).toContain("BEGIN:VTIMEZONE");
    expect(text).toContain("TZOFFSETTO:+0300");
  });

  it("makes an override for 'this' edits and an EXDATE for 'this' deletes", async () => {
    const store = await freshStore();
    const first = await store.createEvent(
      draft({ title: "Daily", start: at(2, 9), end: at(2, 9, 30), recurrence: { frequency: "daily", interval: 1, days_of_week: null, count: 4, until: null } }),
    );
    const ids = (await store.listEvents(at(1, 0), at(10, 0))).map((e) => e.id);
    await store.updateEvent(ids[1]!, { title: "Daily (moved)", start: at(3, 15), end: at(3, 15, 30) }, "this");
    await store.deleteEvent(ids[2]!, "this");
    const events = await store.listEvents(at(1, 0), at(10, 0));
    expect(events.map((e) => [e.title, IST.date(e.start).d, IST.wall(e.start).h])).toEqual([
      ["Daily", 2, 9],
      ["Daily (moved)", 3, 15],
      ["Daily", 5, 9],
    ]);
    expect(events[1]!.id).toBe(ids[1]); // the id survives the move
    await expect(store.getEvent(ids[2]!)).rejects.toBeInstanceOf(NotFoundError);
    expect((await store.getEvent(first.id)).title).toBe("Daily");
    const text = await raw(store, first.id);
    expect(text).toContain("RECURRENCE-ID");
    expect(text).toContain("EXDATE");
  });

  it("quotes foreign UIDs so they can't escape the calendar", () => {
    expect(objectHref("https://dav.example/123/calendars/work/", "../../other/evil")).toBe(
      "https://dav.example/123/calendars/work/..%2F..%2Fother%2Fevil.ics",
    );
  });
});

describe("CalDAV client", () => {
  it("reports an unreachable server without leaking the password", async () => {
    const store = new CaldavStore(IST, { url: "http://127.0.0.1:9/", username: "x", password: "secret-pw" });
    const err = await store.listCalendars().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(BackendError);
    expect(String((err as Error).message)).not.toContain("secret-pw");
  });

  it("turns a rejected login into a hint about app-specific passwords", async () => {
    const store = new CaldavStore(IST, {
      url: "https://caldav.icloud.com",
      username: "x",
      password: "y",
      fetcher: async () => new Response("", { status: 401 }),
    });
    await expect(store.listCalendars()).rejects.toThrow(PermissionDeniedError);
    await expect(store.listCalendars()).rejects.toThrow(/app-specific password/);
  });

  it("follows same-site redirects with credentials, refuses others", async () => {
    const seen: [string, string | null][] = [];
    const fetcher = async (url: string, init: RequestInit): Promise<Response> => {
      seen.push([url, new Headers(init.headers).get("authorization")]);
      if (url.startsWith("https://caldav.icloud.com")) {
        return new Response(null, { status: 301, headers: { location: "https://p52-caldav.icloud.com/x" } });
      }
      if (url.startsWith("https://p52-caldav.icloud.com")) {
        return new Response(null, { status: 302, headers: { location: "https://evil.example/steal" } });
      }
      return new Response("", { status: 200 });
    };
    const client = new DavClient("https://caldav.icloud.com/", "me", "pw", fetcher);
    await expect(client.request("PROPFIND", "https://caldav.icloud.com/")).rejects.toThrow(/redirected somewhere unexpected/);
    expect(seen.map(([u]) => new URL(u).hostname)).toEqual(["caldav.icloud.com", "p52-caldav.icloud.com"]);
    expect(seen.every(([, auth]) => auth?.startsWith("Basic "))).toBe(true);
  });
});

describe("recurrence fast-forward", () => {
  const rules = [
    ["20150131T090000", "FREQ=MONTHLY"], // the 31st: skips short months
    ["20120229T090000", "FREQ=YEARLY"], // leap day
    ["20150105T090000", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH"],
    ["20150101T090000", "FREQ=DAILY;INTERVAL=3"],
    ["20150101T090000", "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"],
    ["20150115T090000", "FREQ=MONTHLY;BYDAY=-1FR"],
  ] as const;

  it.each(rules)("gives the same answers as walking from %s (%s)", async (start, rule) => {
    const { Expander, Resource } = await import("../../src/caldav/ical");
    const ICAL = (await import("ical.js")).default;
    const text =
      "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:t\r\nBEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20260101T000000Z\r\n" +
      `DTSTART;TZID=Europe/Istanbul:${start}\r\nDTEND;TZID=Europe/Istanbul:${start.slice(0, 9)}100000\r\n` +
      `RRULE:${rule}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n`;
    const exp = new Expander(new Resource("h", "/", null, new ICAL.Component(ICAL.parse(text))), IST);
    for (const [from, to] of [[at(1, 0), at(30, 0)], [IST.instant({ y: 2028, m: 2, d: 1, h: 0, mi: 0, s: 0 }), IST.instant({ y: 2028, m: 3, d: 31, h: 0, mi: 0, s: 0 })]] as const) {
      const walked = exp.originals(-Infinity, to).filter((t) => t >= from);
      expect(exp.originals(from, to)).toEqual(walked);
    }
  });
});
