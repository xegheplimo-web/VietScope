# Third-Party Licenses — Search-Hub

> Phase 0 cleanup (2026-09-23). Inventory of every third-party component the
> stack ships or depends on, its license, how we use it, and the resulting
> obligations. Review when bumping versions in `docker-compose.yml` /
> `requirements.txt` / `.gitmodules`.

## AGPL ảnh hưởng gì khi bán API?

Firecrawl và SearXNG đều là **AGPL-3.0** — license có điều khoản "network use"
(§13): nếu bạn *sửa* chương trình AGPL rồi cho người dùng tương tác với nó qua
mạng, bạn phải cung cấp source code của bản đã sửa cho những người dùng đó.

Điểm mấu chốt cho mô hình "bán API search":

1. **Chạy như container riêng ≠ phân phối software.** Search-Hub gọi
   SearXNG/Firecrawl qua HTTP trên Docker network. Bán quyền truy cập API
   `/v1/*` của search-router **không phải** "convey" (phân phối) code AGPL —
   khách hàng nhận kết quả search, không nhận binary/source. Không phát sinh
   nghĩa vụ copyleft cho search-router.
2. **Nghĩa vụ phát sinh khi (a) phân phối** — ship container/binary cho khách
   tự host → phải kèm source + license; **hoặc (b) sửa code AGPL rồi expose
   qua network** — §13 bắt buộc cung cấp source bản đã sửa cho user của service
   đó. ⚠️ Caveat: worktree `firecrawl/`/`searxng/` đôi khi có local edits
   (`git submodule status` hiện `m`). Build image từ tree đã sửa rồi phục vụ
   scrape/search công khai = kích hoạt §13 cho service đó. Giữ submodule sạch,
   pin upstream; nếu phải patch thì publish patch.
3. **Không nhét code AGPL vào search-router.** Search-router (first-party,
   MIT-compatible) chỉ nói chuyện qua process boundary. Không copy module
   Python/TS từ `firecrawl/` hay `searxng/` vào `search-router/` — copy =
   derivative work → toàn bộ phần dính phải AGPL.

Khuyến nghị vận hành: giữ tách biệt process (đúng kiến trúc hiện tại), không
link/copy AGPL code, pin submodule upstream sạch, và nếu có patch thì publish
fork. Apache-2.0 components chỉ cần giữ NOTICE; MIT/BSD chỉ cần giữ license text.

## Docker images (`docker-compose.yml` — pinned 2026-09-23)

| Component | Version pinned | License | Cách dùng | Nghĩa vụ chính | Modified? |
|---|---|---|---|---|---|
| SearXNG | `searxng/searxng:2026.9.11-61d660276` | AGPL-3.0-or-later | Image pull + mount `searxng/container/core-config/settings.yml` | §13 nếu sửa & expose; giữ config riêng không tính sửa program | No (config mount chỉ là settings) |
| Valkey | `valkey/valkey:9.1.2-alpine` | BSD-3-Clause | Image pull ×2 (`searxng-valkey`, `hub-redis`) | Giữ license text | No |
| Redis | `redis:8.10.1-alpine` | RSALv2 **hoặc** SSPLv1 **hoặc** AGPLv3 (tri-license từ Redis 8) | Image pull (`firecrawl-redis`) | Chọn AGPLv3 path → §13 nếu sửa & expose; không sửa → chỉ attribution | No |
| RabbitMQ | `rabbitmq:3.13.7-management` | MPL-2.0 | Image pull (`firecrawl-rabbitmq`) | File-level copyleft khi sửa; không sửa → none | No |
| PostgreSQL + PostGIS | `docker.io/postgis/postgis:16-3.5-alpine` | PostgreSQL License (~MIT) cho engine; **GPL-2.0** cho PostGIS extension | Image pull (`hub-postgres`) + mount `search-router/db/init.sql`; PostGIS dùng cho `businesses`/`administrative_units` geo queries | Giữ license text; PostGIS chạy in-process như extension — không sửa code → chỉ cần giữ notices | No |
| MinIO | `elestio/minio` (official MinIO `RELEASE.2025-09-07T16-13-09Z` binary — quay.io pull-gated Oct-2025) | AGPL-3.0 (server; commercial license có sẵn từ MinIO) | Image pull (`minio`) — S3-compatible object store cho raw snapshots (`document_snapshots.storage_key`), bucket `sh-raw-snapshots` | §13 giống SearXNG/Firecrawl: không sửa code → chỉ attribution; nếu patch MinIO rồi expose qua network → phải publish source bản sửa | No |
| FoundationDB | `foundationdb/foundationdb:7.3.63` | Apache-2.0 | Image pull (`firecrawl-foundationdb`) | NOTICE | No |
| OpenSearch | `opensearchproject/opensearch:2.18.0` | Apache-2.0 | Image pull | NOTICE | No |
| OpenSearch Dashboards | `opensearchproject/opensearch-dashboards:2.18.0` | Apache-2.0 | Image pull | NOTICE | No |
| Qdrant | `qdrant/qdrant:v1.19.1` | Apache-2.0 | Image pull | NOTICE | No |
| Firecrawl API | `search-hub-firecrawl-api` (built) | AGPL-3.0 | Build từ `firecrawl/apps/api` (submodule @ v2.11.199) | §13 như trên; image build từ source = phải có source availability nếu phân phối image | Worktree dirty — giữ upstream-sạch trước khi build prod |
| Firecrawl Playwright | `search-hub-firecrawl-playwright` (built) | AGPL-3.0 (thuộc firecrawl monorepo) | Build từ `firecrawl/apps/playwright-service-ts` | Như trên | Như trên |
| Firecrawl NuQ Postgres | `search-hub-firecrawl-postgres` (built) | AGPL-3.0 (build scripts) / PostgreSQL License (runtime) | Build từ `firecrawl/apps/nuq-postgres` | Như trên | Như trên |
| search-router / reranker / embedding | `search-hub-*` (built) | First-party | Build từ `search-router/` | — | — |

## Git submodules (`.gitmodules`)

| Component | Pin | License | Cách dùng | Modified? |
|---|---|---|---|---|
| `firecrawl` | `fe49e4c` (v2.11.199) | AGPL-3.0 | Source build cho 3 images trên; KHÔNG import code | Worktree dirty (local edits — xem §AGPL caveat) |
| `searxng` | `d7bd922` (master) | AGPL-3.0-or-later | Chỉ mount `container/core-config/settings.yml`; image dùng bản official pinned | Worktree dirty |

## Models (pull từ HuggingFace lúc container start)

| Model | License | Cách dùng | Nguồn |
|---|---|---|---|
| `BAAI/bge-m3` | MIT | embedding-service :8892 | HF model card (checked 2026-09-23) |
| `BAAI/bge-reranker-v2-m3` | Apache-2.0 | reranker-service :8891 | HF model card (checked 2026-09-23) |

## Python deps (`search-router/requirements.txt`)

| Package | Constraint | License |
|---|---|---|
| fastapi | >=0.115.0 | MIT |
| uvicorn[standard] | >=0.30.0 | BSD-3-Clause |
| httpx | >=0.27.0 | BSD-3-Clause |
| pydantic | >=2.0.0 | MIT |
| redis (py) | >=5.0 | MIT |
| ddgs | >=9.14.0 | MIT |
| sentence-transformers | >=2.7.0 | Apache-2.0 |
| transformers | >=4.40.0 | Apache-2.0 |
| torch | >=2.1.0 | BSD-3-Clause |
| mdurl | ==0.1.2 | MIT |
| opensearch-py | ==2.8.0 | Apache-2.0 |
| asyncpg | >=0.30.0 | Apache-2.0 |
| trafilatura | >=2.0 | Apache-2.0 |
| mcp | >=1.0 | MIT |

Tất cả pip deps đều permissive — không có copyleft trong search-router
runtime. Kiểm tra lại bằng `pip-licenses` khi bump version.

## Đã xóa / archive (Phase 0)

| Component | License | Trạng thái |
|---|---|---|
| `qdrant-master/` | Apache-2.0 | Vendored source đã xóa khỏi main (vẫn còn trong git history); runtime dùng image `qdrant/qdrant:v1.19.1` |
| `Vane-master/` | MIT (itzcrazykns/vane) | Archive sang branch `archive/vane-master` — không còn trong main |
| `AI-Search-Hub` (submodule) | — (upstream repo) | Đã bỏ gitlink |
| `llm-answer-engine` (submodule) | MIT (upstream) | Đã bỏ gitlink; reranker.py chỉ còn comment "inspired by" |
