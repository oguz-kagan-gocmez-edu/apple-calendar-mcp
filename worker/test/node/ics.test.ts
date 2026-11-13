/** iCalendar export and import (port of tests/test_ics.py). */
import { beforeEach, describe, expect, it } from "vitest";

import { type IcsEvent, parseIcs } from "../../src/ics";
import { MemoryStore } from "../../src/memory-store";
import { CalendarService } from "../../src/service";
import { InvalidRequestError } from "../../src/store";
import { MINUTE } from "../../src/time";
import { IST, NOW, at, freshService } from "./helpers";

const cal = (...events: string[]) =>
  `BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n${events
    .map((e) => `BEGIN:VEVENT\r\n${e}END:VEVENT\r\n`)
    .join("")}END:VCALENDAR\r\n`;

function one(event: string): IcsEvent {
  const [events, skipped] = parseIcs(cal(event), IST);
  expect(skipped).toEqual([]);
  expect(events).toHaveLength(1);
  return events[0]!;
}

function why(event: string): string {
  const [events, skipped] = parseIcs(cal(event), IST);
  expect(events).toEqual([]);
  return skipped[0]!.reason;
}

const withZones = (zones: string, ev: string): IcsEvent => {
  const [events, skipped] = parseIcs(cal(ev).replace("PRODID:-//test//EN\r\n", `PRODID:-//test//EN\r\n${zones}`), IST);
  expect(skipped).toEqual([]);
  return events[0]!;
};
const zoneDef = (tzid: string, body: string) => `BEGIN:VTIMEZONE\r\nTZID:${tzid}\r\n${body}END:VTIMEZONE\r\n`;

describe("files from Outlook and Teams", () => {
  it("maps Windows zone names", () => {
    expect(one("SUMMARY:a\r\nDTSTART;TZID=W. Europe Standard Time:20261103T090000\r\n").start).toBe(at(3, 11));
    expect(one('SUMMARY:a\r\nDTSTART;TZID="Turkey Standard Time":20261103T090000\r\n').start).toBe(at(3, 9));
    expect(one("SUMMARY:a\r\nDTSTART;TZID=pacific standard time:20261103T090000\r\n").start).toBe(at(3, 20));
  });

  it("uses zones the file defines, but not ones with daylight time", () => {
    const lic = zoneDef("Custom 1", "X-LIC-LOCATION:Europe/Berlin\r\n");
    expect(withZones(lic, "SUMMARY:a\r\nDTSTART;TZID=Custom 1:20261103T090000\r\n").start).toBe(at(3, 11));
    const fixed = zoneDef(
      "(UTC+03:00) Istanbul",
      "BEGIN:STANDARD\r\nDTSTART:16010101T000000\r\nTZOFFSETFROM:+0300\r\nTZOFFSETTO:+0300\r\nEND:STANDARD\r\n",
    );
    const ev = 'SUMMARY:a\r\nDTSTART;TZID="(UTC+03:00) Istanbul":20261103T090000\r\n';
    expect(withZones(fixed, ev).start).toBe(at(3, 9));
    const dst = zoneDef(
      "Somewhere",
      "BEGIN:STANDARD\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\nTZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\n",
    );
    const text = cal("SUMMARY:a\r\nDTSTART;TZID=Somewhere:20261103T090000\r\n").replace(
      "PRODID:-//test//EN\r\n",
      `PRODID:-//test//EN\r\n${dst}`,
    );
    expect(parseIcs(text, IST)[1][0]!.reason).toContain("isn't one this server knows");
  });

  it("turns meeting links into the URL, keeping an existing one", () => {
    const teams = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d";
    expect(one(`SUMMARY:Sync\r\nDTSTART:20261103T060000Z\r\nX-MICROSOFT-SKYPETEAMSMEETINGURL:${teams}\r\n`).url).toBe(teams);
    const zoom = one(
      "SUMMARY:Call\r\nDTSTART:20261103T060000Z\r\n" +
        "DESCRIPTION:Join Zoom Meeting\\n<https://us02web.zoom.us/j/81234567890?pwd=abc>.\\nID: 812\r\n",
    );
    expect(zoom.url).toBe("https://us02web.zoom.us/j/81234567890?pwd=abc");
    const kept = one(
      "SUMMARY:x\r\nDTSTART:20261103T060000Z\r\nURL:https://example.com/agenda\r\n" +
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij\r\nDESCRIPTION:Agenda attached\r\n",
    );
    expect([kept.url, kept.notes]).toEqual([
      "https://example.com/agenda",
      "Join: https://meet.google.com/abc-defg-hij\n\nAgenda attached",
    ]);
    expect(one("SUMMARY:x\r\nDTSTART:20261103T060000Z\r\nDESCRIPTION:See https://zoom.us/pricing\r\n").url).toBeNull();
  });
});

describe("parseIcs", () => {
  it("reads UTC, zoned and floating times", () => {
    expect(one("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T070000Z\r\n").start).toBe(at(3, 9));
    const zoned = one("SUMMARY:a\r\nDTSTART;TZID=Europe/Berlin:20261103T090000\r\nDURATION:PT90M\r\n");
    expect([zoned.start, zoned.end]).toEqual([at(3, 11), at(3, 12, 30)]);
    const floating = one("SUMMARY:a\r\nDTSTART:20261103T090000\r\n");
    expect([floating.start, floating.end, floating.all_day]).toEqual([at(3, 9), at(3, 9), false]);
  });

  it("reads all-day events", () => {
    const e = one("SUMMARY:Trip\r\nDTSTART;VALUE=DATE:20261106\r\nDTEND;VALUE=DATE:20261109\r\n");
    expect([e.start, e.end, e.all_day]).toEqual([at(6, 0), at(9, 0), true]);
    expect(one("SUMMARY:Day\r\nDTSTART;VALUE=DATE:20261106\r\n").end).toBe(at(7, 0));
  });

  it("unfolds lines and unescapes text", () => {
    const e = one(
      "SUMMARY:Lunch\\, then a very long title that goes on\r\n  and is folded\r\n" +
        "DTSTART:20261103T060000Z\r\n" +
        "DESCRIPTION:Line one\\nLine two\; done\r\n" +
        'LOCATION;ALTREP="cid:x;y":Room 1\r\n' +
        "URL:https://example.com/a\r\nTRANSP:TRANSPARENT\r\n",
    );
    expect(e.title).toBe("Lunch, then a very long title that goes on and is folded");
    expect(e.notes).toBe("Line one\nLine two; done");
    expect([e.location, e.url, e.availability]).toEqual(["Room 1", "https://example.com/a", "free"]);
  });

  it("reads alarms set before the start", () => {
    const e = one(
      "SUMMARY:a\r\nDTSTART:20261103T060000Z\r\n" +
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n" +
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-P1D\r\nEND:VALARM\r\n" +
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;RELATED=END:-PT5M\r\nEND:VALARM\r\n" +
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;VALUE=DATE-TIME:20261103T050000Z\r\nEND:VALARM\r\n",
    );
    expect(e.alarms).toEqual([1440, 15]);
  });

  it.each([
    ["FREQ=DAILY;COUNT=5", { frequency: "daily", interval: 1, count: 5 }],
    ["FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;WKST=MO", { frequency: "weekly", interval: 2, days_of_week: ["tue", "thu"] }],
    ["FREQ=WEEKLY;UNTIL=20261130T205959Z", { frequency: "weekly", until: "2026-11-30" }],
    ["FREQ=MONTHLY;BYMONTHDAY=3", { frequency: "monthly", days_of_week: null }],
    ["FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=3", { frequency: "yearly" }],
  ])("reads the repeat rule %s", (rrule, expected) => {
    const e = one(`SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T070000Z\r\nRRULE:${rrule}\r\n`);
    expect(e.recurrence).toMatchObject(expected);
  });

  it("reads removed occurrences", () => {
    const e = one(
      "SUMMARY:a\r\nDTSTART;TZID=Europe/Istanbul:20261103T090000\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\n" +
        "EXDATE;TZID=Europe/Istanbul:20261110T090000,20261124T090000\r\n",
    );
    expect(e.exdates).toEqual([at(10, 9), at(24, 9)]);
  });

  it.each([
    ["SUMMARY:a\r\n", "no start time"],
    ["SUMMARY:a\r\nDTSTART:tomorrow\r\n", "can't be read"],
    ["SUMMARY:a\r\nDTSTART:20261341T060000Z\r\n", "can't be read"],
    ["SUMMARY:a\r\nDTSTART;TZID=Middle Earth Time:20261103T090000\r\n", "time zone"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T050000Z\r\n", "ends before"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nSTATUS:CANCELLED\r\n", "canceled"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRECURRENCE-ID:20261103T060000Z\r\n", "changed"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYDAY=2TU\r\n", "second Tuesday"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYMONTHDAY=15\r\n", "BYMONTHDAY"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=HOURLY\r\n", "hourly"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRDATE:20261104T060000Z\r\n", "extra dates"],
    ["SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=DAILY\r\nEXDATE:20261103T060000Z\r\n", "first occurrence"],
  ])("skips %j with a reason", (event, reason) => {
    expect(why(event)).toContain(reason);
  });

  it("ignores other components and broken lines", () => {
    const text =
      "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" +
      "BEGIN:VTIMEZONE\r\nTZID:X\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n" +
      "BEGIN:VTODO\r\nSUMMARY:todo\r\nEND:VTODO\r\nthis line is broken\r\n" +
      "BEGIN:VEVENT\r\nSUMMARY:kept\r\nDTSTART:20261103T060000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n";
    const [events, skipped] = parseIcs(text, IST);
    expect([events.map((e) => e.title), skipped]).toEqual([["kept"], []]);
  });

  it.each(["", "hello", "BEGIN:VEVENT\r\nEND:VEVENT\r\n", "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VCALENDAR\r\n"])(
    "rejects %j as not iCalendar",
    (text) => {
      expect(() => parseIcs(text, IST)).toThrow(/iCalendar/);
    },
  );
});

const FILE = cal(
  "UID:1\r\nSUMMARY:Kickoff\r\nDTSTART;TZID=Europe/Istanbul:20261103T100000\r\nDURATION:PT1H\r\n" +
    "LOCATION:Room 2\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n",
  "UID:2\r\nSUMMARY:Standup\r\nDTSTART;TZID=Europe/Istanbul:20261104T090000\r\nDURATION:PT15M\r\n" +
    "RRULE:FREQ=DAILY;COUNT=4\r\nEXDATE;TZID=Europe/Istanbul:20261105T090000\r\n",
  "UID:3\r\nSUMMARY:Offsite\r\nDTSTART;VALUE=DATE:20261112\r\nDTEND;VALUE=DATE:20261114\r\n",
  "UID:4\r\nSUMMARY:Board\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYDAY=1TU\r\n",
);
const NOVEMBER = ["2026-11-01", "2026-11-30"] as const;

describe("export", () => {
  let service: CalendarService;
  beforeEach(() => {
    service = new CalendarService(new MemoryStore(), IST, () => NOW);
  });

  it("writes folded, escaped iCalendar", async () => {
    await service.createEvent({
      title: "Café; notes, etc.", start: "2026-11-03T14:00", duration_minutes: 45, location: "Lab 3",
      notes: `Bring\nlaptop ${"x".repeat(80)}`, url: "https://example.com", alarms_minutes_before: [15],
      availability: "free",
    });
    await service.createEvent({ title: "Trip", start: "2026-11-06", end: "2026-11-08", all_day: true });
    const r = await service.exportIcs(...NOVEMBER);
    expect([r.count, r.truncated]).toEqual([2, false]);
    expect(r.ics.startsWith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")).toBe(true);
    expect(r.ics.endsWith("END:VCALENDAR\r\n")).toBe(true);
    const bytes = new TextEncoder();
    expect(r.ics.split("\r\n").every((line) => bytes.encode(line).length <= 75)).toBe(true);
    const unfolded = r.ics.replace(/\r\n /g, "");
    expect(unfolded).toContain("SUMMARY:Café\\; notes\\, etc.\r\n");
    expect(unfolded).toContain("DTSTART:20261103T110000Z\r\nDTEND:20261103T114500Z\r\n");
    expect(unfolded).toContain("DESCRIPTION:Bring\\nlaptop xxx");
    expect(unfolded).toContain("TRANSP:TRANSPARENT\r\n");
    expect(unfolded).toContain("TRIGGER:-PT15M\r\n");
    expect(unfolded).toContain("DTSTART;VALUE=DATE:20261106\r\nDTEND;VALUE=DATE:20261109\r\n");
  });

  it("caps the number of events", async () => {
    for (const hour of ["09", "10", "11"]) await service.createEvent({ title: `E${hour}`, start: `2026-11-03T${hour}:00` });
    const r = await service.exportIcs("2026-11-03", "2026-11-03", null, 2);
    expect([r.count, r.truncated, r.ics.split("BEGIN:VEVENT").length - 1]).toEqual([2, true, 2]);
  });
});

describe("import", () => {
  it("imports what it can over CalDAV, flattens on export, and undoes as one change", async () => {
    const service = await freshService();
    const r = await service.importIcs(FILE, "work");
    expect(r.imported.map((e) => e.title)).toEqual(["Kickoff", "Standup", "Offsite"]);
    expect(r.skipped.map((s) => [s.title, s.reason.includes("second Tuesday")])).toEqual([["Board", true]]);
    const { events } = await service.listEvents(...NOVEMBER);
    expect(new Set(events.map((e) => e.calendar_id))).toEqual(new Set(["work"]));
    const kickoff = events.find((e) => e.title === "Kickoff")!;
    expect([kickoff.start, kickoff.end, kickoff.location, kickoff.alarms_minutes_before]).toEqual([
      at(3, 10), at(3, 11), "Room 2", [10],
    ]);
    // 5 Nov was removed with EXDATE
    expect(events.filter((e) => e.title === "Standup").map((e) => e.start)).toEqual([at(4, 9), at(6, 9), at(7, 9)]);
    const offsite = events.find((e) => e.title === "Offsite")!;
    expect([offsite.start, offsite.end, offsite.all_day]).toEqual([at(12, 0), at(14, 0), true]);

    const exported = await service.exportIcs(...NOVEMBER);
    expect(exported.count).toBe(5);
    expect(exported.ics).not.toContain("RRULE");
    const other = new CalendarService(new MemoryStore(), IST, () => NOW);
    expect((await other.importIcs(exported.ics)).imported).toHaveLength(5);

    expect((await service.importIcs(FILE, "work")).change_id).toBeNull(); // all already there
    const undone = await service.undoChange(r.change_id);
    expect(undone.removed_ids).toHaveLength(3);
    expect((await service.listEvents(...NOVEMBER)).count).toBe(0);
  });

  describe("with the memory store", () => {
    let service: CalendarService;
    const kickoff = "SUMMARY:Kickoff\r\nDTSTART;TZID=Europe/Istanbul:20261103T100000\r\nDURATION:PT1H\r\n";
    beforeEach(() => {
      service = new CalendarService(new MemoryStore(), IST, () => NOW);
    });

    it("previews an import without adding anything", async () => {
      await service.createEvent({ title: "Kickoff", start: "2026-11-03T10:00", calendar_id: "work" });
      const r = await service.importIcs(FILE, "work", true, true);
      expect([r.dry_run, r.change_id]).toEqual([true, null]);
      expect(r.imported.map((e) => [e.id, e.title, e.calendar_title])).toEqual([
        ["", "Standup", "Work"],
        ["", "Offsite", "Work"],
      ]);
      expect(new Set(r.skipped.map((s) => s.title))).toEqual(new Set(["Board", "Kickoff"]));
      expect((await service.listEvents(...NOVEMBER)).events.map((e) => e.title)).toEqual(["Kickoff"]);
      expect(await service.listChanges()).toHaveLength(1); // only the create above
      await expect(service.importIcs(FILE, "nowhere", true, true)).rejects.toThrow(/nowhere/);
    });

    it("skips events already there, and copies inside the file", async () => {
      await service.createEvent({ title: "kickoff", start: "2026-11-03T10:00" });
      const fresh = "SUMMARY:New\r\nDTSTART;TZID=Europe/Istanbul:20261103T120000\r\n";
      const again = cal(kickoff, fresh, fresh);
      const r = await service.importIcs(again);
      expect(r.imported.map((e) => e.title)).toEqual(["New"]);
      expect(r.skipped).toEqual([
        { title: "Kickoff", reason: "Skipped: it is already in the calendar." },
        { title: "New", reason: "Skipped: it is already in the calendar." },
      ]);
      expect((await service.importIcs(again, null, false)).imported).toHaveLength(3);
    });

    it("checks only the named calendar, and events years apart", async () => {
      await service.createEvent({ title: "Kickoff", start: "2026-11-03T10:00", calendar_id: "home" });
      expect((await service.importIcs(cal(kickoff), "work")).imported).toHaveLength(1);
      const far = cal("SUMMARY:Old\r\nDTSTART:20200101T060000Z\r\n", "SUMMARY:Far\r\nDTSTART:20290101T060000Z\r\n");
      expect((await service.importIcs(far)).imported).toHaveLength(2);
      expect((await service.importIcs(far)).skipped).toHaveLength(2);
    });

    it("skips events with invalid fields instead of failing", async () => {
      const r = await service.importIcs(
        cal(
          "DTSTART:20261103T060000Z\r\n",
          "SUMMARY:Link\r\nDTSTART:20261103T060000Z\r\nURL:javascript:alert(1)\r\n",
          "SUMMARY:Fine\r\nDTSTART:20261103T070000Z\r\n",
        ),
      );
      expect(r.imported.map((e) => e.title)).toEqual(["Fine"]);
      expect(r.skipped.map((s) => s.title)).toEqual(["(no title)", "Link"]);
    });

    it("rolls back when a write fails", async () => {
      await expect(service.importIcs(cal(kickoff), "nope")).rejects.toThrow(/Kickoff.*Nothing was imported/s);
      expect((await service.listEvents(...NOVEMBER)).count).toBe(0);
      expect(await service.listChanges()).toEqual([]);
    });

    it("enforces the limits", async () => {
      const many = cal(
        ...Array.from({ length: 101 }, (_, n) => {
          const t = new Date(at(3, 0) + n * MINUTE).toISOString().replace(/[-:]|\.\d+/g, "");
          return `SUMMARY:E${n}\r\nDTSTART:${t}\r\n`;
        }),
      );
      await expect(service.importIcs(many)).rejects.toThrow(/at most 100/);
      await expect(service.importIcs("x".repeat(1_000_001))).rejects.toThrow(/too large/);
      await expect(service.importIcs("not a calendar")).rejects.toThrow(InvalidRequestError);
      expect((await service.listEvents(...NOVEMBER)).count).toBe(0);
    });
  });
});
