"""Expansion of the :class:`Recurrence` subset into concrete start times.

EventKit expands recurring events itself; this module is used by
``MemoryStore`` so that tests and the demo backend behave like the real one.
Semantics follow RFC 5545: invalid dates (e.g. 31 Feb) are skipped and do not
count toward COUNT; wall-clock time is preserved across DST changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta

from .models import WEEKDAYS, Recurrence

MAX_OCCURRENCES = 10_000  # hard stop against runaway rules


def _at(day: date, template: datetime) -> datetime:
    return datetime.combine(day, template.timetz())


def _add_months(d: date, months: int) -> date | None:
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    try:
        return d.replace(year=year, month=month)
    except ValueError:  # e.g. 31 April
        return None


def _candidates(first: datetime, rule: Recurrence) -> Iterator[datetime]:
    """Unbounded, ordered candidate starts (before COUNT/UNTIL)."""
    step = rule.interval
    if rule.frequency == "daily":
        for k in range(MAX_OCCURRENCES):
            yield _at(first.date() + timedelta(days=k * step), first)
    elif rule.frequency == "weekly":
        if not rule.days_of_week:
            for k in range(MAX_OCCURRENCES):
                yield _at(first.date() + timedelta(weeks=k * step), first)
            return
        offsets = sorted(WEEKDAYS.index(d) for d in rule.days_of_week)
        monday = first.date() - timedelta(days=first.weekday())
        for k in range(MAX_OCCURRENCES):
            week = monday + timedelta(weeks=k * step)
            for off in offsets:
                candidate = _at(week + timedelta(days=off), first)
                if candidate >= first:
                    yield candidate
    else:
        months = step if rule.frequency == "monthly" else 12 * step
        for k in range(MAX_OCCURRENCES):
            day = _add_months(first.date(), k * months)
            if day is not None:
                yield _at(day, first)


def occurrence_starts(first: datetime, rule: Recurrence | None) -> Iterator[datetime]:
    """Yield occurrence start times in ascending order, honouring COUNT/UNTIL.

    ``first`` is always the first occurrence. The iterator is lazy; callers
    stop consuming once they pass the end of their query window.
    """
    if rule is None:
        yield first
        return
    for produced, start in enumerate(_candidates(first, rule), start=1):
        if rule.until is not None and start.date() > rule.until:
            return
        yield start
        if rule.count is not None and produced >= rule.count:
            return
        if produced >= MAX_OCCURRENCES:
            return


def count_before(first: datetime, rule: Recurrence, boundary: datetime) -> int:
    """Number of occurrences that start strictly before ``boundary``."""
    n = 0
    for start in occurrence_starts(first, rule):
        if start >= boundary:
            break
        n += 1
    return n
