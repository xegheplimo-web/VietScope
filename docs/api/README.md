# VietScope — Public API (`/v1`)

Self-hosted search, answer, and verification API for external clients
(agents, third parties). Served by the `search-router` FastAPI service.

- **Base URL (local)**: `http://localhost:8888`
- **Base URL (production)**: `https://search.duyai.app` — Nginx proxies **only
  `/v1/*`**; all other paths return `404` (`deploy/nginx/search-hub.conf`).
- **OpenAPI spec**: [`openapi.yaml`](./openapi.yaml)
- **Examples**: [`examples.md`](./examples.md)
- **Onboarding**: [`getting-started.md`](./getting-started.md)

## Architecture

```
Internet / LAN clients (Hermes · Vane · 3rd-party)
        │  Authorization: Bearer dsa_live_…
        ▼
   Cloudflare (optional) ──► Nginx :443  ── only /v1/* proxied,
        │                     10 r/s per IP, burst 20            else 404
        ▼
┌─────────────────────────── SEARCH ROUTER :8888 ───────────────────────────┐
│  require_api_key (router-wide dependency on /v1)                          │
│    key lookup → scope check → per-key rate limit → daily quota            │
│    hub-postgres (api_keys, usage_daily, query_logs)                       │
│    hub-redis    (sliding-window rate limit, response caches)              │
│                                                                           │
│  /v1/search ──┬─ no mode: SearXNG → DDGS fallback → raw results           │
│               └─ mode:    research pipeline (plan → search → RRF rerank → │
│                           scrape → passages → gaps → synthesize → verify) │
│                           + hybrid lane: OpenSearch BM25 ∥ Qdrant dense   │
│                             → RRF fusion (P11-T2, additive)               │
│  /v1/answer ── research pipeline → answer + sources + citations           │
│               (stream=true → SSE)                                         │
│  /v1/research ── multi-hop orchestrator → EvidencePack                    │
│               (/v1/research/stream → SSE)                                 │
│  /v1/news · /v1/images · /v1/read · /v1/verify · /v1/business/search      │
└───────────────────────────────────────────────────────────────────────────┘
        │              │               │              │            │
        ▼              ▼               ▼              ▼            ▼
    SearXNG :8080  Firecrawl :3002  DDGS        OpenSearch    Qdrant :6333
    (metasearch)   (scrape/read)   (fallback)   BM25 :9200    (dense lane)
        │
        └── engines: Brave, Google CSE, Wikipedia, DuckDuckGo, …
```

Supporting services (internal, loopback-only — never proxied): embedding
service `:8892`, reranker service `:8891`, OpenSearch `:9200/:9600`, Qdrant
`:6333/:6334`, hub-redis, hub-postgres.

## Authentication

All `POST /v1/*` endpoints require an API key when `API_AUTH_ENABLED=true`
(default is `false` for local/dev — the dependency then passes through and no
key is needed). Send the key as a Bearer token:

```
Authorization: Bearer dsa_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Key format

```
dsa_live_<32 urlsafe chars>     # production key
dsa_test_<32 urlsafe chars>     # test key
```

Keys are generated with `secrets.token_urlsafe(24)`. Only `sha256(key)` is
stored in `hub-postgres.api_keys` — the plaintext key is shown **once** at
creation (`manage_keys.py create`) and cannot be recovered.

### Scopes

Each endpoint maps to a required scope (`security/apikeys.py::_SCOPE_MAP`).
A key's `scopes` list must contain the required scope, or the wildcard `*`
(which covers everything, including `admin:*`). The `admin:*` wildcard covers
only admin scopes — it does **not** grant product scopes like `search:read`.

| Endpoint | Method | Required scope |
|---|---|---|
| `/v1/search` | POST | `search:read` |
| `/v1/answer` | POST | `answer:use` |
| `/v1/research` | POST | `research:use` |
| `/v1/research/stream` | POST | `research:use` |
| `/v1/news` | POST | `news:use` |
| `/v1/images` | POST | `images:use` |
| `/v1/read` | POST | `read:use` |
| `/v1/evidence` | POST | `read:use` |
| `/v1/verify` | POST | `verify:use` |
| `/v1/business/search` | POST | `business:use` |
| `/v1/health` | GET | *public* — detail fields gated by `admin:debug` |
| `/v1/capabilities` | GET | *public* |
| `/v1/auth/check` | GET | *public* — body reports `authenticated`/`tenant`/`scopes` of the presented key (credential probe; never 401/403) |
| `/v1` | GET | *public* |
| `/v1/providers` | GET | `admin:debug` |
| any other `GET /v1/*` | GET | `admin:debug` |
| any unmapped `POST /v1/*` | POST | `admin:debug` |

Key management is CLI-only (no self-service endpoint):

```powershell
python search-router/manage_keys.py create --tenant acme --name "ci bot" `
    --scopes "search:read,answer:use" --rpm 60 --quota 1000
python search-router/manage_keys.py list --tenant acme
python search-router/manage_keys.py revoke --key-id <uuid>   # or --prefix dsa_live_Ab3
```

## Rate limits & quota

Two independent limits per key, enforced inside `require_api_key` **after** the
scope check:

| Limit | Source | Exceeded → |
|---|---|---|
| Requests/minute | `api_keys.rpm_limit` (default 60) | `429 rate_limit_exceeded` |
| Requests/day | `api_keys.daily_quota` (default 1000; `-1` = unlimited) | `429 daily_quota_exceeded` |

- **Per-minute limit** — sliding 60-second window keyed `api:<key_id>`,
  implemented as a Redis sorted set on `hub-redis`. If Redis is down the
  limiter falls back to an in-process sliding window (never fails open/closed
  — it keeps enforcing locally).
- **Daily quota** — `SUM(requests)` from `usage_daily` for the current UTC day.
  If hub-postgres is unreachable the quota check is skipped (fail-open on
  quota only), but key lookup still fails closed with `503`.
- **Edge layer** — Nginx adds a separate per-IP limit: `10 r/s`, `burst=20`
  (`limit_req_zone sh_api`), independent of per-key limits.
- **Metering** — every `/v1/*` request is logged to `query_logs` and
  `usage_daily` (fire-and-forget; the query body is stored only as a
  truncated SHA-256 hash, never plaintext).

## Error codes

Error bodies use FastAPI's `detail` envelope — auth/scope/limit errors return
a JSON object inside `detail`:

| Status | `detail.error` | Meaning |
|---|---|---|
| `401` | `missing_or_invalid_api_key` | Missing `Authorization`, bad prefix, unknown or revoked key. `WWW-Authenticate: Bearer` header is set. |
| `403` | `insufficient_scope` | Key valid but lacks the required scope. `detail.required` names it. |
| `429` | `rate_limit_exceeded` | Per-minute limit hit. `detail.rpm` = the key's limit. |
| `429` | `daily_quota_exceeded` | Daily quota exhausted. `detail.quota` = the key's quota. |
| `503` | `auth_backend_unavailable` | Auth enabled but hub-postgres unreachable — fails closed. |
| `422` | *(FastAPI validation)* | Invalid request body (also SSRF-blocked URLs on `/v1/read` — `detail` is a string starting `URL blocked by SSRF policy:`). |

Example `403` body:

```json
{"detail": {"error": "insufficient_scope", "required": "research:use"}}
```

## SSE streaming protocol

`POST /v1/answer` with `"stream": true` and `POST /v1/research/stream` return
`text/event-stream`. Wire format per event:

```
event: <name>
data: <one-line JSON>

```

Response headers: `Cache-Control: no-cache`, `Connection: keep-alive`,
`X-Accel-Buffering: no` (safe behind Nginx — `proxy_buffering off` is set).

### `/v1/answer` stream (canonical events)

Emitted by `_canonical_answer_events` — the canonical tail order is
`source` → `answer.delta` → `citation` → `done`:

| Event | Data | When |
|---|---|---|
| `init` | `{query, query_id, mode}` | First event, always |
| `source` | `{title, url, score, domain}` | One per source, before answer text |
| `answer.delta` | `{text}` | Answer chunked into ~600-char deltas |
| `citation` | `{claim, verified, evidence_count}` | One per claim, only when `citations=true` |
| `done` | `{query_id, coverage, verified, timings}` | Terminal event, always |
| `warning` | `{message}` | Pipeline error — followed by `done` with `verified: false` |

### `/v1/research/stream` events

Emits the orchestrator's progress events, each forwarded verbatim; selected
events are *additionally* duplicated under a canonical alias
(`_SSE_EVENT_MAP`: `planning`→`plan`, `followup`→`research.round`,
`verifying`→`evidence`, `error`→`warning`):

| Event | Data | When |
|---|---|---|
| `init` | `{query, mode}` | First event |
| `planning` (+ `plan`) | `{query, mode, max_hops, sub_queries, intent, freshness_required, budget}` | Query decomposition done |
| `searching` | `{query, providers, freshness}` | Once per sub-query |
| `fetching` | `{to_fetch, total_sources}` | Before Firecrawl scrape |
| `fetched` | `{fetched, failed}` | After scrape |
| `followup` (+ `research.round`) | `{round, query, reason}` | Gap-driven follow-up round |
| `verifying` | `{claims, sources}` | Claim extraction/verification starting |
| `evidence` | `{claims, sources}` | Evidence pack assembled |
| `source` | full `Source` object | One per source |
| `answer.delta` | `{text}` | ~600-char answer chunks |
| `answer` | `{pack: EvidencePack}` | Final evidence pack |
| `done` | `{claims, sources, coverage}` | Terminal event |
| `error` (+ `warning`) | `{message}` | Fatal error — stream ends |

Clients should treat unknown event names as forward-compatible and read until
`done`/`error`.

## Conversation context (P4)

`POST /v1/search` (with `mode`) and `POST /v1/answer` (JSON + SSE) keep a
per-session state so follow-up questions work:

- **`session_id`** (`^[A-Za-z0-9_-]{1,64}$`) — loads/stores server-side
  state under Redis `convctx:{owner_hash}:{session_id}` (TTL `86400s`,
  refreshed on write; in-memory fallback when Redis is down, reconciled
  back on recovery). `owner_hash` is a truncated sha256 of the
  authenticated API key id (`anonymous` when auth is off) — a session is
  only ever read or written by the key that owns it. After each answer
  the turn is folded into a compact blob: last query, ≤500-char answer
  summary, resolved entities/locations/constraints, ≤5 recent sources,
  rolling 6-turn window.
- **`history`** — stateless alternative: `[role, text]` turns supplied by
  the client. When both are sent they merge; stored state wins on
  conflicting fields.

When context exists and the query looks like a follow-up (short or
anaphoric — "ông ấy", "chỗ đó", "he", "that place"…; matching is
accent-insensitive, so unaccented Vietnamese like "ong ay" marks too), it
is rewritten to a standalone query by the LLM resolver (≤2s bound) with a
deterministic fallback. The pipeline executes the standalone query.
`session_id` requests always echo `session_id` + `followup_resolved`;
`resolved_query` appears when a rewrite happened. `history`-only requests
get `followup_resolved`/`resolved_query` only on an actual rewrite — a
standalone question carrying history stays byte-identical to a
context-free request. SSE streams carry the same fields on `init`/`done`.
Requests without `session_id`/`history` get the unchanged pre-P4
response shape.

Env: `CONVERSATION_CONTEXT_ENABLED` (default `true`),
`CONVERSATION_CONTEXT_TTL_SECONDS` (`86400`),
`CONVERSATION_CONTEXT_MAX_TURNS` (`6`),
`CONVERSATION_CONTEXT_MAX_SOURCES` (`5`),
`CONVERSATION_CONTEXT_RESOLUTION_TIMEOUT` (`2.0`).

## Endpoints at a glance

| Endpoint | Purpose |
|---|---|
| `POST /v1/search` | Unified search — raw results by default (`type`: `web` \| `news` \| `image` \| `video`); full research pipeline when `mode` is set |
| `POST /v1/answer` | Answer + sources + citations; `stream=true` → SSE |
| `POST /v1/research` | Multi-hop research → `EvidencePack` (claims, citations, coverage, budget) |
| `POST /v1/research/stream` | Same, streamed as SSE progress events |
| `POST /v1/news` | News-category search with freshness handling |
| `POST /v1/images` | Image search — Qdrant corpus (`QDRANT_IMAGES_ENABLED`) first, live web backfill (SearXNG → DDGS) |
| `POST /v1/read` | Fetch a URL → title + chunked passages (SSRF-guarded) |
| `POST /v1/evidence` | Structured sources → bounded-parallel guarded reads → reranked cited passages (provenance preserved) |
| `POST /v1/verify` | Verify claim strings against supplied clusters/sources |
| `POST /v1/business/search` | Local business search (query-resolved anchor → PostGIS → OSM → web extraction) |
| `GET /v1/health` | Liveness; detailed service map with `admin:debug` |
| `GET /v1/capabilities` | Modes, providers, feature flags — for client discovery |
| `GET /v1/providers` | Provider status list (`admin:debug`) |

See [`openapi.yaml`](./openapi.yaml) for request/response schemas.
