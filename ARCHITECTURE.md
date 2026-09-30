# Architecture — Search Hub (VietScope)

Canonical architecture document. If code and docs disagree, **code wins** —
update this file.

## System overview

```
                        CLIENTS
            agents (MCP :8901) · HTTP clients
                          │  Authorization: Bearer dsa_live_… (prod)
                          ▼
        ┌───────── SEARCH ROUTER :8888 (FastAPI) ─────────┐
        │  auth → scope → rate limit → quota (hub-postgres)│
        │  /search /fetch /code_search /answer            │
        │  /v1/search|answer|research|news|images|read|…  │
        └────┬─────────────┬──────────────┬───────────────┘
             │             │              │
   DISCOVERY │   RETRIEVAL │   AI RANKING │   SYNTHESIS
             ▼             ▼              ▼              ▼
      SearXNG :8080   OpenSearch :9200  embedding :8892  LLM gateway
      (metasearch)    (BM25 passages)   (BGE-M3)         (OpenAI-compat)
      Firecrawl :3002 Qdrant :6333/34   reranker  :8891
      (scrape/crawl)  (dense vector)    (bge-reranker-v2-m3)
                        hub-postgres :5433 (keys, usage, logs, frontier)
                        hub-redis :6379 (cache, rate limit, state)
                        MinIO :9000 (raw snapshot objects)
```

## Service contracts

| Service | Port | Contract consumer | Interface |
|---|---|---|---|
| **search-router** | 8888 | clients | FastAPI HTTP/JSON — see `docs/api/openapi.yaml` |
| **SearXNG** | 8080 | search-router | `GET /search?q=…&format=json`; config `searxng/container/core-config/settings.yml` |
| **Firecrawl** | 3002 | search-router, SearXNG | REST `scrape/crawl/map/extract` → Markdown; submodule `firecrawl/` |
| **OpenSearch** | 9200/9600 | search-router | BM25 lexical index (`web_passages_*`); client `search-router/opensearch/` |
| **Qdrant** | 6333/6334 | search-router | dense vector lane `web_passages_v1`; client `search-router/qdrant/` |
| **hub-postgres** | 5433 | search-router | api_keys, tenants, usage, query_logs, domain_profiles, crawl_frontier (PostGIS) |
| **hub-redis** | 6379 | search-router | response cache, sliding-window rate limit, request state |
| **embedding-service** | 8892 | search-router | BGE-M3 embeddings |
| **reranker-service** | 8891 | search-router | bge-reranker-v2-m3 cross-encoder |
| **MinIO** | 9000/9001 | search-router | S3 raw snapshots bucket `sh-raw-snapshots` |
| **searxng-valkey** | internal | searxng | result cache |
| **firecrawl-*** | 3002 | internal stack | api + playwright + redis + rabbitmq + nuq-postgres (+foundationdb) |
| **MCP adapter** | 8901 | agents (optional) | `search-router/adapters/mcp_server.py` — streamable-http |

All inter-service traffic uses Docker service names on `search-hub-net`.
Only 8080, 3002, 8888 (and MCP 8901 on host) are meant to be reachable.

**Source Federation Layer** (Phase 2): provider selection is *not* a static
ordered list — `core/source_router.py` (`SourceRouter`) maps query intent
(news/legal/ecommerce/places/academic/code/forum/social/image/video signals)
to lane weights (`OFF`→`VERY_HIGH`), then picks providers whose
`ProviderSpec.source_types` overlap active lanes, filtered by
`ProviderSpec.enabled`/locale fit, budget caps, and circuit state.
`core/provider_health.py` (`ProviderHealthMonitor`) is the provider circuit
breaker: rolling-window metrics (success/timeout/captcha rates, p50/p95
latency, yield, consecutive_failures) → `health_score` + status
(`healthy|degraded|unhealthy`). `closed → open` on score/consecutive/captcha
trips, `open → half_open` after cooldown admits exactly one probe (success →
`closed`, failure → `open` + exponential backoff). `core/federation.py`
(`FederatedExecutor`) runs the parallel fan-out — per-provider timeouts,
outcome classification (success/empty/timeout/captcha/rate_limited/error),
budget consumption, health recording; SearXNG `unresponsive_engines` feed
the engine-level `EngineHealthManager` (e.g. Qwant CAPTCHA → engine marked
down without killing the provider). Provider schema is
`providers/base.py::ProviderResult` + `ProviderSpec` — adding a source is a
`providers/<name>.py` + one `PROVIDER_SPECS` row (or
`HUB_PROVIDERS_CONFIG` JSON / `PROVIDER_<NAME>_ENABLED` env); no
orchestrator changes needed. `GET /v1/providers/health` exposes scores +
circuit state.

## Data flow — `/answer` (canonical pipeline)

1. Query analysis → adaptive provider fan-out (SourceRouter → FederatedExecutor)
2. Rerank: keyword overlap + domain authority + engine score
3. Tiered reader: top-N URLs → clean text (HTTP→Trafilatura→Firecrawl→Playwright)
4. Chunk + rerank content chunks
5. RAG synthesis: LLM answer from top chunks (extractive fallback)
6. Evidence pack: sources + claim→source citations + follow-up questions

`/v1/research` runs the internal agent pipeline (plan → retrieve → gap
analysis → synthesize → verify) — multi-hop, `mode=fast|balanced|deep`.

**Inference**: every LLM call goes through `core/inference_gateway.py`
(`InferenceGateway`) — the single OpenAI-compatible client owning
timeouts, retries, circuit breaker, and streaming. Model roles
(`planner`/`synthesizer`/`verifier`/`extractor`/`default`) map to
`LLM_PLANNER_MODEL`/`LLM_SYNTH_MODEL`/`LLM_VERIFY_MODEL`/`LLM_EXTRACT_MODEL`
env vars, falling back to `LLM_MODEL`. Pointing `LLM_BASE_URL` at a
LiteLLM proxy later requires no code changes. No other module calls
provider endpoints directly.

**Reader**: the answer/research lanes read URLs through
`pipeline/reader.py` (`ReaderService`) — page cache → direct HTTP →
Trafilatura extraction → quality gate → Firecrawl → Playwright.
Low-quality or failed extraction escalates to the next tier instead of
serving boilerplate; partial results return degraded rather than
dropped. `pipeline/tiered_fetch.py` owns the raw fetch chain; the crawl
lane keeps its own fetcher (`crawler/fetcher.py`) for the
snapshot/robots invariants. `/v1/read` stays an explicit Firecrawl
endpoint.

**Citations**: verified claims map back to fetched text via
`evidence/citation.py::build_passage_citations` — each research citation
carries `claim_id` + `evidence[{source_id, passage_id, url, quote,
quote_start, quote_end, retrieved_at}]` (offsets index into the full
fetched document). The same list feeds `/v1/answer`,
`/v1/research`, and the SSE `citation` event.

**Streaming**: `run_research(emit=)` accepts an async event sink —
state transitions emit `planning`/`plan`/`search.*`/`source`/
`source.read`/`evidence`/`synthesizing`/`verified` live, and synthesis
streams real LLM tokens as `answer.delta` via
`pipeline.rag.stream_research_answer` (gateway `stream()` under the
hood). If verification edits the streamed text, `answer.final` carries
the canonical answer. `/v1/answer?stream=true` and
`/v1/research/stream` pump these through an `asyncio.Queue` with 20s
keepalive comments — no finished-answer replay.

## Data acquisition — crawl → snapshot → extract → index

Search-Hub owns its corpus; it does not only query other providers' data.

```
crawl_frontier (hub-postgres, claim-token lease)
   → fetch (httpx; per-hop robots + politeness; conditional GET)
   → MinIO raw snapshot (immutable bytes) + document_snapshots row
   → ExtractionService (Trafilatura for HTML, passthrough for text)
   → quality gate → documents.main_text + metadata/provenance
   → IndexingWorker → OpenSearch web_documents/web_passages
                    → embeddings → Qdrant web_passages_v1
```

Invariants:

- **Snapshot first.** Raw bytes are durable before extraction; an
  extraction/indexing failure can never lose the capture.
- **Honest provenance.** Extraction records the `snapshot_id` it actually
  read — including Firecrawl-render fallback bodies, which persist as
  their own snapshot (`metadata.provenance.render_fallback`).
- **Stage statuses.** `documents.extraction_status` / `indexing_status` /
  `embedding_status` fail independently and are retryable.
- **Deterministic IDs.** `doc_id` = sha256(`canonical_url`); passage IDs
  `doc_id#p_NNN` → stable Qdrant point IDs; re-index deletes stale
  passages only after new ones are durable.
- **`canonical_url` preserves scheme** (`http` stays `http` — the URL
  must stay fetchable). `canonical_identity()` is the scheme-less key
  for alias dedupe; `cluster_sources` groups on it.

## Local-place serving (P17)

The place read path keeps ordinary local search off the LLM entirely.
Canonical Postgres (P16) stays the source of truth; everything else is a
rebuildable projection.

```
canonical_places / place_sources / place_source_records   (P16, truth)
        │  serving/places/projection.py → PlaceDocumentV1 (frozen contract)
        ▼
serving/places/indexer.py            scripts/index_places.py (CLI)
        │  full rebuild → fresh concrete index + atomic alias swap
        │  incremental → (updated_at, place_id) cursor in serving_index_state
        │  reconcile → drop index docs with no canonical row
        ├──────────────┬─────────────┐
        ▼              ▼             ▼
   OpenSearch      PostGIS        Redis
   "places" alias  (same tables)  hot query / detail / suggest caches
   text+filters    ST_DWithin /   versioned keys + epoch invalidation
   + geo_point     ST_Contains /  (memory fallback when Redis is down)
                   ST_DistanceSphere
        └──────────────┴─────────────┘
                       ▼
        PlaceService (serving/places/service.py)
        cache → OpenSearch lane → PostGIS lane (fallback/precision)
        → deterministic weighted rank (ranking.py) → /v1/places/*
```

- **Lanes degrade independently.** OpenSearch down → PostGIS candidate
  lane serves the same filters (`search_candidates`); PostGIS down →
  index results still serve with haversine distances; both down →
  explicit `degraded` metadata, never fabricated rows. `admin_contains`
  is PostGIS-only (polygon containment via `administrative_units`).
- **Ranking is deterministic and debuggable.** Weighted components —
  text (OS score), distance, category, confidence, freshness, source
  count, status — tunable via `PLACES_RANK_WEIGHTS`; `debug=1` returns
  the component breakdown per result.
- **Query parsing is rule-based.** `query.py` folds Vietnamese
  diacritics, strips proximity filler ("gần tôi", "ở đâu"), and maps
  category hints ("nhà thuốc"→health, "cà phê"→food). No LLM anywhere on
  the read path.
- **Sync is durable and idempotent.** `serving_index_state` (migration
  012) records the cursor, generation, and counters; bulk upserts retry;
  index failures never write back to canonical tables. Tombstones =
  `PlaceIndexer.delete` + epoch bump (invalidates cached answers).
- **API**: `GET /v1/places/search` (q, lat, lon, radius_m, category,
  admin_unit_id, status, bbox, admin_contains, debug), `GET
  /v1/places/autocomplete`, `GET /v1/places/{id}` (503 when neither
  Postgres nor the index can serve), `POST /v1/places/reindex`.
  `X-Places-Lanes|Cache|Ms|Degraded` headers expose lane metadata.

Bench: `scripts/bench_places.py --size N` seeds isolated fixtures and a
throwaway `places_bench` index, measures p50/p95/p99 + per-lane timings,
then cleans up.

## Boundaries

- `firecrawl/` and `searxng/` are **git submodules** — upstream code, never
  edit in place; wrap changes in `search-router/`.
- `search-router/` is the only first-party Python tree (ruff/pyright scope).
- MCP is an **adapter**, not core runtime: the stack runs without it.
- `.env` is local-only (gitignored); `.env.example` is the committed template.
