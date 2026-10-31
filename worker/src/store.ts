/** The storage interface the service talks to, plus user-safe errors. */

import type { Calendar, Event, EventDraft, EventPatch, Span } from "./models";

/** Base class for errors whose message is safe to show to Claude and the user. */
export class CalendarError extends Error {
  override name = "CalendarError";
}
export class NotFoundError extends CalendarError {
  override name = "NotFoundError";
}
export class PermissionDeniedError extends CalendarError {
  override name = "PermissionDeniedError";
}
export class ReadOnlyCalendarError extends CalendarError {
  override name = "ReadOnlyCalendarError";
}
export class InvalidRequestError extends CalendarError {
  override name = "InvalidRequestError";
}
export class BackendError extends CalendarError {
  override name = "BackendError";
}

export interface CalendarStore {
  listCalendars(): Promise<Calendar[]>;
  /** Occurrences overlapping [start, end), recurring events expanded. */
  listEvents(start: number, end: number, calendarIds?: string[] | null): Promise<Event[]>;
  getEvent(eventId: string): Promise<Event>;
  createEvent(draft: EventDraft): Promise<Event>;
  /** Apply ``patch``; return the updated targeted occurrence. */
  updateEvent(eventId: string, patch: EventPatch, span: Span): Promise<Event>;
  /** Delete and return the event as it was before. */
  deleteEvent(eventId: string, span: Span): Promise<Event>;
}

/** '<series id>|<original start, unix seconds>' */
export function makeEventId(seriesId: string, occurrenceMs: number): string {
  return `${seriesId}|${Math.trunc(occurrenceMs / 1000)}`;
}

/** Inverse of ``makeEventId``. Plain series ids are accepted too (second part null). */
export function splitEventId(eventId: string): [string, number | null] {
  const at = eventId.lastIndexOf("|");
  if (at < 0) return [eventId, null];
  const ts = eventId.slice(at + 1);
  if (!/^-?\d+$/.test(ts)) return [eventId, null];
  return [eventId.slice(0, at), Number(ts) * 1000];
}
