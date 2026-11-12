/**
 * The change log behind undo_change (port of changes.py).
 *
 * Each write is recorded as steps with the event before and after. Undo puts
 * things back only where it can do so exactly: created events are deleted,
 * edits to a one-off event or one occurrence get their old fields back, and a
 * deleted one-off event without attendees is created again. Before undoing,
 * the event must still look exactly as the change left it.
 *
 * A Worker request has no memory of the previous one, so the deployed log
 * lives in KV (``KvJournal``) and expires after a day.
 */

import type { Event, EventDraft, EventPatch, Span } from "./models";
import { type CalendarStore, InvalidRequestError, NotFoundError } from "./store";

export const MAX_CHANGES = 50;
export const CHANGE_TTL_SECONDS = 24 * 60 * 60;

const FIELDS = [
  "title",
  "start",
  "end",
  "all_day",
  "calendar_id",
  "location",
  "notes",
  "url",
  "alarms_minutes_before",
  "availability",
  "recurrence",
] as const;
const TEXT = new Set(["location", "notes", "url"]);

export const SERIES_EDIT = "Changes to several occurrences of a repeating event can't be undone automatically.";
export const SERIES_DELETE = "Deleted occurrences of a repeating event can't be restored automatically.";
export const INVITE_DELETE =
  "A deleted event with attendees can't be restored: the invitations can't be recreated.";

export interface Step {
  action: "create" | "update" | "delete";
  before: Event | null;
  after: Event | null;
  span: Span;
}

export interface ChangeRecord {
  id: string;
  at: number;
  summary: string;
  steps: Step[];
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

export function stepEvent(s: Step): Event {
  return (s.after ?? s.before)!;
}

/** Why this step can't be undone, or null if it can. */
export function stepBlocker(s: Step): string | null {
  if (s.action === "update") {
    if (s.before!.is_recurring && s.span !== "this") return SERIES_EDIT;
    if (!same(s.before!.recurrence, s.after!.recurrence)) return SERIES_EDIT;
  }
  if (s.action === "delete") {
    if (s.before!.is_recurring) return SERIES_DELETE;
    if (s.before!.attendees.length) return INVITE_DELETE;
  }
  return null;
}

export function recordBlocker(r: ChangeRecord): string | null {
  for (const s of r.steps) {
    const b = stepBlocker(s);
    if (b) return b;
  }
  return null;
}

export function summarize(steps: Step[]): string {
  if (steps.length === 1) {
    const verb = { create: "Created", update: "Changed", delete: "Deleted" }[steps[0]!.action];
    return `${verb} "${stepEvent(steps[0]!).title}"`;
  }
  return `${steps.length} changes in one batch`;
}

function newId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return `chg_${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function notFound(id: string): NotFoundError {
  return new NotFoundError(
    `No change with id '${id}'. It may be too old (changes are kept for a day), already undone; see list_changes.`,
  );
}

// ------------------------------------------------------------------ journals
export interface ChangeJournal {
  record(steps: Step[], at: number): Promise<ChangeRecord>;
  /** ``null``: the latest change. */
  get(id: string | null): Promise<ChangeRecord>;
  remove(id: string): Promise<void>;
  /** Newest first. */
  recent(): Promise<ChangeRecord[]>;
}

/** In memory; tests and the memory backend. */
export class MemoryJournal implements ChangeJournal {
  private readonly records = new Map<string, ChangeRecord>();

  record(steps: Step[], at: number): Promise<ChangeRecord> {
    const rec = { id: newId(), at, summary: summarize(steps), steps };
    this.records.set(rec.id, rec);
    while (this.records.size > MAX_CHANGES) this.records.delete(this.records.keys().next().value!);
    return Promise.resolve(rec);
  }

  get(id: string | null): Promise<ChangeRecord> {
    if (id === null) {
      const last = [...this.records.values()].pop();
      if (!last) return Promise.reject(new NotFoundError("There is no change to undo."));
      return Promise.resolve(last);
    }
    const rec = this.records.get(id);
    return rec ? Promise.resolve(rec) : Promise.reject(notFound(id));
  }

  remove(id: string): Promise<void> {
    this.records.delete(id);
    return Promise.resolve();
  }

  recent(): Promise<ChangeRecord[]> {
    return Promise.resolve([...this.records.values()].reverse());
  }
}

/** The subset of a KV namespace the journal uses. */
export interface KvLike {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, options?: { expirationTtl?: number }): Promise<void>;
  delete(key: string): Promise<void>;
}

const INDEX = "chg:index";

/** In Workers KV, so it survives between requests. Entries expire after a day. */
export class KvJournal implements ChangeJournal {
  constructor(
    private readonly kv: KvLike,
    private readonly ttl = CHANGE_TTL_SECONDS,
  ) {}

  private async index(): Promise<string[]> {
    const raw = await this.kv.get(INDEX);
    return raw ? (JSON.parse(raw) as string[]) : [];
  }

  async record(steps: Step[], at: number): Promise<ChangeRecord> {
    const rec = { id: newId(), at, summary: summarize(steps), steps };
    await this.kv.put(`chg:${rec.id}`, JSON.stringify(rec), { expirationTtl: this.ttl });
    const ids = [...(await this.index()), rec.id].slice(-MAX_CHANGES);
    await this.kv.put(INDEX, JSON.stringify(ids), { expirationTtl: this.ttl });
    return rec;
  }

  private async load(id: string): Promise<ChangeRecord | null> {
    const raw = await this.kv.get(`chg:${id}`);
    return raw ? (JSON.parse(raw) as ChangeRecord) : null;
  }

  async get(id: string | null): Promise<ChangeRecord> {
    if (id === null) {
      for (const candidate of (await this.index()).reverse()) {
        const rec = await this.load(candidate);
        if (rec) return rec;
      }
      throw new NotFoundError("There is no change to undo.");
    }
    if (!/^chg_[0-9a-f]{8}$/.test(id)) throw notFound(id);
    const rec = await this.load(id);
    if (!rec) throw notFound(id);
    return rec;
  }

  async remove(id: string): Promise<void> {
    await this.kv.delete(`chg:${id}`);
    const ids = (await this.index()).filter((x) => x !== id);
    await this.kv.put(INDEX, JSON.stringify(ids), { expirationTtl: this.ttl });
  }

  async recent(): Promise<ChangeRecord[]> {
    const out: ChangeRecord[] = [];
    for (const id of (await this.index()).reverse()) {
      const rec = await this.load(id);
      if (rec) out.push(rec);
    }
    return out;
  }
}

// ------------------------------------------------------------------ undo mechanics
/** A patch that turns ``after`` back into ``before``. */
export function inversePatch(before: Event, after: Event): EventPatch {
  const patch: Record<string, unknown> = {};
  for (const f of FIELDS) {
    if (f === "recurrence" || same(before[f], after[f])) continue;
    patch[f] = TEXT.has(f) && before[f] == null ? "" : before[f];
  }
  if ("start" in patch || "end" in patch) {
    patch.start = before.start;
    patch.end = before.end;
  }
  return patch as EventPatch;
}

export function draftOf(e: Event): EventDraft {
  return {
    title: e.title,
    start: e.start,
    end: e.end,
    all_day: e.all_day,
    calendar_id: e.calendar_id,
    location: e.location,
    notes: e.notes,
    url: e.url,
    alarms_minutes_before: e.alarms_minutes_before,
    recurrence: e.recurrence,
    availability: e.availability === "not_supported" ? null : e.availability,
  };
}

/** Refuse to undo over a later edit (or a deleted event). */
export async function checkUnchanged(store: CalendarStore, step: Step): Promise<void> {
  if (!step.after) return;
  const title = step.after.title;
  let now: Event;
  try {
    now = await store.getEvent(step.after.id);
  } catch (err) {
    if (err instanceof NotFoundError) {
      throw new InvalidRequestError(`"${title}" no longer exists, so this change can't be undone.`);
    }
    throw err;
  }
  if (FIELDS.some((f) => !same(now[f], step.after![f]))) {
    throw new InvalidRequestError(
      `"${title}" was changed again after this change; undoing would overwrite that. Edit it directly instead.`,
    );
  }
}

/** Undo one step. Returns [restored event, removed event id]. */
export async function revert(store: CalendarStore, step: Step): Promise<[Event | null, string | null]> {
  if (step.action === "create") {
    await store.deleteEvent(step.after!.id, step.after!.is_recurring ? "all" : "this");
    return [null, step.after!.id];
  }
  if (step.action === "update") {
    const patch = inversePatch(step.before!, step.after!);
    if (!Object.keys(patch).length) return [step.after, null];
    return [await store.updateEvent(step.after!.id, patch, "this"), null];
  }
  return [await store.createEvent(draftOf(step.before!)), null];
}
