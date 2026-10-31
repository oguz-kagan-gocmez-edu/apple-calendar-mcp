# apple-calendar-mcp on Cloudflare Workers

The remote connector as a Cloudflare Worker, so it can run on the free plan with no server. It is a TypeScript port of the Python service layer and CalDAV backend: the same nine tools, names, descriptions and output shapes. Setup steps are in the [main README](../README.md#free-on-cloudflare-workers-recommended).

## Layout

| File | Python counterpart | What it does |
|---|---|---|
| `src/index.ts` | `remote.py` | Entry point: OAuth provider, `/mcp`, `/authorize`, `/healthz` |
| `src/auth.ts` | `remote.py` (login page) | Sign-in page, password check, KV lockout |
| `src/tools.ts` | `server.py` | MCP tools and their schemas |
| `src/service.ts` | `service.py` | Input parsing, defaults, search, free slots |
| `src/caldav/client.ts` | `caldav` library | PROPFIND/REPORT/PUT/DELETE over fetch, same-site redirects only |
| `src/caldav/store.ts` | `caldav_store.py` | iCalendar mapping and `this`/`future`/`all` edits |
| `src/caldav/ical.ts` | `recurrence.py` | Parsing, recurrence expansion, VTIMEZONE generation |
| `src/time.ts`, `src/scheduling.ts`, `src/models.ts`, `src/store.ts` | `timeutil.py`, then same names | Time zones, free-slot maths, data shapes, errors |
| `src/memory-store.ts` | `memory_store.py` | One-off events in memory, for tests (`BACKEND=memory`) |

## Commands

```bash
npm ci
npm run typecheck && npm run lint
npm test          # needs uv: the CalDAV tests start a local Radicale server from the Python dev dependencies
npx wrangler dev  # local server on http://localhost:8787; put test secrets in .dev.vars (gitignored)
npx wrangler deploy
```

## Free plan limits

- **10 ms CPU per request.** Waiting on iCloud doesn't count; parsing and recurrence expansion do. `test/node/cpu.test.ts` expands a busy week (42 calendar objects, recurring ones included) and fails above 5 ms. Long-running weekly or monthly series are fast-forwarded instead of stepped through from their first occurrence.
- **100,000 requests and 1,000 KV writes a day.** A sign-in costs a few KV writes; MCP calls cost none.
