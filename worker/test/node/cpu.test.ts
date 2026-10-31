/**
 * CPU budget check. Workers' free plan allows 10 ms of CPU per request;
 * network time doesn't count, parsing and expanding events does. This
 * builds a heavy but realistic calendar and times the CPU-bound part of
 * list_events (parse + expand + convert) for one week.
 */
import ICAL from "ical.js";
import { expect, it } from "vitest";

import { Expander, Resource } from "../../src/caldav/ical";
import { IST } from "./helpers";

function ics(uid: string, body: string): string {
  return `BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:bench\r\nBEGIN:VEVENT\r\nUID:${uid}\r\nDTSTAMP:20260101T000000Z\r\n${body}\r\nSUMMARY:Event ${uid}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n`;
}

function calendar(): string[] {
  const out: string[] = [];
  // 250 one-off events spread over the year.
  for (let i = 0; i < 250; i++) {
    const day = String((i % 28) + 1).padStart(2, "0");
    const month = String((i % 12) + 1).padStart(2, "0");
    out.push(ics(`one${i}`, `DTSTART;TZID=Europe/Istanbul:2026${month}${day}T100000\r\nDTEND;TZID=Europe/Istanbul:2026${month}${day}T110000`));
  }
  // 30 long-running series, some started years ago, with exceptions.
  for (let i = 0; i < 30; i++) {
    const rule = ["FREQ=DAILY", "FREQ=WEEKLY;BYDAY=MO,WE,FR", "FREQ=MONTHLY;BYMONTHDAY=15", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"][i % 4];
    const ex = "EXDATE;TZID=Europe/Istanbul:20261104T090000,20261111T090000";
    out.push(ics(`series${i}`, `DTSTART;TZID=Europe/Istanbul:2015${String((i % 12) + 1).padStart(2, "0")}02T090000\r\nDTEND;TZID=Europe/Istanbul:2015${String((i % 12) + 1).padStart(2, "0")}02T093000\r\nRRULE:${rule}\r\n${ex}`));
  }
  return out;
}

it("expands a heavy week well inside the free-plan CPU budget", () => {
  const objects = calendar();
  const from = IST.instant({ y: 2026, m: 11, d: 2, h: 0, mi: 0, s: 0 });
  const to = from + 7 * 86_400_000;
  // The server only sends objects that touch the week: all 30 series, ~25 one-offs.
  const sent = objects.filter((o) => o.includes("RRULE") || /2026(11|10)0[2-8]/.test(o));
  const run = (): number => {
    let n = 0;
    for (const text of sent) {
      const res = new Resource("home", "/x.ics", null, new ICAL.Component(ICAL.parse(text)));
      n += new Expander(res, IST).between(from, to).length;
    }
    return n;
  };
  const count = run(); // warm up (JIT), like a reused isolate
  // Best of several runs: wall-clock time on a busy machine is noisy.
  let ms = Infinity;
  for (let i = 0; i < 5; i++) {
    const t0 = performance.now();
    run();
    ms = Math.min(ms, performance.now() - t0);
  }
  console.log(`expanded ${sent.length} objects into ${count} occurrences in ${ms.toFixed(2)} ms`);
  expect(count).toBeGreaterThan(30);
  // Half the 10 ms budget, leaving room for JSON and the MCP layer. Shared CI
  // runners are several times slower than a laptop or Cloudflare's edge, so
  // there the check only catches gross regressions (e.g. no fast-forward).
  expect(ms).toBeLessThan(process.env.CI ? 20 : 5);
});
