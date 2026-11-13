from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from apple_calendar_mcp.models import Event
from apple_calendar_mcp.scheduling import (
    busy_time,
    find_conflicts,
    find_duplicates,
    find_free_slots,
    merge_intervals,
    subtract,
)

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


# ------------------------------------------------------------------ declined, buffers, conflicts
def test_declined_invitations_do_not_block() -> None:
    events = [ev(at(2, 10), at(2, 12), my_status="declined"), ev(at(2, 13), at(2, 14), my_status="pending")]
    slots = _slots(events, duration=timedelta(hours=3))
    assert slots == [(at(2, 9), at(2, 13)), (at(2, 14), at(2, 18))]


def test_buffer_keeps_time_clear_around_events() -> None:
    slots = _slots([ev(at(2, 12), at(2, 13))], buffer=timedelta(minutes=15))
    assert slots == [(at(2, 9), at(2, 11, 45)), (at(2, 13, 15), at(2, 18))]


def test_buffer_ignores_zero_length_markers() -> None:
    slots = _slots([ev(at(2, 12), at(2, 12))], buffer=timedelta(minutes=30))
    assert slots == [(at(2, 9), at(2, 18))]


def named(title: str, start: datetime, end: datetime, **kw: object) -> Event:
    return Event(
        id=f"{title}|0", calendar_id="c", calendar_title="C", title=title, start=start, end=end, **kw
    )


def test_conflicts_are_overlapping_pairs() -> None:
    events = [
        named("a", at(2, 9), at(2, 11)),
        named("b", at(2, 10), at(2, 12)),
        named("c", at(2, 10, 30), at(2, 10, 45)),
        named("d", at(2, 12), at(2, 13)),  # touches b: no conflict
    ]
    found = [(a.title, b.title, s, e) for a, b, s, e in find_conflicts(events)]
    assert found == [
        ("a", "b", at(2, 10), at(2, 11)),
        ("a", "c", at(2, 10, 30), at(2, 10, 45)),
        ("b", "c", at(2, 10, 30), at(2, 10, 45)),
    ]


@pytest.mark.parametrize(
    "kw", [{"availability": "free"}, {"status": "canceled"}, {"my_status": "declined"}, {"all_day": True}]
)
def test_conflicts_skip_events_that_do_not_take_time(kw: dict[str, object]) -> None:
    events = [named("a", at(2, 9), at(2, 11)), named("b", at(2, 10), at(2, 12), **kw)]
    assert find_conflicts(events) == []


def test_busy_time_counts_overlaps_once_inside_the_window() -> None:
    events = [
        named("a", at(2, 8), at(2, 10)),  # half outside the 9:00 window start
        named("b", at(2, 9, 30), at(2, 10, 30)),
        named("free", at(2, 14), at(2, 15), availability="free"),
    ]
    assert busy_time(events, (at(2, 9), at(2, 18))) == timedelta(minutes=90)


def copy(uid: str, title: str, start: datetime, end: datetime, **kw: object) -> Event:
    stamp = int(start.timestamp())
    return Event(
        id=f"{uid}|{stamp}", calendar_id="c", calendar_title="C", title=title, start=start, end=end, **kw
    )


def test_duplicates_share_title_and_times() -> None:
    events = [
        copy("a", "Dentist", at(4, 15), at(4, 16)),
        copy("b", "  dentist ", at(4, 15), at(4, 16)),  # case and spacing ignored
        copy("c", "Dentist", at(4, 15), at(4, 17)),  # other end
        copy("d", "Dentist", at(5, 15), at(5, 16)),  # other day
        copy("e", "Dentists", at(4, 15), at(4, 16)),  # other title
        copy("f", "Dentist", at(4, 15), at(4, 16), status="canceled"),
        copy("g", "Dentist", at(4, 15), at(4, 16), all_day=True),
    ]
    assert [([e.id.split("|")[0] for e in copies], n) for copies, n in find_duplicates(events)] == [
        (["a", "b"], 1)
    ]


def test_occurrences_of_one_series_are_not_duplicates() -> None:
    events = [copy("a", "Sync", at(4, 15), at(4, 16)), copy("a", "Sync", at(4, 15), at(4, 16))]
    assert find_duplicates(events) == []


def test_duplicated_series_give_one_group() -> None:
    events = [
        copy(uid, "Lecture", at(day, 10), at(day, 12), is_recurring=True)
        for day in (3, 10, 17)
        for uid in ("a", "b")
    ]
    events.append(copy("c", "Lecture", at(10, 10), at(10, 12)))  # a one-off copy of one occurrence
    found = [([e.id for e in copies], n) for copies, n in find_duplicates(events)]
    first, second = int(at(3, 10).timestamp()), int(at(10, 10).timestamp())
    assert found == [
        ([f"a|{first}", f"b|{first}"], 2),  # 3 Nov and 17 Nov
        ([f"a|{second}", f"b|{second}", f"c|{second}"], 1),
    ]
