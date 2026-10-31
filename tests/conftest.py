from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.service import CalendarService

IST = ZoneInfo("Europe/Istanbul")
# Monday 2 Nov 2026, 08:00 local
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=IST)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def tz() -> ZoneInfo:
    return IST


@pytest.fixture
def store(tz: ZoneInfo) -> MemoryStore:
    return MemoryStore(tz)


@pytest.fixture
def service(store: MemoryStore, tz: ZoneInfo) -> CalendarService:
    return CalendarService(store, tz, clock=lambda: NOW)
