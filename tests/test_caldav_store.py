"""CaldavStore against a real CalDAV server (Radicale, started for the session).

The service-layer scenarios from test_service.py are re-run here unchanged,
so the CalDAV backend must give exactly the same answers as the in-memory one.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("caldav")
pytest.importorskip("radicale")

import caldav
from caldav.lib import error as dav_error

from apple_calendar_mcp.caldav_store import CaldavStore, _object_href
from apple_calendar_mcp.models import EventDraft, EventPatch, Recurrence, Span
from apple_calendar_mcp.service import CalendarService
from apple_calendar_mcp.store import BackendError, NotFoundError, PermissionDeniedError

from .conftest import IST, NOW
from .test_service import TestCreate as _TestCreate

# Re-run the service scenarios against CalDAV (pytest collects imported classes).
from .test_service import (  # noqa: F401
    TestQueries,
    TestRecurring,
    TestUpdate,
    test_timestamps_are_aware,
    test_unknown_id,
    weekly,
)


class TestCreate(_TestCreate):
    @pytest.mark.skip(reason="Radicale test calendars are all writable")
    def test_read_only_calendar(self, service: CalendarService) -> None:  # type: ignore[override]
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def radicale_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    folder: Path = tmp_path_factory.mktemp("radicale")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--server-hosts",
            f"127.0.0.1:{port}",
            "--storage-filesystem-folder",
            str(folder),
            "--auth-type",
            "none",
            "--rights-type",
            "owner_only",
            "--logging-level",
            "warning",
        ],
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("Radicale did not start")
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def store(radicale_url: str) -> CaldavStore:  # type: ignore[override]
    """A fresh account per test, with 'home' (default) and 'work' calendars."""
    user = f"u{uuid.uuid4().hex[:10]}"
    # One client per test, sending credentials up front (skips a 401 round trip).
    client = caldav.DAVClient(url=radicale_url, username=user, password="x", auth_type="basic")
    principal = client.principal()
    principal.make_calendar(name="Home", cal_id="home")
    principal.make_calendar(name="Work", cal_id="work")
    return CaldavStore(
        IST, url=radicale_url, username=user, password="x", default_calendar="home", client=client
    )


@pytest.fixture
def service(store: CaldavStore) -> CalendarService:
    return CalendarService(store, IST, clock=lambda: NOW)


# ------------------------------------------------------------------ backend specifics
def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def test_calendars(store: CaldavStore) -> None:
    cals = {c.id: c for c in store.list_calendars()}
    assert set(cals) == {"home", "work"}
    assert cals["home"].is_default and cals["home"].title == "Home"
    assert cals["home"].source == "127.0.0.1"


def test_round_trip_fields(store: CaldavStore) -> None:
    e = store.create_event(
        EventDraft(
            title="Dentist",
            start=at(3, 9),
            end=at(3, 10),
            location="Kadıköy",
            notes="Bring card",
            url="https://example.com",
            alarms_minutes_before=[60, 10],
            availability="free",
        )
    )
    got = store.get_event(e.id)
    assert (got.title, got.location, got.notes) == ("Dentist", "Kadıköy", "Bring card")
    assert got.url == "https://example.com"
    assert got.alarms_minutes_before == [60, 10]
    assert got.availability == "free"
    assert (got.start, got.end) == (at(3, 9), at(3, 10))


def test_all_day_is_stored_as_dates(store: CaldavStore) -> None:
    e = store.create_event(EventDraft(title="Trip", start=at(5, 0), end=at(8, 0), all_day=True))
    home = store._calendar("home", for_write=False)[0]
    raw = home.object_by_uid(e.id.split("|")[0]).data
    assert "DTSTART;VALUE=DATE:20261105" in raw
    assert "DTEND;VALUE=DATE:20261108" in raw
    assert store.get_event(e.id).all_day


def test_rrule_written_in_rfc_form(store: CaldavStore) -> None:
    e = store.create_event(
        EventDraft(
            title="Gym",
            start=at(2, 7),
            end=at(2, 8),
            recurrence=Recurrence(
                frequency="weekly", days_of_week=["mon", "thu"], until=datetime(2026, 11, 30).date()
            ),
        )
    )
    got = store.get_event(e.id)
    assert got.recurrence == Recurrence(
        frequency="weekly", days_of_week=["mon", "thu"], until=datetime(2026, 11, 30).date()
    )
    assert len(store.list_events(at(1, 0), at(30, 23))) == 9


def test_this_edit_creates_override_and_delete_adds_exdate(store: CaldavStore) -> None:
    first = store.create_event(
        EventDraft(
            title="Daily", start=at(2, 9), end=at(2, 9, 30), recurrence=Recurrence(frequency="daily", count=4)
        )
    )
    ids = [e.id for e in store.list_events(at(1, 0), at(10, 0))]
    store.update_event(
        ids[1], EventPatch(title="Daily (moved)", start=at(3, 15), end=at(3, 15, 30)), Span.THIS
    )
    store.delete_event(ids[2], Span.THIS)
    events = store.list_events(at(1, 0), at(10, 0))
    assert [(e.title, e.start.day, e.start.hour) for e in events] == [
        ("Daily", 2, 9),
        ("Daily (moved)", 3, 15),
        ("Daily", 5, 9),
    ]
    assert events[1].id == ids[1]  # id survives the move
    with pytest.raises(NotFoundError):
        store.get_event(ids[2])
    assert store.get_event(first.id).title == "Daily"


def test_unreachable_server_is_a_clean_backend_error() -> None:
    port = _free_port()  # nothing listens here
    store = CaldavStore(IST, url=f"http://127.0.0.1:{port}/", username="x", password="secret-pw")
    with pytest.raises(BackendError) as exc:
        store.list_calendars()
    assert "secret-pw" not in str(exc.value)


def test_rejected_login_is_a_permission_error() -> None:
    class Rejecting:
        def principal(self) -> None:
            raise dav_error.AuthorizationError("401")

    store = CaldavStore(IST, username="x", password="y", client=Rejecting())
    with pytest.raises(PermissionDeniedError, match="app-specific password"):
        store.list_calendars()


def test_caldav_library_cannot_log_event_contents() -> None:
    import logging

    assert logging.getLogger("caldav").getEffectiveLevel() >= logging.ERROR


def test_foreign_uids_cannot_escape_the_calendar() -> None:
    class Cal:
        url = "https://dav.example/123/calendars/work/"

    href = _object_href(Cal(), "../../other/evil")
    assert href == "https://dav.example/123/calendars/work/..%2F..%2Fother%2Fevil.ics"
