# Search Quality Eval Harness (`eval/`)

Benchmark for the Search Router — measures **retrieval quality** of `POST /search`
and `POST /v1/search` **and answer quality** of `POST /v1/answer` (P13) against
labeled Vietnamese/English ground-truth datasets,
independent of the 215 unit tests in `search-router/tests/` (those cover
*software correctness*; this harness covers *search quality*).

No Search Router code is modified — this package lives at the repo root and only
talks to the router over HTTP (or a deterministic mock for offline runs).

```
python -m eval list-datasets
python -m eval run --dataset vi_general --top-k 10
python -m eval run --dataset vi_general --top-k 10 --baseline eval/reports/vi_general_<ts>.json
```

## Metrics

Per query (computed against ground-truth `expected_urls`):

| Metric | Definition |
|--------|-----------|
| `ndcg@10` | Normalized DCG with binary relevance, ideal ranking = all expected docs first |
| `mrr` | Reciprocal rank of the first relevant result (0 if none) |
| `recall@5/10/20` | Distinct expected docs covered by top-k ÷ total expected (≤ 1.0) |
| `precision@10` | Relevant positions in top-10 ÷ 10 |
| `freshness_ok` | Fraction of results carrying a non-empty `retrieved_at` provenance field |
| `source_diversity` | Unique domains ÷ total results — how many *independent* sources the engine surfaced (1.0 = every result from a distinct domain) |
| `authority` | Mean `ranking.authority.authority_for` over unique retrieved domains (~0..2.5, higher = more authoritative). Only when `--authority` is passed; accepts `--authority-vertical` for intent-dependent scoring |
| `latency_ms` | mean / p50 / p95 / p99 request latency (client-measured) |
| `cost_usd` | Explicit API cost if returned, else `--cost-per-query` × queries (default 0 — self-hosted SearXNG is free) |

Aggregate metrics are the mean over all queries; the report also breaks down by
`category` and per query. `error_rate` = fraction of queries whose request failed.

**Answer-level metrics** (P13, `--endpoint answer`):

Each `/v1/answer` response also feeds its `sources` through the standard
retrieval metrics, so one run produces both rows of the table above and:

| Metric | Definition |
|--------|-----------|
| `answer_present` | Response carried non-empty answer text (also covers the degraded extractive fallback) |
| `answer_correctness` | Fraction of `expected_facts` found in the answer — accent-folded substring match, only on queries declaring `expected_facts` |
| `citation_precision` | Cited URLs matching `expected_urls` ÷ total cited URLs |
| `citation_recall` | `expected_urls` covered by ≥1 cited URL ÷ total expected |
| `unsupported_claim_rate` | CitationV2 entries with empty `evidence` ÷ emitted citations (lower-bound proxy: dropped claims are not in the HTTP response) |
| `evidence_quote_rate` | Citations carrying ≥1 non-empty evidence `quote` — CitationV2 well-formedness |
| `cited_source_coverage` | Response sources cited at least once ÷ total sources (domain-matched) |
| `verified` | Fraction of responses flagged `verified` by the pipeline |
| `coverage` | Mean response `coverage` (pipeline confidence) |

Metrics whose inputs are absent (no `expected_facts`, no citations, no
ground-truth URLs) are simply not emitted for that query; the summary reports
both the mean and the number of queries that produced the metric
(`<metric>_queries`).

**Cross-provider comparison** (new in 8C):

* `domain_overlap(a_domains, b_domains)` — Jaccard overlap between two result
  sets. Compare the same dataset run through two providers/endpoints.
* `report_source_diversity(report)` and `report_domain_overlap(report_a,
  report_b)` — aggregators that run on **existing report JSONs** (only read
  `per_query[].retrieved_domains`, present since wave 7B, so old reports work
  unchanged):

```bash
# source_diversity from a saved report
D:/Miniconda3/python.exe -m eval report --file eval/reports/multi_source_15q_<ts>.json

# domain_overlap between two providers on the same dataset
D:/Miniconda3/python.exe -m eval report --file <legacy>.json --compare <v1>.json
```

**Ground-truth matching** (`eval/matching.py`):

* `expected_urls` entries may be a **bare domain** (`vnexpress.net`) or a **full
  URL** (`https://vnexpress.net/kinh-doanh/gia-vang-hom-nay...`).
* Domains match on the result's registered domain (case-insensitive, `www.`
  stripped). URLs match after normalization: lowercase, `http`/`https` treated
  as the same scheme, default ports / trailing slashes / duplicate slashes
  stripped.
* Recall counts **distinct** expected docs once, so it never exceeds 1.0 even
  when several results hit the same domain. nDCG/precision count matching
  positions (standard).

## Adding a dataset

1. Create `eval/datasets/<name>.jsonl`.
2. One labeled query per line, JSON:
   ```json
   {"query": "cách nấu phở bò ngon tại nhà",
    "expected_urls": ["vnexpress.net", "monngonmoingay.com"],
    "category": "vi_general",
    "region": "vn"}
   ```
3. `category` must be one of: `vi_general | current_events | technical | code |
   local_business | ambiguous | adversarial | vn_news | vn_government |
   vn_legal | vn_market | vn_product | vn_company | vn_places | vn_community |
   vn_research`. `region` is `vn` or `global`.
   Blank lines and `#` comments are ignored. You can also pass a file path to
   `--dataset`.
4. Optional field `time_sensitive` (bool, default `false`) labels queries whose
   best answer must be *fresh* (e.g. `giá vàng hôm nay`) — used by the
   freshness datasets. Old rows without the field load unchanged
   (backward-compatible).
5. Optional field `expected_facts` (list of strings, P13) declares short
   strings a correct `/v1/answer` text should contain (accent-folded substring
   match). Queries without it skip `answer_correctness` entirely.

### Wave-8C datasets

| Dataset | Queries | Purpose |
|---------|---------|---------|
| `multi_source_15q` | 15 | Recall across **≥3 independent domains** (tech / health / finance / history / product comparison) — answers that need many sources |
| `freshness_10q` | 10 | Time-sensitive queries (`time_sensitive: true`) — gold price, BTC, lottery, EPL, weather, FX, pork price, tech news |

### VN benchmark suite (P12)

`eval/datasets/vietnam/` — one file per lane the VN expansion added. Ground
truth favors first-party VN sources (vbpl.vn, chinhphu.vn, hsx.vn, …) so
authority + recall measure whether the VN providers actually surface them.

| Dataset | Queries | Lane under test |
|---------|---------|-----------------|
| `vietnam/general` | 12 | Everyday VI queries (recipes, admin procedures, services) |
| `vietnam/news` | 10 | Current-affairs → VnExpress/Tuổi Trẻ/Nhân Dân lanes |
| `vietnam/government` | 12 | Gov portals → chinhphu.vn, ministry sites |
| `vietnam/legal` | 12 | Legal docs → vbpl.vn, vanban.chinhphu.vn, thuvienphapluat |
| `vietnam/company` | 14 | Company intel → corporate sites, hsx.vn |
| `vietnam/places` | 14 | Local/places → OSM, Foody, pharmacy chains |
| `vietnam/market` | 12 | Gold/FX/stock quotes → sjc/webgia, er-api, VNDirect |
| `vietnam/product` | 12 | Product+pricing → TGDD/FPTShop/VinFast/otofun |
| `vietnam/community` | 14 | Forums → otofun, webtretho, voz, tinhte |
| `vietnam/deep_research` | 8 | Multi-source synthesis queries (policy/market/sector) |

```bash
# VN suite with the authority metric (uses ranking/authority.py)
python -m eval run --dataset vietnam/legal --top-k 10 --authority --authority-vertical legal

# Authority of a saved report post-hoc
python -m eval report --file eval/reports/legal_<ts>.json --authority
```

```bash
# P13 answer benchmark — /v1/answer + answer-level metrics
python -m eval run --dataset vietnam/legal --endpoint answer \
    --answer-mode fast --authority --authority-vertical legal
```

Provider Failure Recovery is exercised by the federation tests in
`search-router/tests/`.

## Running

```bash
# List datasets
D:/Miniconda3/python.exe -m eval list-datasets

# Live run against the Search Router on :8888 (default legacy POST /search)
D:/Miniconda3/python.exe -m eval run --dataset vi_general --top-k 10

# Wave-8C benchmarks (multi-source recall, freshness)
D:/Miniconda3/python.exe -m eval run --dataset multi_source_15q --top-k 10
D:/Miniconda3/python.exe -m eval run --dataset freshness_10q --top-k 10

# Same queries against the v1 evidence API (POST /v1/search)
D:/Miniconda3/python.exe -m eval run --dataset current_events --top-k 10 --endpoint v1

# Offline deterministic run (no server needed) — useful for CI / fixture checks
D:/Miniconda3/python.exe -m eval run --dataset vi_general --top-k 5 --mock

# Regression check: diff against a previous report
D:/Miniconda3/python.exe -m eval run --dataset vi_general --top-k 10 \
    --baseline eval/reports/vi_general_20260816_165714_541374.json
```

### CLI options (`python -m eval run --help`)

| Option | Default | Meaning |
|--------|---------|---------|
| `--dataset NAME|path` | (required) | dataset to run |
| `--top-k N` | `10` | results retrieved per query |
| `--endpoint legacy\|v1\|answer` | `legacy` | `POST /search`, `POST /v1/search`, or `POST /v1/answer` |
| `--answer-mode` | `balanced` | `/v1/answer` mode (`fast`/`balanced`/`deep`/`research`) |
| `--server-url` | `http://localhost:8888` | Search Router base URL |
| `--timeout S` | `60` | per-request timeout |
| `--providers a,b` | `searxng` | provider names recorded in the report (informational) |
| `--search-type web\|news` | `web` | `type` sent to the API |
| `--mock` | off | force the deterministic mock client |
| `--no-fallback` | off | fail instead of falling back to mock when the server is down |
| `--cost-per-query USD` | `0.0` | cost estimate when the API doesn't report cost |
| `--baseline FILE` | — | diff current run against a previous report |
| `--out-dir DIR` | `eval/reports` | where the report JSON is written |
| `--note TEXT` | — | free-form note stored in the report meta |
| `--authority` | off | add per-query `authority` metric from the VN authority table |
| `--authority-vertical` | — | pin a vertical for intent-dependent authority scoring |

`python -m eval report --file A.json [--compare B.json] [--authority]` computes
`source_diversity` (from one report), `domain_overlap` (A vs B) and `authority`
on saved reports — works on pre-8C reports as well.

If the server is unreachable the runner **falls back to the deterministic mock**
(unless `--no-fallback`) so runs and reports still produce valid output.

## Reading a report

Reports are written to `eval/reports/<dataset>_<UTC ts>.json` (gitignored):

```jsonc
{
  "meta":     { "dataset", "generated_at", "endpoint", "top_k", "providers", ... },
  "dataset":  { "queries", "by_category", "by_region" },
  "summary":  { "ndcg@10", "mrr", "recall@5/10/20", "precision@10",
                "freshness_ok", "source_diversity", "latency_ms": {mean,p50,p95,p99},
                "cost_usd", "error_rate", "mock_used" },
  "per_query":[ { "index", "query", "category", "region", "time_sensitive",
                  "expected_urls", "retrieved_urls", "retrieved_domains",
                  "latency_ms", "metrics" } ],
  "by_category": { "<category>": { ... same shape as summary ... } }
}
```

### Baseline diff mode

`--baseline <report.json>` prints the aggregate diff with one row per metric and
a per-query nDCG@10 delta. Every row ends with one of:

* `+` improved vs baseline
* `-` regressed vs baseline
* `=` unchanged

For quality metrics (nDCG/MRR/Recall/Precision/freshness) higher is better; for
`latency_ms` and `cost_usd` lower is better — the sign reflects that.

## Tests

The harness has its own suite in `eval/tests/` (metrics math verified by hand,
matching edge cases, mock end-to-end run, report shape, diff logic, CLI smoke):

```bash
# From the repo root
D:/Miniconda3/python.exe -m pytest eval/tests -q
```

The Search Router's existing 215 tests must keep passing (untouched):

```bash
cd search-router && env -u PYTHONPATH D:/Miniconda3/python.exe -m pytest tests/ -q
```

## Layout

```
eval/
├── __init__.py        # package exports
├── __main__.py        # python -m eval
├── cli.py             # argparse CLI (list-datasets, run)
├── datasets.py        # JSONL loading/validation + dataset summaries
├── client.py          # HTTPClient (legacy + v1), MockClient, FallbackClient
├── runner.py          # RunConfig/QueryRun, run_dataset, run_answer_dataset, aggregate_metrics
├── metrics.py         # nDCG, MRR, Recall, Precision, freshness, source diversity, authority, cost, latency
├── answer_metrics.py  # P13 answer-level metrics (correctness, citation P/R, unsupported claims)
├── matching.py        # domain/URL ground-truth matching + normalization
├── vn_authority.py    # loads the router's ranking.authority table as a scorer
├── diff.py            # baseline comparison (+/-/=)
├── report.py          # report build + JSON write + console formatting
├── datasets/          # *.jsonl labeled datasets (+ vietnam/ subdir)
├── reports/           # generated reports (gitignored, .gitkeep kept)
└── tests/             # unit + integration + CLI smoke tests
```