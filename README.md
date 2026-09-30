# Search Hub (VietScope)

Self-hosted search, answer, and verification stack: metasearch (SearXNG) →
fetch/render (Firecrawl) → index (OpenSearch lexical + Qdrant vector) →
rerank → synthesize. **No commercial search API key required.**

- **Product**: VietScope — public API surface; see `docs/api/`.
- **Repo**: Search-Hub — internal name for the containers, networks, and env vars.
- **Router**: `search-router` — a FastAPI application that orchestrates every stage.
- **Version / runtime**: `v3.0.0` · Python 3.12.14 (`>=3.12,<3.14`).
- Live health: `curl http://localhost:8888/v1/health` → `{"status":"ok","version":"3.0.0"}`

## Architecture

16-container stack driven by `docker-compose.yml`:

```
                    CLIENTS (agents · curl · apps)
                              |
                  SEARCH ROUTER :8888  (FastAPI — orchestration)
                              |
        +---------+-----------+-----------+
        |                     |           |
    SearXNG :8080      Firecrawl :3002    Code search
    (metasearch)      (scrape / crawl)    (GitHub + grep.app)
        |                     |
        +---------+-----------+
                  |
        Rerank -> RAG synthesis -> answer + evidence + citations
                  |
  OpenSearch:9200 | Qdrant:6333 | hub-postgres:5433 (PostGIS 3.5)
  hub-redis:6380 (valkey 9.1) | embedding:8892 (bge-m3) | reranker:8891 (bge-reranker-v2-m3) | MinIO:9000
```

Supporting services: forgejo (Codeberg mirror, :3000), hermes-kestra (cron/workflow, :8086),
agentmemory (:3111, Hermes memory plugin), hermes-ops-pg (:15432).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full contract map and
[AGENTS.md](AGENTS.md) for operational details.

## Quick start

```powershell
git submodule update --init --recursive
cp .env.example .env          # 12 vars: LLM, SEARXNG, FIRECRAWL, MINIO, EMBEDDING, RERANKER, ...
docker compose up -d          # 16 services
curl http://localhost:8888/v1/health
```

## Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET`  | `/v1/health` | router + provider status | — |
| `POST` | `/v1/search` | web/news/image search via SearXNG | Bearer |
| `POST` | `/v1/fetch` | scrape / crawl / map a URL via Firecrawl | Bearer |
| `POST` | `/v1/code_search` | code search via GitHub + grep.app | Bearer |
| `POST` | `/v1/research` | full pipeline → answer + evidence + citations | Bearer |
| `POST` | `/v1/answer` | RAG synthesis over indexed context | Bearer |
| `GET`  | `/v1/places/search` | canonical place search (P16.1) | Bearer (`admin:debug`) |
| `GET`  | `/v1/places/{id}` | canonical place projection (P17) | Bearer |
| `GET`  | `/v1/admin/resolve` | entity resolution (P0) | Bearer (`admin:debug`) |

All `/v1/*` paths are **Bearer-protected** when `API_AUTH_ENABLED=*` (`true` on the
live stack). Manage keys: `python scripts/manage_keys.py create`; add
`scope=places:read` for `/v1/places/*` reads, `admin:debug` for `/v1/admin/*`.

MCP adapter (optional, host process on `:8901`): `search-router/adapters/mcp_server.py`
— see `SETUP.md` §7. Public API contract: `docs/api/api.md`.

## Development

```bash
cd search-router
uv sync                       # Python 3.12.14 — CI / Docker / pyright pin 3.12
uv run pytest tests/ -q       # 1882 pass / 33 skip (1915 collected)
ruff check . && uv run pyright
```

- **Python**: runtime is **3.12.14** (`requires-python = ">=3.12,<3.14"` in
  `pyproject.toml`). `crawler/fetcher.py` uses `HTMLParser(scripting=)`, which
  3.14 removed — stay on 3.12. Use `search-router/.venv`.
- **Deps**: `pyproject.toml` + `uv.lock` (source of truth). `requirements.txt`
  is **generated** (`uv export --frozen --no-dev -o requirements.txt`); never hand-edit.
- **CI / quality**: 10 gates — ruff, tests + diff-coverage, gitleaks, osv-scanner,
  Trivy, CodeQL, pyright ratchet, actionlint, zizmor, hadolint — see
  `docs/quality-pipeline.md` + `quality-gates.yml`. **Fail-closed**: a PR merges
  only when all gates are green (E2E inclusive).
- **Commits**: conventional commits, enforced via lefthook + commitlint
  (`cliff.toml`).

## Layout

```
search-router/   FastAPI app — the only first-party code tree
searxng/         submodule — metasearch upstream
firecrawl/       submodule — web acquisition upstream
deploy/          reverse-proxy / deployment scaffolding
docs/            canonical docs (api/, research-notes/, quality-*.md)
eval/            offline evaluation harness
scripts/         operational scripts
docker-compose.yml
baseline/        pyright / ruff baseline
```

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) ·
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) (the repo ships no LICENSE
file; third-party licenses only).
