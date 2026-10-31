import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

/** Bindings, vars (wrangler.jsonc) and secrets (wrangler secret put). */
export interface Env {
  OAUTH_KV: KVNamespace;
  /** Per-IP limit on the public endpoints that write to KV (/register, /authorize). */
  AUTH_RATE_LIMIT?: RateLimit;
  /** Injected by the OAuth provider. */
  OAUTH_PROVIDER: OAuthHelpers;

  // secrets
  CONNECTOR_PASSWORD?: string;
  CALDAV_USERNAME?: string;
  CALDAV_PASSWORD?: string;

  // vars
  TZ?: string;
  CALDAV_URL?: string;
  CALDAV_DEFAULT_CALENDAR?: string;
  READ_ONLY?: string;
  /** Extra OAuth redirect URIs, comma-separated. */
  OAUTH_ALLOWED_REDIRECTS?: string;
  /** "memory" for tests and demos; anything else means CalDAV. */
  BACKEND?: string;
}
