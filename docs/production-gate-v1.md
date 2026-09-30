# Production Gate V1 — Search-Hub

Date: 2026-09-25 · Validator: Devin · Scope: P16.1 + P17 + P0 + A9 on latest main

## 1. HEAD under test

- main HEAD: `de48f1267205f5e7acbdebd4be23f6a66bc7259b` — `feat(inference): A9 per-role backend routing — lanes, fallback, probe (#44)`
- Working tree at audit time: clean, no dirty/untracked files.
- Two minimal gate fixes were made on branch `devin/1790349559-pgate-v1-jsonb-fix` (see §16). The verdict applies to the tree including those fixes.

## 2. Environment

- Ubuntu, 8 CPU, 31G RAM, 104G free disk
- Docker 27.4.1 · Compose v2.32.1
- Python 3.12.14 (uv-managed, `uv sync --locked --python 3.12`; required — 3.12.10/3.13 lack `HTMLParser(scripting=)` used by `crawler/fetcher.py`)
- Live stack env: `HUB_DATABASE_URL=…:5433/searchhub`, `REDIS_URL=redis://127.0.0.1:6380/0`, OpenSearch `localhost:9200`

## 3. Service health matrix (Phase A)

| Service | Port | State | Evidence |
|---|---|---|---|
| hub-postgres (PostGIS 3.5) | 5433 | HEALTHY | healthy, migrations applied |
| hub-redis (Valkey) | 6380 | HEALTHY | healthy, cache keys observed |
| OpenSearch 2.18 | 9200 | HEALTHY | cluster green, `places` alias live |
| Qdrant | 6333 | HEALTHY | container up, router /v1/health=ok |
| MinIO | 9000 | HEALTHY | healthy |
| SearXNG | 8080 | HEALTHY | `/search?format=json` 200 (see §16 notes) |
| Firecrawl API | 3002 | HEALTHY | `POST /v0/scrape` real scrape succeeded |
| embedding-service (bge-m3) | 8892 | HEALTHY | `available: true` |
| reranker-service (bge-reranker-v2-m3) | 8891 | HEALTHY | lazy-load; `/score` returned 0.40 vs 0.045 on probe pair |
| search-router | 8888 | HEALTHY | `/v1/health` all services ok |
| firecrawl-postgres / rabbitmq / redis / playwright / foundationdb | — | HEALTHY | up; scrape path verified |
| searxng-valkey | — | HEALTHY | up |

FAILED: none.

Deviation recorded: the pinned searxng submodule ref `d7bd9225…` ("engine-matrix-54") no longer resolves upstream; compose falls back to a generated config. `container/core-config/settings.yml` needed `search.formats: [html, json]` added (was 403 on JSON) — environment/config note, not a code regression.

## 4. Migrations

14/14 migrations (`001_base` … `014_source_category_mappings`) applied cleanly via `db/migrate.py`. Additive-only invariant respected.

## 5. Unit / integration results (Phase B)

| Gate | Result |
|---|---|
| pytest suite (host, `env -u PYTHONPATH uv run pytest tests/ -q`) | **1679 passed / 26 skipped / 0 failed** (93s) |
| ruff check | PASS |
| ruff format --check | PASS (265 files) |
| pyright ratchet | PASS — 65 errors = `GATE_PYRIGHT_ERRORS_MAX=65` |
| Baseline contract tests | 9/9 PASS |
| `uv lock --check` | PASS |
| hadolint | clean |
| gitleaks | 2 findings — history-only, vendored `qdrant-master/` upstream test fixtures (deleted from HEAD); not production secrets |
| OSV scan (`ghcr.io/google/osv-scanner:v2.3.0`) | clean |
| pip-audit (py3.12 venv) | clean |

## 6. E2E result

`test_crawl_to_search.py` — **PASS** (37.5s): real crawl → ingest → resolution → canonical → index → search round-trip. Requires `HUB_DATABASE_URL` pointed at :5433 for host runs.

## 7. Benchmarks (Phase C) — `scripts/bench_places.py`

All executed — nothing extrapolated. 40 iterations per scenario, cache namespace `places_bench`.

| Metric | 10k | 100k | 500k |
|---|---|---|---|
| Docs indexed | 9,999 | 99,999 | 499,998 |
| Full rebuild (projection+index) | 1,963 ms (~5.1k docs/s) | 11,392 ms (~8.8k docs/s) | 67,800 ms (~7.4k docs/s) |
| Index size | 2.6 MB | 27.1 MB | 132.5 MB |
| Incremental upsert (500 touched rows) | 2,494 ms | — | — |
| Incremental no-op (cursor steady) | 9 ms | — | — |
| Tombstone reconcile (1 deleted) | 322 ms, removed=1 | — | — |
| Sequential QPS | 105.7 | 112.7 | 56.4 |
| Concurrency-10 QPS | 312.5 | — | — |
| Concurrency-25 QPS | 474.3 | 468.9 | 319.2 |

## 8. Latency — P50 / P95 / P99 (ms)

| Scenario | 10k p50/p95/p99 | 100k p50/p95/p99 | 500k p50/p95/p99 |
|---|---|---|---|
| text_only | 0.91 / 1.04 / 13.4 | 0.78 / 0.97 / 108.9 | 0.90 / 3.13 / 498.3 |
| geo_only | 0.67 / 0.81 / 25.5 | 0.73 / 0.90 / 67.6 | 0.85 / 1.08 / 91.6 |
| text_geo | 0.90 / 1.05 / 13.6 | 0.79 / 1.03 / 18.3 | 0.98 / 1.28 / 51.2 |
| near_me_intent | 0.71 / 0.82 / 13.9 | 0.67 / 0.82 / 27.4 | 0.68 / 1.01 / 321.8 |
| category | 0.83 / 0.98 / 13.9 | 0.91 / 1.03 / 15.3 | 1.08 / 2.42 / 35.4 |
| admin_area | 0.76 / 0.94 / 8.3 | 0.78 / 0.88 / 11.0 | 1.14 / 4.24 / 44.0 |
| status_open | 0.67 / 0.82 / 16.7 | 0.92 / 1.05 / 14.0 | 0.75 / 0.90 / 32.7 |
| bbox | 0.67 / 1.31 / 7.3 | 0.83 / 2.85 / 9.7 | 0.87 / 3.92 / 19.0 |
| autocomplete | 0.56 / 0.72 / 17.7 | 0.60 / 0.73 / 63.2 | 0.81 / 1.01 / 353.7 |
| by_id | 0.26 / 0.38 / 7.1 | 0.27 / 0.45 / 6.3 | 0.37 / 1.88 / 6.7 |

Cold vs warm: `cold.total_ms` equals p99 in every scenario (first-hit, uncached); warm p50 < 1.1 ms at all sizes.

## 9. QPS

Sequential ~106–113 at 10k/100k; 56.4 at 500k (cold misses hit a 132 MB index). Concurrency-25 ≈ 470 at ≤100k, 319 at 500k.

## 10. Cache measurements

- Redis hit rate: **0.975** at all sizes (misses are first-hit/cold).
- `X-Places-Cache: miss → hit` verified live; hit latency ~0.5 ms.
- Invalidation: canonical UPDATE → incremental reindex → same query re-misses and serves the new name (epoch-bumped keyset; no stale serve).
- Stale-key behavior: versioned keys (`places_bench:v1:s:*`); reconcile/reindex bumps the epoch so orphaned keys are unreachable rather than wrong.
- Redis down: search stays correct via in-memory `_MemStore` fallback (see §11).

## 11. Degraded-mode results (Phase E)

| Test | Result |
|---|---|
| Redis unavailable | PASS — text & geo queries return correct results, `X-Places-Cache: miss`, in-memory fallback |
| OpenSearch unavailable | PASS — geo queries served by `postgis` lane with `X-Places-Degraded: opensearch`; uncached text query returns honest `[]` (no fabrication); `/places/{id}` served by `postgres` lane |
| Index worker restart / idempotent resume | PASS — incremental no-op 9 ms; durable cursor in `places_index_state`; no duplicated docs |
| Duplicate canonical update | PASS — two identical UPDATEs → incremental scanned/indexed exactly 1; index docs == canonical count (index `_id` = place_id) |
| Canonical update → projection | PASS — rename applied; incremental picked it up; API served new name |
| Delete / tombstone | PASS — reconcile `extra_ids=1, removed=1`; place gone from search; by-id → 404. A later adversarial check found stale by-id cache *if the place had been fetched by-id before deletion* — fixed on this branch (§15.3) and re-verified: DELETE + rebuild → by-id 404 |
| Cache invalidation | PASS — reindex bumps cache epoch; old result not re-served. Same caveat fixed per §15.3 |

## 12. A9 routing validation (Phase F — real mock OpenAI backends)

Mocks on 127.0.0.1:18430 (default/k1), :18431 (planner/k2), :18432 (extractor/k3); verifier pointed at dead :18999. 26/26 checks passed:

- Role isolation: planner → :18431 `Bearer k2`; extractor → :18432 `Bearer k3`; default → :18430 `Bearer k1`. No cross-talk.
- Circuit isolation: 3×500 on :18431 opens planner's lane only; extractor + default lanes stay closed; open breaker rejects without touching the backend.
- Fallback only where configured: extractor (no `LLM_EXTRACT_FALLBACK`) → `None`, zero default hits; planner (`LLM_PLANNER_FALLBACK=default`) → retried once on :18430, `fallbacks` metric counted.
- Streaming: pre-first-token failure replays to default leg (3 tokens); mid-stream abort yields `TOK1` then ends — **no replay after first token** (no second request to default).
- Error kinds distinguishable: `auth` (401/403), `model_missing` (404), `server_error` (5xx), `rate_limited` (429), `timeout` (read timeout), `network` (conn refused). `probe()` reports `auth_failed`/`healthy`/`unreachable` correctly without touching breakers.
- API keys never appear in WARNING logs or the metrics dump.
- No `LLM_*` endpoint is configured at `:18434` in `.env`; the only configured default is `api.openai.com` with an empty key — "llm: not configured" on `/v1/health` is the honest state, not a blocker. No LocalAI installed.
- **Confirmed** the PR #44 nit: one transient failure left the role `degraded` for the process lifetime. Fixed on this branch (see §16) and re-verified: `degraded → healthy`, `last_error_kind` cleared, cumulative `failures` preserved.

## 13. Blockers

None blocking. Environment notes:
- searxng pinned submodule ref unresolvable upstream (custom engine-matrix commit) — compose fallback config works; JSON format enabled manually. Residual risk for fresh checkouts.
- `test_crawl_to_search.py` needs `HUB_DATABASE_URL` exported for host runs (fixture defaults to :5432).
- `quality.yml` references `aquasec/osv-scanner` (image does not exist; CI soft-fails via `|| echo skipped`) — used `ghcr.io/google/osv-scanner:v2.3.0` instead.
- Full-suite run against a *populated* live index makes `test_p16_pg::test_places_search_and_detail` see real docs (env coupling); green on an empty index. Test could isolate the OS lane; not a code bug.

## 14. Bottlenecks

- Cold first-hit on 500k: p99 spikes (~0.5 s) on text/autocomplete — the uncached OS round-trip on a 132 MB index. Warm path stays sub-ms.
- Sequential QPS at 500k drops to ~56 (cold misses dominate); concurrency-25 ~319.
- Projection+index pipeline ~7.4–8.8k docs/s — a 1M rebuild is ~2 minutes.

## 15. Regressions found

1. `serving/places/projection.py` — `opening_hours` (jsonb) arrived as `str` from asyncpg (no codec registered on any pool), so any canonical place with non-null opening hours made `/v1/places/search` and `/v1/places/{id}` 500 via pydantic validation. Latent since P17; all test fixtures used `opening_hours=None`.
2. `core/inference_gateway.py` — `_role_metrics_view` derived `state` from cumulative `failures` + never-cleared `last_error_kind` → permanent "degraded" (Codex nit on #44, confirmed live).
3. `serving/places/cache.py` — by-id `p:` keys embedded **no epoch** while `invalidate_all()` only bumps the epoch, so a canonical DELETE + rebuild/reconcile left `/v1/places/{id}` serving the deleted place from Redis until `ttl_place` (3600s) expiry; `indexer.delete()` has no HTTP route, so no API path could clear it. Reproduced live: `x-places-lanes: cache` 200 for a Postgres-deleted row. Latent since P17.

## 16. Fixes made (branch `devin/1790349559-pgate-v1-jsonb-fix`)

- `projection.py`: `_jsonb_obj()` — decode str-encoded jsonb to dict (reject non-dict) for `opening_hours`. + `test_jsonb_str_opening_hours`.
- `inference_gateway.py`: `_record_success()` clears lane/default `last_error_kind` and the role's `last_error_kind` on success; `state` now derives "degraded" from `last_error_kind` (current error) not cumulative `failures`. + `test_success_clears_role_error_state`.
- `cache.py`: `p:` keys now embed the epoch (`p:<epoch>:<id>`) like `s:`/`a:` — one epoch bump orphans stale by-id entries on rebuild/reconcile; `invalidate_place` still deletes the current-epoch key. + `test_stale_by_id_cache_cleared_on_invalidate_all`. Verified live: DELETE + rebuild → by-id 404; UPDATE + incremental → new name served.
- Gates re-run after fixes: suite green (1679 pass), ruff/format clean, pyright 65/65.
- Environment config note (no code): searxng `settings.yml` JSON formats.

## 17. Remaining risks

- No real LLM endpoint configured in env — A9 validated against mocks only; first real-provider use should re-verify.
- p17_live suite runs 8/8; the 100k/500k benches are one-shot measurements on this box.
- `PlaceOut.address`/`website_domain` are always null — response-model fields the projection never populates (vestigial P16 surface, harmless).
- Stale `p:*` entries written *before* this fix remain in Redis until TTL expiry under the old key shape (unreachable under the new `p:<epoch>:` shape) — one cold flush or TTL wait clears them; a one-off `redis-cli` keyspace cleanup may be warranted on deploy.
- searxng config is hand-adjusted; a fresh compose-up on a clean clone needs the same JSON-format step (or a committed settings file).
- PostGIS `postgis-dist` lane ordering verified for exact distance via fixture tests; `admin_contains` verified via bench polygon fixtures.

## Verdict

**PRODUCTION_GATE_V1 = PASS**

(on the fix branch; main has the three latent defects listed in §15 — merge this PR to carry the minimal fixes + regression tests.)
