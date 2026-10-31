/**
 * Worker entry point: an OAuth-protected MCP server for iCloud Calendar.
 *
 * /mcp                         MCP over streamable HTTP (stateless), needs a token with scope "calendar"
 * /authorize                   the owner's sign-in page (auth.ts)
 * /token, /register, /.well-known/*   served by @cloudflare/workers-oauth-provider
 * /healthz                     liveness
 */

import { OAuthProvider, insufficientScope, type OAuthResourceAuth } from "@cloudflare/workers-oauth-provider";
import { createMcpHandler } from "agents/mcp/server";

import { SCOPE, message, passwordProblem, showLogin, submitLogin } from "./auth";
import { CaldavStore } from "./caldav/store";
import type { Env } from "./env";
import { MemoryStore } from "./memory-store";
import { CalendarService } from "./service";
import type { CalendarStore } from "./store";
import { Zone } from "./time";
import { buildServer } from "./tools";

export const DEFAULT_REDIRECTS = [
  "https://claude.ai/api/mcp/auth_callback",
  "https://claude.com/api/mcp/auth_callback",
];

const ACCESS_TTL = 60 * 60;
const REFRESH_TTL = 30 * 24 * 60 * 60;
const CLIENT_TTL = 90 * 24 * 60 * 60;

// ------------------------------------------------------------------ config
function zoneOf(env: Env): Zone {
  const name = env.TZ || "UTC";
  if (!Zone.isValid(name)) throw new ConfigError(`TZ '${name}' is not a valid IANA time zone.`);
  return new Zone(name);
}

class ConfigError extends Error {}

let memory: MemoryStore | null = null; // per isolate; tests and demos only

function storeOf(env: Env, zone: Zone): CalendarStore {
  if (env.BACKEND === "memory") return (memory ??= new MemoryStore());
  if (!env.CALDAV_USERNAME || !env.CALDAV_PASSWORD) {
    throw new ConfigError("CALDAV_USERNAME and CALDAV_PASSWORD are not set. Use npx wrangler secret put.");
  }
  return new CaldavStore(zone, {
    url: env.CALDAV_URL || undefined,
    username: env.CALDAV_USERNAME,
    password: env.CALDAV_PASSWORD,
    defaultCalendar: env.CALDAV_DEFAULT_CALENDAR || null,
  });
}

function allowedRedirects(env: Env): Set<string> {
  const extra = (env.OAUTH_ALLOWED_REDIRECTS ?? "").split(",").map((u) => u.trim()).filter(Boolean);
  return new Set([...DEFAULT_REDIRECTS, ...extra]);
}

// ------------------------------------------------------------------ handlers
const api: ExportedHandler<Env> = {
  async fetch(request, env, ctx) {
    // The provider checked the token; the scope is ours to enforce.
    const auth = (ctx as ExecutionContext & { auth: OAuthResourceAuth }).auth;
    if (!auth.scope.includes(SCOPE)) return insufficientScope(auth, [SCOPE]);
    const zone = zoneOf(env);
    const service = new CalendarService(storeOf(env, zone), zone);
    const host = new URL(request.url).hostname;
    const handler = createMcpHandler(() => buildServer(service, { readOnly: env.READ_ONLY === "1" }), {
      route: "/mcp",
      allowedHostnames: [host],
      allowedOriginHostnames: [host, "claude.ai", "claude.com"],
    });
    return handler(request, env, ctx);
  },
};

const pages: ExportedHandler<Env> = {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/healthz" && request.method === "GET") return Response.json({ ok: true });
    if (pathname === "/authorize") {
      if (request.method === "GET") return showLogin(request, env);
      if (request.method === "POST") return submitLogin(request, env, env.CONNECTOR_PASSWORD ?? "");
      return new Response(null, { status: 405, headers: { Allow: "GET, POST" } });
    }
    return new Response("Not found", { status: 404 });
  },
};

// One provider per origin (workers.dev, a custom domain, localhost in dev).
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string, env: Env): OAuthProvider<Env> {
  let provider = providers.get(origin);
  if (!provider) {
    const redirects = allowedRedirects(env);
    provider = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: api as never,
      defaultHandler: pages,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/token",
      clientRegistrationEndpoint: "/register",
      accessTokenTTL: ACCESS_TTL,
      refreshTokenTTL: REFRESH_TTL,
      clientRegistrationTTL: CLIENT_TTL,
      scopesSupported: [SCOPE],
      requiredScopes: [SCOPE],
      resourceMetadata: { resource: `${origin}/mcp`, resource_name: "Apple Calendar" },
      clientRegistrationCallback: ({ clientMetadata }) => {
        const uris = clientMetadata.redirect_uris;
        if (!Array.isArray(uris) || !uris.length || !uris.every((u) => typeof u === "string" && redirects.has(u))) {
          return {
            code: "invalid_redirect_uri",
            description:
              "This connector only accepts Claude's redirect URIs. Add others with OAUTH_ALLOWED_REDIRECTS.",
          };
        }
      },
    });
    providers.set(origin, provider);
  }
  return provider;
}

export default {
  async fetch(request, env, ctx) {
    const problem = passwordProblem(env.CONNECTOR_PASSWORD);
    if (problem) {
      console.error(problem);
      return message("This connector isn't set up yet. See the server logs.", 503);
    }
    try {
      zoneOf(env);
      return await providerFor(new URL(request.url).origin, env).fetch(request, env, ctx);
    } catch (err) {
      if (err instanceof ConfigError) {
        console.error(err.message);
        return message("This connector isn't set up yet. See the server logs.", 503);
      }
      throw err;
    }
  },
} satisfies ExportedHandler<Env>;
