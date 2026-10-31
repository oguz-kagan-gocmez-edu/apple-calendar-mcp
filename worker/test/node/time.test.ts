import { describe, expect, it } from "vitest";

import { Zone, isoWeek, parseClock, parseDateTime } from "../../src/time";

const IST = new Zone("Europe/Istanbul");
const BERLIN = new Zone("Europe/Berlin");

describe("parseDateTime", () => {
  it.each([
    ["2026-11-03", "2026-11-03T00:00:00+03:00"],
    ["2026-11-03T14:00", "2026-11-03T14:00:00+03:00"],
    ["2026-11-03 14:00", "2026-11-03T14:00:00+03:00"],
    ["2026-11-03T14:00:00+01:00", "2026-11-03T16:00:00+03:00"],
    ["2026-11-03T11:00:00Z", "2026-11-03T14:00:00+03:00"],
  ])("%s", (input, iso) => {
    expect(IST.iso(parseDateTime(input, IST).ms)).toBe(iso);
  });

  it.each(["", "tomorrow", "2026-13-01", "2026-02-30", "2026-11-03T25:00"])("rejects %j", (input) => {
    expect(() => parseDateTime(input, IST)).toThrow(RangeError);
  });

  it("keeps the written date for offsets", () => {
    expect(parseDateTime("2026-11-03T23:30:00-05:00", IST).date).toEqual({ y: 2026, m: 11, d: 3 });
  });
});

describe("Zone", () => {
  it("handles DST like Python's zoneinfo", () => {
    // 2026-03-29 02:30 doesn't exist in Berlin: moves forward to 03:30.
    expect(BERLIN.iso(BERLIN.instant({ y: 2026, m: 3, d: 29, h: 2, mi: 30, s: 0 }))).toBe("2026-03-29T03:30:00+02:00");
    // 2026-10-25 02:30 happens twice: the earlier one (still +02:00).
    expect(BERLIN.iso(BERLIN.instant({ y: 2026, m: 10, d: 25, h: 2, mi: 30, s: 0 }))).toBe("2026-10-25T02:30:00+02:00");
  });

  it("names weekdays and ISO weeks", () => {
    const ms = parseDateTime("2026-11-02T08:00", IST).ms;
    expect(IST.weekdayName(ms)).toBe("Monday");
    expect(isoWeek({ y: 2026, m: 11, d: 2 })).toBe(45);
    expect(isoWeek({ y: 2027, m: 1, d: 1 })).toBe(53);
  });
});

it("parses clock times", () => {
  expect(parseClock("09:30")).toBe(570);
  expect(() => parseClock("9am")).toThrow(RangeError);
});
