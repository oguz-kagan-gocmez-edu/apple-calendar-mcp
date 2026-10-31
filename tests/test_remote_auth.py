"""Remote connector: OAuth sign-in flow and MCP over streamable HTTP, in-process."""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from apple_calendar_mcp.memory_store import MemoryStore
from apple_calendar_mcp.remote import RemoteConfig, RemoteServer, build_remote
from apple_calendar_mcp.service import CalendarService

from .conftest import IST, NOW

pytestmark = pytest.mark.anyio

BASE = "https://cal.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def remote(tmp_path: Path, clock: Clock) -> RemoteServer:
    service = CalendarService(MemoryStore(IST), IST, clock=lambda: NOW)
    config = RemoteConfig(public_url=BASE, password=PASSWORD, data_dir=tmp_path / "data")
    return build_remote(service, config, clock=clock)


@asynccontextmanager
async def http(remote: RemoteServer) -> AsyncIterator[httpx2.AsyncClient]:
    app = remote.app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def register(client: httpx2.AsyncClient, redirect: str = CALLBACK) -> httpx2.Response:
    return await client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


async def start_login(client: httpx2.AsyncClient) -> tuple[str, str, str]:
    """Register + /authorize. Returns (client_id, verifier, login request id)."""
    reg = await register(client)
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    auth = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "calendar",
        },
    )
    assert auth.status_code == 302, auth.text
    login = urlparse(auth.headers["location"])
    assert f"{login.scheme}://{login.netloc}{login.path}" == f"{BASE}/login"
    return client_id, verifier, parse_qs(login.query)["request"][0]


async def sign_in(client: httpx2.AsyncClient) -> dict[str, Any]:
    client_id, verifier, request_id = await start_login(client)
    approved = await client.post(
        "/login", data={"request": request_id, "password": PASSWORD, "action": "allow"}
    )
    assert approved.status_code == 303
    target = urlparse(approved.headers["location"])
    query = parse_qs(target.query)
    assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
    assert query["state"] == ["xyz"]
    token = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return {"client_id": client_id, "code": query["code"][0], "verifier": verifier, **token.json()}


# ------------------------------------------------------------------ discovery
async def test_metadata_advertises_endpoints(remote: RemoteServer) -> None:
    async with http(remote) as client:
        meta = (await client.get("/.well-known/oauth-authorization-server")).json()
        prm = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert meta["issuer"].rstrip("/") == BASE
    assert meta["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in meta["code_challenge_methods_supported"]
    assert prm.status_code == 200
    assert prm.json()["resource"] == f"{BASE}/mcp"


async def test_mcp_requires_a_token(remote: RemoteServer) -> None:
    async with http(remote) as client:
        res = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        bad = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer nope"},
        )
    assert res.status_code == 401
    assert "resource_metadata" in res.headers.get("www-authenticate", "")
    assert bad.status_code == 401


async def test_health_is_public(remote: RemoteServer) -> None:
    async with http(remote) as client:
        assert (await client.get("/healthz")).json() == {"ok": True}


# ------------------------------------------------------------------ registration
async def test_unknown_redirect_uri_is_rejected(remote: RemoteServer) -> None:
    async with http(remote) as client:
        res = await register(client, "https://evil.example/callback")
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_redirect_uri"


async def test_localhost_redirect_allowed_for_inspector(remote: RemoteServer) -> None:
    async with http(remote) as client:
        res = await register(client, "http://localhost:6274/oauth/callback")
    assert res.status_code == 201


# ------------------------------------------------------------------ login page
async def test_login_page_escapes_client_name(remote: RemoteServer) -> None:
    async with http(remote) as client:
        reg = await client.post(
            "/register",
            json={
                "redirect_uris": [CALLBACK],
                "client_name": "<script>x</script>",
                "token_endpoint_auth_method": "none",
            },
        )
        _, challenge = pkce()
        auth = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg.json()["client_id"],
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        page = await client.get(auth.headers["location"].removeprefix(BASE))
    assert page.status_code == 200
    assert "<script>x</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


async def test_wrong_password_then_rate_limited(remote: RemoteServer) -> None:
    async with http(remote) as client:
        _, _, request_id = await start_login(client)
        for _ in range(5):
            res = await client.post(
                "/login", data={"request": request_id, "password": "wrong", "action": "allow"}
            )
            assert res.status_code == 401
        locked = await client.post(
            "/login", data={"request": request_id, "password": PASSWORD, "action": "allow"}
        )
    assert locked.status_code == 429


async def test_deny_returns_access_denied(remote: RemoteServer) -> None:
    async with http(remote) as client:
        _, _, request_id = await start_login(client)
        res = await client.post("/login", data={"request": request_id, "password": "", "action": "deny"})
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert res.status_code == 303
    assert query["error"] == ["access_denied"]


async def test_login_link_expires(remote: RemoteServer, clock: Clock) -> None:
    async with http(remote) as client:
        _, _, request_id = await start_login(client)
        clock.now += 11 * 60
        res = await client.get(f"/login?request={request_id}")
    assert res.status_code == 400
    assert "expired" in res.text


# ------------------------------------------------------------------ tokens
async def test_code_is_single_use(remote: RemoteServer) -> None:
    async with http(remote) as client:
        grant = await sign_in(client)
        again = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": grant["code"],
                "redirect_uri": CALLBACK,
                "client_id": grant["client_id"],
                "code_verifier": grant["verifier"],
            },
        )
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


async def test_refresh_rotates_and_revoke_ends_grant(remote: RemoteServer) -> None:
    async with http(remote) as client:
        grant = await sign_in(client)
        refreshed = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": grant["refresh_token"],
                "client_id": grant["client_id"],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        new = refreshed.json()
        assert new["access_token"] != grant["access_token"]
        reuse = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": grant["refresh_token"],
                "client_id": grant["client_id"],
            },
        )
        assert reuse.status_code == 400  # old refresh token no longer works
        revoked = await client.post(  # the SDK's revoke form requires the field, even if empty
            "/revoke",
            data={"token": new["refresh_token"], "client_id": grant["client_id"], "client_secret": ""},
        )
        assert revoked.status_code == 200, revoked.text
        after = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {new['access_token']}"},
        )
    assert after.status_code == 401


async def test_access_token_expires(remote: RemoteServer, clock: Clock) -> None:
    async with http(remote) as client:
        grant = await sign_in(client)
        clock.now += 3601
        res = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {grant['access_token']}"},
        )
    assert res.status_code == 401


async def test_secrets_are_stored_hashed(remote: RemoteServer, tmp_path: Path) -> None:
    async with http(remote) as client:
        grant = await sign_in(client)
    dump = "\n".join(sqlite3.connect(tmp_path / "data" / "auth.db").iterdump())
    for secret in (grant["access_token"], grant["refresh_token"], grant["code"], PASSWORD):
        assert secret not in dump


# ------------------------------------------------------------------ end to end
async def test_tools_work_over_http_with_token(remote: RemoteServer) -> None:
    app = remote.app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            grant = await sign_in(client)
        authed = httpx2.AsyncClient(
            transport=transport, base_url=BASE, headers={"Authorization": f"Bearer {grant['access_token']}"}
        )
        async with (
            authed,
            streamable_http_client(f"{BASE}/mcp", http_client=authed) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "create_event" in tools
            created = await session.call_tool(
                "create_event", {"title": "Remote test", "start": "2026-11-03T14:00"}
            )
            assert not created.is_error, created.content
            listed = await session.call_tool("list_events", {"start": "2026-11-03", "end": "2026-11-03"})
            assert not listed.is_error
            titles = [e["title"] for e in (listed.structured_content or {})["events"]]
    assert titles == ["Remote test"]


async def test_wrong_host_header_is_rejected(remote: RemoteServer) -> None:
    async with http(remote) as client:
        grant = await sign_in(client)
        res = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {grant['access_token']}", "Host": "attacker.example"},
        )
    assert res.status_code in (400, 421)


def test_config_rejects_weak_password(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="12 characters"):
        RemoteConfig(public_url=BASE, password="short", data_dir=tmp_path)
