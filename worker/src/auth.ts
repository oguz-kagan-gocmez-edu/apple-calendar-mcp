/**
 * The owner's sign-in page (port of remote.py's /login), on top of the
 * OAuth provider's consent helpers:
 *
 * GET  /authorize  validates the request, starts a consent transaction and
 *                  shows the page (client name, where the code goes, password).
 * POST /authorize  Deny: back to the client with access_denied.
 *                  Allow: check the password (rate limited), then issue a code.
 *
 * The page posts back to the same URL, so a wrong password can be shown again
 * without starting over. Nothing here logs the password or tokens.
 */

import { AuthorizationError, type AuthRequest, type OAuthHelpers } from "@cloudflare/workers-oauth-provider";

import type { Env } from "./env";

export const SCOPE = "calendar";
export const MAX_FORM_BYTES = 8 * 1024;
export const MAX_CLIENT_NAME = 80;
export const LOCKOUT = { perIp: 5, overall: 20, windowSeconds: 15 * 60 };
// Values shipped in .env.example / docs: never accept one of these.
export const PLACEHOLDER_PASSWORDS = new Set(["change-me-to-a-long-random-string", "ci-only-password-123"]);

const EXPIRED = "This sign-in link has expired. Start connecting again from Claude.";

/** Why the configured connector password can't be used, or null when it's fine. */
export function passwordProblem(password: string | undefined): string | null {
  if (!password) return "CONNECTOR_PASSWORD is not set. Run: npx wrangler secret put CONNECTOR_PASSWORD";
  if (password.length < 12) return "CONNECTOR_PASSWORD must be at least 12 characters.";
  if (PLACEHOLDER_PASSWORDS.has(password)) return "CONNECTOR_PASSWORD is still the example value. Generate a new one.";
  return null;
}

// ------------------------------------------------------------------ html
const escapeHtml = (s: string): string =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

function securityHeaders(formTarget?: string): Headers {
  // Chrome applies form-action to the redirect after the POST too, so the
  // client's origin (e.g. https://claude.ai) has to be allowed next to 'self'.
  const formAction = ["'self'", formTarget].filter(Boolean).join(" ");
  return new Headers({
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": `default-src 'none'; style-src 'unsafe-inline'; form-action ${formAction}; frame-ancestors 'none'`,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=31536000",
  });
}

export function message(text: string, status: number): Response {
  return new Response(
    `<!doctype html><meta charset=utf-8><title>Apple Calendar</title><p>${escapeHtml(text)}</p>`,
    { status, headers: securityHeaders() },
  );
}

interface Consent {
  clientName: string;
  destination: string; // host the sign-in code is sent to
  origin: string; // its origin, for the CSP
  action: string; // where the form posts (this URL, with the OAuth query)
}

async function consentFor(helpers: OAuthHelpers, request: Request, authRequest: AuthRequest): Promise<Consent> {
  const d = await helpers.describeConsent(authRequest);
  const name = d.clientName.split(/\s+/).filter(Boolean).join(" ").slice(0, MAX_CLIENT_NAME) || "Claude";
  const url = new URL(request.url);
  return {
    clientName: name,
    destination: d.redirectHost,
    origin: new URL(d.redirectUri).origin,
    action: `${url.pathname}${url.search}`,
  };
}

function page(c: Consent, handle: string, extra: Headers | null, error?: string, status = 200): Response {
  const err = error ? `<p class="err">${escapeHtml(error)}</p>` : "";
  const body = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Apple Calendar</title>
<style>
 :root { color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; }
 body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }
 main { width: min(380px, 100% - 32px); }
 h1 { font-size: 1.3rem; margin: 0 0 .5rem; }
 p { line-height: 1.45; opacity: .85; }
 input { width: 100%; box-sizing: border-box; padding: .7rem; font-size: 1rem; border-radius: 8px;
         border: 1px solid #8886; margin: .75rem 0; }
 .row { display: flex; gap: .5rem; }
 button { flex: 1; padding: .7rem; font-size: 1rem; border-radius: 8px; border: 0; cursor: pointer; }
 .ok { background: #0a84ff; color: white; } .no { background: #8883; color: inherit; }
 .err { color: #ff453a; opacity: 1; }
 .dest { font-size: .9rem; }
</style></head><body><main>
<h1>Connect Apple Calendar</h1>
<p><strong>${escapeHtml(c.clientName)}</strong> wants to read and change events in your calendar.
Enter this connector's password to allow it.</p>
<p class="dest">After you allow it, you'll go to <strong>${escapeHtml(c.destination)}</strong>.
Only continue if you started connecting there.</p>${err}
<form method="post" action="${escapeHtml(c.action)}">
 <input type="hidden" name="handle" value="${escapeHtml(handle)}">
 <input type="password" name="password" autocomplete="current-password" placeholder="Connector password" autofocus>
 <div class="row"><button class="no" name="action" value="deny">Deny</button>
 <button class="ok" name="action" value="allow">Allow</button></div>
</form></main></body></html>`;
  const headers = securityHeaders(c.origin);
  extra?.forEach((value, key) => {
    if (key.toLowerCase() === "set-cookie") headers.append(key, value);
  });
  return new Response(body, { status, headers });
}

function redirect(location: string, extra: Headers): Response {
  const headers = new Headers(extra);
  headers.set("Location", location);
  headers.set("Cache-Control", "no-store");
  return new Response(null, { status: 303, headers });
}

/** An invalid authorization request: back to the client when that's safe, else a local message. */
function authError(err: unknown): Response {
  if (err instanceof AuthorizationError) {
    if (err.redirectTo) return redirect(err.redirectTo, new Headers());
    if (err.code === "invalid_request" || err.code === "access_denied") return message(EXPIRED, 400);
  }
  throw err;
}

// ------------------------------------------------------------------ lockout
/**
 * Failed logins in KV: per IP and overall, in a fixed 15-minute window.
 * KV is eventually consistent, so the limit is approximate across regions;
 * it still turns online guessing of a 12+ character password into a non-option.
 */
export class Lockout {
  constructor(
    private readonly kv: KVNamespace,
    private readonly now: () => number = Date.now,
  ) {}

  private window(): number {
    return Math.floor(this.now() / 1000 / LOCKOUT.windowSeconds);
  }

  private keys(ip: string): [string, string] {
    const w = this.window();
    return [`lockout:${w}:ip:${ip}`, `lockout:${w}:all`];
  }

  async blocked(ip: string): Promise<boolean> {
    const [mine, all] = await Promise.all(this.keys(ip).map((k) => this.kv.get(k)));
    return Number(mine ?? 0) >= LOCKOUT.perIp || Number(all ?? 0) >= LOCKOUT.overall;
  }

  async fail(ip: string): Promise<void> {
    await Promise.all(
      this.keys(ip).map(async (k) => {
        const n = Number((await this.kv.get(k)) ?? 0) + 1;
        await this.kv.put(k, String(n), { expirationTtl: LOCKOUT.windowSeconds * 2 });
      }),
    );
  }
}

/** Constant-time comparison: hash both sides, then compare the digests. */
async function samePassword(given: string, expected: string): Promise<boolean> {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all(
    [given, expected].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))),
  );
  return crypto.subtle.timingSafeEqual(a!, b!);
}

// ------------------------------------------------------------------ handlers
export async function showLogin(request: Request, env: Env): Promise<Response> {
  const helpers = env.OAUTH_PROVIDER;
  let authRequest: AuthRequest;
  try {
    authRequest = await helpers.parseAuthRequest(request);
  } catch (err) {
    return authError(err);
  }
  const consent = await consentFor(helpers, request, authRequest);
  const { handle, headers } = await helpers.beginConsent(authRequest);
  return page(consent, handle, headers);
}

export async function submitLogin(request: Request, env: Env, password: string): Promise<Response> {
  // The form is three short fields; refuse anything bigger before parsing it.
  const size = request.headers.get("content-length") ?? "";
  if (!/^\d+$/.test(size) || Number(size) > MAX_FORM_BYTES) return message("Request too large.", 413);
  const type = (request.headers.get("content-type") ?? "").split(";")[0]!.trim();
  if (type !== "application/x-www-form-urlencoded") return message("Unsupported form.", 415);

  const helpers = env.OAUTH_PROVIDER;
  let authRequest: AuthRequest;
  try {
    authRequest = await helpers.parseAuthRequest(request);
  } catch (err) {
    return authError(err);
  }
  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");
  try {
    if (form.get("action") === "deny") {
      const denied = await helpers.denyConsent(request, handle);
      return redirect(denied.redirectTo, denied.headers);
    }
    const consent = await consentFor(helpers, request, authRequest);
    // CF-Connecting-IP is set by Cloudflare's edge; clients can't forge it.
    const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
    const lockout = new Lockout(env.OAUTH_KV);
    if (await lockout.blocked(ip)) return page(consent, handle, null, "Too many attempts. Wait 15 minutes.", 429);
    if (!(await samePassword(String(form.get("password") ?? ""), password))) {
      await lockout.fail(ip);
      console.warn("failed connector login");
      return page(consent, handle, null, "Wrong password.", 401);
    }
    const approved = await helpers.approveConsent(request, handle, { scope: [SCOPE] });
    const { redirectTo } = await helpers.completeAuthorization({
      request: approved.request,
      userId: "owner",
      metadata: {},
      scope: [SCOPE],
      props: {},
    });
    console.log("connector authorised");
    return redirect(redirectTo, approved.headers);
  } catch (err) {
    return authError(err);
  }
}
