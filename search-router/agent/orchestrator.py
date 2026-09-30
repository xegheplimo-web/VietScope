"""Orchestrator — State machine controller for the Research Agent.

Canonical *research agent loop* for ``/v1/search?mode=`` and ``/v1/answer``
(distinct role from ``core.orchestrator.SearchOrchestrator``, which owns
provider-registry search fan-out for the raw /v1 endpoints, and from the
deprecated legacy ``pipeline.router``).  See ``docs/phase0-dedup-map.md``.

Pipeline (spec_searchhub_001):

    START → ANALYZE → PLAN → SEARCH → RERANK → SCRAPE → EXTRACT_EVIDENCE
    → CHECK_GAPS → (SEARCH, follow-up) or (SYNTHESIZE → VERIFY → END)

SEARCH runs all sub-queries in parallel and keeps per-query lists so RERANK
can fuse them with pure RRF (:func:`ranking.fusion.fuse_queries`) before the
AI cross-encoder / v2 quality ordering.  EXTRACT_EVIDENCE is passage-level:
scraped pages are chunked and passage-reranked before synthesis.
CHECK_GAPS bounds the follow-up loop by the search mode's ``max_followups``.
The answer is synthesized first, then its claims are verified and
unsupported ones removed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from config import settings
from evidence.citation import build_passage_citations
from models import Source
from pipeline.passage_reranker import chunk_and_rerank
from pipeline.reader import read_batch
from pipeline.search_modes import SearchModeConfig, get_mode, mode_for_depth
from research_models.research_state import (
    ResearchContext,
    ResearchState,
    SearchIntent,
    SourceResult,
)

from agent.evidence import extract_evidence_from_passages
from agent.gap_analyzer import analyze_gaps
from agent.intent import analyze_intent
from agent.query_generator import generate_queries_from_gaps
from agent.query_planner import detect_lang, plan_queries
from agent.reranker import get_default_reranker, rerank_multi_query
from agent.retriever import retrieve
from agent.synthesizer import synthesize_answer
from agent.verifier import verify_answer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pipeline.federated_retrieval import FederatedResult

logger = logging.getLogger(__name__)

# Follow-up rounds never emit more queries than this, regardless of mode.
_MAX_FOLLOWUP_QUERIES = 6

# Lane key under which hybrid index results join the rerank candidate pool.
_HYBRID_LANE_KEY = "__hybrid__"


async def _run_hybrid_lane(factory: Callable[[], Awaitable[FederatedResult]]) -> FederatedResult:
    """Await the hybrid-lane factory — a lane failure degrades, never kills the run."""
    from pipeline.federated_retrieval import FederatedResult

    try:
        return await factory()
    except Exception as exc:  # noqa: BLE001 — hybrid lane is additive
        logger.warning("hybrid retrieval lane failed: %s", exc)
        return FederatedResult(degraded=True, degraded_reason="lane_error")


def _resolve_mode(context: ResearchContext) -> SearchModeConfig:
    """Map ``context.mode`` onto a canonical SearchModeConfig.

    ``auto`` defers to the intent's depth; every other value resolves
    through :func:`get_mode` (aliases: quick→fast, normal→balanced).
    """
    if context.mode == "auto" and context.intent is not None:
        return mode_for_depth(context.intent.depth)
    return get_mode(context.mode)


async def _search_round(
    queries: list[str],
    langs: dict[str, str],
    per_query_results: int,
    default_lang: str | None = None,
) -> dict[str, list[SourceResult]]:
    """Retrieve all sub-queries in parallel → ``{query: [SourceResult]}``."""

    async def _one(query: str) -> list[SourceResult]:
        try:
            return await retrieve(
                query,
                max_results=per_query_results,
                lang=langs.get(query) or default_lang or detect_lang(query),
            )
        except Exception as exc:  # noqa: BLE001 — one bad query ≠ dead run
            logger.warning("retrieve failed for %r: %s", query, exc)
            return []

    per_query = await asyncio.gather(*(_one(q) for q in queries))
    return dict(zip(queries, per_query, strict=False))


def _flatten_unique(
    results_by_query: dict[str, list[SourceResult]],
) -> list[SourceResult]:
    """URL-deduplicated flatten used when the ranking path yields nothing."""
    seen: set[str] = set()
    out: list[SourceResult] = []
    for results in results_by_query.values():
        for r in results:
            if r.url and r.url not in seen:
                seen.add(r.url)
                out.append(r)
    return out


async def run_research(
    context: ResearchContext,
    *,
    language: str | None = None,
    freshness: str | None = None,
    hybrid_lane: Callable[[], Awaitable[FederatedResult]] | None = None,
    emit: Callable[[str, dict], Awaitable[None]] | None = None,
) -> dict:
    """Run the full research pipeline and return answer + provenance stats.

    ``language`` biases retrieval for queries without an explicit planner
    language (mainly follow-up queries); ``freshness``
    (``any``/``day``/``week``/``month``) overrides the intent's freshness
    hint, which the planner uses to bias recency-focused sub-queries.

    ``hybrid_lane`` (P11) is an optional zero-arg factory returning the
    federated-retrieval coroutine (OpenSearch BM25 + Qdrant dense → RRF).
    It runs in parallel with the first live-web search round and its fused
    hits join ``results_by_query`` before reranking — an additive lane,
    never a replacement for live-web providers.

    ``emit`` (P3) is an optional async ``(event, data)`` sink receiving
    live progress events — ``planning``/``plan``/``search.*``/``source``/
    ``source.read``/``evidence``/``synthesizing``/``answer.delta``/
    ``verified``/``answer.final`` — so SSE callers stream real pipeline
    state instead of replaying a finished result.
    """
    started = time.perf_counter()
    timings: dict = {}
    stats: dict = {
        "generated_queries": [],
        "plan_source": "heuristic",
        "raw_results": 0,
        "unique_results": 0,
        "pages_read": 0,
        "followup_rounds": 0,
        "passages": 0,
        "verify": {},
    }
    # Per-run working set (kept out of ResearchContext — the state model is
    # a pydantic contract shared with the /research-agent route).
    mode: SearchModeConfig = get_mode("balanced")
    langs: dict[str, str] = {}
    results_by_query: dict[str, list[SourceResult]] = {}
    passages: list[dict] = []
    scraped_urls: set[str] = set()
    reranker = get_default_reranker()

    def _mark(name: str, t0: float) -> None:
        timings[name] = round(timings.get(name, 0.0) + (time.perf_counter() - t0), 3)

    streamed_tokens: list[str] = []
    emitted_sources: set[str] = set()

    async def _emit(event: str, data: dict) -> None:
        if emit is None:
            return
        try:
            await emit(event, data)
        except Exception:  # noqa: BLE001 — a broken sink must not kill the run
            logger.debug("emit failed for event %s", event)

    async def _delta(token: str) -> None:
        streamed_tokens.append(token)
        await _emit("answer.delta", {"text": token})

    while context.state != ResearchState.END:
        match context.state:
            case ResearchState.START:
                context.state = ResearchState.ANALYZE

            case ResearchState.ANALYZE:
                t0 = time.perf_counter()
                context.intent = analyze_intent(context.query)
                if freshness in ("any", "day", "week", "month"):
                    context.intent.freshness = freshness
                mode = _resolve_mode(context)
                # search_round counts completed rounds; max_rounds bounds the
                # total so follow-up rounds == mode.max_followups.
                context.max_rounds = mode.max_followups + 1
                _mark("analyze", t0)
                await _emit(
                    "planning",
                    {
                        "query": context.query,
                        "intent": getattr(context.intent, "intent", None),
                        "mode": mode.name,
                    },
                )
                context.state = ResearchState.PLAN

            case ResearchState.PLAN:
                t0 = time.perf_counter()
                plan = await plan_queries(
                    context.query,
                    context.intent or SearchIntent(),
                    max_queries=mode.num_queries,
                )
                context.search_plan = plan.texts
                stats["generated_queries"] = plan.texts
                stats["plan_source"] = plan.source
                langs.update({p.query: p.lang for p in plan.queries})
                _mark("plan", t0)
                await _emit(
                    "plan",
                    {
                        "queries": list(context.search_plan or []),
                        "source": stats["plan_source"],
                    },
                )
                context.state = ResearchState.SEARCH

            case ResearchState.SEARCH:
                t0 = time.perf_counter()
                await _emit(
                    "search.started",
                    {
                        "round": context.search_round + 1,
                        "queries": list(context.search_plan or []),
                    },
                )
                # P11: on the first round the hybrid index lane (OpenSearch
                # BM25 + Qdrant dense → RRF) runs alongside the live-web
                # providers and feeds the same candidate pool.
                hybrid_result: FederatedResult | None = None
                if hybrid_lane is not None and context.search_round == 0:
                    round_results, hybrid_result = await asyncio.gather(
                        _search_round(context.search_plan, langs, mode.per_query_results, language),
                        _run_hybrid_lane(hybrid_lane),
                    )
                else:
                    round_results = await _search_round(
                        context.search_plan, langs, mode.per_query_results, language
                    )
                for q, results in round_results.items():
                    results_by_query.setdefault(q, []).extend(results)
                    if q not in context.queries_used:
                        context.queries_used.append(q)
                stats["raw_results"] += sum(len(r) for r in round_results.values())
                if hybrid_result is not None:
                    stats["hybrid"] = {
                        "os_hits": hybrid_result.opensearch_count,
                        "qdrant_hits": hybrid_result.qdrant_count,
                        "fused": hybrid_result.fused_count,
                        "degraded": hybrid_result.degraded,
                    }
                    if hybrid_result.degraded_reason:
                        stats["hybrid"]["reason"] = hybrid_result.degraded_reason
                    lane = hybrid_result.to_source_results(top_n=settings.hybrid_fused_top)
                    if lane:
                        results_by_query.setdefault(_HYBRID_LANE_KEY, []).extend(lane)
                        stats["hybrid"]["merged"] = len(lane)
                context.search_round += 1
                _mark("search", t0)
                await _emit(
                    "search.done",
                    {
                        "round": context.search_round,
                        "new_results": sum(len(r) for r in round_results.values()),
                        "total_results": stats["raw_results"],
                    },
                )
                context.state = ResearchState.RERANK

            case ResearchState.RERANK:
                t0 = time.perf_counter()
                try:
                    ranked = rerank_multi_query(
                        results_by_query,
                        context.query,
                        reranker=reranker,
                        top_n=mode.max_results,
                    )
                except Exception as exc:  # noqa: BLE001 — degrade, don't die
                    logger.warning("rerank_multi_query failed: %s", exc)
                    ranked = []
                if not ranked:
                    ranked = _flatten_unique(results_by_query)
                context.search_results = ranked
                stats["unique_results"] = len(ranked)
                for s in ranked[: mode.scrape_top_n]:
                    if s.url in emitted_sources:
                        continue
                    emitted_sources.add(s.url)
                    await _emit(
                        "source",
                        {
                            "source_id": s.source_id,
                            "url": s.url,
                            "title": s.title,
                            "domain": s.domain,
                            "score": s.score,
                        },
                    )
                _mark("rerank", t0)
                context.state = ResearchState.SCRAPE

            case ResearchState.SCRAPE:
                t0 = time.perf_counter()
                top_urls = [
                    r.url
                    for r in context.search_results[: mode.scrape_top_n]
                    if r.url and r.url not in scraped_urls
                ]
                new_scraped = []
                if top_urls:
                    try:
                        read_results = await read_batch(
                            top_urls,
                            timeout=settings.scrape_timeout,
                        )
                        new_scraped = [r.to_scrape_result() for r in read_results]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("reader batch failed: %s", exc)
                        new_scraped = []
                scraped_urls.update(top_urls)
                context.scraped_content.extend(new_scraped)
                # L17: backfill scraped pages into the internal indexes
                # (OpenSearch + Qdrant) — fire-and-forget.
                for _sc in new_scraped:
                    if getattr(_sc, "error", None) or not getattr(_sc, "markdown", ""):
                        continue
                    try:
                        from workers.indexing_worker import submit_document

                        submit_document(
                            _sc.url,
                            getattr(_sc, "title", "") or "",
                            _sc.markdown,
                        )
                    except Exception:  # noqa: BLE001 — indexing is best-effort
                        pass
                stats["pages_read"] += sum(
                    1
                    for s in new_scraped
                    if not getattr(s, "error", None) and getattr(s, "markdown", "")
                )
                for sc in new_scraped:
                    await _emit(
                        "source.read",
                        {
                            "url": sc.url,
                            "success": not getattr(sc, "error", None),
                            "tier": (sc.metadata or {}).get("reader_tier", ""),
                            "chars": len(sc.markdown or ""),
                        },
                    )
                # Passage rerank this round's pages; merge across rounds.
                try:
                    new_passages = chunk_and_rerank(
                        context.query,
                        new_scraped,
                        top_n=mode.passage_top_n,
                        reranker=reranker,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("passage rerank failed: %s", exc)
                    new_passages = []
                passages.extend(new_passages)
                _mark("scrape", t0)
                context.state = ResearchState.EXTRACT_EVIDENCE

            case ResearchState.EXTRACT_EVIDENCE:
                t0 = time.perf_counter()
                # Dedupe merged passages by (url, chunk_index), keep the top
                # scores bounded so synthesis context stays finite.
                seen_keys: set[tuple] = set()
                merged: list[dict] = []
                for p in sorted(passages, key=lambda x: x.get("score", 0.0), reverse=True):
                    key = (p.get("source_url"), p.get("chunk_index"))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    merged.append(p)
                    if len(merged) >= mode.passage_top_n * 2:
                        break
                passages[:] = merged
                context.evidence = await extract_evidence_from_passages(merged)
                stats["passages"] = len(context.evidence)
                _mark("extract_evidence", t0)
                await _emit("evidence", {"count": len(context.evidence)})
                context.state = ResearchState.CHECK_GAPS

            case ResearchState.CHECK_GAPS:
                t0 = time.perf_counter()
                gap_result = await analyze_gaps(context.evidence, context.query)
                context.gaps = gap_result.missing

                if gap_result.need_more_search and context.search_round < context.max_rounds:
                    followups = await generate_queries_from_gaps(gap_result.missing)
                    context.search_plan = followups[:_MAX_FOLLOWUP_QUERIES]
                    stats["followup_rounds"] += 1
                    _mark("check_gaps", t0)
                    await _emit(
                        "research.round",
                        {
                            "round": stats["followup_rounds"],
                            "missing": gap_result.missing,
                            "queries": list(context.search_plan),
                        },
                    )
                    context.state = ResearchState.SEARCH
                else:
                    _mark("check_gaps", t0)
                    context.state = ResearchState.SYNTHESIZE

            case ResearchState.SYNTHESIZE:
                t0 = time.perf_counter()
                await _emit("synthesizing", {})
                answer, confidence = await synthesize_answer(
                    context.query,
                    context.claims,
                    context.evidence,
                    on_delta=_delta if emit is not None else None,
                )
                context.answer = answer
                context.confidence = confidence
                _mark("synthesize", t0)
                context.state = ResearchState.VERIFY

            case ResearchState.VERIFY:
                t0 = time.perf_counter()
                filtered, claims, vstats = await verify_answer(
                    context.answer or "", context.evidence
                )
                context.answer = filtered
                context.claims = claims
                stats["verify"] = vstats
                # Confidence is deterministic: verified-claim ratio boosted by
                # average evidence support (mirrors synthesizer's formula).
                if vstats["claims_total"]:
                    base = vstats["claims_verified"] / vstats["claims_total"]
                    if context.evidence:
                        avg_support = sum(e.support for e in context.evidence) / len(
                            context.evidence
                        )
                        context.confidence = round(min(base * (1 + avg_support), 1.0), 3)
                    else:
                        context.confidence = round(base, 3)
                _mark("verify", t0)
                await _emit(
                    "verified",
                    {
                        "claims_total": vstats["claims_total"],
                        "claims_verified": vstats["claims_verified"],
                        "claims_removed": vstats.get("claims_removed", 0),
                    },
                )
                # Reconcile: if verification or citation-repair changed the
                # answer after tokens were streamed, send the final text so
                # clients can correct what they rendered.
                streamed_text = "".join(streamed_tokens)
                if streamed_text and context.answer != streamed_text:
                    await _emit("answer.final", {"text": context.answer})
                context.state = ResearchState.END

    timings["total"] = round(time.perf_counter() - started, 3)
    # P11: surface the hybrid lane's fused/degraded counters on the response.
    if "hybrid" in stats:
        timings["hybrid"] = stats["hybrid"]

    return {
        "answer": context.answer,
        "confidence": context.confidence,
        "search_rounds": context.search_round,
        "queries": context.queries_used,
        "sources": [
            {
                "title": s.title,
                "url": s.url,
                "score": s.score,
                "domain": s.domain,
            }
            for s in context.search_results[:10]
        ],
        "citations": _build_citations(context),
        "search": {
            "generated_queries": stats["generated_queries"],
            "plan_source": stats["plan_source"],
            "raw_results": stats["raw_results"],
            "unique_results": stats["unique_results"],
            "pages_read": stats["pages_read"],
            "followup_rounds": stats["followup_rounds"],
            "passages": stats["passages"],
        },
        "verification": stats["verify"],
        "timings": timings,
    }


def _build_citations(context: ResearchContext) -> list[dict]:
    """P2: map verified claims to quote offsets inside the fetched source text.

    ``build_passage_citations`` needs candidate sources whose ``source_id``
    matches the ids attached to each claim's evidence.  EvidenceItems carry
    per-passage ids (``psg_*``), so each gets a pseudo-Source backed by the
    full page content of its URL — quote offsets then index into the real
    fetched document.  ``page_*`` sources cover claims whose evidence list is
    empty (keyword fallback over every fetched page).
    """
    claims = context.claims or []
    if not claims:
        return []

    content_by_url = {
        getattr(sc, "url", ""): getattr(sc, "markdown", "") or "" for sc in context.scraped_content
    }
    cite_sources: list[Source] = []
    seen_ids: set[str] = set()
    for ev in context.evidence or []:
        body = content_by_url.get(getattr(ev, "url", ""))
        sid = getattr(ev, "source_id", "")
        if not body or sid in seen_ids:
            continue
        seen_ids.add(sid)
        cite_sources.append(
            Source(
                source_id=sid,
                url=getattr(ev, "url", "") or "",
                title=getattr(ev, "title", "") or "",
                content=body,
            )
        )
    for i, sc in enumerate(context.scraped_content):
        body = getattr(sc, "markdown", "") or ""
        if not body:
            continue
        cite_sources.append(
            Source(
                source_id=f"page_{i:03d}",
                url=getattr(sc, "url", "") or "",
                title=getattr(sc, "title", "") or "",
                content=body,
            )
        )

    cite_claims = [
        {
            "claim_id": f"c{i}",
            "text": c.claim,
            "evidence": [ev.source_id for ev in c.evidence],
        }
        for i, c in enumerate(claims)
    ]
    by_id = {cv.claim_id: cv for cv in build_passage_citations(cite_claims, cite_sources)}

    out: list[dict] = []
    for i, c in enumerate(claims):
        cv = by_id.get(f"c{i}")
        out.append(
            {
                "claim_id": f"c{i}",
                "claim": c.claim,
                "verified": c.verified,
                "evidence": cv.evidence if cv else [],
                "evidence_count": len(cv.evidence) if cv else 0,
                "citation_text": cv.citation_text if cv else "",
            }
        )
    return out
