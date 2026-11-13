"""iCalendar export and import: the parser, the service and the MCP tools."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from mcp.client.client import Client

from apple_calendar_mcp.ics import IcsEvent, parse_ics, windows_zones
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.models import Recurrence
from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import MAX_IMPORT_EVENTS, CalendarService
from apple_calendar_mcp.store import CalendarError, InvalidRequestError

from .conftest import IST, NOW


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def cal(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{e}END:VEVENT\r\n" for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}END:VCALENDAR\r\n"


def one(event: str) -> IcsEvent:
    events, skipped = parse_ics(cal(event), IST)
    assert skipped == [], skipped
    (parsed,) = events
    return parsed


def why(event: str) -> str:
    events, skipped = parse_ics(cal(event), IST)
    assert events == []
    return skipped[0].reason


# ------------------------------------------------------------------ parser
def zone_def(tzid: str, body: str) -> str:
    return f"BEGIN:VTIMEZONE\r\nTZID:{tzid}\r\n{body}END:VTIMEZONE\r\n"


def with_zones(zones: str, event: str) -> IcsEvent:
    text = cal(event).replace("PRODID:-//test//EN\r\n", f"PRODID:-//test//EN\r\n{zones}")
    events, skipped = parse_ics(text, IST)
    assert skipped == [], skipped
    return events[0]


class TestOutlook:
    """Files from Outlook and Teams: Windows zone names and meeting links."""

    def test_windows_zone_names(self) -> None:
        e = one("SUMMARY:a\r\nDTSTART;TZID=W. Europe Standard Time:20261103T090000\r\n")
        assert e.start == at(3, 11)  # Berlin is UTC+1 in November, Istanbul UTC+3
        quoted = one('SUMMARY:a\r\nDTSTART;TZID="Turkey Standard Time":20261103T090000\r\n')
        assert quoted.start == at(3, 9)
        assert one("SUMMARY:a\r\nDTSTART;TZID=pacific standard time:20261103T090000\r\n").start == at(3, 20)

    def test_the_zone_table_is_valid_and_shared_with_the_worker(self) -> None:
        table = windows_zones()
        assert len(table) > 130 and table["Turkey Standard Time"] == "Europe/Istanbul"
        for iana in table.values():
            ZoneInfo(iana)
        worker = Path(__file__).resolve().parent.parent / "worker" / "src" / "windows-zones.json"
        assert json.loads(worker.read_text("utf-8")) == table

    def test_zones_the_file_defines(self) -> None:
        lic = zone_def("Custom 1", "X-LIC-LOCATION:Europe/Berlin\r\n")
        e = with_zones(lic, "SUMMARY:a\r\nDTSTART;TZID=Custom 1:20261103T090000\r\n")
        assert e.start == at(3, 11)
        fixed = zone_def(
            "(UTC+03:00) Istanbul",
            "BEGIN:STANDARD\r\nDTSTART:16010101T000000\r\nTZOFFSETFROM:+0300\r\nTZOFFSETTO:+0300\r\nEND:STANDARD\r\n",
        )
        e = with_zones(fixed, 'SUMMARY:a\r\nDTSTART;TZID="(UTC+03:00) Istanbul":20261103T090000\r\n')
        assert e.start == at(3, 9)

    def test_a_defined_zone_with_daylight_time_is_still_skipped(self) -> None:
        dst = zone_def(
            "Somewhere",
            "BEGIN:STANDARD\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\n"
            "BEGIN:DAYLIGHT\r\nTZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\n",
        )
        text = cal("SUMMARY:a\r\nDTSTART;TZID=Somewhere:20261103T090000\r\n").replace(
            "PRODID:-//test//EN\r\n", f"PRODID:-//test//EN\r\n{dst}"
        )
        events, skipped = parse_ics(text, IST)
        assert events == [] and "isn't one this server knows" in skipped[0].reason

    def test_meeting_links_become_the_url(self) -> None:
        teams = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d"
        e = one(
            "SUMMARY:Sync\r\nDTSTART:20261103T060000Z\r\n"
            f"X-MICROSOFT-SKYPETEAMSMEETINGURL:{teams}\r\n"
            "DESCRIPTION:Join here\\n________________\r\n"
        )
        assert e.url == teams
        zoom = one(
            "SUMMARY:Call\r\nDTSTART:20261103T060000Z\r\n"
            "DESCRIPTION:Join Zoom Meeting\\n<https://us02web.zoom.us/j/81234567890?pwd=abc>.\\nID: 812\r\n"
        )
        assert zoom.url == "https://us02web.zoom.us/j/81234567890?pwd=abc"
        assert one(
            "SUMMARY:x\r\nDTSTART:20261103T060000Z\r\nLOCATION:https://meet.google.com/abc-defg-hij\r\n"
        ).url == ("https://meet.google.com/abc-defg-hij")

    def test_an_existing_url_is_kept_and_the_link_goes_in_the_notes(self) -> None:
        e = one(
            "SUMMARY:x\r\nDTSTART:20261103T060000Z\r\nURL:https://example.com/agenda\r\n"
            "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij\r\nDESCRIPTION:Agenda attached\r\n"
        )
        assert e.url == "https://example.com/agenda"
        assert e.notes == "Join: https://meet.google.com/abc-defg-hij\n\nAgenda attached"

    def test_ordinary_links_are_not_meeting_links(self) -> None:
        e = one("SUMMARY:x\r\nDTSTART:20261103T060000Z\r\nDESCRIPTION:See https://zoom.us/pricing\r\n")
        assert e.url is None


class TestParse:
    def test_time_forms(self) -> None:
        assert one("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T070000Z\r\n").start == at(3, 9)
        zoned = one("SUMMARY:a\r\nDTSTART;TZID=Europe/Berlin:20261103T090000\r\nDURATION:PT90M\r\n")
        assert (zoned.start, zoned.end) == (at(3, 11), at(3, 12, 30))
        floating = one("SUMMARY:a\r\nDTSTART:20261103T090000\r\n")
        assert (floating.start, floating.end, floating.all_day) == (at(3, 9), at(3, 9), False)

    def test_all_day(self) -> None:
        e = one("SUMMARY:Trip\r\nDTSTART;VALUE=DATE:20261106\r\nDTEND;VALUE=DATE:20261109\r\n")
        assert (e.start, e.end, e.all_day) == (at(6, 0), at(9, 0), True)
        single = one("SUMMARY:Day\r\nDTSTART;VALUE=DATE:20261106\r\n")
        assert single.end == at(7, 0)

    def test_text_folding_and_escapes(self) -> None:
        e = one(
            "SUMMARY:Lunch\\, then a very long title that goes on\r\n  and is folded\r\n"
            "DTSTART:20261103T060000Z\r\n"
            "DESCRIPTION:Line one\\nLine two\\; done\r\n"
            'LOCATION;ALTREP="cid:x;y":Room 1\r\n'
            "URL:https://example.com/a\r\n"
            "TRANSP:TRANSPARENT\r\n"
        )
        assert e.title == "Lunch, then a very long title that goes on and is folded"
        assert e.notes == "Line one\nLine two; done"
        assert (e.location, e.url, e.availability) == ("Room 1", "https://example.com/a", "free")

    def test_alarms(self) -> None:
        e = one(
            "SUMMARY:a\r\nDTSTART:20261103T060000Z\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-P1D\r\nEND:VALARM\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;RELATED=END:-PT5M\r\nEND:VALARM\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;VALUE=DATE-TIME:20261103T050000Z\r\nEND:VALARM\r\n"
        )
        assert e.alarms == [1440, 15]

    @pytest.mark.parametrize(
        ("rrule", "expected"),
        [
            ("FREQ=DAILY;COUNT=5", Recurrence(frequency="daily", count=5)),
            (
                "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;WKST=MO",
                Recurrence(frequency="weekly", interval=2, days_of_week=["tue", "thu"]),
            ),
            (
                "FREQ=WEEKLY;UNTIL=20261130T205959Z",
                Recurrence(frequency="weekly", until=date(2026, 11, 30)),
            ),
            ("FREQ=MONTHLY;BYMONTHDAY=3", Recurrence(frequency="monthly")),
            ("FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=3", Recurrence(frequency="yearly")),
        ],
    )
    def test_repeat_rules(self, rrule: str, expected: Recurrence) -> None:
        e = one(f"SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T070000Z\r\nRRULE:{rrule}\r\n")
        assert e.recurrence == expected

    def test_removed_occurrences(self) -> None:
        e = one(
            "SUMMARY:a\r\nDTSTART;TZID=Europe/Istanbul:20261103T090000\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\n"
            "EXDATE;TZID=Europe/Istanbul:20261110T090000,20261124T090000\r\n"
        )
        assert e.exdates == [at(10, 9), at(24, 9)]

    @pytest.mark.parametrize(
        ("event", "reason"),
        [
            ("SUMMARY:a\r\n", "no start time"),
            ("SUMMARY:a\r\nDTSTART:tomorrow\r\n", "can't be read"),
            ("SUMMARY:a\r\nDTSTART;TZID=Middle Earth Time:20261103T090000\r\n", "time zone"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nDTEND:20261103T050000Z\r\n", "ends before"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nSTATUS:CANCELLED\r\n", "canceled"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRECURRENCE-ID:20261103T060000Z\r\n", "changed"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYDAY=2TU\r\n", "second Tuesday"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYMONTHDAY=15\r\n", "BYMONTHDAY"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=HOURLY\r\n", "hourly"),
            ("SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRDATE:20261104T060000Z\r\n", "extra dates"),
            (
                "SUMMARY:a\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=DAILY\r\nEXDATE:20261103T060000Z\r\n",
                "first occurrence",
            ),
        ],
    )
    def test_skipped_with_a_reason(self, event: str, reason: str) -> None:
        assert reason in why(event)

    def test_other_components_and_bad_lines_are_ignored(self) -> None:
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VTIMEZONE\r\nTZID:X\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nEND:STANDARD\r\n"
            "END:VTIMEZONE\r\n"
            "BEGIN:VTODO\r\nSUMMARY:todo\r\nEND:VTODO\r\n"
            "this line is broken\r\n"
            "BEGIN:VEVENT\r\nSUMMARY:kept\r\nDTSTART:20261103T060000Z\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        events, skipped = parse_ics(text, IST)
        assert [e.title for e in events] == ["kept"] and skipped == []

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "hello",
            "BEGIN:VEVENT\r\nEND:VEVENT\r\n",
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VCALENDAR\r\n",
        ],
    )
    def test_not_icalendar(self, text: str) -> None:
        with pytest.raises(ValueError, match="iCalendar"):
            parse_ics(text, IST)


# ------------------------------------------------------------------ export
def test_export_format(service: CalendarService) -> None:
    service.create_event(
        "Café; notes, etc.",
        "2026-11-03T14:00",
        duration_minutes=45,
        location="Lab 3",
        notes="Bring\nlaptop " + "x" * 80,
        url="https://example.com",
        alarms_minutes_before=[15],
        availability="free",
    )
    service.create_event("Trip", "2026-11-06", end="2026-11-08", all_day=True)
    r = service.export_ics("2026-11-01", "2026-11-30")
    assert (r.count, r.truncated) == (2, False)
    assert r.ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and r.ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in r.ics.split("\r\n"))
    unfolded = r.ics.replace("\r\n ", "")
    assert "SUMMARY:Café\\; notes\\, etc.\r\n" in unfolded
    assert "DTSTART:20261103T110000Z\r\nDTEND:20261103T114500Z\r\n" in unfolded
    assert "DESCRIPTION:Bring\\nlaptop xxx" in unfolded
    assert "TRANSP:TRANSPARENT\r\n" in unfolded and "TRIGGER:-PT15M\r\n" in unfolded
    assert "DTSTART;VALUE=DATE:20261106\r\nDTEND;VALUE=DATE:20261109\r\n" in unfolded


def test_export_flattens_series_and_round_trips(service: CalendarService, tz: ZoneInfo) -> None:
    service.create_event("Lecture", "2026-11-03T10:00", recurrence=Recurrence(frequency="weekly", count=3))
    ids = [e.id for e in service.list_events("2026-11-01", "2026-11-30").events]
    service.update_event(ids[1], title="Lecture (moved)", start="2026-11-11T10:00")
    service.delete_event(ids[2])
    r = service.export_ics("2026-11-01", "2026-11-30")
    assert r.count == 2 and "RRULE" not in r.ics

    other = CalendarService(MemoryStore(tz), tz, clock=lambda: NOW)
    got = other.import_ics(r.ics)
    assert got.skipped == []
    assert [(e.title, e.start) for e in other.list_events("2026-11-01", "2026-11-30").events] == [
        ("Lecture", at(3, 10)),
        ("Lecture (moved)", at(11, 10)),
    ]


def test_export_leaves_out_canceled_and_respects_the_cap(
    service: CalendarService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for hour in (9, 10, 11):
        service.create_event(f"E{hour}", f"2026-11-03T{hour:02}:00")
    monkeypatch.setattr("apple_calendar_mcp.service.MAX_EXPORT_EVENTS", 2)
    r = service.export_ics("2026-11-03", "2026-11-03")
    assert (r.count, r.truncated) == (2, True) and r.ics.count("BEGIN:VEVENT") == 2


# ------------------------------------------------------------------ import
FILE = cal(
    "UID:1\r\nSUMMARY:Kickoff\r\nDTSTART;TZID=Europe/Istanbul:20261103T100000\r\nDURATION:PT1H\r\n"
    "LOCATION:Room 2\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n",
    "UID:2\r\nSUMMARY:Standup\r\nDTSTART;TZID=Europe/Istanbul:20261104T090000\r\nDURATION:PT15M\r\n"
    "RRULE:FREQ=DAILY;COUNT=4\r\nEXDATE;TZID=Europe/Istanbul:20261105T090000\r\n",
    "UID:3\r\nSUMMARY:Offsite\r\nDTSTART;VALUE=DATE:20261112\r\nDTEND;VALUE=DATE:20261114\r\n",
    "UID:4\r\nSUMMARY:Board\r\nDTSTART:20261103T060000Z\r\nRRULE:FREQ=MONTHLY;BYDAY=1TU\r\n",
)


class TestImport:
    def test_imports_what_it_can(self, service: CalendarService) -> None:
        r = service.import_ics(FILE, calendar_id="work")
        assert [e.title for e in r.imported] == ["Kickoff", "Standup", "Offsite"]
        assert [(s.title, "second Tuesday" in s.reason) for s in r.skipped] == [("Board", True)]
        events = service.list_events("2026-11-01", "2026-11-30").events
        assert {e.calendar_id for e in events} == {"work"}
        kickoff = next(e for e in events if e.title == "Kickoff")
        assert (kickoff.start, kickoff.end, kickoff.location) == (at(3, 10), at(3, 11), "Room 2")
        assert kickoff.alarms_minutes_before == [10]
        assert [e.start.day for e in events if e.title == "Standup"] == [4, 6, 7]  # 5 Nov was removed
        offsite = next(e for e in events if e.title == "Offsite")
        assert (offsite.start, offsite.end, offsite.all_day) == (at(12, 0), at(14, 0), True)

    def test_dry_run_lists_without_adding(self, service: CalendarService) -> None:
        service.create_event("Kickoff", "2026-11-03T10:00", end="2026-11-03T11:00", calendar_id="work")
        r = service.import_ics(FILE, calendar_id="work", dry_run=True)
        assert r.dry_run and r.change_id is None
        assert [(e.id, e.title, e.calendar_title) for e in r.imported] == [
            ("", "Standup", "Work"),
            ("", "Offsite", "Work"),
        ]
        assert {s.title for s in r.skipped} == {"Board", "Kickoff"}
        assert [e.title for e in service.list_events("2026-11-01", "2026-11-30").events] == ["Kickoff"]
        assert len(service.list_changes()) == 1  # only the create above
        with pytest.raises(CalendarError, match="read-only"):
            service.import_ics(FILE, calendar_id="holidays", dry_run=True)

    def test_whole_import_is_one_undoable_change(self, service: CalendarService) -> None:
        r = service.import_ics(FILE)
        assert r.change_id == service.list_changes()[0].change_id
        undone = service.undo_change()
        assert len(undone.removed_ids) == 3
        assert service.list_events("2026-11-01", "2026-11-30").count == 0

    def test_skips_events_already_there(self, service: CalendarService) -> None:
        service.create_event("kickoff", "2026-11-03T10:00")
        again = cal(
            "SUMMARY:Kickoff\r\nDTSTART;TZID=Europe/Istanbul:20261103T100000\r\nDURATION:PT1H\r\n",
            "SUMMARY:New\r\nDTSTART;TZID=Europe/Istanbul:20261103T120000\r\n",
            "SUMMARY:New\r\nDTSTART;TZID=Europe/Istanbul:20261103T120000\r\n",  # twice in the file
        )
        r = service.import_ics(again)
        assert [e.title for e in r.imported] == ["New"]
        assert [(s.title, s.reason) for s in r.skipped] == [
            ("Kickoff", "Skipped: it is already in the calendar."),
            ("New", "Skipped: it is already in the calendar."),
        ]
        assert service.import_ics(again).change_id is None  # nothing left to add
        assert len(service.import_ics(again, skip_existing=False).imported) == 3

    def test_existing_check_is_per_calendar_when_one_is_named(self, service: CalendarService) -> None:
        service.create_event("Kickoff", "2026-11-03T10:00", calendar_id="home")
        text = cal("SUMMARY:Kickoff\r\nDTSTART;TZID=Europe/Istanbul:20261103T100000\r\nDURATION:PT1H\r\n")
        assert len(service.import_ics(text, calendar_id="work").imported) == 1

    def test_far_apart_events_are_checked_too(self, service: CalendarService) -> None:
        text = cal(
            "SUMMARY:Old\r\nDTSTART:20200101T060000Z\r\n",
            "SUMMARY:Far\r\nDTSTART:20290101T060000Z\r\n",
        )
        assert len(service.import_ics(text).imported) == 2
        assert len(service.import_ics(text).skipped) == 2

    def test_invalid_fields_are_skipped_not_fatal(self, service: CalendarService) -> None:
        text = cal(
            "DTSTART:20261103T060000Z\r\n",  # no title
            "SUMMARY:Link\r\nDTSTART:20261103T060000Z\r\nURL:javascript:alert(1)\r\n",
            "SUMMARY:Fine\r\nDTSTART:20261103T070000Z\r\n",
        )
        r = service.import_ics(text)
        assert [e.title for e in r.imported] == ["Fine"]
        assert [s.title for s in r.skipped] == ["(no title)", "Link"]

    def test_failure_rolls_back(self, service: CalendarService) -> None:
        with pytest.raises(Exception, match="Nothing was imported") as err:
            service.import_ics(FILE, calendar_id="holidays")  # read-only calendar
        assert "Kickoff" in str(err.value)
        assert service.list_events("2026-11-01", "2026-11-30").count == 0
        assert service.list_changes() == []

    def test_limits(self, service: CalendarService) -> None:
        many = cal(
            *(
                f"SUMMARY:E{n}\r\nDTSTART:{(at(3, 0) + timedelta(minutes=n)):%Y%m%dT%H%M%S}\r\n"
                for n in range(MAX_IMPORT_EVENTS + 1)
            )
        )
        with pytest.raises(InvalidRequestError, match="at most 100"):
            service.import_ics(many)
        with pytest.raises(InvalidRequestError, match="too large"):
            service.import_ics("x" * 1_000_001)
        with pytest.raises(InvalidRequestError, match="iCalendar"):
            service.import_ics("not a calendar")
        assert service.list_events("2026-11-01", "2026-11-30").count == 0


pytestmark_anyio = pytest.mark.anyio


@pytestmark_anyio
async def test_tools(service: CalendarService) -> None:
    async with Client(build_server(service)) as client:
        res = await client.call_tool("import_ics", {"ics": FILE})
        assert not res.is_error and res.structured_content is not None
        assert len(res.structured_content["imported"]) == 3
        assert res.structured_content["change_id"].startswith("chg_")
        out = await client.call_tool("export_ics", {"start": "2026-11-01", "end": "2026-11-30"})
        assert not out.is_error and out.structured_content is not None
        assert out.structured_content["count"] == 5
        assert out.structured_content["ics"].count("BEGIN:VEVENT") == 5
        bad = await client.call_tool("import_ics", {"ics": "nope"})
        assert bad.is_error


@pytestmark_anyio
async def test_read_only_server_exports_but_cannot_import(service: CalendarService) -> None:
    async with Client(build_server(service, read_only=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "export_ics" in names and "import_ics" not in names
