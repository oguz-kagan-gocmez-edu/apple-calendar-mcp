"""Remote connector: the MCP server over streamable HTTP, protected by OAuth.

claude.ai custom connectors sign in with OAuth 2.1 and dynamic client
registration. This module is a single-owner authorization server built on
the MCP SDK's ``OAuthAuthorizationServerProvider``:

1. Claude registers itself as a client (redirect URIs must be on the allowlist).
2. ``/authorize`` sends the owner's browser to ``/login``, which asks for
   ``CONNECTOR_PASSWORD``.
3. Claude swaps the one-time code for an access token (1 h) and a rotating
   refresh token (30 d). Tokens and codes are stored only as SHA-256 hashes.

Everything lives in one SQLite file under ``DATA_DIR`` so a restart doesn't
sign Claude out. Nothing here logs secrets, tokens or calendar contents.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .server import build_server
from .service import CalendarService

log = logging.getLogger("apple_calendar_mcp.remote")

SCOPE = "calendar"
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
CODE_TTL = 5 * 60
LOGIN_TTL = 10 * 60
MAX_CLIENTS = 100
MAX_PENDING = 50  # unfinished sign-ins; /authorize is public, so cap what it can store
MAX_FORM_BYTES = 8 * 1024
MAX_CLIENT_NAME = 80
# Values shipped in .env.example / docs: a server must never start with one of these.
_PLACEHOLDER_PASSWORDS = {"change-me-to-a-long-random-string", "ci-only-password-123"}
DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class RemoteConfig:
    public_url: str
    password: str
    data_dir: Path
    allowed_redirects: tuple[str, ...] = DEFAULT_REDIRECTS
    allow_localhost_redirects: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        self.public_url = self.public_url.rstrip("/")
        parsed = urlparse(self.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("PUBLIC_URL must be a full URL, e.g. https://calendar.example.com")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_URL must be just the address, without a path (no /mcp).")
        if len(self.password) < 12:
            raise ValueError("CONNECTOR_PASSWORD must be at least 12 characters.")
        if self.password in _PLACEHOLDER_PASSWORDS and not self.is_local:
            raise ValueError("CONNECTOR_PASSWORD is still the example value. Generate a new one.")

    @property
    def is_local(self) -> bool:
        return (urlparse(self.public_url).hostname or "") in _LOCAL_HOSTS

    @classmethod
    def from_env(cls, *, host: str | None = None, port: int | None = None) -> RemoteConfig:
        public_url = os.environ.get("PUBLIC_URL", "").strip()
        password = os.environ.get("CONNECTOR_PASSWORD", "")
        if not public_url or not password:
            raise SystemExit(
                "serve-http needs PUBLIC_URL (the https address Claude will use) and "
                "CONNECTOR_PASSWORD (12+ characters, you type it once when connecting). See .env.example."
            )
        extra = tuple(
            u.strip() for u in os.environ.get("OAUTH_ALLOWED_REDIRECTS", "").split(",") if u.strip()
        )
        local = (urlparse(public_url).hostname or "") in _LOCAL_HOSTS
        # http://localhost redirects are for the MCP Inspector during local testing.
        # On a public server they're off unless asked for.
        allow_local = os.environ.get("OAUTH_ALLOW_LOCALHOST", "1" if local else "0") == "1"
        try:
            return cls(
                public_url=public_url,
                password=password,
                data_dir=Path(os.environ.get("DATA_DIR", "./data")),
                allowed_redirects=DEFAULT_REDIRECTS + extra,
                allow_localhost_redirects=allow_local,
                host=host or os.environ.get("HOST", "127.0.0.1"),
                port=port or int(os.environ.get("PORT", "8000")),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc


# ---------------------------------------------------------------- storage
class AuthDB:
    """Tiny SQLite store. Secrets are stored only as SHA-256 hashes."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                params TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                scopes TEXT NOT NULL, resource TEXT, family TEXT NOT NULL, expires REAL NOT NULL);
            """
        )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def purge_expired(self) -> None:
        now = time.time()
        with self.tx() as db:
            for table in ("pending", "codes", "tokens"):
                db.execute(f"DELETE FROM {table} WHERE expires < ?", (now,))


# ---------------------------------------------------------------- provider
class OwnerAuthProvider:
    """OAuth authorization server for a single owner (password-protected)."""

    def __init__(self, config: RemoteConfig, db: AuthDB, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.db = db
        self.clock = clock

    # -- clients
    def redirect_allowed(self, uri: str) -> bool:
        if uri in self.config.allowed_redirects:
            return True
        parsed = urlparse(uri)
        return (
            self.config.allow_localhost_redirects
            and parsed.scheme == "http"
            and (parsed.hostname or "") in _LOCAL_HOSTS
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.db.tx() as db:
            row = db.execute("SELECT info FROM clients WHERE id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in client_info.redirect_uris or [] if not self.redirect_allowed(str(u))]
        if bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "This connector only accepts Claude's redirect URIs. Add others with OAUTH_ALLOWED_REDIRECTS.",
            )
        with self.db.tx() as db:
            (count,) = db.execute("SELECT COUNT(*) FROM clients").fetchone()
            if count >= MAX_CLIENTS:
                # Registration is public. Make room by dropping the oldest clients that
                # were never signed in, so a flood can't push out Claude's real client.
                db.execute(
                    "DELETE FROM clients WHERE id IN (SELECT id FROM clients WHERE id NOT IN "
                    "(SELECT client_id FROM tokens WHERE expires >= ?) ORDER BY created LIMIT ?)",
                    (self.clock(), count - MAX_CLIENTS + 1),
                )
                (count,) = db.execute("SELECT COUNT(*) FROM clients").fetchone()
                if count >= MAX_CLIENTS:
                    raise RegistrationError("invalid_client_metadata", "Too many registered clients.")
            db.execute(
                "INSERT OR REPLACE INTO clients (id, info, created) VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), self.clock()),
            )

    # -- authorization (owner login)
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        now = self.clock()
        with self.db.tx() as db:
            db.execute("DELETE FROM pending WHERE expires < ?", (now,))
            db.execute("DELETE FROM codes WHERE expires < ?", (now,))
            (count,) = db.execute("SELECT COUNT(*) FROM pending").fetchone()
            if count >= MAX_PENDING:  # drop the oldest unfinished sign-ins
                db.execute(
                    "DELETE FROM pending WHERE id IN (SELECT id FROM pending ORDER BY expires LIMIT ?)",
                    (count - MAX_PENDING + 1,),
                )
            db.execute(
                "INSERT INTO pending (id, client_id, params, expires) VALUES (?, ?, ?, ?)",
                (request_id, client.client_id, params.model_dump_json(), now + LOGIN_TTL),
            )
        return f"{self.config.public_url}/login?request={request_id}"

    def pending(self, request_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        with self.db.tx() as db:
            row = db.execute(
                "SELECT p.params, c.info FROM pending p JOIN clients c ON c.id = p.client_id "
                "WHERE p.id = ? AND p.expires >= ?",
                (request_id, self.clock()),
            ).fetchone()
        if not row:
            return None
        return OAuthClientInformationFull.model_validate_json(
            row[1]
        ), AuthorizationParams.model_validate_json(row[0])

    def approve(self, request_id: str) -> str | None:
        """Owner approved: issue a one-time code and return the redirect URL."""
        found = self.pending(request_id)
        if found is None:
            return None
        client, params = found
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(
            code="",
            scopes=params.scopes or [SCOPE],
            expires_at=self.clock() + CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        with self.db.tx() as db:
            db.execute("DELETE FROM pending WHERE id = ?", (request_id,))
            db.execute(
                "INSERT INTO codes (hash, data, expires) VALUES (?, ?, ?)",
                (_hash(code), data.model_dump_json(), data.expires_at),
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, request_id: str) -> str | None:
        found = self.pending(request_id)
        if found is None:
            return None
        with self.db.tx() as db:
            db.execute("DELETE FROM pending WHERE id = ?", (request_id,))
        _, params = found
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # -- codes and tokens
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self.db.tx() as db:
            row = db.execute(
                "SELECT data FROM codes WHERE hash = ? AND expires >= ?",
                (_hash(authorization_code), self.clock()),
            ).fetchone()
        if not row:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        if code.client_id != client.client_id:
            return None
        return code.model_copy(update={"code": authorization_code})

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, family: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = self.clock()
        with self.db.tx() as db:
            for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
                db.execute(
                    "INSERT INTO tokens (hash, kind, client_id, scopes, resource, family, expires) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_hash(token), kind, client_id, json.dumps(scopes), resource, family, now + ttl),
                )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self.db.tx() as db:
            deleted = db.execute(
                "DELETE FROM codes WHERE hash = ?", (_hash(authorization_code.code),)
            ).rowcount
        if not deleted:  # already used: one-time codes
            raise TokenError("invalid_grant", "Authorization code was already used.")
        return self._issue(
            client.client_id, authorization_code.scopes, authorization_code.resource, secrets.token_hex(16)
        )

    def _load(self, token: str, kind: str) -> tuple[str, list[str], str | None, str, float] | None:
        with self.db.tx() as db:
            row = db.execute(
                "SELECT client_id, scopes, resource, family, expires FROM tokens "
                "WHERE hash = ? AND kind = ? AND expires >= ?",
                (_hash(token), kind, self.clock()),
            ).fetchone()
        if not row:
            return None
        return row[0], json.loads(row[1]), row[2], row[3], row[4]

    def _revoke_family(self, family: str) -> None:
        with self.db.tx() as db:
            db.execute("DELETE FROM tokens WHERE family = ?", (family,))

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._load(refresh_token, "refresh")
        if row is None:
            replayed = self._load(refresh_token, "refresh_used")
            if replayed is not None:
                # A rotated-out refresh token came back: someone holds a copy.
                # End the whole grant so the stolen copy and its successors stop working.
                log.warning("refresh token reuse detected; grant revoked")
                self._revoke_family(replayed[3])
            return None
        if row[0] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row[0],
            scopes=row[1],
            expires_at=int(row[4]),
            resource=row[2],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self._load(refresh_token.token, "refresh")
        if row is None:
            raise TokenError("invalid_grant", "Refresh token is invalid or expired.")
        granted = row[1]
        if scopes and not set(scopes) <= set(granted):
            raise TokenError("invalid_scope", "Requested scopes exceed the original grant.")
        with self.db.tx() as db:  # rotate: the old refresh token stops working (kept to spot replays)
            rotated = db.execute(
                "UPDATE tokens SET kind = 'refresh_used' WHERE hash = ? AND kind = 'refresh'",
                (_hash(refresh_token.token),),
            ).rowcount
        if not rotated:  # a concurrent request already used it
            raise TokenError("invalid_grant", "Refresh token is invalid or expired.")
        return self._issue(client.client_id, scopes or granted, row[2], row[3])

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row[0],
            scopes=row[1],
            expires_at=int(row[4]),
            resource=row[2],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.db.tx() as db:
            row = db.execute("SELECT family FROM tokens WHERE hash = ?", (_hash(token.token),)).fetchone()
        if row:  # revoke the whole grant (access + refresh)
            self._revoke_family(row[0])

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: Any
    ) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Identity assertions are not supported.")


# ---------------------------------------------------------------- login page
class LoginLimiter:
    """Slow down password guessing: per-IP and global failure windows."""

    def __init__(self, per_ip: int = 5, overall: int = 20, window: float = 15 * 60) -> None:
        self.per_ip, self.overall, self.window = per_ip, overall, window
        self._fails: deque[tuple[float, str]] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._fails and self._fails[0][0] < now - self.window:
            self._fails.popleft()

    def blocked(self, ip: str, now: float) -> bool:
        with self._lock:
            self._trim(now)
            mine = sum(1 for _, i in self._fails if i == ip)
            return mine >= self.per_ip or len(self._fails) >= self.overall

    def fail(self, ip: str, now: float) -> None:
        with self._lock:
            self._fails.append((now, ip))


_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=31536000",  # ignored by browsers over plain http
}


@dataclass(frozen=True)
class _Consent:
    """What the owner is asked to approve on the login page."""

    request_id: str
    client_name: str
    destination: str  # host the sign-in code is sent to

    @classmethod
    def of(cls, request_id: str, client: OAuthClientInformationFull, params: AuthorizationParams) -> _Consent:
        name = " ".join((client.client_name or "Claude").split())[:MAX_CLIENT_NAME] or "Claude"
        return cls(request_id, name, urlparse(str(params.redirect_uri)).netloc)


def _login_page(consent: _Consent, error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Apple Calendar</title>
<style>
 :root {{ color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; }}
 body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
 main {{ width: min(380px, 100% - 32px); }}
 h1 {{ font-size: 1.3rem; margin: 0 0 .5rem; }}
 p {{ line-height: 1.45; opacity: .85; }}
 input {{ width: 100%; box-sizing: border-box; padding: .7rem; font-size: 1rem; border-radius: 8px;
         border: 1px solid #8886; margin: .75rem 0; }}
 .row {{ display: flex; gap: .5rem; }}
 button {{ flex: 1; padding: .7rem; font-size: 1rem; border-radius: 8px; border: 0; cursor: pointer; }}
 .ok {{ background: #0a84ff; color: white; }} .no {{ background: #8883; color: inherit; }}
 .err {{ color: #ff453a; opacity: 1; }}
 .dest {{ font-size: .9rem; }}
</style></head><body><main>
<h1>Connect Apple Calendar</h1>
<p><strong>{html.escape(consent.client_name)}</strong> wants to read and change events in your calendar.
Enter this connector's password to allow it.</p>
<p class="dest">After you allow it, you'll go to <strong>{html.escape(consent.destination)}</strong>.
Only continue if you started connecting there.</p>{err}
<form method="post" action="/login">
 <input type="hidden" name="request" value="{html.escape(consent.request_id)}">
 <input type="password" name="password" autocomplete="current-password" placeholder="Connector password" autofocus>
 <div class="row"><button class="no" name="action" value="deny">Deny</button>
 <button class="ok" name="action" value="allow">Allow</button></div>
</form></main></body></html>"""
    return HTMLResponse(body, status_code=status, headers=_SECURITY_HEADERS)


def _message(text: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Apple Calendar</title><p>{html.escape(text)}</p>",
        status_code=status,
        headers=_SECURITY_HEADERS,
    )


@dataclass
class RemoteServer:
    mcp: MCPServer
    provider: OwnerAuthProvider
    config: RemoteConfig
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    def app(self) -> Starlette:
        public = urlparse(self.config.public_url)
        hosts = {public.netloc, f"{self.config.host}:{self.config.port}", f"localhost:{self.config.port}"}
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=[f"{public.scheme}://{public.netloc}", "https://claude.ai", "https://claude.com"],
        )
        return self.mcp.streamable_http_app(transport_security=security, host=self.config.host)


Handler = Callable[[Request], Awaitable[Response]]


def _route(mcp: MCPServer, path: str, method: str) -> Callable[[Handler], Handler]:
    """Typed wrapper around the SDK's (untyped) custom_route decorator."""
    return cast(
        Callable[[Handler], Handler], mcp.custom_route(path, methods=[method], include_in_schema=False)
    )


def build_remote(
    service: CalendarService,
    config: RemoteConfig,
    *,
    read_only: bool = False,
    clock: Callable[[], float] = time.time,
) -> RemoteServer:
    db = AuthDB(config.data_dir / "auth.db")
    db.purge_expired()
    provider = OwnerAuthProvider(config, db, clock=clock)
    auth = AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=f"{config.public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=False,
    )
    mcp = build_server(service, read_only=read_only, auth_server_provider=provider, auth=auth)
    server = RemoteServer(mcp=mcp, provider=provider, config=config)
    limiter = server.limiter

    expired = "This sign-in link has expired. Start connecting again from Claude."

    @_route(mcp, "/login", "GET")
    async def login_form(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        found = provider.pending(request_id)
        if found is None:
            return _message(expired, 400)
        return _login_page(_Consent.of(request_id, *found))

    @_route(mcp, "/login", "POST")
    async def login_submit(request: Request) -> Response:
        # The form is three short fields; refuse anything bigger before parsing it.
        size = request.headers.get("content-length", "")
        if not size.isdigit() or int(size) > MAX_FORM_BYTES:
            return _message("Request too large.", 413)
        if request.headers.get("content-type", "").split(";")[0].strip() != (
            "application/x-www-form-urlencoded"
        ):
            return _message("Unsupported form.", 415)
        form = await request.form()
        request_id = str(form.get("request", ""))
        found = provider.pending(request_id)
        if found is None:
            return _message(expired, 400)
        consent = _Consent.of(request_id, *found)
        if form.get("action") == "deny":
            target = provider.deny(request_id)
            return RedirectResponse(target or "/", status_code=303)
        ip = request.client.host if request.client else "?"
        now = clock()
        if limiter.blocked(ip, now):
            return _login_page(consent, "Too many attempts. Wait 15 minutes.", 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), config.password.encode()):
            limiter.fail(ip, now)
            log.warning("failed connector login")
            return _login_page(consent, "Wrong password.", 401)
        target = provider.approve(request_id)
        log.info("connector authorised")
        return RedirectResponse(target or "/", status_code=303)

    @_route(mcp, "/healthz", "GET")
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    return server


def serve(
    service: CalendarService, config: RemoteConfig, *, read_only: bool = False
) -> None:  # pragma: no cover
    import uvicorn

    if not config.public_url.startswith("https://") and not config.is_local:
        log.warning("PUBLIC_URL is not https; claude.ai requires https. Put the server behind TLS.")
    remote = build_remote(service, config, read_only=read_only)
    log.info("serving MCP at %s/mcp", config.public_url)
    uvicorn.run(
        remote.app(),
        host=config.host,
        port=config.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level="info",
    )
