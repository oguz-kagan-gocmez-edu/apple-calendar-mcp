from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from apple_calendar_mcp.models import Event
from apple_calendar_mcp.scheduling import find_free_slots, merge_intervals, subtract

IST = ZoneInfo("Europe/Istanbul")


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def ev(start: datetime, end: datetime, **kw: object) -> Event:
    return Event(id="x|0", calendar_id="c", calendar_title="C", title="t", start=start, end=end, **kw)


def test_merge_overlapping_and_touching() -> None:
    merged = merge_intervals([(at(2, 11), at(2, 12)), (at(2, 9), at(2, 10)), (at(2, 10), at(2, 11, 30))])
    assert merged == [(at(2, 9), at(2, 12))]


def test_merge_drops_empty() -> None:
    assert merge_intervals([(at(2, 9), at(2, 9))]) == []


def test_subtract() -> None:
    free, _ = subtract((at(2, 9), at(2, 18)), [(at(2, 8), at(2, 10)), (at(2, 12), at(2, 13))])
    assert free == [(at(2, 10), at(2, 12)), (at(2, 13), at(2, 18))]


def _slots(events: list[Event], **kw: object) -> list[tuple[datetime, datetime]]:
    params: dict[str, object] = {
        "window_start": at(2, 0),
        "window_end": at(3, 0),
        "duration": timedelta(minutes=30),
        "tz": IST,
    }
    params.update(kw)
    slots, _ = find_free_slots(events, **params)  # type: ignore[arg-type]
    return [(s.start, s.end) for s in slots]


def test_empty_day_is_working_hours() -> None:
    assert _slots([]) == [(at(2, 9), at(2, 18))]


def test_busy_blocks_split_day() -> None:
    events = [ev(at(2, 10), at(2, 11)), ev(at(2, 10, 30), at(2, 12))]
    assert _slots(events) == [(at(2, 9), at(2, 10)), (at(2, 12), at(2, 18))]


def test_short_gaps_filtered() -> None:
    events = [ev(at(2, 9, 20), at(2, 17, 50))]
    assert _slots(events) == []


def test_free_canceled_and_all_day_ignored() -> None:
    events = [
        ev(at(2, 9), at(2, 18), availability="free"),
        ev(at(2, 9), at(2, 18), status="canceled"),
        ev(at(2, 0), at(3, 0), all_day=True),
    ]
    assert _slots(events) == [(at(2, 9), at(2, 18))]


def test_all_day_can_block() -> None:
    assert _slots([ev(at(2, 0), at(3, 0), all_day=True)], ignore_all_day=False) == []


def test_weekends_skipped_by_default() -> None:
    # 7-8 Nov 2026 is a weekend
    assert _slots([], window_start=at(7, 0), window_end=at(9, 0)) == []
    assert len(_slots([], window_start=at(7, 0), window_end=at(9, 0), include_weekends=True)) == 2


def test_window_clips_working_hours() -> None:
    assert _slots([], window_start=at(2, 15)) == [(at(2, 15), at(2, 18))]


def test_overnight_working_hours() -> None:
    got = _slots([], window_start=at(2, 0), window_end=at(3, 12), day_start=time(22), day_end=time(2))
    assert (at(2, 22), at(3, 2)) in got


def test_max_slots_truncates() -> None:
    slots, truncated = find_free_slots(
        [], window_start=at(2, 0), window_end=at(7, 0), duration=timedelta(minutes=30), tz=IST, max_slots=2
    )
    assert len(slots) == 2 and truncated


def test_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError):
        find_free_slots([], window_start=at(2, 0), window_end=at(3, 0), duration=timedelta(0), tz=IST)
