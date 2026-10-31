"""Command-line entry point.

    apple-calendar-mcp                 # serve over stdio with EventKit (macOS)
    apple-calendar-mcp --read-only     # expose only the read tools
    apple-calendar-mcp --backend memory --demo   # fake data, any OS
    apple-calendar-mcp doctor          # check/prompt calendar permission
    apple-calendar-mcp install-app     # build the helper app that owns the permission (macOS)
    apple-calendar-mcp --backend caldav doctor   # iCloud/CalDAV, configured via CALDAV_* env vars
    apple-calendar-mcp serve-http      # remote connector for claude.ai (CalDAV + OAuth over HTTP)

stdout carries the MCP protocol, so all diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from . import __version__, macos_app
from .memory_store import MemoryStore, seed_demo
from .server import build_server
from .service import CalendarService
from .store import CalendarError, CalendarStore
from .timeutil import resolve_local_tz, tz_name

log = logging.getLogger("apple_calendar_mcp")


def caldav_store_from_env(tz: tzinfo) -> CalendarStore:
    """Build the CalDAV backend from CALDAV_* environment variables."""
    try:
        from .caldav_store import DEFAULT_ICLOUD_URL, CaldavStore
    except ImportError as exc:  # optional extra not installed
        raise SystemExit(
            "The CalDAV backend needs the 'remote' extra: uv sync --extra remote "
            "(or pip install 'apple-calendar-mcp[remote]')."
        ) from exc
    username = os.environ.get("CALDAV_USERNAME", "").strip()
    password = os.environ.get("CALDAV_PASSWORD", "").strip()
    if not username or not password:
        raise SystemExit(
            "Set CALDAV_USERNAME (your Apple ID) and CALDAV_PASSWORD (an app-specific password "
            "from appleid.apple.com). See .env.example."
        )
    return CaldavStore(
        tz,
        url=os.environ.get("CALDAV_URL", "").strip() or DEFAULT_ICLOUD_URL,
        username=username,
        password=password,
        default_calendar=os.environ.get("CALDAV_DEFAULT_CALENDAR", "").strip() or None,
    )


def _make_store(backend: str, tz_override: str | None, demo: bool) -> tuple[CalendarStore, CalendarService]:
    tz = resolve_local_tz(tz_override)
    store: CalendarStore
    if backend == "caldav":
        store = caldav_store_from_env(tz)
    elif backend == "memory":
        mem = MemoryStore(tz)
        if demo:
            seed_demo(mem)
        store = mem
    else:
        if sys.platform != "darwin":
            raise SystemExit(
                "The EventKit backend needs macOS. Use '--backend memory --demo' to try the server elsewhere."
            )
        from .eventkit_store import EventKitStore

        store = EventKitStore(tz)
    return store, CalendarService(store, tz)


def _print_calendars(store: CalendarStore, tz: tzinfo) -> None:
    cals = store.list_calendars()
    print(f"calendars          : {len(cals)}")
    for c in cals:
        flag = "rw" if c.writable else "ro"
        print(f"  [{flag}] {c.title}  ({c.source})  id={c.id}{'  (default)' if c.is_default else ''}")
    now = datetime.now(tz)
    print(f"events next 7 days : {len(store.list_events(now, now + timedelta(days=7)))}")


def _doctor_caldav(tz: tzinfo) -> int:
    store = caldav_store_from_env(tz)
    print(f"backend            : CalDAV ({os.environ.get('CALDAV_URL') or 'iCloud'})")
    try:
        _print_calendars(store, tz)
    except CalendarError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


def _doctor(args: argparse.Namespace) -> int:
    print(f"apple-calendar-mcp {__version__}")
    tz = resolve_local_tz(args.timezone)
    print(f"time zone          : {tz_name(tz)}")
    if args.backend == "caldav":
        return _doctor_caldav(tz)
    if sys.platform != "darwin":
        print("platform           : not macOS -> only '--backend memory' is available")
        return 1

    from .eventkit_store import EventKitStore

    app = macos_app.default_app_path()
    if os.environ.get(macos_app.VIA_APP_ENV):
        print(f"helper app         : running via {app}")
    elif os.access(macos_app.launcher_path(app), os.X_OK):
        print("helper app         : installed, but not used (APPLE_CALENDAR_MCP_NO_APP is set)")
    else:
        print("helper app         : not installed (run: apple-calendar-mcp install-app)")
    print(f"permission (before): {EventKitStore.authorization_status()}")
    store = EventKitStore(tz)
    try:
        store.list_calendars()  # triggers the consent prompt if needed
    except CalendarError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"permission (after) : {EventKitStore.authorization_status()}")
    _print_calendars(store, tz)
    if not os.environ.get(macos_app.VIA_APP_ENV):
        print(
            "\nNote: macOS grants calendar access per *host app*. This check used the app\n"
            "you ran it from (e.g. Terminal). Run `apple-calendar-mcp install-app` so the\n"
            "permission belongs to the server itself, whichever app starts it."
        )
    return 0


def _install_app(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("install-app is only needed on macOS.", file=sys.stderr)
        return 1
    try:
        app = macos_app.build_app(Path(args.dest) if args.dest else None)
    except macos_app.AppBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {app}")
    print("Next: run `apple-calendar-mcp doctor` and click Allow Full Access for 'Apple Calendar MCP'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apple-calendar-mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--backend",
        choices=["eventkit", "caldav", "memory"],
        help="default: caldav for serve-http, eventkit otherwise",
    )
    parser.add_argument("--demo", action="store_true", help="seed the memory backend with sample events")
    parser.add_argument("--read-only", action="store_true", help="do not expose create/update/delete")
    parser.add_argument("--timezone", help="IANA zone, e.g. Europe/Istanbul (default: system)")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument(
        "--dest", help="install-app: bundle path (default: ~/Applications/Apple Calendar MCP.app)"
    )
    parser.add_argument("--host", help="serve-http: bind address (default: $HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="serve-http: port (default: $PORT or 8000)")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "serve-http", "doctor", "install-app"], default="serve"
    )
    real_argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(real_argv)
    if args.backend is None:
        args.backend = "caldav" if args.command == "serve-http" else "eventkit"

    logging.basicConfig(
        stream=sys.stderr, level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )

    if args.command == "install-app":
        return _install_app(args)

    # Real invocations (not tests) on macOS re-run through the helper app, if
    # installed, so the calendar permission belongs to it.
    if argv is None and args.backend == "eventkit":
        cmd = macos_app.relaunch_command(real_argv)
        if cmd is not None:
            log.debug("relaunching via %s", cmd[0])
            macos_app.relaunch(cmd)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "serve-http":
        return _serve_http(args)

    _, service = _make_store(args.backend, args.timezone, args.demo)
    log.info("serving backend=%s tz=%s read_only=%s", args.backend, tz_name(service.tz), args.read_only)
    build_server(service, read_only=args.read_only).run("stdio")
    return 0


def _serve_http(args: argparse.Namespace) -> int:
    from . import remote

    if args.backend == "eventkit":
        raise SystemExit("serve-http runs on a server, so it uses --backend caldav (or memory for testing).")
    config = remote.RemoteConfig.from_env(host=args.host, port=args.port)
    _, service = _make_store(args.backend, args.timezone, args.demo)
    remote.serve(service, config, read_only=args.read_only)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
