/**
 * The OAuth flow, sign-in page and MCP endpoint inside workerd, mirroring
 * tests/test_remote_auth.py for the Python connector.
 */
import { createExecutionContext, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import worker from "../../src/index";
import type { Env } from "../../src/env";
import { LOCKOUT } from "../../src/auth";
import {
  BASE,
  CLAUDE,
  PASSWORD,
  authorize,
  callTool,
  exchange,
  freshIp,
  mcp,
  openLogin,
  register,
  req,
  signIn,
  startFlow,
  submit,
  token,
  type Tokens,
} from "./helpers";

// ------------------------------------------------------------------ discovery
describe("discovery", () => {
  it("advertises the endpoints", async () => {
    const meta = (await (await req("/.well-known/oauth-authorization-server")).json()) as Record<string, unknown>;
    expect(String(meta.issuer).replace(/\/$/, "")).toBe(BASE);
    expect(meta.authorization_endpoint).toBe(`${BASE}/authorize`);
    expect(meta.registration_endpoint).toBe(`${BASE}/register`);
    expect(meta.code_challenge_methods_supported).toContain("S256");
    const prm = await req("/.well-known/oauth-protected-resource/mcp");
    expect(prm.status).toBe(200);
    const body = (await prm.json()) as Record<string, unknown>;
    expect(body.resource).toBe(`${BASE}/mcp`);
    expect(body.scopes_supported).toEqual(["calendar"]);
  });

  it("requires a token for MCP", async () => {
    const none = await req("/mcp", { method: "POST", body: "{}" });
    expect(none.status).toBe(401);
    expect(none.headers.get("www-authenticate")).toContain("resource_metadata");
    const bad = await mcp("nope", "ping");
    expect(bad.status).toBe(401);
  });

  it("serves a public health check", async () => {
    expect(await (await req("/healthz")).json()).toEqual({ ok: true });
  });
});

// ------------------------------------------------------------------ registration
describe("registration", () => {
  it("accepts Claude's redirect URIs", async () => {
    expect((await register()).status).toBe(201);
    expect((await register(["https://claude.com/api/mcp/auth_callback"])).status).toBe(201);
  });

  it.each([["https://evil.example/cb"], ["http://localhost:6274/oauth/callback"], [CLAUDE, "https://evil.example/cb"]])(
    "rejects other redirect URIs %#",
    async (...uris) => {
      const res = await register(uris);
      expect(res.status).toBe(400);
      expect(((await res.json()) as { error: string }).error).toBe("invalid_redirect_uri");
    },
  );
});

describe("rate limiting", () => {
  it("caps /register per client IP", async () => {
    const ip = freshIp();
    const statuses: number[] = [];
    for (let i = 0; i < 12; i++) {
      const res = await req("/register", {
        method: "POST",
        headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
        body: JSON.stringify({ redirect_uris: [CLAUDE], token_endpoint_auth_method: "none" }),
      });
      statuses.push(res.status);
    }
    expect(statuses.slice(0, 10).every((s) => s === 201)).toBe(true);
    expect(statuses.slice(10)).toEqual([429, 429]);
    // Other clients are unaffected.
    expect((await register()).status).toBe(201);
  });

  it("caps /authorize per client IP", async () => {
    const flow = await startFlow();
    const ip = freshIp();
    const statuses: number[] = [];
    for (let i = 0; i < 11; i++) statuses.push((await req(flow.url, { headers: { "CF-Connecting-IP": ip } })).status);
    expect(statuses.slice(0, 10).every((s) => s === 200)).toBe(true);
    expect(statuses[10]).toBe(429);
  });
});

// ------------------------------------------------------------------ login page
describe("login page", () => {
  it("escapes the client name and shows where the code goes", async () => {
    const flow = await startFlow('<script>alert("x")</script>');
    const page = await openLogin(flow);
    expect(page.res.status).toBe(200);
    expect(page.html).not.toContain("<script>");
    expect(page.html).toContain("&lt;script&gt;");
    expect(page.html).toContain("<strong>claude.ai</strong>");
    const csp = page.res.headers.get("content-security-policy")!;
    expect(csp).toContain("form-action 'self' https://claude.ai");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(page.res.headers.get("x-frame-options")).toBe("DENY");
    expect(page.res.headers.get("cache-control")).toBe("no-store");
  });

  it("truncates long client names", async () => {
    const page = await openLogin(await startFlow("A".repeat(500)));
    expect(page.html).toContain(`<strong>${"A".repeat(80)}</strong>`);
    expect(page.html).not.toContain("A".repeat(81));
  });

  it("rejects unknown clients and bad redirect URIs locally", async () => {
    expect((await req("/authorize?response_type=code&client_id=nope&redirect_uri=x")).status).toBe(400);
    const flow = await startFlow();
    const bad = flow.url.replace(encodeURIComponent(CLAUDE), encodeURIComponent("https://evil.example/cb"));
    const res = await req(bad);
    expect(res.status).toBe(400);
    expect(res.headers.get("location")).toBeNull();
  });

  it("refuses oversized and non-form posts", async () => {
    const flow = await startFlow();
    const big = await req(flow.url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `password=${"x".repeat(10_000)}`,
    });
    expect(big.status).toBe(413);
    const json = await req(flow.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    expect(json.status).toBe(415);
  });

  it("rate limits wrong passwords per IP", async () => {
    const flow = await startFlow();
    const page = await openLogin(flow);
    const ip = freshIp();
    for (let i = 0; i < LOCKOUT.perIp; i++) {
      const res = await submit(flow, page, { password: "wrong password!", action: "allow" }, ip);
      expect(res.status).toBe(401);
      expect(await res.text()).toContain("Wrong password.");
    }
    const blocked = await submit(flow, page, { password: PASSWORD, action: "allow" }, ip);
    expect(blocked.status).toBe(429);
    // Another IP can still sign in with the same, still valid, page.
    const ok = await submit(flow, page, { password: PASSWORD, action: "allow" });
    expect(ok.status).toBe(303);
  });

  it("denies back to the client with access_denied", async () => {
    const flow = await startFlow();
    const res = await submit(flow, await openLogin(flow), { action: "deny" });
    expect(res.status).toBe(303);
    const to = new URL(res.headers.get("location")!);
    expect(`${to.origin}${to.pathname}`).toBe(CLAUDE);
    expect(to.searchParams.get("error")).toBe("access_denied");
    expect(to.searchParams.get("state")).toBe(flow.state);
  });

  it("needs the browser that opened the page", async () => {
    const flow = await startFlow();
    const page = await openLogin(flow);
    const res = await submit(flow, { ...page, cookie: "" }, { password: PASSWORD, action: "allow" });
    expect(res.status).toBe(400);
  });

  it("uses each page once", async () => {
    const flow = await startFlow();
    const page = await openLogin(flow);
    expect((await submit(flow, page, { password: PASSWORD, action: "allow" })).status).toBe(303);
    expect((await submit(flow, page, { password: PASSWORD, action: "allow" })).status).toBe(400);
  });
});

// ------------------------------------------------------------------ tokens
describe("tokens", () => {
  it("issues tokens for the calendar scope", async () => {
    const { tokens } = await signIn();
    expect(tokens.scope).toBe("calendar");
    expect(tokens.expires_in).toBe(3600);
    expect(tokens.refresh_token).toBeTruthy();
  });

  it("uses each code once", async () => {
    const flow = await startFlow();
    const code = await authorize(flow);
    expect((await exchange(flow, code)).status).toBe(200);
    const again = await exchange(flow, code);
    expect(again.status).toBe(400);
    expect(((await again.json()) as { error: string }).error).toBe("invalid_grant");
  });

  it("checks the PKCE verifier", async () => {
    const flow = await startFlow();
    const code = await authorize(flow);
    expect((await exchange({ ...flow, verifier: "x".repeat(43) }, code)).status).toBe(400);
  });

  it("rotates refresh tokens and revokes the grant", async () => {
    const { flow, tokens } = await signIn();
    const refresh = (rt: string) => token({ grant_type: "refresh_token", refresh_token: rt, client_id: flow.clientId });

    const res = await refresh(tokens.refresh_token);
    expect(res.status).toBe(200);
    const next = (await res.json()) as Tokens;
    expect(next.refresh_token).not.toBe(tokens.refresh_token);
    expect((await mcp(next.access_token, "ping")).status).toBe(200);

    // Once the new refresh token is used, the old one is dead.
    const third = (await (await refresh(next.refresh_token)).json()) as Tokens;
    expect((await refresh(tokens.refresh_token)).status).toBe(400);

    const meta = (await (await req("/.well-known/oauth-authorization-server")).json()) as { revocation_endpoint: string };
    const revoke = await req(new URL(meta.revocation_endpoint).pathname, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: third.refresh_token, client_id: flow.clientId }).toString(),
    });
    expect(revoke.status).toBe(200);
    expect((await mcp(third.access_token, "ping")).status).toBe(401);
    expect((await refresh(third.refresh_token)).status).toBe(400);
  });
});

// ------------------------------------------------------------------ MCP
describe("mcp", () => {
  it("lists the tools and runs them with a token", async () => {
    const { tokens } = await signIn();
    const init = await mcp(tokens.access_token, "initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "test", version: "1" },
    });
    expect(init.status).toBe(200);
    expect(init.body!.result!.instructions).toContain("get_time_context");

    const list = await mcp(tokens.access_token, "tools/list");
    const tools = (list.body!.result!.tools as { name: string; annotations: Record<string, unknown> }[]).map(
      (t) => [t.name, t.annotations.readOnlyHint],
    );
    expect(Object.fromEntries(tools)).toEqual({
      get_time_context: true,
      list_calendars: true,
      list_events: true,
      search_events: true,
      get_event: true,
      find_free_slots: true,
      create_event: false,
      update_event: false,
      delete_event: false,
    });

    const created = await callTool(tokens.access_token, "create_event", {
      title: "Worker test",
      start: "2030-01-07T10:00",
      duration_minutes: 30,
    });
    expect(created.isError).toBeFalsy();
    const event = created.structuredContent!;
    expect(event.start).toBe("2030-01-07T10:00:00+03:00");
    expect(event.end).toBe("2030-01-07T10:30:00+03:00");

    const listed = await callTool(tokens.access_token, "list_events", { start: "2030-01-07", end: "2030-01-07" });
    const out = listed.structuredContent!;
    expect(out.timezone).toBe("Europe/Istanbul");
    expect((out.events as { id: string }[]).map((e) => e.id)).toContain(event.id);

    const cals = await callTool(tokens.access_token, "list_calendars");
    expect((cals.structuredContent!.result as { id: string }[]).map((c) => c.id)).toEqual(["home", "work"]);

    const bad = await callTool(tokens.access_token, "create_event", { title: "X", start: "not a date" });
    expect(bad.isError).toBe(true);
    expect(bad.content[0]!.text).toContain("start");

    const deleted = await callTool(tokens.access_token, "delete_event", { event_id: event.id });
    expect(deleted.structuredContent).toMatchObject({ deleted: true, title: "Worker test" });
  });

  it("refuses browser origins other than Claude's", async () => {
    const { tokens } = await signIn();
    const res = await mcp(tokens.access_token, "ping", {}, { Origin: "https://evil.example" });
    expect(res.status).toBe(403);
    expect((await mcp(tokens.access_token, "ping", {}, { Origin: "https://claude.ai" })).status).toBe(200);
  });
});

// ------------------------------------------------------------------ config
describe("config", () => {
  const call = (overrides: Partial<Env>) =>
    worker.fetch(new Request(`${BASE}/healthz`), { ...(env as unknown as Env), ...overrides }, createExecutionContext());

  it.each([undefined, "short", "change-me-to-a-long-random-string"])("refuses to run with password %s", async (pw) => {
    const res = await call({ CONNECTOR_PASSWORD: pw });
    expect(res.status).toBe(503);
    expect(await res.text()).not.toContain(pw ?? "CONNECTOR_PASSWORD");
  });

  it("refuses an unknown time zone", async () => {
    expect((await call({ TZ: "Mars/Olympus" })).status).toBe(503);
  });
});
