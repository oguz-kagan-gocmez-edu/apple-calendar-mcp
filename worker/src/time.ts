/**
 * Time zones and ISO 8601, without a date library.
 *
 * Instants are epoch milliseconds (`number`). A calendar date is a `CivilDate`
 * and a wall-clock time is a `Wall`; converting between them and instants
 * needs a `Zone`, which wraps an IANA name and uses `Intl` (Workers and Node
 * ship the full time-zone database).
 */

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

export interface CivilDate {
  y: number;
  m: number; // 1-12
  d: number;
}

export interface Wall extends CivilDate {
  h: number;
  mi: number;
  s: number;
}

// ---------------------------------------------------------------- civil dates
/** Days since 1970-01-01 (proleptic Gregorian). */
export function dayNumber({ y, m, d }: CivilDate): number {
  return Math.floor(Date.UTC(y, m - 1, d) / DAY);
}

export function fromDayNumber(n: number): CivilDate {
  const t = new Date(n * DAY);
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

export function addDays(date: CivilDate, days: number): CivilDate {
  return fromDayNumber(dayNumber(date) + days);
}

export function compareDates(a: CivilDate, b: CivilDate): number {
  return dayNumber(a) - dayNumber(b);
}

/** 0 = Monday ... 6 = Sunday (like Python's ``date.weekday()``). */
export function weekday(date: CivilDate): number {
  return (dayNumber(date) + 3) % 7; // 1970-01-01 was a Thursday
}

const pad = (n: number, width = 2): string => String(n).padStart(width, "0");

export function formatDate({ y, m, d }: CivilDate): string {
  return `${pad(y, 4)}-${pad(m)}-${pad(d)}`;
}

export function parseDate(value: string): CivilDate {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) throw new RangeError(`Invalid date ${JSON.stringify(value)}; expected YYYY-MM-DD.`);
  const date = { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]) };
  if (formatDate(fromDayNumber(dayNumber(date))) !== formatDate(date)) {
    throw new RangeError(`Invalid date ${JSON.stringify(value)}.`);
  }
  return date;
}

// ---------------------------------------------------------------- zones
const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export class Zone {
  readonly name: string;
  private readonly fmt: Intl.DateTimeFormat;
  // Offsets only change on quarter-hour boundaries, so caching per 15 minutes is exact.
  private readonly offsets = new Map<number, number>();

  constructor(name: string) {
    this.fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: name, // throws RangeError for unknown zones
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    this.name = this.fmt.resolvedOptions().timeZone;
  }

  static isValid(name: string): boolean {
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: name });
      return true;
    } catch {
      return false;
    }
  }

  /** Offset from UTC in minutes at ``ms``. */
  offset(ms: number): number {
    const bucket = Math.floor(ms / (15 * MINUTE));
    let cached = this.offsets.get(bucket);
    if (cached === undefined) {
      const at = bucket * 15 * MINUTE;
      const w = this.rawWall(at);
      cached = Math.round((Date.UTC(w.y, w.m - 1, w.d, w.h, w.mi, w.s) - at) / MINUTE);
      if (this.offsets.size > 4096) this.offsets.clear();
      this.offsets.set(bucket, cached);
    }
    return cached;
  }

  private rawWall(ms: number): Wall {
    const parts: Record<string, number> = {};
    for (const p of this.fmt.formatToParts(new Date(ms))) {
      if (p.type !== "literal") parts[p.type] = Number(p.value);
    }
    return { y: parts.year!, m: parts.month!, d: parts.day!, h: parts.hour!, mi: parts.minute!, s: parts.second! };
  }

  /** Wall-clock time at ``ms`` in this zone. */
  wall(ms: number): Wall {
    const local = new Date(ms + this.offset(ms) * MINUTE);
    return {
      y: local.getUTCFullYear(),
      m: local.getUTCMonth() + 1,
      d: local.getUTCDate(),
      h: local.getUTCHours(),
      mi: local.getUTCMinutes(),
      s: local.getUTCSeconds(),
    };
  }

  date(ms: number): CivilDate {
    const { y, m, d } = this.wall(ms);
    return { y, m, d };
  }

  /**
   * The instant a wall-clock time denotes. Times in a DST gap move forward,
   * ambiguous times take the earlier instant (like Python's ``fold=0``).
   */
  instant(w: Wall): number {
    const asUtc = Date.UTC(w.y, w.m - 1, w.d, w.h, w.mi, w.s);
    // The offsets in force a day either side cover any single transition.
    const before = this.offset(asUtc - DAY);
    const after = this.offset(asUtc + DAY);
    const valid = [asUtc - before * MINUTE, asUtc - after * MINUTE].filter(
      (t) => t + this.offset(t) * MINUTE === asUtc,
    );
    // Ambiguous: earlier reading. In a gap: read with the old offset (moves forward).
    return valid.length ? Math.min(...valid) : asUtc - before * MINUTE;
  }

  startOfDay(date: CivilDate): number {
    return this.instant({ ...date, h: 0, mi: 0, s: 0 });
  }

  /** ISO 8601 with this zone's offset, e.g. ``2026-11-03T14:00:00+03:00``. */
  iso(ms: number): string {
    const off = this.offset(ms);
    const w = this.wall(ms);
    const sign = off < 0 ? "-" : "+";
    const a = Math.abs(off);
    const suffix = off === 0 ? "+00:00" : `${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
    return `${formatDate(w)}T${pad(w.h)}:${pad(w.mi)}:${pad(w.s)}${suffix}`;
  }

  weekdayName(ms: number): string {
    return WEEKDAY_NAMES[weekday(this.date(ms))]!;
  }
}

// ---------------------------------------------------------------- parsing
/** A parsed point in time and the calendar date as written (in its own offset). */
export interface Parsed {
  ms: number;
  date: CivilDate;
}

export function isDateOnly(value: string): boolean {
  const v = value.trim();
  return v.length === 10 && !v.includes("T") && !v.includes(" ");
}

const ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?$/i;

/**
 * Parse ISO 8601: '2026-11-03', '2026-11-03T14:00', '2026-11-03 14:00',
 * '2026-11-03T14:00:00+03:00' or a trailing 'Z'. Date-only values are local
 * midnight; values without an offset are read in ``zone``.
 */
export function parseDateTime(value: string, zone: Zone): Parsed {
  const raw = value.trim();
  const fail = (): never => {
    throw new RangeError(
      `Invalid date/time ${JSON.stringify(value)}. Use ISO 8601, e.g. ` +
        "'2026-11-03', '2026-11-03T14:00' or '2026-11-03T14:00:00+03:00'.",
    );
  };
  if (!raw) throw new RangeError("empty datetime string");
  if (isDateOnly(raw)) {
    let date: CivilDate;
    try {
      date = parseDate(raw);
    } catch {
      return fail();
    }
    return { ms: zone.startOfDay(date), date };
  }
  const m = ISO.exec(raw);
  if (!m) return fail();
  const wall: Wall = {
    y: Number(m[1]),
    m: Number(m[2]),
    d: Number(m[3]),
    h: Number(m[4]),
    mi: Number(m[5]),
    s: Number(m[6] ?? 0),
  };
  if (wall.h > 23 || wall.mi > 59 || wall.s > 59) fail();
  try {
    parseDate(formatDate(wall));
  } catch {
    return fail();
  }
  const date = { y: wall.y, m: wall.m, d: wall.d };
  const frac = m[7] ? Math.floor(Number(`0.${m[7]}`) * 1000) : 0;
  const tz = m[8];
  if (!tz) return { ms: zone.instant(wall) + frac, date };
  const asUtc = Date.UTC(wall.y, wall.m - 1, wall.d, wall.h, wall.mi, wall.s) + frac;
  if (tz.toUpperCase() === "Z") return { ms: asUtc, date };
  const digits = tz.replace(":", "");
  const offset = (digits[0] === "-" ? -1 : 1) * (Number(digits.slice(1, 3)) * 60 + Number(digits.slice(3, 5)));
  return { ms: asUtc - offset * MINUTE, date };
}

/** 'HH:MM' or 'HH:MM:SS' (24h) as minutes after midnight. */
export function parseClock(value: string): number {
  const m = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59 || Number(m[3] ?? 0) > 59) {
    throw new RangeError(`Invalid clock time ${JSON.stringify(value)}; expected 'HH:MM'.`);
  }
  return Number(m[1]) * 60 + Number(m[2]);
}

/** Wall time ``minutes`` after midnight on ``date``. */
export function at(date: CivilDate, minutes: number): Wall {
  return { ...date, h: Math.floor(minutes / 60), mi: minutes % 60, s: 0 };
}

/**
 * Half-open overlap of [aStart, aEnd) and [bStart, bEnd). A zero-length
 * interval counts when it lies inside the other, so instant events aren't lost.
 */
export function overlaps(aStart: number, aEnd: number, bStart: number, bEnd: number): boolean {
  if (aStart === aEnd) return bStart <= aStart && aStart < bEnd;
  return aStart < bEnd && bStart < aEnd;
}

/** ISO week number (1-53). */
export function isoWeek(date: CivilDate): number {
  const n = dayNumber(date);
  const thursday = n - weekday(date) + 3;
  const year = fromDayNumber(thursday).y;
  return Math.floor((thursday - dayNumber({ y: year, m: 1, d: 1 })) / 7) + 1;
}
