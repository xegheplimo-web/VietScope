---
name: testing-search-router
description: How to run tests, the dev server, and UI-driven verification for search-router (uv-managed Python; Docker availability varies by box — Windows no, Linux usually yes).
---

# Testing search-router

## Environment facts (verified on this box)

- Repo root `<repo-root>` (current dev checkout: `F:\VietScope-main`); package
  dir `<repo-root>/search-router/`.
- **No system Python.** Always run through uv from `search-router/`:
  `uv run --project . python -m pytest tests/ -q`
  The `.venv` (incl. torch/sentence-transformers) was created by `uv run`/`uv sync` — do not create a second env.
- Export `PYTHONIOENCODING=utf-8` before running scripts/tests that print Vietnamese (admin names, seed stats) or output garbles/fails.
- `torch` intermittently crashes this Windows env (`OSError [WinError 1114] c10.dll`) during full-suite collection in `tests/test_bge_embedding.py`. The file passes standalone; for full runs use `--ignore tests/test_bge_embedding.py` — pre-existing, unrelated to app code.
- `gh` CLI is not installed.

## Box portability

- The repo may be checked out on Linux (e.g. `/home/ubuntu/VietScope`) instead of Windows. `uv`, `uv run`, pytest, and `main.py` behave identically.
- **Check `docker info` before assuming Docker is unavailable** — the "Docker unavailable" note was verified on the Windows box; on Linux the daemon usually works, which unlocks the full live P17 stack and the `E2E=1` harness (the only way to runtime-test OpenSearch/indexer paths end-to-end locally).

## Live local stack (PostGIS + OpenSearch) — Linux/Docker path

Matches docker-compose service specs:

```
docker run -d --name p-pg -e POSTGRES_USER=searchhub -e POSTGRES_PASSWORD=searchhub \
  -e POSTGRES_DB=searchhub -p 127.0.0.1:5433:5432 \
  -v "$PWD/db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" \
  postgis/postgis:16-3.5-alpine
docker run -d --name p-os -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
  -p 127.0.0.1:9200:9200 opensearchproject/opensearch:2.18.0
uv run python -m db.migrate --dsn postgresql://searchhub:searchhub@127.0.0.1:5433/searchhub
```

- OpenSearch client env defaults already match the container:
  `OPENSEARCH_HOST=localhost`, `PORT=9200`, `USER=admin`, `PASSWORD=admin`,
  `USE_SSL=false` — nothing to set.
- Serve with `HUB_DATABASE_URL=postgresql://searchhub:searchhub@127.0.0.1:5433/searchhub`
  to light up the live lanes; leave it unset for the degraded lanes.
- `E2E=1 uv run pytest tests/test_p17_live.py -q` self-seeds canonical fixtures
  (marker `p17live`), runs the real indexer + service, and cleans up — covers
  rebuild/sync/delete/geo-search/admin_contains/autocomplete/detail/OS-down
  against real OpenSearch + PostGIS.
- `POST /v1/places/reindex` modes: `incremental` (durable cursor) | `full`
  (fresh concrete index + atomic alias swap; sets `serving_index_state.docs_indexed`
  to the absolute count) | `reconcile` (drops index docs missing from
  `canonical_places`) | `status` (ledger + index stats). To prove reconcile
  end-to-end: `DELETE FROM canonical_places WHERE place_id=N` then mode=reconcile
  → expect `extra_ids=1, removed=1`.
- Verify the ledger: `SELECT generation, concrete_index, docs_indexed, last_error
  FROM serving_index_state` — `docs_indexed` is an ABSOLUTE count after rebuild.
- On a no-Redis box, each `PlaceCache` instance owns a private `_MemStore` epoch
  (cache.py) — index-time `invalidate_all` can't reach the serving cache, so a
  repeat identical query may briefly serve stale rows. Expected degrade-by-design
  behavior, not a bug; Redis deployments share the epoch.

## Full compose stack (Linux box, repo root `docker-compose.yml`)

- Compose service names/ports: `hub-postgres`→127.0.0.1:5433 (POSTGIS),
  `hub-redis`→127.0.0.1:6380, `opensearch`→127.0.0.1:9200, `search-router`→127.0.0.1:8888.
- **No `psql`/`redis-cli` on host** — always `docker exec`:
  `docker exec hub-postgres psql -U searchhub -d searchhub -c "SQL"`,
  `docker exec hub-redis redis-cli <cmd>` (DB/user/pass all `searchhub`).
- `API_AUTH_ENABLED=false` in `.env` → `/v1/*` needs no key and `/v1/health`
  returns the detailed `services` map.
- Seed `canonical_places` directly for serving tests (marker prefix, delete after):
  `place_id` is IDENTITY; `location=ST_SetSRID(ST_MakePoint(lon,lat),4326)`;
  `status` vocab is `open|temporarily_closed|permanently_closed|unknown`.
- Search response headers `X-Places-Lanes/Cache/Ms` tell which lane answered
  (`opensearch`, `postgis`, `cache`) — capture `-D` to prove lane/degradation.
- asyncpg pools register **no jsonb codec** → jsonb columns arrive as `str`;
  seed non-NULL `opening_hours` to exercise `projection._jsonb_obj`.
- `permanently_closed` never surfaces in default search (`DEFAULT_STATUSES`);
  needs explicit `status=` param on both OS (`terms` filter) and PostGIS lanes.
- Known cache quirk (verified live): `invalidate_all()` bumps only the epoch —
  `places:v1:p:*` by-id keys embed no epoch, so DELETE+`reindex full`/`reconcile`
  leaves `/v1/places/{id}` serving the stale cached doc until `ttl_place` (3600s).
  Incremental `sync` is correct (`invalidate_place` deletes `p:` keys). When
  testing tombstone flows, check `docker exec hub-redis redis-cli KEYS 'places:v1:p:*'`
  and DEL residue to distinguish cache-stale from real rows.
- `indexer.delete()` has no HTTP route — deletes propagate only via
  `reindex full`/`reconcile` (see cache quirk above).

### Backend-change validation on the compose stack

- **Verify the image carries the commit before testing.** `docker compose ps`
  image build time can predate the PR commit —
  `docker exec search-router grep -rn "<new symbol>" /app` should hit;
  otherwise `docker compose build search-router &&
  docker compose up -d --no-deps search-router` and re-check `/v1/health`.
- **`Source.domain` wire surface is narrow.** It serializes only on
  `POST /v1/research` (+`/v1/research/stream`) via `core/orchestrator._normalize`.
  Legacy `POST /v1/search` and `POST /v1/news` build their own row dicts and
  **omit `domain` entirely** (and legacy search only calls searxng+ddgs — no
  registry fan-out, so gnews lanes never appear there). The agent pipeline
  (`POST /v1/answer`, `POST /v1/search?mode=*`) normalizes in
  `agent/retriever.py` — a **separate** domain derivation
  (`r.url.split("/")[2]`) that does not consume `item.metadata`; verify the
  same fix class on BOTH pipelines.
- `SearchResultItem.metadata` is `exclude=True` — never on the wire; observe
  via `domain` on serialized source rows only.
- gnews lanes need container egress to `news.google.com`. Host curl may 400
  (TLS fingerprint) while the container works — probe inside:
  `docker exec search-router python -c "import httpx;print(httpx.get('https://news.google.com',...))"`.
- `/v1/research` has no semantic-cache layer (deterministic per call);
  `/v1/answer` does (`cache.hit`) — use a fresh query/endpoint when testing
  retrieval-side behavior.
- **OpenAI-compat gateway (P1)**: `GET /v1/models` + `POST /v1/chat/completions`
  live on the same app (compose `:8888`; the doc's `:8000` is an example).
  `/v1/chat/completions` routes through `run_research` (agent pipeline — same
  normalizer as `/v1/answer`, so gnews wrapper-URL caveats apply to Sources
  footers). Expect `usage` zeros (documented stub), `**Sources:**` +
  `[n] title — url` footer inside content, `search_hub` extension field, and
  OpenAI error schema `{error:{message,type,param,code}}` (`code` may be null).
  To validate with the real SDK: `uv run --with openai python <script>` —
  `openai` is not in the project venv or container.

## Commands

- Tests: `uv run --project . python -m pytest tests/ -q` (scoped: `tests/test_x.py -q`)
- Lint/format: `uv run --project . python -m ruff check` / `python -m ruff format`
- Typecheck: `uv run --project . pyright` (CI ratchet: total errors must stay ≤ `GATE_PYRIGHT_ERRORS_MAX`)
- Dev server: `uv run --project . python main.py` → FastAPI on port 8888 (override via `PORT`). Works without Postgres/Redis — degrade-by-design returns empty/fallback responses rather than errors.
- DB migrations (needs Postgres, CI-only here): `uv run --project . python -m db.migrate`
- Admin seed load: `uv run --project . python -m db.seed_admin --dsn postgresql://...` (idempotent)
- Baseline regen after intentional API/schema changes: `uv run --project . python scripts/freeze_baseline.py` (required by `tests/test_baseline_contracts.py`)

## UI-driven testing

- API docs at `http://localhost:8888/docs`; health at `GET /health`.
- API auth is OFF by default (`API_AUTH_ENABLED=false`), so `/v1/*` endpoints are callable with no credentials.
- Vietnamese query params must be URL-encoded (use `curl -G --data-urlencode` or `encodeURIComponent`).
- `/v1/admin/resolve?q=` and `/v1/admin/lookup?lat&lon=` run fully on the bundled seed — no DB needed. `/v1/admin/lookup` returns `[]` until P14B loads geometry (expected, not an error).
- P16 resolution endpoints degrade differently per route (no Postgres pool): `POST /v1/resolve` → `200 {"status":"unavailable","available":false}` (NOT 503); `GET /v1/places/search` + `GET /v1/resolution/runs` → `200 []`; only `GET /v1/places/{id}` → `503 postgres unavailable`.
- `python -m scripts.resolve` with DB down exits 1 with a ConnectionRefusedError traceback — `_resolve_dsn` always synthesizes a DSN (`--dsn` > `HUB_DATABASE_URL` > `HUB_PG_*` defaults 127.0.0.1:5433), so its clean exit-2 path never triggers.
- Dict-store resolution smoke (no DB): `run_resolution(None, store=DictCanonicalStore(), sources_feed=<async gen>)`; row keys mirror `runner._PAGE_SQL` columns. Idempotent rerun should show `created=0, merged=0, scored=0` — `scored==0` proves record-based relink short-circuits before candidate scoring.
- Swagger UI body editor is CodeMirror: auto-closes brackets/quotes, so typing raw JSON corrupts the body. Prefer Reset → double-click the value word → type replacement without quotes.
- Swagger pages with long response bodies scroll poorly by wheel; reload the page at the `#/v1/<operation-id>` anchor to land on an endpoint.

## Rebuilding the admin seed

`python scripts/build_vn_admin_seed.py --dvhcvn <dvhcvn clone> --snapshots <dir with sorted-YYYYMMDD.json>` → writes `db/seeds/vn_admin_units.json` (minified single-line JSON — it's a generated artifact; the PR size gate counts diff lines).
Inputs live outside the repo (`C:\Users\Administrator\dvhcvn-20250701`, `C:\Users\Administrator\dvhcvn-snapshots`) — needed only to regenerate, not to run tests.

## CI notes

- Gates: `quality` (ruff + ≤7000 net LOC on `search-router/**`), `test` (diff-coverage ≥80%), `typecheck` (pyright ratchet), `security`, `sca`, `Test search-router`, `e2e`, `CodeQL`.
- Keep generated/large data artifacts compact — the LOC gate counts additions+deletions in the net diff.
