# Search-Hub — Working Rules for Agents

Search-Hub is a self-hosted search + data-acquisition stack. **VietScope** is
the commercial product name — use it for the public API surface and
product-facing docs (`docs/api/*`, STATUS title). "Search-Hub" stays the
internal name: repo, containers (`search-hub-*`), network (`search-hub-net`),
env vars (`SEARCH_HUB_*`, `HUB_*`), paths.

Canonical architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it first
for service map, ports, and data flow. This file holds rules and commands,
not a second architecture description.

## Layout ownership

| Path | Owns |
|---|---|
| `search-router/` | The only first-party Python tree — FastAPI app, crawler, extraction, workers, storage, canonical, providers, pipeline, core, agent, api, adapters, tests |
| `search-router/db/migrations/` | Versioned SQL (`NNN_*.sql`), applied by `db/migrate.py` — additive only, never edit an applied migration |
| `firecrawl/`, `searxng/` | Git submodules (upstream code). Never edit in place; wrap changes in `search-router/` |
| `docs/` | `api/` public contract, `research-notes/`, `GOVERNANCE.md`, quality docs |
| `scripts/`, `deploy/` | Start scripts, nginx scaffold |
| `eval/` | Offline eval framework |

Inside `search-router/`:

- `core/` — web-search orchestration (`/v1/search`, `/v1/answer` lanes)
- `agent/` — multi-hop research pipeline (`/v1/research`,
  `/v1/search?mode`) — live code, not legacy
- `crawler/` — fetcher, pipeline, robots, politeness, netguard (SSRF)
- `extraction/` — MIME dispatch + quality gate + provenance (storage-agnostic)
- `workers/` — freshness/frontier, indexing (OpenSearch + Qdrant)
- `storage/` — pg_client, redis_client, object_store (MinIO/S3)
- `canonical/` — `canonical_url` / `canonical_identity`, fingerprints
- `adapters/` — optional surfaces (`mcp_server.py` — see below)

## Data invariants — do not break

1. **Snapshot first.** Raw fetch bytes land in MinIO +
   `document_snapshots` before extraction runs. Extraction/indexing
   failures must never lose the capture.
2. **Honest provenance.** Extraction records the `snapshot_id` it
   actually read. Firecrawl-rendered fallback bodies persist as their own
   snapshot; `metadata.provenance.render_fallback` links them to the
   original. Never claim extraction from a snapshot that does not hold
   the bytes it read.
3. **Stage statuses stay separate.** `extraction_status`,
   `indexing_status`, `embedding_status` fail independently and are
   individually retryable — do not collapse them into one flag.
4. **Deterministic IDs.** `doc_id` = sha256(`canonical_url`); passage IDs
   `doc_id#p_NNN` map to stable Qdrant point IDs. Re-index deletes stale
   passages only after new ones are durable.
5. **`canonical_url` preserves scheme.** `http` stays `http` — a
   canonical URL must remain fetchable; only redirect evidence may
   upgrade a page to `https`. Use `canonical_identity()` when schemeless
   dedupe is what you actually need.
6. **Extraction is infrastructure-blind.** `extraction/` knows nothing
   about Postgres columns, OpenSearch indexes, or Qdrant points — the
   pipeline maps its output onto stores. Keep it that way.
7. **Claim guard.** Every document/frontier mutation is guarded by the
   frontier claim token; a lost claim stops all writes.

## Coding rules

- Python 3.12 canonical (`pyproject.toml` `requires-python`); deps live
  in `pyproject.toml` + `uv.lock` — `requirements.txt` is generated
  (`uv export`), never hand-edit.
- Degrade-by-design: missing service/dependency → status field or
  fallback path, not a crash. Follow existing patterns in
  `storage/pg_client.py`, `storage/object_store.py`.
- Ruff is the linter/formatter; `pyright` runs non-blocking — keep new
  code clean anyway.
- Comments explain *why*, and only where the code is not obvious.
- No new framework imports without a real gap. **Do not add**: Dagster,
  Pelias, Imposm, Haystack, Frontera, Dedupe, Apache AGE, second NLP
  stacks alongside Underthesea. Planned-later components live in
  `docs/` planning notes — ask before importing anything that size.
- Submodules are read-only upstream — wrap, don't patch.

## MCP adapter (optional)

`adapters/mcp_server.py` exposes the router over streamable-http MCP on
`:8901` for agent hosts. It is an **adapter**: the stack runs without it,
the `mcp` package is an optional extra (`uv sync --extra mcp` /
`pip install mcp`; `uv sync` and `requirements.txt` on the host already
cover it), and the core router must never import it. Start on host:
`scripts/start-mcp-server.ps1` (Windows, `python.exe -WindowStyle
Hidden` — `pythonw` exits immediately on this machine). Session id comes
from the `Mcp-Session-Id` response header of `initialize` — never
self-generate.

## Commands

```bash
# Stack
docker compose up -d                                  # 17 services (16 core + debug profile)
docker compose up -d --no-deps --build search-router  # rebuild router only

# Migrations (idempotent, advisory-locked)
cd search-router && python -m db.migrate --status && python -m db.migrate

# Tests — unit suite (no stack needed)
cd search-router
env -u PYTHONPATH python -m pytest tests/ -q -p no:cacheprovider   # 1000+ tests

# E2E — needs live stack + creates/revokes a dsa_test_ key
scripts/e2e_run.sh                                    # all e2e
E2E=1 python -m pytest tests/e2e/test_crawl_to_search.py -q        # crawl→search lane

# Lint / format
python -m ruff check search-router && python -m ruff format --check search-router

# Deps after touching pyproject.toml
cd search-router && uv lock && uv export --locked --hashes --format requirements-txt -o requirements.txt
```

## Quality gates (CI)

`.github/workflows/ci.yml` — unit tests + Docker build on main.
`.github/workflows/quality.yml` — ruff changed-files, format, size/test
floor, coverage, gitleaks, OSV, pip-audit, pyright (non-blocking).
`.github/workflows/e2e.yml` — builds the image, brings up the data
services, runs `test_crawl_to_search.py` (BM25 lane; Qdrant assertion is
conditional on `embedding_status`).

## E2E rules

- E2E tests skip unless `E2E=1`; they must never touch env or live
  connections at collection time (bootstrap inside fixtures).
- `NetGuard(allow_private=True)` exists only inside the e2e fixture —
  production SSRF guard stays on.
- Fixture data uses a unique token per run and cleans Postgres,
  OpenSearch, and Qdrant in `finally` — cleanup is idempotent.
- Assertions are per-stage: `extraction_status`, `indexing_status`,
  `embedding_status` are checked independently; Qdrant assertions are
  conditional on the embedding lane being up.
