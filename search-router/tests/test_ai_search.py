"""Tests for the AI Search pipeline modules.

Covers: search-mode budgets, RRF fusion, passage chunking/reranking, and
LLM query planning (mocked).  The ``/ai-search`` endpoint itself and the
``agent.ai_reranker`` compat shim were removed in phase-0 dedup (T1b) — the
endpoint duplicated ``/v1/search?mode=`` exercised in test_research_engine.
"""

import asyncio
import json

from agent import query_planner
from models import ScrapeResult
from pipeline.passage_reranker import (
    chunk_and_rerank,
    chunk_pages,
    rerank_passages,
)
from pipeline.reranker import _chunk_text
from pipeline.search_modes import SEARCH_MODES, get_mode, mode_for_depth
from ranking.fusion import fuse_queries, fusion
from ranking.types import RankedItem
from research_models.research_state import SearchIntent

# ─── search_modes.get_mode ────────────────────────────────────────────────────


def test_get_mode_canonical():
    for name in ("fast", "balanced", "deep"):
        mode = get_mode(name)
        assert mode.name == name
        assert mode.num_queries > 0
        assert mode.per_query_results > 0
        assert mode.scrape_top_n > 0
        assert mode.passage_top_n > 0


def test_get_mode_aliases():
    assert get_mode("quick").name == "fast"
    assert get_mode("normal").name == "balanced"
    assert get_mode("auto").name == "balanced"


def test_get_mode_unknown_falls_back():
    assert get_mode("nonexistent").name == "balanced"
    assert get_mode("").name == "balanced"
    assert get_mode(None).name == "balanced"


def test_mode_budgets_increase_with_depth():
    fast, balanced, deep = (SEARCH_MODES[k] for k in ("fast", "balanced", "deep"))
    assert fast.num_queries < balanced.num_queries < deep.num_queries
    assert fast.scrape_top_n < balanced.scrape_top_n < deep.scrape_top_n
    assert fast.max_followups == 0
    assert balanced.max_followups < deep.max_followups


def test_mode_for_depth():
    assert mode_for_depth("quick").name == "fast"
    assert mode_for_depth("normal").name == "balanced"
    assert mode_for_depth("deep").name == "deep"


# ─── ranking.fusion (RRF) ─────────────────────────────────────────────────────


def _item(url: str, provider: str = "searxng") -> RankedItem:
    return RankedItem(
        url=url,
        title=f"title {url}",
        description="desc",
        provider=provider,
        canonical_url=url,
    )


def test_fuse_queries_dedupes_and_orders():
    q1 = [_item("https://a.com/x"), _item("https://b.com/y")]
    q2 = [_item("https://b.com/y"), _item("https://c.com/z")]
    fused = fuse_queries({"q1": q1, "q2": q2})

    assert len(fused) == 3
    # Present in both query lists → highest combined RRF score.
    assert fused[0].url == "https://b.com/y"
    assert fused[0].metadata["query_hits"] == 2
    assert set(fused[0].metadata["matched_queries"]) == {"q1", "q2"}
    assert all(0.0 <= i.normalized_score <= 1.0 for i in fused)


def test_fuse_queries_empty():
    assert fuse_queries({}) == []
    assert fuse_queries({"q": []}) == []


def test_fusion_provider_weights():
    items = {
        "searxng": [_item("https://a.com/1")],
        "reddit": [_item("https://b.com/2")],
    }
    fused = fusion(items)
    assert len(fused) == 2
    # Same rank position → searxng (weight 1.0) beats reddit (0.85).
    assert fused[0].url == "https://a.com/1"


# ─── passage chunking + reranking ─────────────────────────────────────────────


def test_chunk_text_overlap():
    text = " ".join(f"w{i}" for i in range(400))  # ~2000 chars
    chunks = _chunk_text(text, 800, 200)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)
    # Short text stays a single chunk.
    assert _chunk_text("tiny", 800, 200) == ["tiny"]


def test_chunk_pages_skips_errors_and_empty():
    scraped = [
        ScrapeResult(url="https://x.com", title="X", markdown="alpha " * 300),
        ScrapeResult(url="https://err.com", error="boom"),
        ScrapeResult(url="https://empty.com", markdown=""),
    ]
    chunks = chunk_pages(scraped, chunk_size=400, chunk_overlap=100)
    assert chunks
    assert all(c["source_url"] == "https://x.com" for c in chunks)
    assert all(c["score"] == 0.0 for c in chunks)


def test_rerank_passages_scores_and_truncates():
    scraped = [
        ScrapeResult(
            url="https://x.com",
            title="X",
            markdown="python async tutorial " * 100,
        ),
        ScrapeResult(url="https://y.com", title="Y", markdown="unrelated " * 100),
    ]
    chunks = chunk_pages(scraped, chunk_size=400, chunk_overlap=100)
    ranked = rerank_passages("python async", chunks, top_n=3)
    assert 0 < len(ranked) <= 3
    scores = [c["score"] for c in ranked]
    assert scores == sorted(scores, reverse=True)
    # Relevant page's passages outrank the unrelated page's.
    assert ranked[0]["source_url"] == "https://x.com"


def test_chunk_and_rerank_one_shot():
    scraped = [ScrapeResult(url="https://x.com", title="X", markdown="data " * 300)]
    ranked = chunk_and_rerank("data", scraped, top_n=5)
    assert 0 < len(ranked) <= 5
    assert chunk_and_rerank("q", [], top_n=5) == []


# ─── query_planner.plan_queries (LLM mocked) ──────────────────────────────────


def _intent(depth: str = "normal") -> SearchIntent:
    return SearchIntent(needs_web=True, depth=depth)


def test_plan_queries_llm_path(monkeypatch):
    async def fake_llm(messages, **kwargs):
        return json.dumps(
            {
                "queries": [
                    {
                        "query": "sub question one",
                        "lang": "en",
                        "purpose": "sub_question",
                    },
                    {"query": "truy vấn phụ", "lang": "vi", "purpose": "vi_variant"},
                ]
            }
        )

    monkeypatch.setattr(query_planner, "llm_chat", fake_llm)
    plan = asyncio.run(query_planner.plan_queries("test query", _intent(), max_queries=5))

    assert plan.source == "llm"
    assert plan.queries[0].query == "test query"
    assert plan.queries[0].purpose == "original"
    assert len(plan.queries) == 3
    assert {q.lang for q in plan.queries[1:]} == {"en", "vi"}


def test_plan_queries_heuristic_fallback(monkeypatch):
    async def no_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(query_planner, "llm_chat", no_llm)
    plan = asyncio.run(query_planner.plan_queries("What is FastAPI", _intent(), max_queries=6))

    assert plan.source == "heuristic"
    assert plan.queries[0].query == "What is FastAPI"
    assert len(plan.queries) >= 1


def test_plan_queries_respects_cap_and_dedupes(monkeypatch):
    async def fake_llm(messages, **kwargs):
        return json.dumps({"queries": ["dup query", "dup query", "other query", "third query"]})

    monkeypatch.setattr(query_planner, "llm_chat", fake_llm)
    plan = asyncio.run(query_planner.plan_queries("original q", _intent(), max_queries=2))

    assert len(plan.queries) <= 2
    assert plan.queries[0].query == "original q"
    texts = [q.query.lower() for q in plan.queries]
    assert len(texts) == len(set(texts))


def test_plan_queries_invalid_llm_output_falls_back(monkeypatch):
    async def bad_llm(messages, **kwargs):
        return "not json at all"

    monkeypatch.setattr(query_planner, "llm_chat", bad_llm)
    plan = asyncio.run(query_planner.plan_queries("q", _intent(), max_queries=3))
    assert plan.source == "heuristic"
