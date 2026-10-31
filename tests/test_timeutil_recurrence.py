from datetime import date, datetime, time, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

import pytest

from apple_calendar_mcp.models import Recurrence
from apple_calendar_mcp.recurrence import count_before, occurrence_starts
from apple_calendar_mcp.timeutil import overlaps, parse_clock, parse_datetime, resolve_local_tz

IST = ZoneInfo("Europe/Istanbul")
NY = ZoneInfo("America/New_York")


class TestParse:
    def test_date_only_is_local_midnight(self) -> None:
        assert parse_datetime("2026-11-03", IST) == datetime(2026, 11, 3, tzinfo=IST)

    def test_naive_gets_local_zone(self) -> None:
        dt = parse_datetime("2026-11-03T14:30", IST)
        assert dt.utcoffset() == timedelta(hours=3)

    def test_space_separator_and_z(self) -> None:
        assert parse_datetime("2026-11-03 14:30", IST).hour == 14
        assert parse_datetime("2026-11-03T11:30:00Z", IST) == datetime(2026, 11, 3, 14, 30, tzinfo=IST)

    def test_explicit_offset_kept(self) -> None:
        dt = parse_datetime("2026-11-03T09:00:00-05:00", IST)
        assert dt.utcoffset() == timedelta(hours=-5)

    @pytest.mark.parametrize("bad", ["", "tomorrow", "2026-13-01", "03/11/2026"])
    def test_rejects_garbage(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_datetime(bad, IST)

    def test_clock(self) -> None:
        assert parse_clock("09:30") == time(9, 30)
        with pytest.raises(ValueError):
            parse_clock("9.30am")

    def test_tz_override(self) -> None:
        assert resolve_local_tz("Asia/Tokyo") == ZoneInfo("Asia/Tokyo")


class TestOverlaps:
    a = datetime(2026, 1, 1, 10, tzinfo=IST)

    def test_touching_intervals_do_not_overlap(self) -> None:
        b = self.a + timedelta(hours=1)
        assert not overlaps(self.a, b, b, b + timedelta(hours=1))

    def test_zero_length_inside(self) -> None:
        assert overlaps(self.a, self.a, self.a - timedelta(hours=1), self.a + timedelta(hours=1))
        assert not overlaps(self.a, self.a, self.a + timedelta(minutes=1), self.a + timedelta(hours=1))


def _take(first: datetime, rule: Recurrence, n: int = 50) -> list[datetime]:
    return list(islice(occurrence_starts(first, rule), n))


class TestRecurrence:
    first = datetime(2026, 11, 2, 10, 40, tzinfo=IST)  # Monday

    def test_daily_interval_count(self) -> None:
        got = _take(self.first, Recurrence(frequency="daily", interval=2, count=3))
        assert [d.day for d in got] == [2, 4, 6]

    def test_weekly_by_day(self) -> None:
        got = _take(self.first, Recurrence(frequency="weekly", days_of_week=["wed", "mon"], count=4))
        assert [d.strftime("%a %d") for d in got] == ["Mon 02", "Wed 04", "Mon 09", "Wed 11"]

    def test_weekly_by_day_skips_days_before_first(self) -> None:
        wed = datetime(2026, 11, 4, 9, tzinfo=IST)
        got = _take(wed, Recurrence(frequency="weekly", days_of_week=["mon", "wed"], count=3))
        assert [d.day for d in got] == [4, 9, 11]

    def test_biweekly(self) -> None:
        got = _take(self.first, Recurrence(frequency="weekly", interval=2, count=3))
        assert [d.day for d in got] == [2, 16, 30]

    def test_until_inclusive(self) -> None:
        got = _take(self.first, Recurrence(frequency="daily", until=date(2026, 11, 4)))
        assert [d.day for d in got] == [2, 3, 4]

    def test_monthly_skips_invalid_dates(self) -> None:
        jan31 = datetime(2026, 1, 31, 9, tzinfo=IST)
        got = _take(jan31, Recurrence(frequency="monthly", count=4))
        assert [(d.month, d.day) for d in got] == [(1, 31), (3, 31), (5, 31), (7, 31)]

    def test_yearly_leap_day(self) -> None:
        leap = datetime(2028, 2, 29, 9, tzinfo=IST)
        got = _take(leap, Recurrence(frequency="yearly", count=2))
        assert [d.year for d in got] == [2028, 2032]

    def test_wall_clock_preserved_across_dst(self) -> None:
        # US DST ends 1 Nov 2026: offset changes, local time must not.
        first = datetime(2026, 10, 30, 9, tzinfo=NY)
        got = _take(first, Recurrence(frequency="daily", count=4))
        assert {d.hour for d in got} == {9}
        assert got[0].utcoffset() != got[-1].utcoffset()

    def test_count_before(self) -> None:
        rule = Recurrence(frequency="daily", count=10)
        assert count_before(self.first, rule, self.first + timedelta(days=3)) == 3

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            Recurrence(frequency="daily", count=3, until=date(2026, 12, 1))
        with pytest.raises(ValueError):
            Recurrence(frequency="daily", days_of_week=["mon"])
        assert Recurrence(frequency="weekly", days_of_week=["fri", "mon", "fri"]).days_of_week == [
            "mon",
            "fri",
        ]
