# Search Hub v3 — Evidence-Driven Search Engine (SPEC)

> Chốt bởi DuyAI (2026-08-15). Đây là nguồn sự thật cho mọi implementation.
> Kiến trúc gốc đúng (8/10) — KHÔNG đập đi làm lại. Nâng cấp từ metasearch → evidence-driven.
> **Nguyên tắc tối thượng: Hermes = não chính; Search Hub = thu thập + chắt lọc + kiểm chứng bằng chứng.**

## 1. Kiến trúc tổng thể

```
Hermes (User LLM/model — não chính)
   │ Tool Call (search / research / read_source / verify)
   ▼
Search Hub (FastAPI :8888, MCP :8901)
   Query Understanding (intent + entity + time + language)
   → Query Planner
   → Budget Controller (fast/normal/deep)
   → Search Orchestrator
       ├─ SearXNG │ GitHub │ arXiv │ RSS │ Official │ (future providers)
   → Result Normalization
   → Deduplication (URL → canonical → fingerprint → semantic)
   → Fetch / Firecrawl
   → Content Extraction → Passage Extraction
   → Relevance + Authority + Freshness Rank
   → Evidence Clustering
   → Claim Verification (6 states + independent-source)
   → Evidence Sufficiency (stop criteria)
       ├─ enough → Citation Engine → Evidence Pack
       └─ missing → Follow-up Search (loop, budget-bound)
   → Hermes model tổng hợp câu trả lời cuối
```

## 2. API Contract (v1 — giữ back-compat 5 tools cũ cho MCP/Hermes)

```
POST /v1/search            — search(query, mode, ...) → results + evidence
POST /v1/research          — research(query, mode=fast|normal|deep) → answer + evidence pack
POST /v1/read              — read(url) → passage-level content
POST /v1/verify            — verify(claims) → verdicts
POST /v1/research/stream   — SSE: planning→searching→fetching→verifying→answer (BẮT BUỘC)
GET  /v1/capabilities      — modes, providers, features (Hermes không hard-code)
GET  /v1/providers         — list providers + status
GET  /v1/health
```

## 3. SearchProvider Protocol + Registry (thay gọi trực tiếp)

```python
class SearchProvider(Protocol):
    async def search(self, query: SearchQuery) -> list[SearchResult]: ...

class ProviderRegistry:
    # searxng, github, arxiv, rss, official_sites, ...
    def register(name, provider); def get(name); def all(); def health()
```

Không hard-code provider nào vào orchestrator.

## 4. Inference Gateway (thay ModelGateway — phạm vi rộng hơn LLM)

```python
class InferenceGateway:
    async def complete(...)    # chat/reasoning
    async def embed(...)       # embeddings
    async def rerank(...)      # reranking
    async def classify(...)    # query classifier
    async def extract(...)     # structured extraction
```

Tất cả optional/configurable. Search-native models (embedding, reranker, classifier) KHÔNG bắt buộc — Hermes sở hữu LLM chính.

## 5. Query Understanding

```json
{
  "intent": "current_fact",
  "entities": ["GPT-5"],
  "language": "vi",
  "geography": null,
  "time_sensitive": true,
  "freshness_required": true,
  "max_age": "24h",
  "preferred_sources": ["official", "news"]
}
```

## 6. Source Authority Engine

```
official docs     1.00
government        1.00
research paper    0.95
major publication 0.90
vendor website    0.85
specialist blog   0.75
forum             0.60
reddit            0.55
unknown SEO site  0.30
```

FinalScore = semantic_relevance + source_authority + freshness + content_quality + query_intent_match + corroboration

## 7. Freshness / Temporal

- Query "hôm nay / mới nhất / CEO hiện tại" → freshness_required=true, max_age=24h
- Không trả bài SEO 2 năm trước cho câu hỏi "hôm nay"

## 8. Deduplication 3 cấp

```
URL dedupe → Canonical URL dedupe → Content fingerprint → Semantic near-duplicate
Evidence Cluster: Reuters original + Site A/B/C/D copy = 1 nguồn độc lập
```

## 9. Verification (6 trạng thái)

```
SUPPORTED | PARTIALLY_SUPPORTED | CONTRADICTED | INSUFFICIENT_EVIDENCE | OUTDATED | SOURCE_CONFLICT
Claim → Evidence A/B/C → independent-source check → verdict
```

## 10. Citation passage-level

```json
{
  "claim_id": "c17",
  "evidence": [{
    "source_id": "s4", "passage_id": "p37",
    "url": "...", "quote_start": 2021, "quote_end": 2189,
    "retrieved_at": "2026-08-15T00:00:00Z"
  }]
}
```

## 11. Budget Controller (biến fast/normal/deep thành hệ thống)

```yaml
fast:   max_queries: 2, max_fetches: 3,  max_followups: 0
normal: max_queries: 5, max_fetches: 8,  max_followups: 1
deep:   max_queries: 12, max_fetches: 20, max_followups: 3
```

## 12. Stop criteria (iterative research loop)

```
Stop khi: coverage >= 0.9 AND confidence >= 0.85 AND critical claims corroborated AND no unresolved contradiction
```

## 13. Storage (MVP: PostgreSQL + Redis — KHÔNG Qdrant ở giai đoạn đầu)

- Redis: response cache, search result cache, URL fetch cache, rate limit, distributed lock, async job state
- PostgreSQL: evidence store (có thể thêm Qdrant sau cho research memory/cached passages)

## 14. Worker

- MVP: FastAPI + asyncio/httpx + Redis (KHÔNG Celery/RQ/Arq ngay)
- Thêm worker queue chỉ khi: crawl hàng nghìn URL / long research / scheduled indexing

## 15. Cây thư mục mục tiêu (đơn giản, không over-engineering)

```
search-router/
├── api/            # v1 endpoints + SSE + capabilities
├── core/           # orchestrator, budget, query understanding, pipeline
├── providers/      # searxng, github, arxiv, rss, official + registry
├── inference/      # InferenceGateway (complete/embed/rerank/classify)
├── fetch/          # firecrawl, content extraction, passage extraction
├── ranking/        # authority, freshness, rerank, dedup
├── evidence/       # clustering, verification, citation
├── storage/        # redis, postgres
├── config.py       # settings v2
├── models.py       # data models v2
└── tests/
```

## 16. Hermes chỉ biết 4 tools (KHÔNG expose implementation detail)

```
search        → /v1/search
research      → /v1/research (+ stream SSE)
read_source   → /v1/read
verify        → /v1/verify
```

KHÔNG expose SearXNG/Firecrawl/Postgres/Qdrant/reranker/crawler cho Hermes.

## Task contracts (per module)

- **Kilo**: `models.py` v2 + `config.py` v2 + `.env.example` v2 — data contracts cho mọi module
- **Hermes**: research notes → `docs/research-notes/` (authority scoring, freshness heuristics, dedup algorithms, evidence clustering từ Exa/Perplexity/You.com)
- **Devin**: `core/` — SearchProvider Protocol + ProviderRegistry, InferenceGateway, SearchBudget, QueryUnderstanding, orchestrator refactor
- **OpenCode**: `storage/redis_cache.py` + thay in-memory cache trong pipeline/cache.py
- **OpenHands**: `evidence/` — verification engine (6 states), passage citation, `/v1/research/stream` SSE
- **Codex**: review toàn bộ + tests + hardening
