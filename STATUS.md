# VietScope — Current Status

> Search-Hub codebase · snapshot 2026-10-01 · `GET /v1/health` →
> `{"status":"ok","version":"3.0.0"}`. This file reflects **current**
> state only — phase history lives in git.

## Release

- Repo: `xegheplimo-web/VietScope` (public), default branch `main`.
- Version: 3.0.0 (`search-router/pyproject.toml`).
- Python 3.12 canonical · uv lockfile · 23-service compose
  (16 core + `observability`/`debug`/`import` profiles).

## Architecture

Canonical: [`ARCHITECTURE.md`](ARCHITECTURE.md). Two data paths:

- **Query path**: client → search-router `:8888` → adaptive provider
  fan-out (SourceRouter → ProviderHealthMonitor → FederatedExecutor over
  SearXNG/DDGS/Exa/arXiv/HN/code_search/OpenSearch) → hybrid retrieval
  (OpenSearch BM25 ∥ Qdrant dense → RRF) → rerank → LLM/extractive
  synthesis.
- **Acquisition path**: `crawl_frontier` → fetch → MinIO raw snapshot →
  `document_snapshots` → ExtractionService (Trafilatura) → `documents`
  → IndexingWorker → OpenSearch + Qdrant.

## Enabled capabilities

| Capability | Surface |
|---|---|
| `/v1/search` unified (raw ∥ research+hybrid by `mode`) | `search:read` |
| `/v1/answer` + SSE stream | `answer:use` |
| `/v1/research` + stream (multi-hop `agent/` pipeline) | `research:use` |
| `/v1/news`, `/v1/read` (SSRF-guarded), `/v1/verify` | per-endpoint scopes |
| `/v1/business/search` (PostGIS) | `business:use` |
| Crawl pipeline: robots RFC 9309, politeness, conditional GET, claim-lease frontier, change-rate EMA | `crawler/` |
| Snapshot → Trafilatura extraction → quality gate → provenance | `extraction/`, migration `005` |
| OpenSearch `web_documents`/`web_passages` + Qdrant `web_passages_v1` (deterministic IDs, stale-passage cleanup) | `workers/indexing_worker.py` |
| Unified LLM gateway — model roles, retry, per-backend-lane circuit breakers, stream, metrics (A9) | `core/inference_gateway.py` |
| Local places serving — canonical → PlaceDocumentV1 → OpenSearch + PostGIS + Redis, `/v1/places/*`, degrade-by-design lanes | `serving/places/`, migration `012` |
| Source federation: unified `ProviderResult`/`ProviderSpec` schema, spec table + JSON/env config | `providers/base.py`, `core/provider_registry.py` |
| Provider health scoring + circuit breaker (rolling metrics → score → closed/open/half_open probe, backoff) | `core/provider_health.py` |
| Adaptive fan-out: intent → lane weights (OFF→VERY_HIGH) → spec/locale/budget/circuit filtered picks | `core/source_router.py`, `core/federation.py` |
| `GET /v1/providers/health` — provider scores + engine health (`admin:debug`) | `api/v1.py` |
| OpenAI-compat gateway: `GET /v1/models` + `POST /v1/chat/completions` (stream/non-stream, `chat:use`, OpenAI error shape) | `api/openai_compat.py` |
| Tiered reader: cache → HTTP → Trafilatura → quality gate → Firecrawl → Playwright | `pipeline/reader.py` |
| Passage-level citations: claim_id + evidence[{source_id, passage_id, url, quote, quote_start/end}] | `agent/orchestrator.py`, `evidence/citation.py` |
| Real SSE streaming: live state events + LLM token deltas + keepalive + `answer.final` reconcile | `run_research(emit=)`, `api/v1.py` |
| Conversation context: `session_id` + Redis `convctx:*` state, follow-up resolution → standalone query | `core/conversation.py`, `api/v1.py` |
| API-key auth + scope map + rpm/quota + metering (sha256 only) | hub-postgres + hub-redis |
| MCP adapter `:8901` (optional, host process) — default surface `search`/`fetch_evidence`/`code_search`; `fetch`+`research` opt-in via `MCP_ENABLE_DEEP_RESEARCH`; `answer` never registered (Hermes synthesizes) | `adapters/mcp_server.py` |

## Disabled / gated capabilities

| Feature | Flag / state |
|---|---|
| Qdrant sparse + multivector lanes | `QDRANT_SPARSE_ENABLED=false`, `QDRANT_MULTIVECTOR_ENABLED=false` (no data yet) |
| Image search (`/v1/images`) | `QDRANT_IMAGES_ENABLED=false` → `count:0` |
| Firecrawl render fallback in crawler | `CRAWLER_FIRECRAWL_FALLBACK` (opt-in; provenance persisted as own snapshot when used) |
| LLM synthesis | works extractive-fallback when `LLM_API_KEY` unset/invalid — slow degraded path |
| Public deploy | nginx scaffold ready (`deploy/nginx/`); no DNS/TLS yet |

## Current test status

- Unit/integration: **1897 passed, 33 skipped, 0 failed** — 1930
  collected (2026-10-01, post-OPS-1, host suite on Python 3.12.14 —
  3.12.10/3.13 lack `HTMLParser(scripting=)` used by
  `crawler/fetcher.py`).
- E2E crawl→search: **PASS** `test_crawl_to_search.py` (37.5s, live
  Docker stack; needs `HUB_DATABASE_URL` pointing at :5433 for host
  runs).
- Static gates: ruff check + format clean; pyright 65/65 ratchet max;
  baseline contracts 9/9; `uv lock --check` clean; OSV + pip-audit
  clean; gitleaks 2 history-only vendored-fixture findings.
- P17 live benches (real runs, 10k/100k/500k places): full rebuild
  1.9s/11.4s/67.8s; warm p50 <1.1ms; c25 QPS 474/469/319; Redis hit
  0.975 — full table in [`docs/production-gate-v1.md`](docs/production-gate-v1.md).
- **PRODUCTION_GATE_V1 = PASS** — report:
  [`docs/production-gate-v1.md`](docs/production-gate-v1.md)
  (16/16 services healthy; degraded lanes honest; A9 26/26 on mock
  backends; 3 latent defects fixed on the gate PR: jsonb
  `opening_hours` projection, A9 role-error clear-on-success, epoch-
  scoped by-id place cache).
- P13 live validation on the degraded stack (236 calls): `/v1/search`
  + `/v1/answer` 0 errors, `answer_present` 1.00, SSE + CitationV2
  verified — report: [`docs/p13-validation.md`](docs/p13-validation.md).
- CI: `ci.yml` (unit + build), `quality.yml` (ruff/coverage/supply
  chain), `e2e.yml` (containerized crawl→search). PR #45 CI: 9 pass /
  0 fail / 1 skip.

## Known blockers

1. **No LLM endpoint configured** — `.env` `LLM_API_KEY` empty,
   `LLM_BASE_URL` defaults to `api.openai.com`; `/v1/health` honestly
   reports `llm: "not configured"` and synthesis runs extractive
   fallback. A9 routing validated against mock OpenAI-compatible
   backends (26/26). Fix: valid `LLM_API_KEY`/`LLM_BASE_URL` (+ per-role
   `LLM_<ROLE>_*` if wanted).
2. ~~**`GET /v1/providers/health` lacks an `admin:debug` gate**~~ —
   RESOLVED: the router-wide `require_api_key` dependency maps every
   non-public GET to `admin:debug` (`security/apikeys.py`), so this
   endpoint is already gated when `API_AUTH_ENABLED=true`. The P13
   finding predates that dependency; keep as a regression-tested gate.
3. **CPU embedding throughput** — a 30k-word doc embeds in ~13 min
   (BGE-M3 CPU). Consider capping passages/doc for crawl-sourced docs.
4. **SearXNG :8080 + Firecrawl :3002 bind `0.0.0.0`** — fine on a dev
   box; bind loopback or put behind nginx before exposing the host.

### Non-blocking environment notes

- **SearXNG submodule pin `d7bd9225` does not resolve upstream**
  (custom `engine-matrix-54` ref). Compose uses the prebuilt
  `docker.io/searxng/searxng` image so runtime is unaffected, but
  `git submodule update --init` fails on a fresh clone — repoint to an
  upstream commit deliberately, don't silently bump.
- **SearXNG config** is now repo-owned (`deploy/searxng/settings.yml`,
  mounted at `/etc/searxng`) — fresh clone + `docker compose up`
  serves `/search?format=json` with no container edits.
- `test_p16_pg::test_places_search_and_detail` observes the live
  OpenSearch index when run against a populated stack — green on an
  empty index; coupling noted in the gate report.
- Docker Desktop stale port-proxies (Windows): after mass recreation
  host TCP drops the handshake — `docker compose up -d --force-recreate
  <svc>` per service.
- `quality.yml` references `aquasec/osv-scanner` (image does not
  exist; step soft-skips) — should be `ghcr.io/google/osv-scanner`.

## Next phase

Answer-engine consolidation (P0→P5, one phase at a time):
P0 unified inference gateway **done** — all LLM calls now route through
`core/inference_gateway.py` (roles, retry, circuit breaker, stream).
P1 reader consolidation **done** — answer/research lanes read via
`pipeline/reader.py` (HTTP→Trafilatura→quality gate→Firecrawl/Playwright
fallback); no direct Firecrawl calls in the research path.
P2 CitationV2 **done** — `run_research` citations carry claim_id +
passage-level evidence (source_id, passage_id, url, quote, quote
offsets) on `/v1/answer`, `/v1/research`, and the SSE `citation` event.
P3 real SSE **done** — `run_research(emit=)` streams live state events
(planning→search→source→source.read→evidence→synthesizing→answer.delta
→verified) and `stream_research_answer` yields real LLM tokens; the
600-char fake chunking is gone. `answer.final` reconciles when
verification edits the streamed text; keepalive comments prevent proxy
idle timeouts.
P4 conversation context **done** — `session_id` on `/v1/search`/`/v1/answer`
loads Redis `convctx:*` state (in-memory fallback), `history` supplies
stateless turns (stored state wins on merge), and anaphoric follow-ups
resolve to standalone queries via the gateway (heuristic fallback, 2s
bound); responses echo `session_id` + `followup_resolved`/`resolved_query`.
P5 real cache **done** — `pipeline/semantic_cache.py` is a 4-layer
Redis-backed cache (exact answer / semantic ≥0.94 cosine via BGE embed /
evidence bundle / page) with in-memory fallback; `/v1/search` research
lane and `/v1/answer` short-circuit on hits (`cache:{hit,layer}` marker,
fresh `query_id`), keyed on the resolved standalone query + endpoint +
citations flag with freshness-class TTLs (realtime 60s … static 30d).
`SEMANTIC_CACHE_ENABLED=false` disables.
BASELINE-V3 **frozen** — `baseline/` holds generated contract artifacts
(OpenAPI, model JSON schemas, `db/migrations` concatenated, OpenSearch
mappings, Qdrant collections, SSE event vocabulary);
`tests/test_baseline_contracts.py` fails CI on drift — regenerate with
`search-router/scripts/freeze_baseline.py` when a contract change is
intentional. `quality-gates.yml` test_floor ratcheted 700 → 1200.
VN taxonomy (P1) **done** — `SourceType` extended with business/company/
finance/market/administrative/product/medical/document; `SourceRouter.
lane_weights` learned the VN signals (same router, same contract — e.g.
"giá vàng" routes market+news and suppresses ecommerce, "đánh giá VF8"
routes forum+social+product). No new providers yet — P2 subscribes VN
providers to these lanes.
VN Source Registry (P2) **done** — `providers/vn_rss.py` is one adapter
class serving 7 verified VN RSS/Atom feeds (VnExpress, Tuổi Trẻ, Thanh
Niên, Dân Trí, Nhân Dân, VietnamPlus, Công Thương) as first-class
providers: spec rows in `PROVIDER_SPECS` (VN/vi, per-source circuit
breaking, news + government/business/market lanes), query-matched by
accent-folded term coverage (empty ≠ error). Dead feeds probed and
dropped (vietnamnet, baochinhphu, laodong, vneconomy — no readable RSS).
New source = 1 `VN_FEEDS` row + 1 spec row.
VN News/Gov/Legal (P3) **done** — `providers/vn_gnews.py` adds three
Google-News-RSS metasearch lanes (server-side queryable, hl=vi/gl=VN):
`gnews_vn` (all VN outlets), `gnews_vn_gov` (site:chinhphu.vn|gov.vn —
full-text nghị định straight from chinhphu.vn), `gnews_vn_legal`
(site:vbpl.vn|vanban/congbao.chinhphu.vn|thuvienphapluat|luatvietnam —
legal lane gets first-party texts without scraping portal search).
Publisher name kept in `engine` for future authority scoring; items are
Google redirect URLs (fetch follows redirects). Verified live: "nghị
định hóa đơn điện tử" returns NĐ-CP 254/2026 full text from
xaydungchinhsach.chinhphu.vn. Router already routes the P3 example
("Hộ kinh doanh...") legal+government VERY_HIGH — no router change.
VN Authority/Freshness (P5-VN) **done** — `authority_for(domain,
vertical)` makes authority intent-dependent: `VN_VERTICAL_SCORES` maps
domains to per-lane scores (vbpl.vn = 1.0 on legal but 0.6 on news;
otofun.net = 0.7 on product but 0.1 on legal — "không một domain_score
duy nhất"), with `VN_SOURCE_META` (official/ownership/geography/update
cadence) and `authority_source_meta()`. `authority_score` keeps its
signature (delegates). Freshness got per-lane horizons
(`FRESHNESS_HALFLIFE_DAYS`: market 6h → legal/gov 10y) wired into both
`ranking/quality.py` scorers and the orchestrator `_freshness_bonus`
(graded 0.3→0 decay per lane; legacy buckets for unlaned items). The
lane flows ProviderResult.source_type → `Source.source_lane` (new
additive field — baseline OpenAPI/schema artifacts regenerated) →
orchestrator `_rank` and the evidence aggregator's trust labels.
Own VN Corpus (P4) **done** — `crawler/seeds.py` registry is now
structured: `SeedSite(url, lane, extra)` ×33 seeds covering government,
administrative, legal (vbpl/vanban/congbao/luatvietnam first-party),
news, market, places; `SEED_LANES`/`seed_lane()` key lanes by host with
registrable-domain fallback so chinhphu.vn sub-portals keep `legal`
while the parent stays `government`. `expand_seeds()` wires the existing
sitemap/feed machinery into seeding: robots-declared `Sitemap:` lines +
seed `extra` feeds + conventional probe paths → page URLs enqueued at
`priority=0.6`, `discovered_from="sitemap:<domain>"`, domain-scoped,
`SEED_EXPAND_CAP`=500/site. `load_seeds(expand=True)` and
`scripts/crawl_seed.py --expand` expose it. Corpus docs now carry
`metadata.source_lane` from `seed_lane(domain)` at ingest — the own
index shares the P5-VN vertical authority/freshness context. Live probe:
tuoitre RSS → 50 items, vbpl.vn → 37-doc sitemapindex.
Entity Resolution (P6) **done** — `core/entity_resolver.py` maps
accent-folded surface forms to canonical `Entity(id, canonical, kind,
aliases, match_forms)`: "Sài Gòn"/"TP HCM"/"Ho Chi Minh City" →
`loc:ho_chi_minh_city`, "VIC"/"CTCP Tập đoàn Vingroup" → `co:vingroup`;
legal citations resolve by pattern ("nđ 254/2026", "Nghị định
254/2026/NĐ-CP" → `legal:nd-254-2026`). `QueryUnderstanding.analyze()`
populates `QueryProfile.entity_ids` (additive — `entities` token list
unchanged, no frozen-contract drift); `SourceRouter.lane_weights` floors
lanes per resolved kind via `_ENTITY_LANE_BOOST` (company→company
VERY_HIGH, loc→places/administrative, legal→legal+gov, product→
product+ecommerce). `resolve_match_forms()` emits folded + kebab
variants for URL-slug matching.
Places/Business (P7) **done** — `/v1/business/search` no longer requires
lat/lon: `_resolve_geo_anchor` resolves the query's location itself
(P6 `loc:` entity canonical → Nominatim; else the phrase after
"gần|near|ở|tại|quanh" → geocode). Response gains `anchor{name,lat,lon,
resolved_from}` and `provider` reports contributing lanes
("geo+osm+web"). New lane order: PostGIS (when configured) → OSM
Overpass POIs (`services/geo.py`, Nominatim VN-biased geocode +
`overpass_amenities` with tag filters) → web-extract backfill. VN
category taxonomy maps "nhà thuốc"→pharmacy, "chợ"→marketplace,
"trạm y tế"→clinic etc. onto OSM amenity/shop/tourism tags
(`osm_tag_for`). All geo calls degrade to empty on timeout/HTTP error.
Geo (`osm2pgsql` + OSM Vietnam + PostGIS) and LiteLLM service come after.
Product/Market (P9) **done** — `providers/vn_market.py` turns market
quotes into provider results on the same contract: three registered
sources, one per instrument family so a walled endpoint trips only its
own circuit — `vn_gold` (webgia.com SJC board scrape; sjc.com.vn itself
is Cloudflare-walled), `vn_fx` (open.er-api.com USD base → VND + computed
EUR/JPY×100/CNY/GBP/AUD crosses), `vn_stock` (VNDirect dchart history →
VN-INDEX/VN30/HNX/UPCOM close + %Δ). Query relevance is a soft keyword
filter on folded tokens ("tỷ giá USD" → USD row only); a non-JSON body
or per-symbol failure degrades that row, never the lane. Probed ~15
candidate endpoints live from the dev box; SJC API, cafef, 24h finance,
goldapi/metals.live are unreachable or key-gated — webgia/giavang HTML,
er-api, and VNDirect dchart are the ones that answer. `market` lane at
weight 1.0 for "giá vàng hôm nay" reaches all three via the existing
fan-out — no router changes needed.
Multimodal (P11) **done** — closed the remaining wiring gaps instead of
adding a multimodal stack: `SearchType` gains `video` (additive; baseline
artifacts regenerated) so `/v1/search type=video` flows to SearXNG's
videos category through the existing `cat_map`/`_LANE_TO_CATEGORY`
machinery; raw search rows now expose `thumbnail`; `_parse_results`
prefers `img_src` over `thumbnail_src` for image-category hits so the
lane hands back a usable visual URL; `/v1/images` keeps Qdrant corpus
first (when `QDRANT_IMAGES_ENABLED`) then backfills from the live lane
(SearXNG images → DDGS) through the same breaker/health path, deduped by
src/page URL — the endpoint answers even before the CLIP collection is
populated, and a corpus failure degrades rather than 500s.
VN Benchmark (P12) **done** — `eval/datasets/vietnam/` adds 10 vertical
suites (general/news/government/legal/company/places/market/product/
community/deep_research, 118 queries) whose ground truth pins first-party
VN sources (vbpl.vn, chinhphu.vn, hsx.vn, …). `eval list-datasets` walks
one subdirectory level so `vietnam/legal` etc. are runnable; the
`vn_*`/`vn_research` categories join `VALID_CATEGORIES`. New `authority`
metric reuses the router's own `ranking/authority.py` table via
`eval/vn_authority.load_scorer` (sys.path injection, degrade to None when
search-router isn't importable) — `evaluate_query`/`aggregate_metrics`
carry it when `--authority` (with `--authority-vertical <lane>`) is passed,
and `eval report --authority` scores saved report JSONs post-hoc off
`per_query[].retrieved_domains`. Known: `test_cli_run_baseline_diff`
fails on this Windows box on clean `main` too (two report writes land
on the same µs-resolution timestamp filename) — pre-existing flake,
not from this phase.
P13 Production Validation **done** — `eval` now measures the answer
lane end-to-end: `EvalQuery.expected_facts` (accent-folded,
`fold()` in `eval/answer_metrics.py`), `SearchClient.answer()` on
`/v1/answer`, `run_answer_dataset` feeding sources through both the
retrieval metrics and the new answer metrics (answer_correctness,
citation_precision/recall vs `expected_urls`, unsupported_claim_rate
as empty-evidence proxy, evidence_quote_rate, cited_source_coverage,
verified, coverage). `--endpoint answer --answer-mode` on `eval run`.
Live run over all 10 VN verticals on the degraded stack (DDGS + 13 VN
providers; SearXNG/Redis/Postgres/OS/Qdrant absent; LLM 401): 0 errors
across ~236 calls, `answer_present` 1.00 — retrieval ndcg@10
.049–.344 by vertical, answer correctness 0–1.00 (extractive floor),
citation precision .00–.17, unsupported claims up to .60 (legal).
Findings: gnews redirect URLs mask publisher domains on legal/gov;
freshness unmeasurable (no `retrieved_at` on sources); providers/health
leaks internals unauthenticated; places has no live path without
PostGIS/Overpass. Full report + DoD: `docs/p13-validation.md`.
P14A Vietnam Administrative Data 2025/2026 **done** — the "63 tỉnh →
huyện → xã" assumption is replaced by a versioned administrative graph
covering both eras. `db/migrations/006_admin_graph.sql` extends
`administrative_units`/`administrative_aliases` (normalized names,
admin_level, status, source, source_updated_at; uniqueness moves from
`code` to `(code, valid_from-era)` since 2025 reassigned codes —
new Bắc Ninh = old Bắc Giang's 24), adds `administrative_relations`
(renamed_to/merged_into/split_into/replaced_by/boundary_changed +
effective_date + legal source), and `businesses.admin_unit_id` so
places reference stable internal IDs, not name strings.
`scripts/build_vn_admin_seed.py` builds the canonical seed
(`db/seeds/vn_admin_units.json`: 14,612 units / 14,357 edges /
479 aliases / 205 city hints) from official sources — Công văn
915+1027/CTK-CSCL under QĐ 19/2025/QĐ-TTg for the current era
(34 provinces, 3,321 communes = 2,621 xã + 687 phường + 13 đặc khu,
effective 2025-07-01), dvhcvn Nov-2024/Mar-2025 snapshots for the
historical era incl. the Dec-2024 dissolution wave, and the 34×
NQ-UBTVQH15 commune resolutions for edge provenance.
`core/geo_resolver.py` resolves addresses through the transition graph
(current: "phường Tân Tiến, TP Bắc Giang" → Bắc Ninh; historical:
"huyện Yên Dũng, tỉnh Bắc Giang" → huyện + tỉnh matched → 6 successor
communes + new:24; renamed: "Thừa Thiên Huế" → TP Huế; parent-fallback
for wave-1-vanished communes; ambiguity surfacing for same-name units).
`storage/admin_store.py` carries DictAdminStore (seed/GeoJSON
point-in-polygon) + PgAdminStore (PostGIS ST_Contains, degrades when
unconfigured).
`db/seed_admin.py` idempotently loads the seed into Postgres.
`core/vn_address.py` city aliases now derive from the seed's city_hints
("bắc giang" → "Bắc Ninh") instead of the hardcoded 63-province table
(canonical city strings updated to official 2025 names). New endpoints:
`GET /v1/admin/resolve?q=` and `GET /v1/admin/lookup?lat&lon`.
tests in `tests/test_admin_graph.py` cover seed integrity,
current/historical resolution, merged/renamed provinces, dissolved
districts and communes, ambiguity, accent-insensitive + alias lookup.
Google Maps/POI ingestion (P15+) stays gated on this foundation.

P14B Boundary geometry **done** — current-era polygons attached to all
3,355 units (34 provinces + 3,321 communes) from the MIT-licensed
thanglequoc/vietnamese-provinces-database GeoJSON tree (OSM has zero
commune-level VN coverage: only ~5 VN-tagged level-8 relations exist).
`build_vn_admin_seed.py --geojson <tree>` Douglas-Peucker-simplifies each
MultiPolygon in pure Python (adaptive tolerance: halve while any exterior
ring would drop below 30 pts; 5-decimal coords; seed grows to ~21MB,
still minified one line). Historical units keep geometry NULL — point
lookup serves the current era. `seed_admin.py` pushes geometry via
`ST_MakeValid(ST_GeomFromGeoJSON(...))` (the 4326 GIST column already
exists); DictAdminStore gained a lazy bbox index so the in-memory lookup
ray-casts only bbox-matched candidates. `/v1/admin/lookup` now returns
commune+province live (Ba Đình square → phường Ba Đình + TP Hà Nội;
Yên Dũng town → Phường Vân Hà + Bắc Ninh; outside VN → []).

P15 Source ingestion (raw staging) **done** — multi-source place
acquisition with a strict ingestion-only boundary: Google/OSM/web-corpus
records land in raw staging, NEVER in `businesses` (P16 owns canonical
merge, cross-provider entity resolution, field provenance, confidence).
`db/migrations/007_place_staging.sql` adds `ingestion_runs` (status,
parameters, source_version, new/changed/unchanged/invalid/failed
counters, cursor + JSONB checkpoint, error_summary),
`place_source_records` (source-native identity
`UNIQUE(provider, external_id)` with external_id NOT forced — a partial
unique index `(provider, content_hash) WHERE external_id IS NULL`
covers id-less sources; verbatim `raw_payload` JSONB + `raw_payload_ref`
MinIO overflow >64KB; `observed_at/fetched_at/ingested_at`; P14 anchor
`admin_unit_id` + `resolver_version` + `resolution_confidence` so records
re-resolve when the graph advances — never province/district name keys),
`place_source_errors` DLQ (reason/detail/payload/retryable — one bad
record never kills a 500k run), and `source_policies` (authority/refresh
/usage hints seeded per provider; scoring itself is P16).
`ingestion/base.py` defines the `PlaceSourceAdapter` protocol
(`ingest(context) -> AsyncIterator[RawPlaceRecord]`, `probe()`) —
deliberately distinct from query-time `SearchProvider` — plus
`RawPlaceRecord` with a canonical `content_hash`.
Adapters: `adapters/gmaps.py` consumes gosom-scraper NDJSON (external
worker contract — the scraper is never a FastAPI subprocess; identity
precedence place_id>cid>data_id>link, byte-offset checkpoint resume,
accepts upstream's `longtitude` misspelling and `longitude`);
`adapters/pbf.py` + `adapters/osm_pbf.py` bootstrap nationwide from
`vietnam-latest.osm.pbf` (pure-Python protobuf/PBF reader — varint,
zigzag sint64 deltas, zlib/lzma blobs, dense-node keys_vals — no
Overpass); `adapters/web_corpus.py` bridges existing crawler documents
with place signals (telephone/address/geo/openingHours).
`ingestion/validate.py` is the ingest-time gate (VN-plausible coords
incl. island extent, non-empty name, observed_at required, phone
normalized to +84 preserving raw, canonical website) with zero fuzzy
matching; `ingestion/runner.py` streams adapters through
validate→DLQ→P14 point-in-polygon anchor→asyncpg COPY into a TEXT
staging table→single INSERT…SELECT…ON CONFLICT merge per ~2k batch
(new/changed/unchanged counted by content_hash diff) — resumable via
adapter checkpoints and idempotent across reruns.
`scripts/ingest.py --provider google_maps|osm|web_corpus --file ...`
drives batch imports; `POST /v1/ingest/{provider}` accepts direct record
pushes (≤10k entries) and `GET /v1/ingest/runs` exposes run observability,
all degrading cleanly without Postgres. 28 tests cover the wire decoder,
adapter contracts, validation gate, runner counters/DLQ/COPY-merge and
endpoints.

P1.1 MCP Evidence Contract **done** — the Hermes retrieval path no longer
touches the SSRF-unguarded legacy `/fetch`: `POST /v1/evidence` (`read:use`)
accepts structured `search` rows (`source_id`/`canonical_url`/`published_at`
/`search_provider`/`score`), dedupes on canonical identity, SSRF-checks
every URL, reads bounded-parallel (`max_concurrent=5`) through the tiered
reader, and reranks via the shared `pipeline/passage_reranker` — the MCP
adapter carries zero retrieval logic. Provenance survives end-to-end:
`passage_id` = `{source_id}:p{NNN}` maps claim → passage → source → URL,
and per-source failures return error items rather than sinking the batch.
Auth moved to a scoped `SEARCH_HUB_ROUTER_KEY` (`search:read`+`read:use`)
with `HUB_ADMIN_KEY` deliberately not a fallback; `MCP_TIMEOUT_S` 360 >
router 300; the two false-positive tests (source_id, URL dedup) are real
assertions; tracked SearXNG `secret_key` moved to a `SEARXNG_SECRET` env
placeholder.

P1.2 Hermes Retrieval Surface Lockdown **done** — the MCP surface Hermes
sees now matches the architecture (Hermes is the synthesizer, Search-Hub
is retrieval/evidence): default tools are `search`/`fetch_evidence`/
`code_search`; `fetch` and `research` register only under
`MCP_ENABLE_DEEP_RESEARCH=true` (deep lane also needs a `research:use`
scope beyond the scoped key); `answer` is never registered — it stays a
plain callable for scripts/ops but is not advertised to the agent. The
opt-in `fetch` maps `scrape` → SSRF-guarded `/v1/read` (legacy `/fetch`
survives only for crawl/map, which have no /v1 equivalent). Source
budgets are enforced twice: MCP rejects >8 sources (>15 deep) before any
HTTP call, and the API model hard-caps at 20 so a runaway batch cannot
outlive the request budget. Reader freshness is a first-class contract:
`POST /v1/evidence` takes `freshness = realtime|high|normal|static` →
`realtime` bypasses the page cache entirely, `high`/`normal` bound its
new caller-supplied `max_age_s` TTL (5m/1h), `static` keeps never-expire
behavior — "giá vàng hôm nay" queries can no longer be served a stale
snapshot until process restart.

P1.2 hardening follow-up **done** — MCP startup verification is now real
end-to-end: `scripts/start-mcp-server.ps1` checks port-8901 ownership
(command line must be `adapters.mcp_server`), completes the MCP
`initialize` → `notifications/initialized` → `tools/list` handshake, and
matches the tool surface exactly (3 default / 5 deep-flag). It also
probes `GET /v1/auth/check` — a new always-public endpoint whose body is
the verdict (`authenticated`/`auth_enabled`/`tenant`/`scopes`, never
401/403) — so a dummy/wrong/revoked `SEARCH_HUB_ROUTER_KEY` or missing
required scopes fails closed instead of printing READY. The interpreter
is pinned to `search-router/.venv/Scripts/python.exe` (no global-Python
ambiguity), foreign-process output prints `Name`/`ExecutablePath` rather
than a raw command line that could carry secrets, and the stale E2E
assertion now expects the locked 3-tool surface.

## P15.1 — Source Ingestion Production Hardening (2026-09-24)

Production-hardening pass over P15 before P16 (audit-driven):
- **OSM full coverage**: `pbf.py` now decodes Way + Relation (member
  refs/roles/types, delta memids) alongside nodes, yielding positioned
  `OsmElement`s (blob offset + in-blob index). The fallback adapter runs
  a four-pass flow — scan (POI ways/relations + member-way resolve),
  coords (full member-node fill, never partial), nodes (POI node emit),
  emit (way/relation records with member-coordinate centroids) — so
  supermarkets/schools/hospitals mapped as polygons are ingested.
  `osm_osmium.py` is the preferred production backend (pyosmium ≥3.7,
  `osmium.FileProcessor` streaming, `pip install search-router[osmium]`);
  the stdlib reader remains the zero-dep fallback.
- **Blob-safe checkpoints**: the nodes stage checkpoints each yielded
  element's own `(blob_offset, index+1)` — a crash mid-blob can no longer
  skip unprocessed elements (the old `f.tell()` pointed past whole blobs).
- **Real resume**: `run_ingestion(resume_of=N)` loads a prior run's
  checkpoint/cursor/parameters (provider-checked) into a new run with
  `resume_of` lineage; `scripts/ingest.py --resume-run` exposes it.
- **Observation history**: `place_source_records` stays current-state
  (merge upsert); every sighting also appends to
  `place_source_observations` (run-scoped, append-only, change_type
  new|changed|unchanged) — re-ingestion no longer destroys history.
- **Hash split**: `identity_hash` (stable dedup key for id-less rows —
  phone/hours edits update instead of duping) vs `observation_hash`
  (all mutable fields incl. opening_hours drive change detection);
  `content_hash` remains a deprecated alias.
- **Run status + exit codes**: result dicts carry `status`
  (done|failed|aborted); the CLI exits 0 only on done.
- **Safe DLQ**: rejected payloads are always valid JSON — small verbatim,
  oversized via MinIO overflow or a `{"truncated", "size_bytes",
  "preview"}` envelope — never a mid-string cut that breaks the jsonb
  cast. Raw staging payloads likewise overflow with `raw_payload_ref`.
- **Dataset versioning**: runs record `adapter_version` separately from
  `source_dataset` (file name + sha256 + size + hash scope).
- **Corpus streaming**: WebCorpusAdapter keyset-pages `documents`
  (`WHERE doc_id > $1`, resumable `checkpoint["after"]`) — the corpus is
  never materialized; JSONL/docs iterables stream lazily too.
20 new tests cover way/relation decode, mid-blob resume, checkpoint
granularity, resume-of lineage, observation history, hours-change
detection, DLQ size safety, failed-run exit codes and corpus paging.

## P16 — Entity Resolution & Canonical Graph (2026-09-24)

P15's append-only staging now resolves into a canonical entity graph —
the first phase of `LegalEntity → Business → Place` per the Vietnam
Local Data Foundation design. `resolution/` is a new package; all
canonical tables live behind migration 010 and every code path degrades
to a dict-backed store when Postgres is absent.

- **Schema (migration 010)**: `legal_entities` (tax_code, partial
  unique), `canonical_businesses`, `canonical_places` (display/canonical
  name + normalized_name, canonical_category, address/normalized_address,
  phone, website, opening_hours jsonb, lat/lon + `location`
  GEOMETRY(4326) with GIST, `admin_unit_id` → P14 units, `status`
  open|closed|unknown, `confidence`, `source_count`,
  `first_seen_at`/`last_seen_at`, `resolution_run_id`),
  `place_sources` (place ↔ source_record lineage, unique
  provider+external_id), `place_field_provenance` (per-field candidate
  value + weight + observed_at + chosen flag), `resolution_runs`
  (status, parameters, resolver_version, counters, keyset `cursor`,
  checkpoint, `resume_of` lineage).
- **Normalize** (`resolution/normalize.py`): Vietnamese-aware name fold
  (diacritics strip + business-prefix removal + punctuation collapse),
  token signature, E.164-ish phone fold (+84), URL/domain fold,
  address reuse of the P14 normalizer, and a ~90-key category ontology
  mapping OSM tags / Google types / VN phrases into 13 buckets
  (food, retail, health, education, lodging, tourism, transport,
  finance, government, services, worship, culture, office, industrial)
  with graded relatedness for matching.
- **Match** (`resolution/match.py`): deterministic scorer — weighted
  name (folded-equality + token Jaccard/containment), haversine geo
  (≤30m → 1.0, ≥200m → 0, missing → 0.4), exact phone/website equality,
  category relatedness — plus two force-match rules (same normalized
  phone; identical normalized name within 80m). MERGE_THRESHOLD 0.62;
  `RESOLVER_VERSION = p16-v1`.
- **Store** (`resolution/store.py`): `CanonicalStore` protocol with
  `PgCanonicalStore` (parameterized SQL; candidate blocking via
  phone/domain/name-token-in-admin + `ST_DWithin` 500m) and
  `DictCanonicalStore` (same interface for tests/degraded mode).
  `PlaceRow`/`ProvRow` dataclasses carry derived `tokens`/`domain` for
  blocking.
- **Provenance** (`resolution/provenance.py`): per-field winner by
  `0.6·authority(source_policies) + 0.3·recency(e^-days/180)
  + 0.1·corroboration`; all candidates persisted with chosen flags —
  auditable, deterministic, and a better source displaces the winner
  without losing history. Place confidence = mean of chosen weights.
- **Runner** (`resolution/runner.py`): keyset-paged scan over
  `place_source_records` (`record_status='valid'`); already-linked
  records re-resolve in place (idempotent); unlinked records block →
  score → merge-or-create; provenance recomputed per touched place.
  Committed-cursor discipline mirrors P15.1 — only the last committed
  batch position survives a failure, `resume_of` picks up there.
- **Surface**: `scripts/resolve.py` (provider/since-id/batch/threshold/
  resume-run/dsn; exits non-zero unless status=done); API —
  `POST /v1/resolve`, `GET /v1/places/search`
  (q/lat/lon/radius/admin/category), `GET /v1/places/{id}` (sources +
  per-field provenance), `GET /v1/resolution/runs`.
- **Scope held**: legacy `businesses`/`/v1/business/search` untouched
  (compatibility layer); no cross-provider field-conflict serving, no
  Product/Inventory tier — serving migration is P17.

24 new tests cover normalization, scoring, cross-provider merge,
idempotent re-resolution, provenance winner/confidence, committed-
cursor resume, and pg-store SQL structure.

## P16.1 — Canonical Graph Quality Hardening (2026-09-24)

Audit-driven hardening of the canonical graph before P17 serving. All
changes are additive on top of migration 011.

- **Operational status**: `place_source_records.raw_status` captured at
  ingest (gmaps `business_status`/`permanently_closed` flags; OSM
  `disused:*`/`abandoned:*`/`temporary_closed` lifecycle tags) →
  `norm_status` canonicalizes to `open|temporarily_closed|
  permanently_closed` → field-level provenance → `canonical_places.status`.
  Places no longer default-blindly to `open` when a source reports
  closure.
- **Field comparison signatures**: corroboration compares
  `field_signature(field, value)` (folded name/address, E.164 phone,
  domain, ~100 m geo grid, canonical status) instead of raw values —
  `Công Ty ABC` / `CONG TY ABC` / `Cong ty ABC` now corroborate.
- **`website_domain` column + index**: candidate blocking uses indexed
  equality instead of `website LIKE '%domain%'`; column written on
  create/update and backfilled by migration.
- **Business/brand separation**: new places reuse an existing
  `canonical_businesses` row by `normalized_name` only when an existing
  place of that business shares evidence — same canonical_category,
  phone, website_domain, or admin commune (brand-level merge — N
  branches share one business; same-named unrelated shops stay split).
- **Per-field freshness decay**: `recency` now takes a per-field tau —
  hours/status ~45d, phone/website ~180d, category ~365d, name/address
  ~730d, location ~1460d. A 400-day-old "closed" no longer beats a
  fresh "open".
- **LegalEntity**: schema kept compatible for future MST/gov adapters;
  no invented legal links.

7 new regression tests cover same-brand multi-location, closed/
temporarily-closed propagation, stale-status decay, signature
corroboration, domain-equality blocking, and provider vocab extraction.

## P17 — Fast Local Serving (2026-09-25)

Done — Production Gate V1 validated end to end.

- **Read path** (`serving/places/`): canonical Postgres →
  `PlaceDocumentV1` projection (migration `012_serving_places.sql`) →
  OpenSearch `places` alias (+ `places_v1_gN` generations, atomic swap)
  → PostGIS geo lane → Redis cache (`s:`/`a:`/`p:<epoch>:` keys) →
  `/v1/places/search|autocomplete|{id}|reindex`.
- **Degrade-by-design**: Redis down → in-memory cache; OpenSearch down
  → `postgis`/`postgres` lanes with `X-Places-Degraded`; closed places
  never surface in default queries (`status` filter is explicit).
- **Ops**: `scripts/index_places.py` incremental/full/reconcile/status
  (+ `--delete`), durable cursor in `places_index_state`;
  `scripts/bench_places.py` drives the size bench.
- **A9 — per-role inference routing** (merged in #44): per-role
  `LLM_<ROLE>_{BASE_URL,API_KEY,MODEL,FALLBACK}` envs; each base_url
  lane has own
  client + breaker + `last_error_kind` (cleared on success, gate fix);
  `probe()` classifies endpoints without touching breakers; streaming
  fallback replays only before the first token.

## A10 — Document Intelligence Pipeline (planned)

Queued after P17 serving + P17.5 hardening — does not interrupt P17.
WeKnora-informed (v0.8.2, MIT): learn the Document/RAG/KB pipeline, not
the agent/MCP/sandbox/wiki layers, and no Neo4j GraphRAG — the Postgres
entity graph (LegalEntity → Business → Place → Product/Inventory, P16)
stays the single source of truth.

Sub-phases: A10.1 document-parser benchmark (anydoc / MinerU /
PaddleOCR-VL vs current), A10.2 structure-aware + parent-child chunking
(heading/article/chapter/table/page anchors for legal+gov corpus),
A10.3 document retrieval on the existing hybrid + RRF + rerank lane
(+ MMR), A10.4 two-stage `search_document()`/`read_document()` reading
API (evidence IDs → deep-read only needed context — big token cut),
A10.5 Legal/Gov corpus integration.

WEB documents stay on Trafilatura/Firecrawl/Playwright; FILE documents
(PDF/Office/OCR) get a separate parser pipeline. Full analysis + phase
breakdown:
[`docs/research-notes/document-intelligence-pipeline.md`](docs/research-notes/document-intelligence-pipeline.md).

## P18.2 — Optional External Acquisition (planned)

Queued after P18 coverage/freshness core — `apify-mcp-server` reviewed
and accepted as a fallback acquisition layer, not a search provider:
Actor runs execute on Apify cloud (cost + external latency), so it
never sits on the user-facing read path — background Discovery Queue
fallback only, output into P15 staging → P16 resolution → canonical
graph. Also a Hermes dev/research tool. Six mandatory conditions:
explicit Actor allowlist (no dynamic marketplace on user paths),
telemetry off, hard per-run + per-day cost budget (Actor-side caps
don't bound work), timeout + circuit breaker at the adapter, staging-
only output, provenance carrying actor_id + run_id + observed_at.
First candidates: `apify/web-fetch` as last-resort reader fallback,
Google-Maps Actor for targeted coverage repair, ecommerce/social
Actors where we have no own infra. `rag-web-browser` excluded —
US/English-biased Google backend is wrong for VN retrieval. Full
analysis:
[`docs/research-notes/apify-external-acquisition.md`](docs/research-notes/apify-external-acquisition.md).
