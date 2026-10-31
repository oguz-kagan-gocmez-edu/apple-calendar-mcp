import { inject } from "vitest";

import { CaldavStore, resetCaches } from "../../src/caldav/store";
import { CalendarService } from "../../src/service";
import { Zone } from "../../src/time";

export const IST = new Zone("Europe/Istanbul");
/** Monday 2 Nov 2026, 08:00 Istanbul (same as the Python tests). */
export const NOW = IST.instant({ y: 2026, m: 11, d: 2, h: 8, mi: 0, s: 0 });

export function at(day: number, h: number, mi = 0): number {
  return IST.instant({ y: 2026, m: 11, d: day, h, mi, s: 0 });
}

async function mkcalendar(url: string, user: string, id: string, name: string): Promise<void> {
  const res = await fetch(`${url}${user}/${id}/`, {
    method: "MKCALENDAR",
    headers: { Authorization: `Basic ${btoa(`${user}:x`)}`, "Content-Type": "application/xml" },
    body:
      '<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' +
      `<d:set><d:prop><d:displayname>${name}</d:displayname></d:prop></d:set></c:mkcalendar>`,
  });
  if (res.status !== 201) throw new Error(`MKCALENDAR ${id}: ${res.status}`);
}

/** A fresh Radicale account with 'home' (default) and 'work' calendars. */
export async function freshStore(): Promise<CaldavStore> {
  resetCaches();
  const url = inject("radicaleUrl");
  const user = `u${crypto.randomUUID().slice(0, 10).replace(/-/g, "")}`;
  await mkcalendar(url, user, "home", "Home");
  await mkcalendar(url, user, "work", "Work");
  return new CaldavStore(IST, { url, username: user, password: "x", defaultCalendar: "home" });
}

export async function freshService(): Promise<CalendarService> {
  return new CalendarService(await freshStore(), IST, () => NOW);
}
