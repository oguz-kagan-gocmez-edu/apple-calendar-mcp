"""The Claude Desktop extension (mcpb/): kept in step with the package and the server."""

import json
import re
import tomllib
from pathlib import Path

import pytest
from mcp.client.client import Client

from apple_calendar_mcp import __version__, cli, macos_app
from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.reminders import MemoryReminderStore
from apple_calendar_mcp.server import build_server
from apple_calendar_mcp.service import CalendarService

from .conftest import IST, NOW

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "mcpb" / "manifest.json").read_text("utf-8"))
PROJECT = tomllib.loads((ROOT / "mcpb" / "pyproject.toml").read_text("utf-8"))["project"]


def test_versions_match_the_package() -> None:
    assert MANIFEST["version"] == PROJECT["version"] == __version__
    assert PROJECT["dependencies"] == [f"apple-calendar-eventkit-mcp=={__version__}"]


@pytest.mark.anyio
async def test_lists_only_tools_the_server_has() -> None:
    service = CalendarService(MemoryStore(IST), IST, clock=lambda: NOW, reminders=MemoryReminderStore(IST))
    async with Client(build_server(service)) as client:
        real = {t.name for t in (await client.list_tools()).tools}
    listed = [t["name"] for t in MANIFEST["tools"]]
    assert len(listed) == len(set(listed)) and set(listed) <= real
    assert real - set(listed) <= {"respond_to_invitation"}  # CalDAV only


def test_environment_is_what_the_server_reads() -> None:
    env = MANIFEST["server"]["mcp_config"]["env"]
    known = {
        macos_app.INSTALL_APP_ENV,
        cli.READ_ONLY_ENV,
        cli.NO_REMINDERS_ENV,
        *cli.ACCESS_ENV.values(),
    }
    assert set(env) == known and env[macos_app.INSTALL_APP_ENV] == "1"
    used = {m for v in env.values() for m in re.findall(r"\$\{user_config\.(\w+)\}", v)}
    assert used == set(MANIFEST["user_config"])


def test_entry_point_exists() -> None:
    assert (ROOT / "mcpb" / MANIFEST["server"]["entry_point"]).is_file()
    assert MANIFEST["compatibility"]["platforms"] == ["darwin"]
