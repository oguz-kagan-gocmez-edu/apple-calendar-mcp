"""Free-slot and conflict search: pure interval arithmetic, no I/O.

Free slots (n events, d days):
  1. Keep events that actually block time (not 'free', not canceled, not
     declined, optionally not all-day), pad them with the buffer and sort
     them by start.                                              O(n log n)
  2. Merge overlapping busy intervals into disjoint ones.        O(n)
  3. For each day, intersect the working-hours window with the
     search window and subtract the busy intervals with a
     two-pointer sweep; keep gaps >= the requested duration.     O(n + d)

Conflicts: sort by start and sweep, keeping the events still running;
each new event overlaps exactly those.                  O(n log n + k)

Duplicates: bucket by (title, start, end, all-day); a bucket holding
more than one series is a set of copies.                        O(n log n)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta, tzinfo

from .models import Event, FreeSlot
from .store import split_event_id
from .timeutil import ONE_DAY

Interval = tuple[datetime, datetime]


def blocks_time(event: Event, *, ignore_all_day: bool) -> bool:
    """Whether the event takes the time: not 'free', not canceled, not declined by you."""
    if event.status == "canceled" or event.availability == "free" or event.my_status == "declined":
        return False
    return not (ignore_all_day and event.all_day)


def find_conflicts(events: Iterable[Event]) -> list[tuple[Event, Event, datetime, datetime]]:
    """Pairs of timed events that both block time and overlap: ``(a, b, start, end)``.

    ``a`` starts first (ties broken by end, then title). Touching events
    (one ends when the next starts) don't conflict.
    """
    timed = sorted(
        (e for e in events if blocks_time(e, ignore_all_day=True) and e.end > e.start),
        key=lambda e: (e.start, e.end, e.title),
    )
    active: list[Event] = []
    found: list[tuple[Event, Event, datetime, datetime]] = []
    for e in timed:
        active = [a for a in active if a.end > e.start]
        for a in active:
            found.append((a, e, e.start, min(a.end, e.end)))
        active.append(e)
    return found


def duplicate_key(
    title: str, start: datetime, end: datetime, all_day: bool
) -> tuple[str, datetime, datetime, bool]:
    """What two events must share to count as copies (title: case and spacing ignored)."""
    return " ".join(title.casefold().split()), start, end, all_day


def find_duplicates(events: Iterable[Event]) -> list[tuple[list[Event], int]]:
    """Events that look like copies of each other: ``(copies, repeats)`` per group.

    Copies share the title (case and spacing ignored), start, end and all-day
    flag, and belong to different series; occurrences of one series are never
    copies of each other. Canceled events are skipped. When two repeating
    series are copies, they give one group (their first shared occurrence)
    and ``repeats`` counts the shared occurrences.
    """
    buckets: dict[tuple[str, datetime, datetime, bool], dict[str, Event]] = {}
    for e in sorted(events, key=lambda e: (e.start, e.end, e.calendar_title, e.id)):
        if e.status == "canceled":
            continue
        key = duplicate_key(e.title, e.start, e.end, e.all_day)
        buckets.setdefault(key, {}).setdefault(split_event_id(e.id)[0], e)

    groups: list[tuple[list[Event], int]] = []
    series_group: dict[frozenset[str], int] = {}
    for by_series in buckets.values():  # insertion order: by start
        if len(by_series) < 2:
            continue
        copies = list(by_series.values())
        if all(e.is_recurring for e in copies):
            seen = series_group.setdefault(frozenset(by_series), len(groups))
            if seen < len(groups):
                groups[seen] = (groups[seen][0], groups[seen][1] + 1)
                continue
        groups.append((copies, 1))
    return groups


def busy_time(events: Iterable[Event], window: Interval) -> timedelta:
    """Time inside ``window`` covered by blocking timed events, overlaps counted once."""
    w_start, w_end = window
    busy = merge_intervals(
        (max(e.start, w_start), min(e.end, w_end)) for e in events if blocks_time(e, ignore_all_day=True)
    )
    return sum((end - start for start, end in busy), timedelta(0))


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping or touching intervals. Input order does not matter."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract(window: Interval, busy: list[Interval], first: int = 0) -> tuple[list[Interval], int]:
    """Return free sub-intervals of ``window`` not covered by sorted, disjoint ``busy``.

    ``first`` is the index to start scanning ``busy`` from. The returned index
    lets the caller resume on the next (later) window without rescanning.
    """
    w_start, w_end = window
    i = first
    while i < len(busy) and busy[i][1] <= w_start:
        i += 1
    resume = i

    free: list[Interval] = []
    cursor = w_start
    while i < len(busy) and busy[i][0] < w_end:
        b_start, b_end = busy[i]
        if b_start > cursor:
            free.append((cursor, b_start))
        cursor = max(cursor, b_end)
        i += 1
    if cursor < w_end:
        free.append((cursor, w_end))
    return free, resume


def find_free_slots(
    events: Iterable[Event],
    *,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    tz: tzinfo,
    day_start: time = time(9, 0),
    day_end: time = time(18, 0),
    include_weekends: bool = False,
    ignore_all_day: bool = True,
    max_slots: int = 20,
    buffer: timedelta = timedelta(0),
) -> tuple[list[FreeSlot], bool]:
    """Find gaps of at least ``duration`` inside daily working hours.

    Returns ``(slots, truncated)``. If ``day_end <= day_start`` the working
    window wraps past midnight (e.g. 22:00-02:00 for night owls). ``buffer``
    keeps that much time clear before and after every busy event.
    """
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    if buffer < timedelta(0):
        raise ValueError("buffer must not be negative")
    if window_end <= window_start:
        return [], False

    # Zero-length markers (e.g. a 23:59 deadline) take no time, buffer or not.
    busy = merge_intervals(
        (e.start - buffer, e.end + buffer)
        for e in events
        if e.end > e.start and blocks_time(e, ignore_all_day=ignore_all_day)
    )

    slots: list[FreeSlot] = []
    cursor_idx = 0
    # Start one day early so a wrap-around window from "yesterday" is included.
    day = window_start.astimezone(tz).date() - ONE_DAY
    last_day = window_end.astimezone(tz).date()
    while day <= last_day:
        if include_weekends or day.weekday() < 5:
            ws = datetime.combine(day, day_start, tzinfo=tz)
            we = datetime.combine(day, day_end, tzinfo=tz)
            if we <= ws:
                we = datetime.combine(day + ONE_DAY, day_end, tzinfo=tz)
            ws, we = max(ws, window_start), min(we, window_end)
            if ws < we:
                gaps, cursor_idx = subtract((ws, we), busy, cursor_idx)
                for g_start, g_end in gaps:
                    if g_end - g_start >= duration:
                        if len(slots) >= max_slots:
                            return slots, True
                        slots.append(
                            FreeSlot(
                                start=g_start,
                                end=g_end,
                                duration_minutes=int((g_end - g_start).total_seconds() // 60),
                            )
                        )
        day += ONE_DAY
    return slots, False
