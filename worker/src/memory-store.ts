/**
 * In-memory store for tests and demos (BACKEND=memory). One-off events only;
 * recurrence is the CalDAV store's job.
 */

import type { Calendar, Event, EventDraft, EventPatch, Span } from "./models";
import { type CalendarStore, InvalidRequestError, NotFoundError, makeEventId, splitEventId } from "./store";
import { overlaps } from "./time";

const CALENDARS: Calendar[] = [
  { id: "home", title: "Home", source: "Memory", color: null, writable: true, is_default: true },
  { id: "work", title: "Work", source: "Memory", color: null, writable: true, is_default: false },
];

export class MemoryStore implements CalendarStore {
  private readonly events = new Map<string, Event>();

  listCalendars(): Promise<Calendar[]> {
    return Promise.resolve(CALENDARS.map((c) => ({ ...c })));
  }

  listEvents(start: number, end: number, calendarIds?: string[] | null): Promise<Event[]> {
    return Promise.resolve(
      [...this.events.values()].filter(
        (e) => (!calendarIds || calendarIds.includes(e.calendar_id)) && overlaps(e.start, e.end, start, end),
      ),
    );
  }

  private find(eventId: string): Event {
    const [series] = splitEventId(eventId);
    const found = this.events.get(series);
    if (!found) throw new NotFoundError(`No event with id '${eventId}'.`);
    return found;
  }

  getEvent(eventId: string): Promise<Event> {
    return Promise.resolve({ ...this.find(eventId) });
  }

  private calendar(id: string | null | undefined): Calendar {
    const cal = id ? CALENDARS.find((c) => c.id === id) : CALENDARS.find((c) => c.is_default);
    if (!cal) throw new NotFoundError(`No calendar with id '${id}'. Call list_calendars.`);
    return cal;
  }

  createEvent(d: EventDraft): Promise<Event> {
    if (d.recurrence) throw new InvalidRequestError("The memory backend doesn't support recurring events.");
    const cal = this.calendar(d.calendar_id);
    const series = crypto.randomUUID();
    const event: Event = {
      id: makeEventId(series, d.start),
      calendar_id: cal.id,
      calendar_title: cal.title,
      title: d.title,
      start: d.start,
      end: d.end,
      all_day: d.all_day,
      location: d.location,
      notes: d.notes,
      url: d.url,
      is_recurring: false,
      recurrence: null,
      alarms_minutes_before: d.alarms_minutes_before,
      availability: d.availability ?? "busy",
      status: "none",
      organizer: null,
      attendees: [],
    };
    this.events.set(series, event);
    return Promise.resolve({ ...event });
  }

  updateEvent(eventId: string, p: EventPatch, _span: Span): Promise<Event> {
    if (p.recurrence) throw new InvalidRequestError("The memory backend doesn't support recurring events.");
    const e = this.find(eventId);
    const text = (v: string | null | undefined, old: string | null) => (v == null ? old : v || null);
    if (p.calendar_id) {
      const cal = this.calendar(p.calendar_id);
      e.calendar_id = cal.id;
      e.calendar_title = cal.title;
    }
    e.title = p.title ?? e.title;
    e.start = p.start ?? e.start;
    e.end = p.end ?? e.end;
    e.all_day = p.all_day ?? e.all_day;
    e.location = text(p.location, e.location);
    e.notes = text(p.notes, e.notes);
    e.url = text(p.url, e.url);
    e.alarms_minutes_before = p.alarms_minutes_before ?? e.alarms_minutes_before;
    e.availability = p.availability ?? e.availability;
    return Promise.resolve({ ...e });
  }

  deleteEvent(eventId: string, _span: Span): Promise<Event> {
    const e = this.find(eventId);
    this.events.delete(splitEventId(eventId)[0]);
    return Promise.resolve(e);
  }
}
