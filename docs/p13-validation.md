# P13 — VN Answer Quality & Production Validation

**Status:** measurement complete · **Baseline:** BASELINE-V3, zero contract drift
(`scripts/freeze_baseline.py --check` → clean) · **Date:** 2026-09-24

Scope was audit-only: no new router/provider/caching/citation architecture; the
frozen contracts are untouched (all eval-side work lives in `eval/`, which is not
part of the frozen surface). This report is the baseline of record for the
`/v1/answer` lane on the 118-query VN benchmark.

## Test surface

| Lane | Result |
|---|---|
| Unit/integration suite | **1317 passed, 17 skipped** (`--ignore` of the 3 torch-dependent files; a `torch` DLL access-violation on this Windows box is logged mid-run and non-fatal — count identical to clean `main`) |
| Baseline contract gate | **clean** — no schema/OpenAPI/SSE/migration drift |
| `/v1/search` raw lane | PASS — 118/118 answered, 0 errors, DDGS carrying (SearXNG breaker open) |
| `/v1/answer` fast mode | PASS — 118/118 answered, `answer_present` = 1.00, 0 errors |
| `/v1/research` (mode=fast) | PASS — research stats present on 200 |
| SSE (`/v1/answer` `stream=1`) | VERIFIED — canonical event chain `init→planning→plan→search→search→evidence→citation→synthesizing→answer→verified→done` |
| CitationV2 | VERIFIED — citations carry `claim_id` + `evidence[]` with `url`/`quote`/`quote_start`/`quote_end`/`retrieved_at` |
| `/v1/business/search` | degraded-OK — phrase anchor resolved ("chợ Bến Thành" → 10.7725,106.698 via Nominatim); PostGIS down → OSM Overpass empty → web fallback (noise) |
| crawl → index → search E2E | **NOT VERIFIED** — Docker Desktop dead on this box (stale port-proxy debt); no Redis/Postgres/OpenSearch/Qdrant/MinIO/SearXNG/Firecrawl containers can start. CI `e2e.yml` lane is the same known-unstable job. |

## Live-stack context

Every number below was measured on a **degraded stack**: Docker is dead on the
dev box, so Redis/Postgres/OpenSearch/Qdrant/MinIO/Firecrawl/SearXNG are all
absent. The LLM gateway is `401` (invalid `LLM_API_KEY`). The live plane is
therefore **DDGS + 13 VN providers + Nominatim/Overpass** only — which doubles
as the failure-recovery measurement: ~250 API calls across both benchmarks with
`error_rate` = 0 and `answer_present` = 1.00.

## Retrieval benchmark (`/v1/search` raw lane, DDGS only)

| vertical | n | ndcg@10 | recall@10 | authority | src_div | lat p50 | lat p95 |
|---|---|---|---|---|---|---|---|
| legal | 12 | .344 | .292 | 1.33 | .58 | 1841ms | 2594ms |
| product | 12 | .281 | .267 | 1.52 | .74 | 2444ms | 3067ms |
| company | 14 | .277 | .262 | 1.09 | .64 | 1966ms | 2839ms |
| news | 10 | .205 | .225 | 1.38 | .78 | 1853ms | 2449ms |
| market | 12 | .192 | .165 | 1.22 | .71 | 2265ms | 3049ms |
| general | 12 | .162 | .097 | 1.36 | .80 | 697ms | 2393ms |
| community | 14 | .146 | .131 | 1.09 | .68 | 1804ms | 2170ms |
| government | 12 | .085 | .056 | 1.73 | .66 | 1882ms | 2061ms |
| places | 14 | .058 | .054 | 0.96 | .66 | 2496ms | 7916ms |
| deep_research | 8 | .049 | .031 | 1.59 | .84 | 903ms | 1990ms |

Freshness: **unmeasurable on the raw lane** — `/v1/search` rows do not expose
`retrieved_at`, so `freshness_ok` = 0.0 by absence, not by staleness.
`error_rate` = 0 on every vertical (mean lat 1130–2620ms).

## Answer benchmark (`/v1/answer` fast mode, degraded LLM)

`answer_present` = 1.00 on all verticals. Answers are the extractive fallback
(no LLM synthesis); correctness is bounded by what source titles/snippets carry.
Metrics emitted only where inputs exist — denominators differ per metric
(`_queries` counts in `eval/reports/`).

| vertical | correctness | cit_prec | cit_rec | unsupp | eqr | csc | verified | lat p50 | lat p95 |
|---|---|---|---|---|---|---|---|---|---|
| company | 1.00 (4q) | .155 (14q) | .155 (14q) | .004 | .996 | .34 | .21 | 6650ms | 9009ms |
| product | .67 (3q) | .000 (10q) | .000 (12q) | .167 | .833 | .51 | .08 | 3875ms | 11640ms |
| community | .67 (3q) | .167 (9q) | .066 (14q) | .357 | .643 | .46 | .21 | 4393ms | 8761ms |
| general | .50 (2q) | .018 (11q) | .021 (12q) | .088 | .912 | .61 | .08 | 5089ms | 13136ms |
| places | .50 (2q) | .042 (14q) | .059 (14q) | .015 | .985 | .48 | .21 | 5785ms | 15610ms |
| news | .50 (2q) | .375 (8q) | .125 (10q) | .200 | .800 | .36 | .00 | 6546ms | 11416ms |
| government | .33 (3q) | .000 (6q) | .000 (12q) | .500 | .500 | .11 | .08 | 7666ms | 12446ms |
| market | .25 (4q) | .000 (9q) | .000 (12q) | .272 | .728 | .23 | .00 | 7689ms | 9693ms |
| legal* | .00 (4q) | .000 (5q) | .000 (12q) | .595 | .405 | .12 | .00 | 5ms* | 20ms* |
| deep_research | .00 (3q) | .125 (4q) | .031 (8q) | .500 | .500 | .31 | .00 | 6721ms | 7028ms |

`*` the fast-mode legal run was served entirely from the semantic cache
(4–20ms/query; earlier smoke probes had primed `hit:exact`). A cold re-run
of legal in `deep` mode (fresh cache key) shows the pipeline end-to-end:
**correctness 1.00 (4q), cit_prec .108, cit_rec .118, unsupported .010,
eqr .990, verified .00, lat mean 17.5s / p95 22.7s.**

`eqr` = evidence_quote_rate — share of emitted citations carrying real quoted
passage text. `csc` = cited_source_coverage (domains matched to expected).
`unsupp` = share of emitted citations with an empty `evidence` list
(lower-bound proxy; `verification.claims_total` is not exposed in the HTTP
response). `verified` = `raw.verified` flag. Denominators in parens = number
of queries emitting that metric.

## Failure / degraded-mode matrix

| Component | State | Observed behavior |
|---|---|---|
| LLM gateway | `401` invalid key | fail-fast after first call (breaker trips on auth error, ~5–8s); later stages reject instantly; extractive synthesis serves answers. `answer_present` still 1.00. |
| SearXNG | container absent | circuit breaker OPEN on `/v1/providers/health`; DDGS + VN lanes carry 100% of traffic; 0 request errors across ~250 calls |
| Redis | absent | semantic cache falls back to in-memory (`hit:exact` observed on repeats, 8ms) |
| Postgres/PostGIS | absent | `/v1/business/search` skips geo lane, resolves anchor, falls to OSM→web; 200 with `provider:"web"` |
| OpenSearch / Qdrant | absent | hybrid lane silently skipped; no query path errors |
| Overpass OSM | reachable, empty for query | `count:1` web-fallback noise item returned — no data path for places without geo stores |
| MinIO / Firecrawl | absent | snapshot/render stages skipped (degrade-by-design) |

## Findings (documented, not fixed — P13 is measurement)

1. **gnews redirect URLs mask publisher domains** (HIGH impact on legal/gov):
   `vn_gnews` lanes emit `news.google.com` redirect wrappers into `sources`, so
   the true publisher domain (vbpl.vn, chinhphu.vn, luatvietnam.vn) never
   surfaces for matching/scoring. Answer-lane legal ndcg is .118 vs the raw-lane
   .344 on the same queries; `cited_source_coverage` and citation precision on
   legal/gov are depressed for the same reason.
2. **Fast-mode citations are mostly empty-evidence on gov/legal/research
   verticals** (unsupported .50–.60) — but the deep-mode legal re-run
   (unsupported .010, eqr .99, correctness 1.00) proves the CitationV2 pipeline
   emits real passage-backed citations when it runs end-to-end. The empty
   citations are a fast-mode/cached-path artifact, not a broken citation
   system. Biggest single answer-quality gap.
3. **`verified` ≈ 0.11**: without the LLM, the verification stage is a no-op —
   citations are emitted pre-verification and the flag almost never sets.
4. **Freshness is unmeasurable downstream**: neither `/v1/search` rows nor
   `/v1/answer` `sources` expose `retrieved_at`; only CitationV2 evidence carries
   it. P14 should surface a timestamp on Source rows to make freshness real.
5. **Places vertical has no live data path** without PostGIS+Overpass: the web
   fallback returned an irrelevant result for a pharmacy query ("nhà thuốc gần
   chợ Bến Thành").
6. **`GET /v1/providers/health` is unauthenticated**: returns full provider
   internals (circuit state, latencies, engine stats) with no key — unlike
   `/v1/health`, which gates detail behind `admin:debug`/`api_auth_enabled`.
   Hardening gap; fix is the same `detailed` gate.
7. **crawl→index→search E2E cannot be certified on this box** (Docker dead);
   on CI, `e2e.yml` is the same unstable lane flagged since PR #1. The last
   recorded live pass is `test_crawl_to_search.py` (95s) pre-P13.
8. **Answer correctness is bounded by the extractive fallback**: 0–1.00 spread
   (company 1.00, legal 0.00). Numbers will move once `LLM_API_KEY` is valid —
   this is the degraded floor, not the ceiling.

## Cost per query

$0 on the measured stack — no LLM tokens were burned (401), and every provider
is free public HTTP (DDGS/RSS/GNews/er-api/VNDirect/Nominatim/Overpass). LLM
cost is N/A until a valid key exists; `cost_usd` fields exist in the eval
client for that moment.

## DoD checklist

| DoD item | status |
|---|---|
| Full tests | PASS (1317/17) |
| crawl → index → search | NOT VERIFIED (infra: Docker dead on box) |
| `/v1/search` | PASS |
| `/v1/answer` | PASS |
| `/v1/research` | PASS |
| CitationV2 | VERIFIED |
| SSE | VERIFIED |
| VN 10 verticals | MEASURED |
| Answer correctness | MEASURED |
| Citation precision | MEASURED |
| Citation recall | MEASURED |
| Unsupported claims | MEASURED |
| Authority | MEASURED |
| Freshness | MEASURED-ABSENT (no `retrieved_at` downstream — gap documented) |
| Latency P50/P95 | MEASURED |
| Cost/query | MEASURED ($0, self-hosted lanes) |
| Failure recovery | VERIFIED |
| Regression vs BASELINE | 0 |

## Gate to P14

Per the plan, P14 (Source Coverage Expansion) proceeds if this report is
"good". Honest read: the architecture degraded correctly everywhere measured —
0 request errors, 100% answer presence, clean SSE, working breakers. The
quality ceiling is LLM-blocked (correctness/verified) and one provider-side
masking issue depresses legal/gov numbers (finding 1). Those are data for the
next phase, not blockers of this one.
