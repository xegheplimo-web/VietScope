"""Tests for the AI research-engine upgrade (spec_searchhub_001).

Covers: search-mode budgets, the LLM query planner (VI+EN, schema
validation, heuristic fallback), multi-query RRF, the v2 quality formula,
the lazy cross-encoder reranker + fallback, passage reranking, claim-level
answer verification, the orchestrator follow-up loop, and the enhanced
``POST /v1/search`` contract.
"""

import asyncio
import json
from typing import ClassVar

from models import ScrapeResult
from pipeline.reader import ReadResult
from ranking.types import RankedItem
from research_models.research_state import (
    EvidenceItem,
    GapResult,
    ResearchContext,
    SearchIntent,
    SourceResult,
)

# ─── Helpers ────────────────────────────────────────────────────────────────


def _src(url: str, score: float = 0.5, title: str = "t", desc: str = "d") -> SourceResult:
    return SourceResult(
        source_id=f"s_{abs(hash(url)) % 9999}",
        url=url,
        title=title,
        description=desc,
        domain=url.split("/")[2] if "://" in url else "",
        score=score,
    )


class FakeReranker:
    """Duck-typed stand-in for CrossEncoderReranker in tests."""

    def __init__(self, scores=None, available=True):
        self._scores = scores
        self._available = available
        self.calls = []

    def available(self) -> bool:
        return self._available

    def score(self, query, docs):
        self.calls.append((query, list(docs)))
        if self._scores is None:
            return [0.9] * len(docs)
        return self._scores[: len(docs)]


# ─── Search modes ───────────────────────────────────────────────────────────


class TestSearchModes:
    def test_fast_budget(self):
        from pipeline.search_modes import get_mode

        m = get_mode("fast")
        assert m.num_queries == 3
        assert m.max_results == 25
        assert m.scrape_top_n == 5
        assert m.max_followups == 0

    def test_balanced_budget(self):
        from pipeline.search_modes import get_mode

        m = get_mode("balanced")
        assert m.num_queries == 6
        assert m.max_results == 50
        assert m.scrape_top_n == 10
        assert m.max_followups == 1

    def test_deep_budget(self):
        from pipeline.search_modes import get_mode

        m = get_mode("deep")
        assert m.num_queries == 10
        assert m.max_results == 100
        assert m.scrape_top_n == 20
        assert m.max_followups == 3

    def test_aliases(self):
        from pipeline.search_modes import get_mode

        assert get_mode("quick").name == "fast"
        assert get_mode("normal").name == "balanced"
        assert get_mode("auto").name == "balanced"
        assert get_mode("bogus").name == "balanced"

    def test_mode_for_depth(self):
        from pipeline.search_modes import mode_for_depth

        assert mode_for_depth("quick").name == "fast"
        assert mode_for_depth("none").name == "fast"
        assert mode_for_depth("normal").name == "balanced"
        assert mode_for_depth("deep").name == "deep"


# ─── Query planner ──────────────────────────────────────────────────────────


class TestQueryPlanner:
    def test_detect_lang(self):
        from agent.query_planner import detect_lang

        assert detect_lang("vụ án lừa đảo ở Bắc Giang") == "vi"
        assert detect_lang("docker compose networking") == "en"

    def test_parse_valid_object(self):
        from agent.query_planner import _parse_llm_queries

        raw = json.dumps(
            {
                "queries": [
                    {"query": "vụ án A", "lang": "vi", "purpose": "sub"},
                    {"query": "case A news", "lang": "en"},
                    "plain string query",
                ]
            }
        )
        parsed = _parse_llm_queries(raw)
        assert parsed is not None
        assert [p.query for p in parsed] == [
            "vụ án A",
            "case A news",
            "plain string query",
        ]
        assert parsed[0].lang == "vi"
        assert parsed[1].lang == "en"

    def test_parse_rejects_garbage(self):
        from agent.query_planner import _parse_llm_queries

        assert _parse_llm_queries("not json") is None
        assert _parse_llm_queries('{"queries": {"a": 1}}') is None
        assert _parse_llm_queries('{"queries": []}') is None
        assert _parse_llm_queries(json.dumps({"nope": 1})) is None

    def test_parse_normalizes_bad_lang(self):
        from agent.query_planner import _parse_llm_queries

        raw = json.dumps({"queries": [{"query": "hello world", "lang": "xx"}]})
        parsed = _parse_llm_queries(raw)
        assert parsed[0].lang == "en"  # falls back to detected language

    def test_llm_plan_bilingual_and_capped(self, monkeypatch):
        import agent.query_planner as qp

        async def fake_llm(messages, **kwargs):
            return json.dumps(
                {
                    "queries": [
                        {"query": "vụ án Phạm Chí Nghị Bắc Giang", "lang": "vi"},
                        {"query": "Pham Chi Nghi case Bac Giang", "lang": "en"},
                        {"query": "extra 1"},
                        {"query": "extra 2"},
                        {"query": "extra 3"},
                        {"query": "extra 4"},
                        {"query": "extra 5"},
                    ]
                }
            )

        monkeypatch.setattr(qp, "llm_chat", fake_llm)
        intent = SearchIntent(depth="normal")
        plan = asyncio.run(qp.plan_queries("Phạm Chí Nghị là ai", intent))
        assert plan.source == "llm"
        assert plan.queries[0].query == "Phạm Chí Nghị là ai"  # original first
        assert len(plan.queries) <= 6  # normal depth cap
        assert {"vi", "en"} & plan.languages  # bilingual coverage

    def test_heuristic_fallback_when_llm_dead(self, monkeypatch):
        import agent.query_planner as qp

        async def dead_llm(messages, **kwargs):
            return None

        monkeypatch.setattr(qp, "llm_chat", dead_llm)
        intent = SearchIntent(depth="quick")
        plan = asyncio.run(qp.plan_queries("original question here", intent))
        assert plan.source == "heuristic"
        assert plan.queries[0].query == "original question here"
        assert len(plan.queries) <= 3  # quick cap

    def test_heuristic_fallback_on_bad_json(self, monkeypatch):
        import agent.query_planner as qp

        async def bad_llm(messages, **kwargs):
            return "sure, here are some queries!"  # not JSON

        monkeypatch.setattr(qp, "llm_chat", bad_llm)
        plan = asyncio.run(qp.plan_queries("q fallback", SearchIntent(depth="normal")))
        assert plan.source == "heuristic"

    def test_generator_delegates(self, monkeypatch):
        import agent.query_planner as qp
        from agent.query_generator import generate_queries

        async def dead_llm(messages, **kwargs):
            return None

        monkeypatch.setattr(qp, "llm_chat", dead_llm)
        queries = asyncio.run(generate_queries("test query", SearchIntent(depth="normal")))
        assert isinstance(queries, list)
        assert queries[0] == "test query"


# ─── Multi-query RRF ────────────────────────────────────────────────────────


class TestFuseQueries:
    def _item(self, url: str) -> RankedItem:
        return RankedItem(url=url, canonical_url=url)

    def test_rrf_math_and_dedupe(self):
        from ranking.fusion import fuse_queries

        a = self._item("https://a.example/1")
        b = self._item("https://b.example/1")
        c = self._item("https://c.example/1")
        fused = fuse_queries({"q1": [a, b], "q2": [b, c]})

        assert len(fused) == 3  # dedupe by canonical URL
        # b hits both queries: 1/62 + 1/61 > a (1/61) > c (1/62)
        assert fused[0].url == "https://b.example/1"
        assert fused[0].metadata["query_hits"] == 2
        assert set(fused[0].metadata["matched_queries"]) == {"q1", "q2"}
        assert fused[0].normalized_score == 1.0  # max normalizes to 1

    def test_single_query_order_preserved(self):
        from ranking.fusion import fuse_queries

        items = [self._item(f"https://x.example/{i}") for i in range(3)]
        fused = fuse_queries({"q": items})
        assert [i.url for i in fused] == [i.url for i in items]

    def test_empty(self):
        from ranking.fusion import fuse_queries

        assert fuse_queries({}) == []
        assert fuse_queries({"q": []}) == []


# ─── v2 quality formula ─────────────────────────────────────────────────────


class TestQualityV2:
    def test_weights_sum_to_one(self):
        from ranking.quality import QUALITY_WEIGHTS_V2

        assert abs(sum(QUALITY_WEIGHTS_V2.values()) - 1.0) < 1e-9

    def test_semantic_signal_dominates(self):
        from ranking.quality import final_quality_score

        base = RankedItem(
            url="https://example.com/a",
            title="docker compose guide",
            description="how to use docker compose",
            normalized_score=0.5,
        )
        plain = final_quality_score(base, "docker compose")

        boosted = RankedItem(
            url="https://example.com/a",
            title="docker compose guide",
            description="how to use docker compose",
            normalized_score=0.5,
            metadata={"semantic_score": 0.95},
        )
        with_sem = final_quality_score(boosted, "docker compose")
        assert with_sem > plain
        assert 0.0 <= with_sem <= 1.0

    def test_coverage_term(self):
        from ranking.quality import final_quality_score

        hit = RankedItem(
            url="https://example.com/x",
            title="rust async tokio runtime tutorial",
            description="",
            normalized_score=0.5,
        )
        miss = RankedItem(
            url="https://example.com/x",
            title="unrelated content entirely",
            description="",
            normalized_score=0.5,
        )
        assert final_quality_score(hit, "rust async tokio") > final_quality_score(
            miss, "rust async tokio"
        )


# ─── Cross-encoder reranker ────────────────────────────────────────────────


class TestCrossEncoderReranker:
    def test_disabled_never_raises(self, monkeypatch):
        from agent.reranker import CrossEncoderReranker
        from config import settings

        monkeypatch.setattr(settings, "reranker_enabled", False)
        r = CrossEncoderReranker()
        assert r.available() is False
        assert r.score("q", ["doc"]) is None

    def test_missing_dep_falls_back(self):
        # If sentence_transformers is absent the loader marks failure instead
        # of raising; if it IS installed we skip this assertion's premise.
        try:
            import pytest
            import sentence_transformers  # noqa: F401

            pytest.skip("sentence-transformers installed in this env")
        except ImportError:
            pass
        from agent.reranker import CrossEncoderReranker

        r = CrossEncoderReranker()
        assert r.available() is False
        assert r.score("q", ["doc"]) is None

    def test_score_sigmoid_normalization(self):
        from agent.reranker import CrossEncoderReranker

        r = CrossEncoderReranker()

        class _StubModel:
            def predict(self, pairs, **kwargs):
                return [10.0, -10.0, 0.5]  # logits outside [0,1]

        r._model = _StubModel()
        scores = r.score("q", ["a", "b", "c"])
        assert scores is not None
        assert all(0.0 <= s <= 1.0 for s in scores)
        assert scores[0] > scores[2] > scores[1]

    def test_heuristic_scorer_still_works(self):
        from agent.reranker import score_source

        out = score_source(
            _src("https://github.com/x", title="rust async tokio"),
            "rust async tokio",
        )
        assert out.authority_score == 0.85
        assert out.query_match_score > 0


# ─── Multi-query rerank ─────────────────────────────────────────────────────


class TestRerankMultiQuery:
    def test_fuse_dedupe_and_semantic(self):
        from agent.reranker import rerank_multi_query

        shared = _src("https://shared.example/a", score=0.5)
        only_q1 = _src("https://only1.example/a", score=0.9)
        only_q2 = _src("https://only2.example/a", score=0.4)
        results_by_query = {
            "q1": [only_q1, shared],
            "q2": [shared, only_q2],
        }
        out = rerank_multi_query(results_by_query, "test query", reranker=FakeReranker())
        urls = [r.url for r in out]
        assert urls.count("https://shared.example/a") == 1  # deduped
        assert len(out) == 3
        # Semantic path applied → query_match_score written from the model.
        assert any(r.query_match_score > 0 for r in out)
        # Descending final score.
        assert [r.score for r in out] == sorted([r.score for r in out], reverse=True)

    def test_fallback_when_reranker_unavailable(self):
        from agent.reranker import rerank_multi_query

        results_by_query = {"q": [_src("https://a.example/1", score=0.8)]}
        out = rerank_multi_query(results_by_query, "q", reranker=FakeReranker(available=False))
        assert len(out) == 1
        assert out[0].url == "https://a.example/1"

    def test_top_n(self):
        from agent.reranker import rerank_multi_query

        results_by_query = {"q": [_src(f"https://x.example/{i}", score=0.5) for i in range(5)]}
        out = rerank_multi_query(results_by_query, "q", top_n=3)
        assert len(out) == 3


# ─── Passage reranker ───────────────────────────────────────────────────────


class TestPassageReranker:
    def _page(self, url: str, markdown: str, error: str | None = None):
        return ScrapeResult(url=url, title=f"page {url}", markdown=markdown, error=error)

    def test_chunk_pages_skips_errors(self):
        from pipeline.passage_reranker import chunk_pages

        pages = [
            self._page("https://a.example", "word " * 200),
            self._page("https://b.example", "", error="boom"),
        ]
        chunks = chunk_pages(pages, chunk_size=200, chunk_overlap=40)
        assert chunks
        assert all(c["source_url"] == "https://a.example" for c in chunks)
        assert all({"source_url", "title", "chunk_index", "chunk_text"} <= set(c) for c in chunks)

    def test_relevant_passage_wins(self):
        from pipeline.passage_reranker import rerank_passages

        chunks = [
            {
                "source_url": "https://a.example",
                "title": "a",
                "chunk_index": 0,
                "chunk_text": "docker compose orchestrates multi-container apps",
            },
            {
                "source_url": "https://b.example",
                "title": "b",
                "chunk_index": 0,
                "chunk_text": "unrelated cooking recipes and kitchen tips",
            },
        ]
        out = rerank_passages("docker compose", chunks, top_n=1)
        assert len(out) == 1
        assert "docker compose" in out[0]["chunk_text"]

    def test_semantic_stage_blend(self):
        from pipeline.passage_reranker import rerank_passages

        chunks = [
            {
                "source_url": "https://a.example",
                "title": "a",
                "chunk_index": 0,
                "chunk_text": "alpha text " * 30,
            },
            {
                "source_url": "https://b.example",
                "title": "b",
                "chunk_index": 0,
                "chunk_text": "beta text " * 30,
            },
        ]
        # Semantic scores invert the heuristic ordering.
        out = rerank_passages("query", chunks, top_n=2, reranker=FakeReranker(scores=[0.05, 0.95]))
        assert out[0]["source_url"] == "https://b.example"
        assert "semantic_score" in out[0]

    def test_chunk_and_rerank_end_to_end(self):
        from pipeline.passage_reranker import chunk_and_rerank

        pages = [self._page("https://a.example", "target phrase here. " * 100)]
        out = chunk_and_rerank("target phrase", pages, top_n=4)
        assert 1 <= len(out) <= 4
        assert all(c["score"] > 0 for c in out)


# ─── Answer verification ────────────────────────────────────────────────────


class TestVerifyAnswer:
    _EVIDENCE: ClassVar[list[EvidenceItem]] = [
        EvidenceItem(
            source_id="e1",
            url="https://a.example",
            title="t",
            quote="The earth orbits the sun once every 365 days on an elliptical path.",
            support=0.9,
        )
    ]

    def test_removes_unsupported_claim(self):
        from agent.verifier import verify_answer

        answer = "The earth orbits the sun every year. Pigs can fly to the moon unaided."
        filtered, _claims, stats = asyncio.run(verify_answer(answer, self._EVIDENCE))
        assert "Pigs" not in filtered
        assert "earth orbits the sun" in filtered
        assert stats["claims_removed"] == 1
        assert stats["claims_verified"] >= 1

    def test_preserves_sources_tail(self):
        from agent.verifier import verify_answer

        answer = (
            "The earth orbits the sun every year. "
            "Pigs can fly to the moon unaided.\n\n"
            "## Sources\n- [1] Example (https://a.example)"
        )
        filtered, _, _stats = asyncio.run(verify_answer(answer, self._EVIDENCE))
        assert "## Sources" in filtered
        assert "https://a.example" in filtered
        assert "Pigs" not in filtered

    def test_all_removed_abstains(self):
        from agent.verifier import verify_answer

        answer = (
            "Completely unsupported claim number one here. Another unsupported claim follows now."
        )
        filtered, _, stats = asyncio.run(verify_answer(answer, self._EVIDENCE))
        # Every claim failed verification → honest abstention, never the
        # original unverifiable text.
        assert "Insufficient reliable evidence" in filtered
        assert "Completely unsupported" not in filtered
        assert stats["all_removed"] is True

    def test_all_removed_abstains_preserves_sources_tail(self):
        from agent.verifier import verify_answer

        answer = (
            "Completely unsupported claim number one here. "
            "Another unsupported claim follows now.\n\n"
            "## Sources\n- [1] Example (https://a.example)"
        )
        filtered, _, stats = asyncio.run(verify_answer(answer, self._EVIDENCE))
        assert "Insufficient reliable evidence" in filtered
        assert "## Sources" in filtered
        assert "https://a.example" in filtered
        assert stats["all_removed"] is True

    def test_no_claims_passthrough(self):
        from agent.verifier import verify_answer

        filtered, claims, stats = asyncio.run(verify_answer("short", self._EVIDENCE))
        assert filtered == "short"
        assert claims == []
        assert stats["claims_total"] == 0

    def test_llm_path_sets_verdict_status(self, monkeypatch):
        """LLM verdicts map to SPEC-v3 statuses: >=2 sources = supported."""
        import agent.verifier as verifier

        async def fake_llm(messages, **kw):
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "claim_index": 0,
                            "supporting_evidence": [0, 1],
                            "support_score": 0.9,
                        },
                        {
                            "claim_index": 1,
                            "supporting_evidence": [],
                            "support_score": 0.0,
                        },
                    ]
                }
            )

        monkeypatch.setattr(verifier, "llm_chat", fake_llm)
        evidence = self._EVIDENCE + [
            EvidenceItem(
                source_id="e2",
                url="https://b.example",
                title="t2",
                quote="Earth completes one solar orbit every year around the sun.",
                support=0.8,
            )
        ]
        _, claims, stats = asyncio.run(
            verifier.verify_answer(
                "The earth orbits the sun every year. Pigs can fly unaided to the moon.",
                evidence,
            )
        )
        assert claims[0].status == "supported" and claims[0].verified
        assert claims[1].status == "insufficient_evidence" and not claims[1].verified
        assert stats["by_status"]["supported"] == 1
        assert stats["by_status"]["insufficient_evidence"] == 1


# ─── Orchestrator follow-up loop ────────────────────────────────────────────


def _patch_pipeline(monkeypatch, *, gap_results=None, retrieve_per_query=None):
    """Stub SearXNG/Firecrawl/gap-analysis so the orchestrator runs offline."""
    import agent.orchestrator as orch

    async def fake_retrieve(query, max_results=10, lang="vi"):
        results = (retrieve_per_query or {}).get(query)
        if results is None:
            slug = abs(hash(query)) % 1000
            results = [
                _src(f"https://r{slug}-{i}.example/page", score=0.9 - i * 0.1) for i in range(3)
            ]
        return results

    async def fake_read(urls, timeout=None, max_concurrent=5):
        return [
            ReadResult(
                url=u,
                success=True,
                text="relevant content about the query topic. " * 60,
                title=f"page {u}",
                tier="http",
            )
            for u in urls
        ]

    calls = {"gaps": 0}

    async def fake_gaps(evidence, query):
        i = calls["gaps"]
        calls["gaps"] += 1
        if gap_results and i < len(gap_results):
            return gap_results[i]
        return GapResult(known=["x"], missing=[], confidence=0.9, need_more_search=False)

    monkeypatch.setattr(orch, "retrieve", fake_retrieve)
    monkeypatch.setattr(orch, "read_batch", fake_read)
    monkeypatch.setattr(orch, "analyze_gaps", fake_gaps)
    monkeypatch.setattr(orch, "get_default_reranker", lambda: FakeReranker())
    return calls


class TestOrchestrator:
    def test_fast_mode_no_followup(self, monkeypatch):
        from agent.orchestrator import run_research

        # Even when gaps are reported, fast mode must not follow up.
        _patch_pipeline(
            monkeypatch,
            gap_results=[GapResult(missing=["x"], confidence=0.1, need_more_search=True)] * 5,
        )
        ctx = ResearchContext(query="test query", mode="fast")
        result = asyncio.run(run_research(ctx))
        assert result["search"]["followup_rounds"] == 0
        assert result["search_rounds"] == 1
        assert len(result["search"]["generated_queries"]) <= 3

    def test_balanced_mode_one_followup(self, monkeypatch):
        from agent.orchestrator import run_research

        _patch_pipeline(
            monkeypatch,
            gap_results=[
                GapResult(missing=["more info"], confidence=0.1, need_more_search=True),
                GapResult(missing=[], confidence=0.9, need_more_search=False),
            ],
        )
        ctx = ResearchContext(query="test query", mode="balanced")
        result = asyncio.run(run_research(ctx))
        assert result["search"]["followup_rounds"] == 1
        assert result["search_rounds"] == 2

    def test_deep_mode_caps_at_three_followups(self, monkeypatch):
        from agent.orchestrator import run_research

        _patch_pipeline(
            monkeypatch,
            gap_results=[GapResult(missing=["m"], confidence=0.1, need_more_search=True)] * 10,
        )
        ctx = ResearchContext(query="test query", mode="deep")
        result = asyncio.run(run_research(ctx))
        assert result["search"]["followup_rounds"] == 3
        assert result["search_rounds"] == 4

    def test_response_contract(self, monkeypatch):
        from agent.orchestrator import run_research

        _patch_pipeline(monkeypatch)
        ctx = ResearchContext(query="test query", mode="fast")
        result = asyncio.run(run_research(ctx))
        for key in (
            "answer",
            "confidence",
            "sources",
            "citations",
            "search",
            "verification",
            "timings",
        ):
            assert key in result, f"missing key: {key}"
        for key in (
            "generated_queries",
            "raw_results",
            "unique_results",
            "pages_read",
            "followup_rounds",
        ):
            assert key in result["search"], f"missing search.{key}"
        assert "total" in result["timings"]

    def test_retrieve_failure_degrades(self, monkeypatch):
        import agent.orchestrator as orch
        from agent.orchestrator import run_research

        async def dead_retrieve(query, max_results=10, lang="vi"):
            raise RuntimeError("searxng down")

        async def fake_read(urls, timeout=None, max_concurrent=5):
            return []

        monkeypatch.setattr(orch, "retrieve", dead_retrieve)
        monkeypatch.setattr(orch, "read_batch", fake_read)
        monkeypatch.setattr(orch, "get_default_reranker", lambda: FakeReranker())
        ctx = ResearchContext(query="test query", mode="fast")
        result = asyncio.run(run_research(ctx))
        assert result["answer"] is not None
        assert result["search"]["raw_results"] == 0


# ─── Enhanced /v1/search endpoint ──────────────────────────────────────────


class TestV1SearchEnhanced:
    def test_enhanced_response_shape(self, monkeypatch):
        import agent.orchestrator as orch
        import main as app_module
        from fastapi.testclient import TestClient

        async def fake_run(context, **kwargs):
            return {
                "answer": "synthesized answer",
                "confidence": 0.8,
                "search_rounds": 1,
                "queries": ["test query"],
                "sources": [
                    {
                        "title": "t",
                        "url": "https://a.example",
                        "score": 0.9,
                        "domain": "a.example",
                    }
                ],
                "citations": [{"claim": "c", "verified": True, "evidence_count": 1}],
                "search": {
                    "generated_queries": ["test query"],
                    "plan_source": "heuristic",
                    "raw_results": 3,
                    "unique_results": 3,
                    "pages_read": 2,
                    "followup_rounds": 0,
                    "passages": 4,
                },
                "verification": {
                    "claims_total": 1,
                    "claims_verified": 1,
                    "claims_removed": 0,
                    "all_removed": False,
                },
                "timings": {"total": 0.5},
            }

        monkeypatch.setattr(orch, "run_research", fake_run)
        c = TestClient(app_module.app)
        r = c.post(
            "/v1/search",
            json={"query": "test query", "mode": "fast", "citations": True},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["answer"] == "synthesized answer"
        assert data["sources"]
        assert data["search"]["generated_queries"] == ["test query"]
        assert data["search"]["followup_rounds"] == 0
        assert "timings" in data
        # Compatibility keys preserved.
        assert "results" in data
        assert "understanding" in data
        assert "citations" in data

    def test_bad_mode_422(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post("/v1/search", json={"query": "x", "mode": "bogus"})
        assert r.status_code == 422

    def test_no_mode_stays_raw(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post("/v1/search", json={"query": "x", "max_results": 2})
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert "understanding" in data
        assert "search" not in data  # enhanced stats absent in raw mode
