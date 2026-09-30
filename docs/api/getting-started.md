# Getting Started — VietScope Search API

For external clients: Hermes, Vane, or any third-party consumer of the
`/v1/*` surface. Five minutes from zero to a streamed answer.

> Local dev note: with `API_AUTH_ENABLED=false` (the default) every `/v1`
> endpoint is reachable without a key — skip to step 3. Keys are required
> once auth is enabled (production / behind the Nginx proxy).

## 0. Prerequisites

```bash
docker compose up -d          # from F:\Search-Hub — starts the whole stack
docker compose ps             # search-router should be healthy on :8888
```

For authenticated calls you also need `hub-postgres` running (it is part of
the compose stack and exposes `127.0.0.1:5433`).

## 1. Health check

```bash
curl http://localhost:8888/v1/health
# → {"status":"ok","version":"3.0.0"}            (public minimal shape)
```

With an `admin:debug` key (or when auth is off) the same endpoint reports
per-service probes — `searxng`, `firecrawl`, `opensearch`, `qdrant`,
`embedding`, `reranker`:

```bash
curl -H "Authorization: Bearer $KEY" http://localhost:8888/v1/health
# → {"status":"ok","service":"search-hub","version":"3.0.0",
#    "llm":"configured","services":{"searxng":"ok","firecrawl":"ok",…}}
```

`status` is `degraded` when any enabled dependency is down — probes are
short-timeout by design, so health never hangs.

## 2. Create an API key

Keys are managed by the operator on the host — there is no self-service
endpoint. From the repo root:

```bash
python search-router/manage_keys.py create \
    --tenant <your-org> --name "<client name>" \
    --scopes "search:read,answer:use" --rpm 60 --quota 1000
```

Output (store `full_key` immediately — only its SHA-256 is persisted):

```
API key created — store it now, it is shown only once:
  key_id   : 5b2c…
  prefix   : dsa_live_Ab3CdE
  tenant   : acme
  full_key : dsa_live_Ab3CdEf…
```

Useful flags: `--test` → `dsa_test_…` key · `--quota -1` → unlimited daily ·
`--scopes "*"` → all scopes. List/revoke:

```bash
python search-router/manage_keys.py list --tenant acme
python search-router/manage_keys.py revoke --key-id <uuid>   # or --prefix dsa_live_Ab3
```

DSN resolution: `--dsn` flag → `HUB_DATABASE_URL` → `HUB_PG_*` vars
(defaults `127.0.0.1:5433`, `searchhub`/`searchhub`).

### Which scopes do you need?

| Your client calls | Grant these scopes |
|---|---|
| `POST /v1/search` | `search:read` |
| `POST /v1/answer` | `answer:use` |
| `POST /v1/research`, `/v1/research/stream` | `research:use` |
| `POST /v1/news` | `news:use` |
| `POST /v1/images` | `images:use` |
| `POST /v1/read`, `/v1/evidence` | `read:use` |
| `POST /v1/verify` | `verify:use` |
| `POST /v1/business/search` | `business:use` |
| `GET /v1/health`, `GET /v1/capabilities` | none — public |
| `GET /v1/providers`, detailed `/v1/health` | `admin:debug` |

Typical read-only agent: `search:read,answer:use,read:use,news:use`.

## 3. First call

```bash
curl -X POST http://localhost:8888/v1/search \
  -H "Authorization: Bearer dsa_live_…" \
  -H "Content-Type: application/json" \
  -d '{"query": "hello vietscope", "max_results": 3}'
```

```python
import httpx

client = httpx.Client(
    base_url="http://localhost:8888",
    headers={"Authorization": "Bearer dsa_live_…"},
    timeout=60,
)
print(client.post("/v1/search", json={"query": "hello vietscope"}).json())
```

A `200` means auth, scope, rate limit, and quota all passed.

## 4. Streaming answers (SSE)

Long-running endpoints stream Server-Sent Events — set `stream: true` on
`/v1/answer`, or use `/v1/research/stream`:

```bash
curl -N -X POST http://localhost:8888/v1/answer \
  -H "Authorization: Bearer dsa_live_…" \
  -H "Content-Type: application/json" \
  -d '{"query": "explain vector databases", "stream": true}'
```

Wire format — every event is `event: <name>` + `data: <json>` + blank line.
Terminal events are `done` (success) or `error`/`warning` (failure). Clients
must keep reading until `done`; treat unknown event names as
forward-compatible. Full event reference:
[README.md → SSE streaming protocol](./README.md#sse-streaming-protocol).

```python
import httpx, json

def stream_answer(query: str):
    with httpx.stream("POST", "http://localhost:8888/v1/answer",
                      headers={"Authorization": "Bearer dsa_live_…"},
                      json={"query": query, "stream": True},
                      timeout=180) as r:
        r.raise_for_status()
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                yield event, json.loads(line[6:])
                event = None

for event, data in stream_answer("explain vector databases"):
    if event == "answer.delta":
        print(data["text"], end="", flush=True)
    elif event == "done":
        print(f"\n— coverage={data.get('coverage')} verified={data.get('verified')}")
```

Notes:

- `answer.delta` chunks are ~600 chars — concatenate `text` fields for the
  full answer.
- `X-Accel-Buffering: no` is set; behind the provided Nginx config SSE is
  already unbuffered (`proxy_buffering off`, 600s timeouts).
- Deep/research modes can take tens of seconds — set client timeouts
  generously (≥120s) and prefer streaming for UX.

## 5. Error handling

| HTTP | `detail.error` | Action |
|---|---|---|
| 401 | `missing_or_invalid_api_key` | Check header `Authorization: Bearer <key>`; key may be revoked or wrong prefix. |
| 403 | `insufficient_scope` | Re-issue the key with `detail.required` scope — scopes are set at creation. |
| 429 | `rate_limit_exceeded` | Back off; limit is `detail.rpm` req/min sliding window. Retry after a few seconds with jitter. |
| 429 | `daily_quota_exceeded` | Quota (`detail.quota`/day) exhausted — ask operator for a higher quota or wait for the UTC day rollover. |
| 503 | `auth_backend_unavailable` | Server-side auth DB down — retry with backoff, alert operator. |
| 422 | *(validation)* | Fix the request body; on `/v1/read` a string `detail` starting `URL blocked by SSRF policy:` means the URL is not fetchable. |

FastAPI wraps errors: the JSON body is `{"detail": {...}}` — read
`resp.json()["detail"]["error"]`, not `resp.json()["error"]`.

Retry guidance: `429`/`503` are retryable with exponential backoff;
`401`/`403`/`422` are not — fix the request first.

## 6. Client checklist

- [ ] `GET /v1/health` → `status: ok` before first real call
- [ ] Key scoped minimally (don't request `*` for production clients)
- [ ] `Authorization: Bearer …` on every POST
- [ ] Parse `{"detail": …}` error envelope
- [ ] SSE: read until `done`; concatenate `answer.delta.text`
- [ ] `GET /v1/capabilities` for feature discovery instead of hard-coding
      (reports `auth.enabled`, `endpoints` flags, mode aliases, budgets)

## Next steps

- [`openapi.yaml`](./openapi.yaml) — machine-readable spec (import into
  Postman/Insomnia, or generate a client)
- [`examples.md`](./examples.md) — every endpoint, curl + httpx
- [`README.md`](./README.md) — architecture, scopes, rate-limit internals
