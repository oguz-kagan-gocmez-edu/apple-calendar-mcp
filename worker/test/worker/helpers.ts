/** OAuth client helpers for the workerd tests: register, sign in, get tokens, call MCP. */
import { SELF } from "cloudflare:test";

export const BASE = "https://calendar.example.workers.dev";
export const CLAUDE = "https://claude.ai/api/mcp/auth_callback";
export const PASSWORD = "correct horse battery";

let ipCounter = 0;
/** A fresh client IP per call, so lockout counters don't leak between tests. */
export const freshIp = (): string => `198.51.100.${++ipCounter}`;

export function req(path: string, init: RequestInit = {}): Promise<Response> {
  return SELF.fetch(`${BASE}${path}`, { redirect: "manual", ...init });
}

function b64url(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = "";
  for (const b of arr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function register(
  redirectUris: string[] = [CLAUDE],
  clientName = "Claude",
): Promise<Response> {
  return req("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      redirect_uris: redirectUris,
      client_name: clientName,
      token_endpoint_auth_method: "none",
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
    }),
  });
}

export async function clientId(clientName = "Claude"): Promise<string> {
  const res = await register([CLAUDE], clientName);
  if (res.status !== 201) throw new Error(`register: ${res.status} ${await res.text()}`);
  return ((await res.json()) as { client_id: string }).client_id;
}

export interface Flow {
  clientId: string;
  verifier: string;
  state: string;
  url: string; // the /authorize path with its query
}

export async function startFlow(name = "Claude"): Promise<Flow> {
  const id = await clientId(name);
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(12)));
  const q = new URLSearchParams({
    response_type: "code",
    client_id: id,
    redirect_uri: CLAUDE,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
    scope: "calendar",
    resource: `${BASE}/mcp`,
  });
  return { clientId: id, verifier, state, url: `/authorize?${q}` };
}

export interface LoginPage {
  res: Response;
  html: string;
  handle: string;
  cookie: string;
}

export async function openLogin(flow: Flow): Promise<LoginPage> {
  const res = await req(flow.url);
  const html = await res.text();
  const handle = /name="handle" value="([^"]+)"/.exec(html)?.[1] ?? "";
  const cookie = res.headers
    .getSetCookie()
    .map((c) => c.split(";")[0])
    .join("; ");
  return { res, html, handle, cookie };
}

export function submit(
  flow: Flow,
  page: LoginPage,
  fields: Record<string, string>,
  ip = freshIp(),
): Promise<Response> {
  const body = new URLSearchParams({ handle: page.handle, ...fields }).toString();
  return req(flow.url, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "Content-Length": String(body.length),
      Cookie: page.cookie,
      "CF-Connecting-IP": ip,
    },
    body,
  });
}

/** Sign in and return the authorization code. */
export async function authorize(flow: Flow): Promise<string> {
  const page = await openLogin(flow);
  const res = await submit(flow, page, { password: PASSWORD, action: "allow" });
  if (res.status !== 303) throw new Error(`login: ${res.status} ${await res.text()}`);
  const location = new URL(res.headers.get("location")!);
  return location.searchParams.get("code")!;
}

export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  scope: string;
}

export function token(fields: Record<string, string>): Promise<Response> {
  return req("/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields).toString(),
  });
}

export function exchange(flow: Flow, code: string): Promise<Response> {
  return token({
    grant_type: "authorization_code",
    code,
    redirect_uri: CLAUDE,
    client_id: flow.clientId,
    code_verifier: flow.verifier,
    resource: `${BASE}/mcp`,
  });
}

export async function signIn(): Promise<{ flow: Flow; tokens: Tokens }> {
  const flow = await startFlow();
  const res = await exchange(flow, await authorize(flow));
  if (res.status !== 200) throw new Error(`token: ${res.status} ${await res.text()}`);
  return { flow, tokens: (await res.json()) as Tokens };
}

// ------------------------------------------------------------------ MCP
let rpcId = 0;

/** One JSON-RPC call to /mcp; returns the parsed result (SSE or JSON response). */
export async function mcp(
  accessToken: string,
  method: string,
  params: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): Promise<{ status: number; body: { result?: Record<string, unknown>; error?: unknown } | null }> {
  const res = await req("/mcp", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": "2025-06-18",
      Host: new URL(BASE).host, // set by Cloudflare's edge in production
      ...headers,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
  });
  const text = await res.text();
  if (res.status !== 200) return { status: res.status, body: null };
  const json = (res.headers.get("content-type") ?? "").includes("text/event-stream")
    ? text
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .pop()!
    : text;
  return { status: res.status, body: JSON.parse(json) };
}

export async function callTool(
  accessToken: string,
  name: string,
  args: Record<string, unknown> = {},
): Promise<{ isError?: boolean; structuredContent?: Record<string, unknown>; content: { text: string }[] }> {
  const { body } = await mcp(accessToken, "tools/call", { name, arguments: args });
  return body!.result as never;
}
