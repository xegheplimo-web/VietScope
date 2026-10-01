"""Search Hub — Unified Search Router for Hermes.

Agent-facing tools (4 simple endpoints):
  POST /search       — search(query, type, max_results) → results
  POST /fetch        — fetch(url, mode, limit) → content
  POST /code_search  — code_search(query, repo) → results
  POST /answer       — answer(query, depth) → answer + evidence + citations

Infrastructure endpoints:
  GET  /health       — check all providers
  GET  /             — service info
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

from api.openai_compat import (
    OpenAIHTTPException,
    openai_http_exception_handler,
    openai_validation_handler,
)
from api.openai_compat import (
    router as openai_router,
)
from api.v1 import router as v1_router
from config import settings
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from metrics import metrics_store, record_query
from models import (
    AnswerToolRequest,
    AnswerToolResponse,
    CodeSearchToolRequest,
    CodeSearchToolResponse,
    FetchMode,
    FetchToolRequest,
    FetchToolResponse,
    HealthStatus,
    SearchCategory,
    # Legacy
    SearchResultItem,
    # Agent-facing models
    SearchToolRequest,
    SearchToolResponse,
    SearchType,
    Source,
)
from observability.logging import (
    configure_json_logging,
    request_id_var,
    safe_request_id,
    search_id_var,
)
from pipeline.cache import (
    TTL_SCRAPE,
    TTL_SCRAPE_NEWS,
    TTL_SEARCH_IMAGES,
    TTL_SEARCH_NEWS,
    TTL_SEARCH_WEB,
    _cache_key,
    scrape_cache,
    search_cache,
)
from pipeline.citation import build_evidence
from pipeline.rag import generate_follow_up_questions, synthesize_answer
from pipeline.reader import read_batch
from pipeline.reranker import rerank_scraped_content, rerank_search_results
from pipeline.resilience import with_retry
from pipeline.router import (
    orchestrate_search,  # wave-9: Search Orchestrator (provider fan-out)
)
from providers.arxiv import arxiv_search
from providers.code_search import github_search, grep_app_search
from providers.firecrawl import (
    firecrawl_crawl,
    firecrawl_health,
    firecrawl_map,
    firecrawl_scrape,
)
from providers.hn import hn_search
from providers.searxng import (
    searxng_health,
    searxng_search,
)
from security.apikeys import require_api_key
from telemetry.tracer import instrument_app, setup_telemetry

from observability.prometheus import IN_FLIGHT, REQUEST_LATENCY, REQUESTS, prometheus_payload

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 6A/6B: bootstrap internal indexes in the background so startup
    # never blocks on (possibly-absent) OpenSearch/Qdrant.
    async def _bootstrap_indexes() -> None:
        from config import settings as _s

        if _s.opensearch_enabled:
            try:
                from opensearch.client import OpenSearchClient

                created = await asyncio.to_thread(OpenSearchClient().ensure_indices)
                logger.info("OpenSearch index bootstrap: %s", created)
            except Exception as exc:  # noqa: BLE001 — optional infra
                logger.warning("OpenSearch bootstrap failed: %s", exc)
        if _s.qdrant_enabled:
            try:
                from qdrant.client import QdrantClient
                from qdrant.collections import bootstrap_collections

                qc = QdrantClient()
                created = await bootstrap_collections(qc)
                await qc.close()
                logger.info("Qdrant collection bootstrap: %s", created)
            except Exception as exc:  # noqa: BLE001 — optional infra
                logger.warning("Qdrant bootstrap failed: %s", exc)

    # Phase 1 (T2): apply hub-postgres schema migrations — fire-and-forget,
    # idempotent, never blocks startup. Skipped when HUB_DATABASE_URL unset.
    async def _run_migrations() -> None:
        from storage import pg_client

        if not pg_client.database_url():
            return
        try:
            from db.migrate import run as run_migrations

            applied = await run_migrations()
            if applied:
                logger.info("schema migrations applied: %s", ", ".join(applied))
        except Exception as exc:  # noqa: BLE001 — optional infra
            logger.warning("schema migrations failed: %s", exc)

    task = asyncio.create_task(_bootstrap_indexes())
    migrations_task = asyncio.create_task(_run_migrations())

    # P10: bootstrap admin API key when auth is enabled.
    try:
        from security.apikeys import bootstrap_admin_key

        await bootstrap_admin_key()
    except Exception as exc:  # noqa: BLE001 — optional infra
        logger.warning("API-key bootstrap failed: %s", exc)

    # Phase 2: crawler engine — strictly opt-in (CRAWLER_ENABLED=true) so a
    # dev compose stack never crawls the live web unintentionally.
    crawler_worker = None
    crawler_task = None
    if settings.crawler_enabled:
        try:
            from crawler.fetcher import Fetcher
            from crawler.pipeline import CrawlPipeline
            from crawler.politeness import DomainRateLimiter
            from crawler.robots import RobotsCache
            from crawler.worker import CrawlWorker
            from extraction.service import ExtractionService
            from storage.object_store import get_object_store
            from workers.freshness_worker import FreshnessWorker
            from workers.indexing_worker import get_indexing_worker

            # Phase 3: snapshot → extract → index. Extraction is on by
            # default; the index push reuses the same gates as
            # submit_document (indexing + OpenSearch must both be on).
            extraction = ExtractionService() if settings.extraction_enabled else None
            indexer = None
            if extraction is not None and settings.indexing_enabled and settings.opensearch_enabled:
                indexer = get_indexing_worker().process_one

            crawler_worker = CrawlWorker(
                CrawlPipeline(
                    frontier=FreshnessWorker(),
                    object_store=get_object_store(),
                    robots=RobotsCache(),
                    limiter=DomainRateLimiter(),
                    fetcher=Fetcher(),
                    extraction=extraction,
                    indexer=indexer,
                ),
                interval=settings.crawler_interval,
                batch_size=settings.crawler_batch_size,
                concurrency=settings.crawler_concurrency,
            )
            crawler_task = asyncio.create_task(crawler_worker.run_forever())
            logger.info(
                "crawler engine started (interval=%ss, batch=%s, concurrency=%s)",
                settings.crawler_interval,
                settings.crawler_batch_size,
                settings.crawler_concurrency,
            )
        except Exception as exc:  # noqa: BLE001 — optional infra
            logger.warning("crawler engine failed to start: %s", exc)

    yield
    task.cancel()
    migrations_task.cancel()
    if crawler_worker is not None:
        crawler_worker.stop()
    if crawler_task is not None:
        crawler_task.cancel()
        with suppress(asyncio.CancelledError):
            await crawler_task


_docs_on = not settings.api_auth_enabled
configure_json_logging()
setup_telemetry()

app = FastAPI(
    title="Search Hub",
    description="Unified search router for Hermes",
    version="3.0.0",
    lifespan=lifespan,
    # Built-in docs bypass require_api_key — a public schema is an auth
    # gap, so hide them whenever the auth gate is armed (dev keeps docs).
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)
instrument_app(app)

# V1 evidence-driven API (SPEC-v3): /v1/search, /v1/research, /v1/research/stream, /v1/verify, /v1/read, /v1/capabilities, /v1/providers
app.include_router(v1_router)

# OpenAI-compat gateway (P1): /v1/models + /v1/chat/completions
app.include_router(openai_router)
# OpenAI-shaped errors on the compat surface only: auth/HTTP errors raised
# inside the router's dependency, plus body-validation 422s. Native /v1
# routes keep FastAPI's {"detail": ...} contract.
app.add_exception_handler(OpenAIHTTPException, openai_http_exception_handler)
app.add_exception_handler(RequestValidationError, openai_validation_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    allow_credentials=False,
)


# Phase-0 deprecation shim — the five legacy tool endpoints are superseded by
# the /v1/* contract (docs/api/openapi.yaml).  They stay live for the MCP
# server (Hermes) but every call is logged and every response carries
# ``Deprecation: true`` (RFC 8594).  They are also gated by the same
# ``require_api_key`` dependency as /v1 — pass-through while
# API_AUTH_ENABLED=false, so local clients keep working.
_LEGACY_TOOL_PATHS = frozenset({"/search", "/fetch", "/code_search", "/answer"})
_AUTH_LEGACY = [Depends(require_api_key)]


@app.middleware("http")
async def correlation_context(request: Request, call_next):
    import uuid

    started = time.monotonic()
    request_id = safe_request_id(request.headers.get("X-Request-ID")) or f"req_{uuid.uuid4().hex}"
    token = request_id_var.set(request_id)
    # Search-Id correlates all log lines of a logical search across the request
    # lifecycle; absent or malformed it falls back to the request id so the
    # field is never "-".
    search_token = search_id_var.set(
        safe_request_id(request.headers.get("Search-Id")) or request_id
    )
    try:
        response = await call_next(request)
        route = request.scope.get("route")
        route_name = getattr(route, "path", "__unmatched__")
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request.completed",
            extra={
                "event": "request.completed",
                "method": request.method,
                "route": route_name,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "outcome": "success" if response.status_code < 400 else "error",
            },
        )
        return response
    finally:
        request_id_var.reset(token)
        search_id_var.reset(search_token)


# Low-cardinality operational metrics. Query text, URL and credentials are
# deliberately excluded from labels to prevent Prometheus cardinality leaks.
@app.middleware("http")
async def prometheus_http_metrics(request: Request, call_next):
    started = time.monotonic()
    IN_FLIGHT.inc()
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed = time.monotonic() - started
        route = request.scope.get("route")
        route_name = getattr(route, "path", request.url.path)
        status = str(getattr(locals().get("response", None), "status_code", 500))
        REQUESTS.labels(request.method, route_name, status).inc()
        REQUEST_LATENCY.labels(route_name).observe(elapsed)
        IN_FLIGHT.dec()


# P10: usage metering middleware — logs endpoint/status/latency for
# authenticated /v1 and legacy-tool calls into hub-postgres (fire-and-forget).
@app.middleware("http")
async def usage_metering(request: Request, call_next):
    import time as _t

    started = _t.monotonic()
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/v1/") or path in _LEGACY_TOOL_PATHS:
        from security.usage import schedule_log

        schedule_log(
            request,
            response.status_code,
            int((_t.monotonic() - started) * 1000),
        )
    return response


@app.middleware("http")
async def mark_legacy_deprecated(request: Request, call_next):
    path = request.url.path
    if path in _LEGACY_TOOL_PATHS:
        logger.warning(
            "deprecated endpoint called: %s %s — migrate to /v1/*",
            request.method,
            path,
        )
    response = await call_next(request)
    if path in _LEGACY_TOOL_PATHS:
        response.headers["Deprecation"] = "true"
    return response


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_lang(query: str, explicit: str) -> str:
    """Auto-detect query language when caller passes the default "en" or "auto".

    Vietnamese tone-mark detection via QueryUnderstanding (cheap, no LLM).
    Any other explicit code (de, ja, ...) is respected as-is.
    """
    if explicit and explicit not in ("en", "auto"):
        return explicit
    try:
        from core.query_understanding import QueryUnderstanding

        return QueryUnderstanding().analyze(query).language
    except Exception:
        return "en"


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""


def _is_news_domain(url: str) -> bool:
    """Heuristic: is this likely a news article?"""
    domain = _domain_from_url(url).lower()
    news_indicators = ["news", "blog", "article", "post"]
    return any(w in domain for w in news_indicators)


# ─── Multi-source expansion helpers ───────────────────────────────────────────

# Keywords that signal a research/technical query → augment with HN + arXiv.
_RESEARCH_KEYWORDS = frozenset(
    [
        "research",
        "paper",
        "study",
        "analysis",
        "algorithm",
        "implementation",
        "framework",
        "library",
        "benchmark",
        "performance",
        "comparison",
        "architecture",
        "design",
        "pattern",
        "paper",
        "arxiv",
        "survey",
        "review",
        "academic",
        "scientific",
        "model",
        "neural",
        "network",
        "machine",
        "learning",
        "deep",
        "learning",
        "transformer",
        "llm",
        "gpt",
        "bert",
        "embedding",
        "vector",
        "database",
        "distributed",
        "system",
        "consensus",
        "protocol",
        "raft",
        "paxos",
        "blockchain",
        "cryptography",
        "paper",
        "survey",
        "evaluation",
        "empirical",
        "theoretical",
    ]
)


def _is_research_technical_query(query: str) -> bool:
    """Heuristic: should we augment this query with HN + arXiv results?"""
    q_lower = query.lower()
    tokens = set(re.findall(r"\w+", q_lower))
    return bool(tokens & _RESEARCH_KEYWORDS)


async def _augment_multi_source(
    query: str,
    base_results: list[SearchResultItem],
    max_extra: int = 5,
) -> tuple[list[SearchResultItem], list[str]]:
    """Fetch HN + arXiv results and merge into base_results (dedup by URL).

    Returns (merged_results, providers_used). Only called for
    research/technical queries. Failures are swallowed — the base
    results are always returned intact.
    """
    providers: list[str] = []
    extra: list[SearchResultItem] = []

    # HN — good for technical discussions, show HN, engineering blog posts.
    if settings.provider_enabled.get("hn", True):
        try:
            hn_results = await hn_search(query, max_results=max_extra)
            if hn_results:
                extra.extend(hn_results)
                providers.append("hn")
        except Exception:
            pass

    # arXiv — good for academic/research papers.
    if settings.provider_enabled.get("arxiv", True):
        try:
            arxiv_results = await arxiv_search(query, max_results=max_extra)
            if arxiv_results:
                extra.extend(arxiv_results)
                providers.append("arxiv")
        except Exception:
            pass

    if not extra:
        return base_results, providers

    # Dedup by URL (case-insensitive).
    seen_urls = {r.url.lower() for r in base_results if r.url}
    merged = list(base_results)
    for r in extra:
        if r.url and r.url.lower() not in seen_urls:
            merged.append(r)
            seen_urls.add(r.url.lower())

    return merged, providers


# ─── Health ──────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthStatus)
async def health():
    searxng_ok = await searxng_health()
    firecrawl_ok = await firecrawl_health()
    llm_ok = "configured" if settings.llm_api_key else "not configured"
    return HealthStatus(
        searxng="ok" if searxng_ok else "unreachable",
        firecrawl="ok" if firecrawl_ok else "unreachable",
        llm=llm_ok,
    )


# ─── Tool 1: search ──────────────────────────────────────────────────────────


@app.post("/search", response_model=SearchToolResponse, dependencies=_AUTH_LEGACY)
async def search(req: SearchToolRequest):
    """search: Find information on the web.

    type=web  → general web search
    type=news → recent news articles
    type=image → image search

    Returns ranked sources with URL, title, description, domain, score.
    """
    start = time.time()

    # Determine category and TTL
    ttl_map = {
        SearchType.web: TTL_SEARCH_WEB,
        SearchType.news: TTL_SEARCH_NEWS,
        SearchType.image: TTL_SEARCH_IMAGES,
    }
    ttl = ttl_map.get(req.type, TTL_SEARCH_WEB)
    lang = _resolve_lang(req.query, req.lang)

    # Check cache
    cache_key = _cache_key("search", req.type.value, req.query, req.max_results, lang)
    cached = await search_cache.get(cache_key)
    if cached:
        cached.elapsed_seconds = 0.0  # cache hit
        record_query(
            query=req.query,
            endpoint="/search",
            query_type=req.type.value,
            results_count=len(cached.results),
            latency_ms=0,
            providers="searxng",
            cache_hit=True,
            relevance_scores=[s.score for s in cached.results if s.score],
            domains=[s.domain for s in cached.results],
        )
        return cached

    # ── Search Orchestrator + Evidence Aggregator (wave-9) ─────────────────
    # type=image keeps the legacy single-provider path (orchestrator covers
    # web/news/research/code families; images are a raw engine concern).
    sources: list[Source] = []
    providers_used: list[str] = []
    if req.type == SearchType.image:
        try:
            searxng_results = await with_retry(
                searxng_search,
                query=req.query,
                categories=[SearchCategory.images],
                max_results=req.max_results,
                lang=req.lang,
                max_retries=1,
            )
            results = rerank_search_results(req.query, searxng_results)
            for i, item in enumerate(results):
                sources.append(
                    Source(
                        source_id=f"src_{i:03d}",
                        url=item.url,
                        canonical_url=item.canonical_url,
                        title=item.title or "",
                        fingerprint=item.fingerprint,
                        domain=_domain_from_url(item.url),
                        description=item.description or "",
                        published_at=item.published_date,
                        score=item.score,
                        search_provider="searxng",
                        content_provider="none",
                        retrieval_observations=list(item.retrieval_observations or []),
                    )
                )
            providers_used = ["searxng"]
        except Exception:
            pass
    else:
        # Orchestrator chooses provider mix by query family; aggregator
        # collects → dedupes → ranks → labels trust (wave-9).
        from pipeline.evidence_aggregator import aggregate

        results_by_provider, family, _ = await orchestrate_search(
            req.query,
            type_hint=req.type.value,
            lang=req.lang,
            max_results=req.max_results,
        )
        package = aggregate(
            results_by_provider,
            req.query,
            max_results=req.max_results,
            lang=lang,
        )
        sources = package.sources
        providers_used = package.aggregation.providers_used

    response = SearchToolResponse(
        query=req.query,
        type=req.type.value,
        results=sources,
        elapsed_seconds=round(time.time() - start, 2),
    )

    # Cache it
    await search_cache.set(cache_key, response, ttl)

    record_query(
        query=req.query,
        endpoint="/search",
        query_type=req.type.value,
        results_count=len(sources),
        latency_ms=int((time.time() - start) * 1000),
        providers=",".join(providers_used) or "searxng",
        cache_hit=False,
        relevance_scores=[s.score for s in sources if s.score],
        domains=[s.domain for s in sources],
    )
    return response


# ─── Tool 2: fetch ───────────────────────────────────────────────────────────


@app.post("/fetch", response_model=FetchToolResponse, dependencies=_AUTH_LEGACY)
async def fetch(req: FetchToolRequest):
    """fetch: Read content from a URL.

    mode=scrape → read single page, return clean Markdown
    mode=crawl  → crawl multiple pages, return content for each
    mode=map    → discover all URLs on site, no content
    """
    start = time.time()

    # Check cache (scrape only — crawl/map are dynamic)
    cache_key = _cache_key("scrape", req.url, req.formats)
    if req.mode == FetchMode.scrape:
        cached = await scrape_cache.get(cache_key)
        if cached:
            cached.elapsed_seconds = 0.0
            record_query(
                query=req.url,
                endpoint="/fetch",
                query_type=req.mode.value,
                results_count=1 if cached.content else 0,
                latency_ms=0,
                providers="firecrawl",
                cache_hit=True,
            )
            return cached

    if req.mode == FetchMode.scrape:
        try:
            result = await with_retry(
                firecrawl_scrape,
                req.url,
                formats=[f.strip() for f in req.formats.split(",")],
                max_retries=1,
            )
        except Exception as e:
            return FetchToolResponse(
                url=req.url,
                mode=req.mode.value,
                error=f"Scrape failed: {e}",
                elapsed_seconds=round(time.time() - start, 2),
            )

        response = FetchToolResponse(
            url=req.url,
            mode=req.mode.value,
            content=result.markdown,
            title=result.title,
            metadata=result.metadata,
            error=result.error,
            elapsed_seconds=round(time.time() - start, 2),
        )
        # Cache with appropriate TTL
        ttl = TTL_SCRAPE_NEWS if _is_news_domain(req.url) else TTL_SCRAPE
        await scrape_cache.set(cache_key, response, ttl)
        record_query(
            query=req.url,
            endpoint="/fetch",
            query_type=req.mode.value,
            results_count=1 if result.markdown else 0,
            latency_ms=int((time.time() - start) * 1000),
            providers="firecrawl",
            cache_hit=False,
            error=result.error,
        )
        return response

    elif req.mode == FetchMode.crawl:
        try:
            result = await firecrawl_crawl(req.url, req.limit)
            pages = result.get("data", [])
            return FetchToolResponse(
                url=req.url,
                mode=req.mode.value,
                pages=pages,
                metadata={"status": result.get("status"), "total": result.get("total")},
                elapsed_seconds=round(time.time() - start, 2),
            )
        except Exception as e:
            return FetchToolResponse(
                url=req.url,
                mode=req.mode.value,
                error=f"Crawl failed: {e}",
                elapsed_seconds=round(time.time() - start, 2),
            )

    elif req.mode == FetchMode.map:
        try:
            urls = await firecrawl_map(req.url, req.limit)
            return FetchToolResponse(
                url=req.url,
                mode=req.mode.value,
                urls=urls,
                elapsed_seconds=round(time.time() - start, 2),
            )
        except Exception as e:
            return FetchToolResponse(
                url=req.url,
                mode=req.mode.value,
                error=f"Map failed: {e}",
                elapsed_seconds=round(time.time() - start, 2),
            )

    return FetchToolResponse(
        url=req.url,
        mode=req.mode.value,
        error=f"Unknown mode: {req.mode}",
        elapsed_seconds=round(time.time() - start, 2),
    )


# ─── Tool 3: code_search ─────────────────────────────────────────────────────


@app.post("/code_search", response_model=CodeSearchToolResponse, dependencies=_AUTH_LEGACY)
async def code_search(req: CodeSearchToolRequest):
    """code_search: Search code repositories via GitHub + grep.app.

    Optionally filter by repo (e.g. repo="firecrawl/firecrawl").
    """
    start = time.time()

    # Build query with repo filter
    query = req.query
    if req.repo:
        query = f"repo:{req.repo} {query}"

    tasks = [
        github_search(query, req.max_results),
        grep_app_search(req.query, req.max_results),  # grep doesn't support repo filter
    ]

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[dict] = []
    providers_used: list[str] = []
    for i, r in enumerate(gathered):
        if isinstance(r, list):
            results.extend(r)
            providers_used.append("github" if i == 0 else "grep")

    response = CodeSearchToolResponse(
        query=req.query,
        results=results[: req.max_results * 2],
        elapsed_seconds=round(time.time() - start, 2),
    )
    record_query(
        query=req.query,
        endpoint="/code_search",
        query_type="code",
        results_count=len(response.results),
        latency_ms=int((time.time() - start) * 1000),
        providers=",".join(providers_used) or "github,grep",
        cache_hit=False,
        relevance_scores=[
            float(r.get("score", 0) or 0) for r in response.results if r.get("score")
        ],
        domains=[_domain_from_url(r.get("url", "")) for r in response.results],
    )
    return response


# ─── Tool 5: answer ──────────────────────────────────────────────────────────


@app.post("/answer", response_model=AnswerToolResponse, dependencies=_AUTH_LEGACY)
async def answer(req: AnswerToolRequest):
    """answer: Full pipeline — search → scrape → rerank → synthesize with citations.

    This is the main tool for synthesized answers with evidence.
    depth=normal → search + scrape top 5 + rerank + synthesize
    depth=deep   → above + deeper synthesis + follow-up questions

    Returns answer + evidence (sources with full metadata + citations).
    """
    start = time.time()

    # 1. Orchestrate + aggregate: pick providers by query, label trust.
    from pipeline.evidence_aggregator import aggregate

    extra_providers: list[str] = []
    lang = _resolve_lang(req.query, req.lang)
    search_results: list[SearchResultItem] = []
    try:
        results_by_provider, family, used = await orchestrate_search(
            req.query,
            type_hint="web",
            lang=req.lang,
            max_results=req.max_results,
        )
        extra_providers = used
        package = aggregate(
            results_by_provider,
            req.query,
            max_results=req.max_results,
            lang=lang,
        )
        # Keep a plain SearchResultItem list for the scrape/rerank pipeline.
        for s in package.sources:
            search_results.append(
                SearchResultItem(
                    url=s.url,
                    title=s.title,
                    description=s.description,
                    score=s.score,
                    category="",
                )
            )
    except Exception:
        # Fallback: SearXNG alone so the endpoint still answers.
        try:
            searxng_results = await with_retry(
                searxng_search,
                query=req.query,
                categories=[SearchCategory.general],
                max_results=req.max_results,
                lang=lang,
                max_retries=1,
            )
            search_results = rerank_search_results(req.query, searxng_results)
        except Exception:
            search_results = []

    # 2. Read top N via the tiered reader (HTTP→Trafilatura→Firecrawl→Playwright)
    top_urls = [r.url for r in search_results[: req.scrape_top_n]]
    try:
        scraped = [
            r.to_scrape_result()
            for r in await read_batch(top_urls, timeout=settings.scrape_timeout)
        ]
    except Exception:
        scraped = []

    # 3. Rerank scraped content
    ranked_chunks = rerank_scraped_content(
        query=req.query,
        scraped=scraped,
        max_chunks_per_source=settings.max_chunks_per_source,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )

    # 4. Synthesize answer
    answer_text = await synthesize_answer(req.query, ranked_chunks)

    # 5. Generate follow-up questions
    follow_ups: list[str] = []
    if settings.llm_api_key:
        with suppress(Exception):
            follow_ups = await generate_follow_up_questions(answer_text)

    # 7. Build evidence with citations
    evidence = build_evidence(search_results, scraped, answer_text, "searxng")

    latency_ms = int((time.time() - start) * 1000)
    providers_str = "searxng,firecrawl"
    if extra_providers:
        providers_str += "," + ",".join(extra_providers)
    record_query(
        query=req.query,
        endpoint="/answer",
        query_type="answer",
        results_count=len(evidence.sources),
        latency_ms=latency_ms,
        providers=providers_str,
        cache_hit=False,
        relevance_scores=[s.score for s in evidence.sources if s.score],
        domains=[s.domain for s in evidence.sources],
    )

    return AnswerToolResponse(
        query=req.query,
        answer=answer_text,
        evidence=evidence,
        follow_up_questions=follow_ups,
        elapsed_seconds=round(time.time() - start, 2),
    )


# ─── Metrics (feedback & evaluation loop) ─────────────────────────────────────


@app.get("/metrics/prometheus", include_in_schema=False)
async def metrics_prometheus():
    """Prometheus exposition endpoint; keep it on the private Docker network."""
    from fastapi.responses import Response

    payload, content_type = prometheus_payload()
    return Response(content=payload, media_type=content_type.split(";")[0])


@app.get("/metrics", dependencies=_AUTH_LEGACY)
async def metrics_overview():
    """Aggregate quality overview across all logged queries."""
    return metrics_store.quality_overview()


@app.get("/metrics/queries", dependencies=_AUTH_LEGACY)
async def metrics_queries(limit: int = 50):
    """Most recent query-log entries (newest first)."""
    return {"queries": metrics_store.recent_queries(limit)}


@app.get("/metrics/quality", dependencies=_AUTH_LEGACY)
async def metrics_quality(days: int = 7):
    """Daily quality trend for the last `days` days (oldest first)."""
    return {"days": days, "trend": metrics_store.quality_trend(days)}


# ─── Root ────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "service": "Search Hub",
        "version": "3.0.0",
        "tools": {
            "search": "POST /search — search(query, type=web|news|image)",
            "fetch": "POST /fetch — fetch(url, mode=scrape|crawl|map)",
            "code_search": "POST /code_search — code_search(query, repo?)",
            "answer": "POST /answer — answer(query, depth=normal|deep) + evidence + citations",
        },
        "metrics": {
            "overview": "GET /metrics — aggregate quality overview",
            "queries": "GET /metrics/queries?limit=N — recent query log",
            "quality": "GET /metrics/quality?days=N — daily quality trend",
        },
        "providers": {
            "searxng": settings.searxng_url,
            "firecrawl": settings.firecrawl_url,
            "llm": settings.llm_model if settings.llm_api_key else "not configured",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
