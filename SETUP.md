# Search-Hub — Bootstrap từ repo này (AI runbook)

> VietScope — built from the Search-Hub codebase.
>
> Mục tiêu: từ một máy Windows/Linux trống, chỉ cần clone repo này + 1 lệnh
> docker compose up là stack search infrastructure chạy y hệt máy dev gốc.
> Tài liệu này viết cho AI agent (Hermes/Devin/Claude) hoặc dev mới đọc và làm theo.

## Sản phẩm & Repo

- **VietScope** = tên sản phẩm thương mại — public search/answer API mà repo
  này build ra. Tài liệu product-facing (`docs/api/*`, tiêu đề STATUS) dùng
  "VietScope". Hệ sản phẩm tương lai: VietScope Search · News · Images ·
  Places · Business · Social · Entities · Data API (domain tương lai
  `api.vietscope…`, `developers.vietscope…`).
- **Search-Hub** = tên repo + stack nội bộ — giữ nguyên trong mọi tham chiếu
  hạ tầng/code: thư mục `F:\Search-Hub`, container `search-hub-*`, network
  `search-hub-net`, env `SEARCH_HUB_*`, tên service `search-hub` trong
  response `/v1/health`.

## 0. Kiểm tra nhanh hiện trạng máy gốc (đối chiếu)

Stack gồm **17 services Docker** (docker-compose.yml — 16 core +
`opensearch-dashboards` ở profile `debug`, tắt mặc định):

| Nhóm | Service | Port | Bind |
|---|---|---|---|
| Discovery | searxng | 8080 | 0.0.0.0 |
| Discovery | firecrawl-api (+playwright, postgres, redis, rabbitmq, fdb) | 3002 | 0.0.0.0 |
| Retrieval | opensearch (+dashboards :5601, profile `debug`) | 9200/9600 | **127.0.0.1** |
| Retrieval | qdrant | 6333/6334 | **127.0.0.1** |
| AI Ranking | embedding-service (BGE-M3) | 8892 | **127.0.0.1** |
| AI Ranking | reranker-service (bge-reranker-v2-m3) | 8891 | **127.0.0.1** |
| Orchestration | search-router (FastAPI) | 8888 | 0.0.0.0 |
| Infra P10 | hub-postgres (PostGIS 16-3.5) | 5433→5432 | **127.0.0.1** |
| Infra P10 | hub-redis | 6380→6379 | **127.0.0.1** |
| Infra P1 | minio (S3 raw snapshots) | 9000/9001 | **127.0.0.1** |

`opensearch-dashboards` mặc định TẮT — bật bằng `docker compose --profile debug up -d`.

Ngoài Docker: **MCP server** chạy host-process :8901 (`search-router/adapters/mcp_server.py`,
start bằng `powershell -File scripts/start-mcp-server.ps1`). **Vane** (Next.js UI) đã tách
sang archive branch `archive/vane-master` — không còn trong repo chính.

## 1. Yêu cầu máy

- Docker Desktop (Windows) hoặc Docker Engine + Compose v2 (Linux) — RAM ≥ 16GB
  (OpenSearch + BGE models ăn ~8GB), disk trống ≥ 40GB (images + models).
- Git ≥ 2.40 (submodules).
- Python 3.12.x trên host (canonical — khớp `pyproject.toml`
  `requires-python = ">=3.12,<3.14"`; chạy MCP server + manage_keys + tests).

## 2. Clone + khởi động

```bash
git clone --recurse-submodules <repo-url> Search-Hub
cd Search-Hub

# .env — copy từ .env.example rồi điền (xem mục 3)
cp .env.example .env

docker compose up -d --build
# Lần đầu: build image search-router + pull opensearch/qdrant/firecrawl (~15-30')
# BGE services: pull model BGE-M3 + bge-reranker-v2-m3 từ HF (start-period 60s)
```

Kiểm tra: `docker compose ps` — 16 services, các service có healthcheck báo
`(healthy)` sau ~60-90s. Sau đó:

```bash
curl http://127.0.0.1:8888/v1/health
# {"status":"ok","services":{"searxng":"ok","firecrawl":"ok","opensearch":"ok",
#  "qdrant":"ok","embedding":"ok","reranker":"ok"}}
```

## 3. Biến môi trường bắt buộc (.env)

Copy `.env.example` → `.env`. Các biến quan trọng nhất (giá trị máy gốc trong
ngoặc — **đổi khi mang sang máy khác**):

```ini
# Auth (P11) — bắt buộc nếu API_AUTH_ENABLED=true
API_AUTH_ENABLED=true
HUB_ADMIN_KEY=<random 32+ chars>          # bootstrap admin key dsa_live_...

# Hub DB (hub-postgres)
HUB_PG_PASSWORD=<đặt riêng>               # default 'searchhub' — đổi khi prod

# Hybrid retrieval (P11)
HYBRID_RETRIEVAL_ENABLED=true
QDRANT_DENSE_ENABLED=true
HYBRID_TOP_K=60
HYBRID_RRF_K=60
HYBRID_FUSED_TOP=40

# LLM synthesis (tùy chọn — không có thì /v1/answer chạy fallback extractive)
LLM_API_KEY=...
LLM_BASE_URL=http://host.docker.internal:18434/v1   # llama.cpp local

# Firecrawl stack (đã có default trong compose; SEARXNG_ENDPOINT trỏ :8080)
```

Lưu ý: `.env` KHÔNG nằm trong repo (gitignored). Trên máy gốc nó chứa sẵn mọi
biến đã cấu hình; máy mới phải tự điền. **API keys thật (dsa_live_...) không
bao giờ nằm trong repo** — chúng sinh ra bằng CLI (mục 5).

## 4. Index dữ liệu (nếu cần khôi phục corpus)

OpenSearch/Qdrant là **stateful volumes**. Máy mới = index rỗng. Corpus máy gốc
(~1.2k passages) được đánh qua hoạt động bình thường của pipeline (mỗi query
/v1/search có mode → indexing worker ghi passages). Nếu cần seed nhanh:

```bash
# chạy vài query research để pipeline tự index
curl -X POST http://127.0.0.1:8888/v1/search \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"query":"docker compose networking","mode":"fast"}'
```

## 5. Tạo API key (tenant mới)

```bash
cd search-router
python manage_keys.py create --tenant <tên> --name "<mô tả>" \
  --scopes "search:read,answer:use,research:use" --rpm 60 --quota 1000
# In ra full key dsa_live_... đúng 1 lần — lưu ngay.
python manage_keys.py list
```

## 6. Canonical storage (hub-postgres + MinIO)

Phase 1 adds two stateful services: **hub-postgres** (PostGIS) — canonical
tables (`documents`, `document_snapshots`, `crawl_frontier`, `businesses`,
`administrative_units`) — and **minio** — S3 bucket `sh-raw-snapshots` giữ raw
HTML capture, key = `document_snapshots.storage_key`.

Schema là versioned migrations (`search-router/db/migrations/NNN_*.sql`),
runner = `search-router/db/migrate.py`. search-router tự chạy migrations lúc
startup (fire-and-forget); chạy tay khi upgrade / provision DB mới:

```bash
# host (DSN từ HUB_DATABASE_URL hoặc HUB_PG_*; default 127.0.0.1:5433)
cd search-router && python -m db.migrate --status   # xem applied/pending
python -m db.migrate                                # apply pending

# hoặc trong container
docker compose exec search-router python -m db.migrate
```

Migrations idempotent (chạy lại = no-op) và serialized bằng pg advisory
lock — nhiều router replica start cùng lúc vẫn an toàn. Verify sau upgrade:

```bash
docker compose exec hub-postgres psql -U searchhub -c \
  "SELECT version FROM schema_migrations ORDER BY version"
docker compose exec hub-postgres psql -U searchhub -c '\dt'   # businesses, crawl_frontier, ...
```

Credentials: `HUB_PG_PASSWORD` (Postgres), `MINIO_ROOT_USER` /
`MINIO_ROOT_PASSWORD` (MinIO, default dev `minioadmin` — đổi khi prod),
`MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY` cho search-router,
`MINIO_BUCKET_RAW` (default `sh-raw-snapshots`). Frontier claim lease:
`FRONTIER_CLAIM_LEASE_SECONDS` (default 900 — row `fetching` quá lease sẽ
được reclaim, chống stuck khi worker crash).

## 7. MCP server (Hermes integration)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-mcp-server.ps1
# → http://127.0.0.1:8901/mcp (streamable-http), Bearer SEARCH_HUB_ROUTER_KEY từ .env
```

MCP adapter là **optional** (`adapters/mcp_server.py`, package `mcp`):
`uv sync` / `pip install -r requirements.txt` trên host đã đủ; install
slim (`uv sync --no-dev`) thì thêm `uv sync --extra mcp` hoặc
`pip install mcp`.

Đăng ký vào Hermes config.yaml:

```yaml
mcp_servers:
  search_hub:
    url: "http://127.0.0.1:8901/mcp"
    timeout: 180
```

## 8. Tests

```bash
cd search-router
env -u PYTHONPATH python -m pytest tests/ -q -p no:cacheprovider
# Mong đợi: 1000+ passed (unit, không cần stack live)
# E2E (cần stack live + key): pytest tests/e2e/ -q  — xem scripts/e2e_run.sh
```

Deps host cho tests: `uv sync` (hoặc `pip install -r requirements.txt`) —
đủ cho unit tests, không cần stack live.

## 9. Vane (đã archive — không còn trong repo chính)

Vane (Next.js search UI, client của :8888) đã tách sang branch
`archive/vane-master` kể từ Phase 0 cleanup. Checkout riêng nếu cần chạy UI:

```bash
git fetch origin archive/vane-master
git worktree add ../vane archive/vane-master   # hoặc clone riêng
```

## 10. Khắc phục sự cố nhanh

- **BGE services unhealthy**: lần đầu pull model chậm — chờ thêm 2-3 phút;
  `docker compose restart embedding-service reranker-service` nếu port-proxy
  Docker Desktop bị gãy (accept-then-close).
- **Port 5433/6380 accept-then-close trên Windows**: Docker Desktop port-proxy
  hỏng → restart Docker Desktop, hoặc thao tác DB qua `docker compose exec`.
- **LLM 401**: /v1/answer vẫn trả kết quả (fallback extractive) nhưng chậm —
  mỗi stage cháy timeout 180s. Sửa LLM_BASE_URL/LLM_API_KEY trong .env.
- **`docker compose up --build` kéo theo rebuild cả firecrawl**: dùng
  `docker compose up -d --no-deps --build search-router` khi chỉ đổi router.
- **Submodules thiếu**: `git submodule update --init --recursive`.

## 11. Cấu trúc repo

```
search-router/        # FastAPI app chính (1000+ tests) — code độc lập
eval/                 # offline eval framework
docs/                 # GOVERNANCE.md, api/ (public
                      # API contract), quality-*.md, research-notes/
deploy/nginx/         # reverse-proxy scaffold cho search.duyai.app (chưa deploy)
firecrawl/ searxng/   # submodules (runtime deps only)
scripts/              # start-all.*, start-mcp-server.ps1, changed-py.sh, e2e_run.sh
THIRD_PARTY_LICENSES.md               # license inventory + AGPL analysis
docker-compose.yml    # 17 services (16 core + debug profile)
```

Đọc thêm: `STATUS.md` (hiện trạng chi tiết + runbook vận hành),
`docs/api/README.md` (public API contract).
