"""The command-line entry point: argument parsing, backend choice and `doctor`."""

import sys
import types
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from apple_calendar_mcp import __version__, cli, macos_app
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.models import Calendar
from apple_calendar_mcp.store import CalendarError


class FakeEventKitStore:
    """Stands in for the EventKit backend so `doctor` runs on any OS."""

    fail = False

    def __init__(self, tz: tzinfo) -> None:
        self.tz = tz

    @staticmethod
    def authorization_status() -> str:
        return "full_access"

    def list_calendars(self) -> list[Calendar]:
        if self.fail:
            raise CalendarError("access was denied")
        return [
            Calendar(
                id="cal-1",
                title="Work",
                source="iCloud",
                color="#ff0000",
                writable=True,
                is_default=True,
            )
        ]

    def list_events(self, start: datetime, end: datetime) -> list[Any]:
        return []


@pytest.fixture(autouse=True)
def isolated_app_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the helper app at an empty temp dir, whatever is installed on this machine."""
    app = tmp_path / "Helper.app"
    monkeypatch.setattr(macos_app, "default_app_path", lambda: app)
    monkeypatch.delenv(macos_app.VIA_APP_ENV, raising=False)
    return app


@pytest.fixture
def fake_eventkit(monkeypatch: pytest.MonkeyPatch) -> type[FakeEventKitStore]:
    module = types.ModuleType("apple_calendar_mcp.eventkit_store")
    module.EventKitStore = FakeEventKitStore  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apple_calendar_mcp.eventkit_store", module)
    monkeypatch.setattr(sys, "platform", "darwin")
    FakeEventKitStore.fail = False
    return FakeEventKitStore


def test_memory_backend_with_demo_data() -> None:
    store, service = cli._make_store("memory", "Europe/Istanbul", demo=True)
    assert isinstance(store, MemoryStore)
    assert store.list_calendars()
    assert str(service.tz) == "Europe/Istanbul"


def test_memory_backend_starts_empty_without_demo() -> None:
    store, _ = cli._make_store("memory", "UTC", demo=False)
    assert isinstance(store, MemoryStore)
    now = datetime.now(store.tz)
    assert store.list_events(now.replace(year=now.year - 1), now.replace(year=now.year + 1)) == []


def test_eventkit_backend_refuses_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit, match="needs macOS"):
        cli._make_store("eventkit", None, demo=False)


def test_eventkit_backend_on_macos(fake_eventkit: type[FakeEventKitStore]) -> None:
    store, _ = cli._make_store("eventkit", "UTC", demo=False)
    assert isinstance(store, fake_eventkit)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_builds_server_with_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeServer:
        def run(self, transport: str) -> None:
            calls["transport"] = transport

    def fake_build(service: Any, read_only: bool = False) -> FakeServer:
        calls["read_only"] = read_only
        calls["store"] = service.store
        return FakeServer()

    monkeypatch.setattr(cli, "build_server", fake_build)
    assert cli.main(["--backend", "memory", "--demo", "--read-only", "--timezone", "UTC"]) == 0
    assert calls["transport"] == "stdio"
    assert calls["read_only"] is True
    assert isinstance(calls["store"], MemoryStore)


def test_doctor_off_macos(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli.main(["doctor", "--timezone", "UTC"]) == 1
    assert "not macOS" in capsys.readouterr().out


def test_doctor_lists_calendars(
    fake_eventkit: type[FakeEventKitStore], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor", "--timezone", "UTC"]) == 0
    out = capsys.readouterr().out
    assert "calendars          : 1" in out
    assert "[rw] Work  (iCloud)" in out
    assert "(default)" in out


def test_doctor_reports_permission_error(
    fake_eventkit: type[FakeEventKitStore], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_eventkit.fail = True
    assert cli.main(["doctor", "--timezone", "UTC"]) == 1
    assert "ERROR: access was denied" in capsys.readouterr().out


def test_doctor_suggests_installing_helper(
    fake_eventkit: type[FakeEventKitStore], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor", "--timezone", "UTC"]) == 0
    out = capsys.readouterr().out
    assert "helper app         : not installed" in out
    assert "install-app" in out


def test_doctor_running_via_helper(
    fake_eventkit: type[FakeEventKitStore],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(macos_app.VIA_APP_ENV, "1")
    assert cli.main(["doctor", "--timezone", "UTC"]) == 0
    out = capsys.readouterr().out
    assert "helper app         : running via" in out
    assert "Note:" not in out


def test_install_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    built: list[Path | None] = []

    def fake_build(app: Path | None = None) -> Path:
        built.append(app)
        return app or tmp_path

    monkeypatch.setattr(macos_app, "build_app", fake_build)
    assert cli.main(["install-app", "--dest", str(tmp_path / "X.app")]) == 0
    assert built == [tmp_path / "X.app"]
    assert "Installed" in capsys.readouterr().out


def test_install_app_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def broken_build(app: Path | None = None) -> Path:
        raise macos_app.AppBuildError("clang not found")

    monkeypatch.setattr(macos_app, "build_app", broken_build)
    assert cli.main(["install-app"]) == 1
    assert "clang not found" in capsys.readouterr().err


def test_install_app_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli.main(["install-app"]) == 1


def test_real_invocation_relaunches_via_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["apple-calendar-mcp", "doctor"])
    monkeypatch.setattr(macos_app, "relaunch_command", lambda argv: ["/helper", *argv])

    class Relaunched(Exception):
        pass

    def fake_relaunch(cmd: list[str]) -> None:
        raise Relaunched(cmd)

    monkeypatch.setattr(macos_app, "relaunch", fake_relaunch)
    with pytest.raises(Relaunched) as exc:
        cli.main()
    assert exc.value.args[0] == ["/helper", "doctor"]


# ------------------------------------------------------------------ remote connector
def test_caldav_backend_needs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALDAV_USERNAME", raising=False)
    monkeypatch.delenv("CALDAV_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="CALDAV_USERNAME"):
        cli._make_store("caldav", "UTC", demo=False)


def test_caldav_backend_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("caldav")
    from apple_calendar_mcp.caldav_store import CaldavStore

    monkeypatch.setenv("CALDAV_USERNAME", "me@icloud.com")
    monkeypatch.setenv("CALDAV_PASSWORD", "abcd-efgh-ijkl-mnop")
    monkeypatch.setenv("CALDAV_DEFAULT_CALENDAR", "home")
    store, _ = cli._make_store("caldav", "UTC", demo=False)
    assert isinstance(store, CaldavStore)
    assert store.url == "https://caldav.icloud.com"
    assert store.default_calendar == "home"


def test_serve_http_refuses_eventkit() -> None:
    with pytest.raises(SystemExit, match="caldav"):
        cli.main(["--backend", "eventkit", "serve-http"])


def test_serve_http_needs_public_url_and_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("CONNECTOR_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="PUBLIC_URL"):
        cli.main(["--backend", "memory", "serve-http"])


def test_serve_http_starts_remote_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from apple_calendar_mcp import remote

    monkeypatch.setenv("PUBLIC_URL", "https://cal.example.com")
    monkeypatch.setenv("CONNECTOR_PASSWORD", "a-long-enough-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen: dict[str, Any] = {}

    def fake_serve(service: Any, config: remote.RemoteConfig, *, read_only: bool = False) -> None:
        seen.update(config=config, read_only=read_only, store=service.store)

    monkeypatch.setattr(remote, "serve", fake_serve)
    assert cli.main(["--backend", "memory", "--demo", "--read-only", "serve-http", "--port", "9123"]) == 0
    assert seen["config"].public_url == "https://cal.example.com"
    assert seen["config"].port == 9123
    assert seen["read_only"] is True
    assert isinstance(seen["store"], MemoryStore)
