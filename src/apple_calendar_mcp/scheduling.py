"""Free-slot search: pure interval arithmetic, no I/O.

Algorithm (n events, d days):
  1. Keep events that actually block time (not 'free', not canceled,
     optionally not all-day) and sort them by start.          O(n log n)
  2. Merge overlapping busy intervals into disjoint ones.        O(n)
  3. For each day, intersect the working-hours window with the
     search window and subtract the busy intervals with a
     two-pointer sweep; keep gaps >= the requested duration.     O(n + d)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta, tzinfo

from .models import Event, FreeSlot
from .timeutil import ONE_DAY

Interval = tuple[datetime, datetime]


def blocks_time(event: Event, *, ignore_all_day: bool) -> bool:
    if event.status == "canceled" or event.availability == "free":
        return False
    return not (ignore_all_day and event.all_day)


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
) -> tuple[list[FreeSlot], bool]:
    """Find gaps of at least ``duration`` inside daily working hours.

    Returns ``(slots, truncated)``. If ``day_end <= day_start`` the working
    window wraps past midnight (e.g. 22:00-02:00 for night owls).
    """
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    if window_end <= window_start:
        return [], False

    busy = merge_intervals((e.start, e.end) for e in events if blocks_time(e, ignore_all_day=ignore_all_day))

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
