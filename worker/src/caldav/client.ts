/**
 * A small CalDAV client over fetch: discovery, calendar listing, time-range
 * and UID queries, PUT and DELETE.
 *
 * Redirects are followed by hand. iCloud answers on caldav.icloud.com and
 * sends you to pNN-caldav.icloud.com; fetch would drop the Authorization
 * header on that cross-host hop, and we only want to send credentials to
 * the same site anyway.
 */

import { XMLParser } from "fast-xml-parser";

import {
  BackendError,
  NotFoundError,
  PermissionDeniedError,
  ReadOnlyCalendarError,
} from "../store";

export interface DavCalendar {
  url: string;
  id: string;
  title: string;
  color: string | null;
  components: string[];
  writable: boolean;
}

export interface DavObject {
  href: string;
  etag: string | null;
  data: string;
}

type Fetch = (input: string, init: RequestInit) => Promise<Response>;

const MAX_REDIRECTS = 5;
const NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"';
const XML_HEADERS = { "Content-Type": "application/xml; charset=utf-8" };

const parser = new XMLParser({
  removeNSPrefix: true,
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  parseTagValue: false,
  isArray: (name) => ["response", "propstat", "comp", "privilege", "href"].includes(name),
});

const escapeXml = (s: string): string =>
  s.replace(/[<>&'"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&apos;", '"': "&quot;" })[c]!);

/** YYYYMMDDTHHMMSSZ */
export function davTime(ms: number): string {
  return new Date(ms).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

function site(host: string): string {
  return host.split(".").slice(-2).join(".");
}

function basicAuth(username: string, password: string): string {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return `Basic ${btoa(bin)}`;
}

type Props = Record<string, unknown>;

interface MultiStatus {
  href: string;
  props: Props; // merged from 200 propstats
}

function text(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === "string") return v;
  if (typeof v === "object" && "#text" in (v as Props)) return String((v as Props)["#text"]);
  return null;
}

function firstHref(v: unknown): string | null {
  if (v && typeof v === "object" && "href" in (v as Props)) {
    const hrefs = (v as Props).href as unknown[];
    return text(hrefs?.[0]);
  }
  return null;
}

export class DavClient {
  private readonly auth: string;

  constructor(
    readonly baseUrl: string,
    username: string,
    password: string,
    private readonly fetcher: Fetch = (input, init) => fetch(input, init),
  ) {
    this.auth = basicAuth(username, password);
  }

  // ------------------------------------------------------------ transport
  private allowedRedirect(from: URL, to: URL): boolean {
    if (to.origin === from.origin) return true;
    return to.protocol === "https:" && site(to.hostname) === site(from.hostname);
  }

  /** One request, following same-site redirects. Returns the response and its final URL. */
  async request(
    method: string,
    url: string,
    init: { headers?: Record<string, string>; body?: string; write?: boolean } = {},
  ): Promise<{ res: Response; url: string }> {
    let current = new URL(url);
    for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
      let res: Response;
      try {
        res = await this.fetcher(current.toString(), {
          method,
          headers: { Authorization: this.auth, ...init.headers },
          body: init.body,
          redirect: "manual",
        });
      } catch {
        throw new BackendError("Could not reach the CalDAV server. Try again shortly.");
      }
      if ([301, 302, 303, 307, 308].includes(res.status)) {
        const location = res.headers.get("location");
        await res.body?.cancel();
        const next = location ? new URL(location, current) : null;
        if (!next || !this.allowedRedirect(current, next)) {
          throw new BackendError("The CalDAV server redirected somewhere unexpected.");
        }
        current = next;
        continue;
      }
      if (res.status >= 400) {
        await res.body?.cancel();
        throw this.error(res.status, init.write ?? false);
      }
      return { res, url: current.toString() };
    }
    throw new BackendError("The CalDAV server redirected too many times.");
  }

  private error(status: number, write: boolean): Error {
    if (status === 401) {
      return new PermissionDeniedError(
        "The CalDAV server rejected the login. Check the username and the " +
          "app-specific password (appleid.apple.com > Sign-In and Security).",
      );
    }
    if (status === 403) {
      return write
        ? new ReadOnlyCalendarError("This calendar can't be changed.")
        : new PermissionDeniedError("The CalDAV server refused access to this calendar.");
    }
    if (status === 404 || status === 410) return new NotFoundError("The calendar or event no longer exists on the server.");
    if (status === 412) return new BackendError("The event changed on the server in the meantime. Try again.");
    return new BackendError(`CalDAV server error (HTTP ${status}). Try again shortly.`);
  }

  private async multistatus(method: string, url: string, depth: "0" | "1", body: string): Promise<{ items: MultiStatus[]; url: string }> {
    const { res, url: final } = await this.request(method, url, {
      headers: { ...XML_HEADERS, Depth: depth },
      body,
    });
    let doc: Props;
    try {
      doc = parser.parse(await res.text()) as Props;
    } catch {
      throw new BackendError("The CalDAV server sent a response that could not be read.");
    }
    const responses = ((doc.multistatus as Props | undefined)?.response as Props[] | undefined) ?? [];
    const items: MultiStatus[] = [];
    for (const r of responses) {
      const href = text((r.href as unknown[] | undefined)?.[0]);
      if (!href) continue;
      const props: Props = {};
      for (const ps of (r.propstat as Props[] | undefined) ?? []) {
        if (!/\s2\d\d\s/.test(` ${text(ps.status) ?? ""} `)) continue;
        Object.assign(props, (ps.prop as Props | undefined) ?? {});
      }
      items.push({ href: new URL(href, final).toString(), props });
    }
    return { items, url: final };
  }

  // ------------------------------------------------------------ discovery
  /** The calendar home collection URL. */
  async home(): Promise<string> {
    const principal = await this.multistatus(
      "PROPFIND",
      this.baseUrl,
      "0",
      `<d:propfind ${NS}><d:prop><d:current-user-principal/></d:prop></d:propfind>`,
    );
    const principalHref = principal.items.map((i) => firstHref(i.props["current-user-principal"])).find(Boolean);
    if (!principalHref) throw new BackendError("The CalDAV server didn't say where your calendars are.");
    const principalUrl = new URL(principalHref, principal.url).toString();
    const home = await this.multistatus(
      "PROPFIND",
      principalUrl,
      "0",
      `<d:propfind ${NS}><d:prop><c:calendar-home-set/></d:prop></d:propfind>`,
    );
    const homeHref = home.items.map((i) => firstHref(i.props["calendar-home-set"])).find(Boolean);
    if (!homeHref) throw new BackendError("The CalDAV server didn't say where your calendars are.");
    return new URL(homeHref, home.url).toString();
  }

  async calendars(homeUrl: string): Promise<DavCalendar[]> {
    const { items } = await this.multistatus(
      "PROPFIND",
      homeUrl,
      "1",
      `<d:propfind ${NS} xmlns:a="http://apple.com/ns/ical/"><d:prop>` +
        "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>" +
        "<d:current-user-privilege-set/><a:calendar-color/></d:prop></d:propfind>",
    );
    const out: DavCalendar[] = [];
    for (const { href, props } of items) {
      const type = props.resourcetype;
      if (!type || typeof type !== "object" || !("calendar" in (type as Props))) continue;
      const comps = ((props["supported-calendar-component-set"] as Props | undefined)?.comp as Props[] | undefined) ?? [];
      const components = comps.map((c) => String(c["@_name"] ?? "").toUpperCase()).filter(Boolean);
      const privileges = new Set<string>();
      for (const p of ((props["current-user-privilege-set"] as Props | undefined)?.privilege as Props[] | undefined) ?? []) {
        for (const k of Object.keys(p)) privileges.add(k);
      }
      const id = decodeURIComponent(href.replace(/\/+$/, "").split("/").pop() ?? "");
      const color = text(props["calendar-color"]);
      out.push({
        url: href.endsWith("/") ? href : `${href}/`,
        id,
        title: text(props.displayname) || id,
        color: color && /^#[0-9a-f]{6}/i.test(color) ? color.slice(0, 7).toUpperCase() : null,
        components: components.length ? components : ["VEVENT"],
        writable: privileges.size === 0 || ["all", "write", "write-content"].some((p) => privileges.has(p)),
      });
    }
    return out;
  }

  // ------------------------------------------------------------ objects
  private async report(calendarUrl: string, filter: string): Promise<DavObject[]> {
    const { items } = await this.multistatus(
      "REPORT",
      calendarUrl,
      "1",
      `<c:calendar-query ${NS}><d:prop><d:getetag/><c:calendar-data/></d:prop>` +
        `<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">${filter}` +
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>",
    );
    const out: DavObject[] = [];
    for (const { href, props } of items) {
      const data = text(props["calendar-data"]);
      if (data) out.push({ href, etag: text(props.getetag), data });
    }
    return out;
  }

  /** Objects with an event overlapping [start, end) (recurring series included whole). */
  query(calendarUrl: string, start: number, end: number): Promise<DavObject[]> {
    return this.report(calendarUrl, `<c:time-range start="${davTime(start)}" end="${davTime(end)}"/>`);
  }

  async byUid(calendarUrl: string, uid: string): Promise<DavObject | null> {
    const found = await this.report(
      calendarUrl,
      `<c:prop-filter name="UID"><c:text-match collation="i;octet">${escapeXml(uid)}</c:text-match></c:prop-filter>`,
    );
    return found[0] ?? null;
  }

  /** PUT an object. ``create`` refuses to overwrite; ``etag`` refuses lost updates. */
  async put(url: string, data: string, opts: { create?: boolean; etag?: string | null } = {}): Promise<string | null> {
    const headers: Record<string, string> = { "Content-Type": "text/calendar; charset=utf-8" };
    if (opts.create) headers["If-None-Match"] = "*";
    else if (opts.etag) headers["If-Match"] = opts.etag;
    const { res } = await this.request("PUT", url, { headers, body: data, write: true });
    await res.body?.cancel();
    return res.headers.get("etag");
  }

  async delete(url: string, etag?: string | null): Promise<void> {
    const headers: Record<string, string> = {};
    if (etag) headers["If-Match"] = etag;
    const { res } = await this.request("DELETE", url, { headers, write: true });
    await res.body?.cancel();
  }
}
