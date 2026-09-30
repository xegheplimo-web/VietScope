"""Tests for P11-T2: hybrid RRF retrieval in the live /v1/search path.

Covers RRF fusion order/dedup across the OpenSearch + Qdrant lanes,
degraded-mode signaling when an index lane dies, the
``HYBRID_RETRIEVAL_ENABLED`` flag, and the mode-research response contract
(``timings.hybrid``) on ``POST /v1/search``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipeline.federated_retrieval import FederatedResult, FederatedRetriever
from pipeline.reader import ReadResult
from qdrant.client import QdrantSearchResult
from research_models.research_state import GapResult, ResearchContext, SourceResult


def _os_hit(doc_id: str, url: str, score: float = 5.0) -> dict:
    return {
        "doc_id": doc_id,
        "score": score,
        "source": {
            "url": url,
            "title": f"title {doc_id}",
            "text": f"passage text {doc_id}",
            "domain": url.split("/")[2],
        },
    }


def _qd_hit(point_id: str, url: str, score: float = 0.9) -> QdrantSearchResult:
    return QdrantSearchResult(
        point_id=point_id,
        score=score,
        payload={"url": url, "title": f"title {point_id}", "domain": url.split("/")[2]},
    )


def _retriever(os_client, qdrant_client) -> FederatedRetriever:
    return FederatedRetriever(
        opensearch_client=os_client,
        qdrant_client=qdrant_client,
        rrf_k=60,
        top_k=60,
    )


# ─── RRF fusion ──────────────────────────────────────────────────────────────


class TestRRFFusion:
    def test_fused_order_and_dedup(self):
        r = _retriever(MagicMock(), MagicMock())
        fused = r._rrf_fuse(
            [
                _os_hit("doc_a#p_000", "https://a.example/x", score=9.0),
                _os_hit("doc_b#p_000", "https://b.example/x", score=7.0),
            ],
            [
                _qd_hit("doc_b#p_000", "https://b.example/x", score=0.9),
                _qd_hit("doc_c#p_000", "https://c.example/x", score=0.8),
            ],
        )
        ids = [d["doc_id"] for d in fused]
        # doc_b ranks in both lanes → fused first; doc_ids deduplicated.
        assert ids == ["doc_b#p_000", "doc_a#p_000", "doc_c#p_000"]
        assert len(set(ids)) == len(ids)
        # Fused score is the RRF score (1/61 + 1/62), not the raw BM25 score.
        assert fused[0]["score"] == pytest.approx(1.0 / 61 + 1.0 / 62)

    def test_retrieve_merges_lanes(self):
        os_client = MagicMock()
        os_client.search = AsyncMock(return_value=[_os_hit("doc_a#p_000", "https://a.example/x")])
        qdrant_client = MagicMock()
        qdrant_client.search_dense = AsyncMock(
            return_value=[_qd_hit("doc_q#p_000", "https://q.example/x")]
        )

        res = asyncio.run(
            _retriever(os_client, qdrant_client).retrieve("q", query_vector=[0.1] * 4)
        )

        assert res.degraded is False
        assert res.opensearch_count == 1
        assert res.qdrant_count == 1
        assert res.fused_count == 2
        assert sorted(res.doc_ids) == ["doc_a#p_000", "doc_q#p_000"]
        urls = {s.url for s in res.to_source_results()}
        assert urls == {"https://a.example/x", "https://q.example/x"}


# ─── Degraded lanes ──────────────────────────────────────────────────────────


class TestDegraded:
    def test_opensearch_raise_still_returns_qdrant(self):
        os_client = MagicMock()
        os_client.search = AsyncMock(side_effect=ConnectionError("opensearch down"))
        qdrant_client = MagicMock()
        qdrant_client.search_dense = AsyncMock(
            return_value=[_qd_hit("doc_q#p_000", "https://q.example/x")]
        )

        res = asyncio.run(
            _retriever(os_client, qdrant_client).retrieve("q", query_vector=[0.1] * 4)
        )

        assert res.degraded is True
        assert res.degraded_reason == "opensearch_failed"
        assert res.opensearch_count == 0
        assert res.qdrant_count == 1
        assert res.doc_ids == ["doc_q#p_000"]

    def test_swallowed_failure_probes_health(self):
        """Clients return [] on connection errors — a failed probe must still
        surface as degraded rather than a legitimate zero-hit response."""
        os_client = MagicMock()
        os_client.search = AsyncMock(return_value=[])
        os_client.health = MagicMock(return_value=False)
        qdrant_client = MagicMock()
        qdrant_client.search_dense = AsyncMock(
            return_value=[_qd_hit("doc_q#p_000", "https://q.example/x")]
        )

        res = asyncio.run(
            _retriever(os_client, qdrant_client).retrieve("q", query_vector=[0.1] * 4)
        )

        assert res.degraded is True
        assert res.degraded_reason == "opensearch_failed"
        assert res.qdrant_count == 1

    def test_empty_hits_healthy_lanes_not_degraded(self):
        os_client = MagicMock()
        os_client.search = AsyncMock(return_value=[])
        os_client.health = MagicMock(return_value=True)
        qdrant_client = MagicMock()
        qdrant_client.search_dense = AsyncMock(return_value=[])
        qdrant_client.collection_exists = AsyncMock(return_value=True)

        res = asyncio.run(
            _retriever(os_client, qdrant_client).retrieve("q", query_vector=[0.1] * 4)
        )

        assert res.degraded is False
        assert res.fused_count == 0
        assert res.doc_ids == []


# ─── Orchestrator hybrid lane ────────────────────────────────────────────────


class _FakeReranker:
    """Duck-typed stand-in for CrossEncoderReranker in tests."""

    def available(self) -> bool:
        return True

    def score(self, query, docs):
        return [0.9] * len(docs)


def _stub_live_pipeline(monkeypatch):
    """Stub the live-web lanes so run_research executes offline."""
    import agent.orchestrator as orch

    async def fake_retrieve(query, max_results=10, lang="vi"):
        return [
            SourceResult(
                source_id=f"s{i}",
                url=f"https://r{i}.example/page",
                title=f"result {i}",
                description="d",
                domain=f"r{i}.example",
                score=0.9 - i * 0.1,
            )
            for i in range(3)
        ]

    async def fake_read(urls, timeout=None, max_concurrent=5):
        return [
            ReadResult(
                url=u,
                success=True,
                text="relevant content about the query topic. " * 40,
                title=f"page {u}",
                tier="http",
            )
            for u in urls
        ]

    async def fake_gaps(evidence, query):
        return GapResult(known=["x"], missing=[], confidence=0.9, need_more_search=False)

    monkeypatch.setattr(orch, "retrieve", fake_retrieve)
    monkeypatch.setattr(orch, "read_batch", fake_read)
    monkeypatch.setattr(orch, "analyze_gaps", fake_gaps)
    monkeypatch.setattr(orch, "get_default_reranker", lambda: _FakeReranker())


def _hybrid_result() -> FederatedResult:
    return FederatedResult(
        doc_ids=["doc_h#p_000"],
        scores=[1.0 / 61],
        results=[
            {
                "doc_id": "doc_h#p_000",
                "score": 1.0 / 61,
                "source": {
                    "url": "https://hybrid.example/doc",
                    "title": "hybrid hit",
                    "text": "hybrid passage",
                    "domain": "hybrid.example",
                },
            }
        ],
        opensearch_count=1,
        qdrant_count=0,
        fused_count=1,
    )


class TestOrchestratorHybridLane:
    def test_lane_merges_before_rerank(self, monkeypatch):
        import agent.orchestrator as orch

        _stub_live_pipeline(monkeypatch)
        captured = {}
        real_rerank = orch.rerank_multi_query

        def spy_rerank(results_by_query, query, **kwargs):
            captured["lanes"] = {k: list(v) for k, v in results_by_query.items()}
            return real_rerank(results_by_query, query, **kwargs)

        monkeypatch.setattr(orch, "rerank_multi_query", spy_rerank)

        async def lane_factory():
            return _hybrid_result()

        ctx = ResearchContext(query="test query", mode="fast")
        result = asyncio.run(orch.run_research(ctx, hybrid_lane=lane_factory))

        hybrid = result["timings"]["hybrid"]
        assert hybrid == {
            "os_hits": 1,
            "qdrant_hits": 0,
            "fused": 1,
            "degraded": False,
            "merged": 1,
        }
        lane_urls = [s.url for s in captured["lanes"].get("__hybrid__", [])]
        assert lane_urls == ["https://hybrid.example/doc"]

    def test_lane_failure_marks_degraded(self, monkeypatch):
        import agent.orchestrator as orch

        _stub_live_pipeline(monkeypatch)

        async def boom():
            raise RuntimeError("both indexes down")

        ctx = ResearchContext(query="test query", mode="fast")
        result = asyncio.run(orch.run_research(ctx, hybrid_lane=boom))

        assert result["timings"]["hybrid"]["degraded"] is True
        assert result["timings"]["hybrid"]["reason"] == "lane_error"
        # Live-web pipeline still completed.
        assert result["answer"] is not None
        assert result["search"]["raw_results"] > 0


# ─── Flag gating at the API layer ────────────────────────────────────────────


def _fake_research_result() -> dict:
    return {
        "answer": "synthesized answer",
        "confidence": 0.8,
        "search_rounds": 1,
        "queries": ["x"],
        "sources": [],
        "citations": [],
        "search": {
            "generated_queries": ["x"],
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


class TestHybridFlag:
    @staticmethod
    def _req():
        import api.v1 as v1

        return v1.SearchRequest(query="x", mode="fast")

    @staticmethod
    def _profile():
        from core.query_understanding import QueryProfile

        return QueryProfile(intent="definition")

    def test_flag_off_never_calls_retriever(self, monkeypatch):
        import agent.orchestrator as orch
        import api.v1 as v1

        monkeypatch.setattr(v1.settings, "hybrid_retrieval_enabled", False)
        spy = AsyncMock(return_value=FederatedResult())
        monkeypatch.setattr(v1, "_hybrid_retrieve", spy)
        captured = {}

        async def fake_run(context, **kwargs):
            captured.update(kwargs)
            return _fake_research_result()

        monkeypatch.setattr(orch, "run_research", fake_run)

        resp = asyncio.run(v1._research_search(self._req(), self._profile()))
        spy.assert_not_called()
        assert captured.get("hybrid_lane") is None
        assert "hybrid" not in resp["timings"]

    def test_flag_on_supplies_lane(self, monkeypatch):
        import agent.orchestrator as orch
        import api.v1 as v1

        monkeypatch.setattr(v1.settings, "hybrid_retrieval_enabled", True)
        calls = []

        async def fake_hybrid(query):
            calls.append(query)
            return _hybrid_result()

        monkeypatch.setattr(v1, "_hybrid_retrieve", fake_hybrid)

        async def fake_run(context, **kwargs):
            lane = kwargs.get("hybrid_lane")
            assert lane is not None, "hybrid_lane not forwarded to run_research"
            res = await lane()
            assert res.fused_count == 1
            return _fake_research_result()

        monkeypatch.setattr(orch, "run_research", fake_run)

        asyncio.run(v1._research_search(self._req(), self._profile()))
        assert calls == ["x"]


# ─── E2E smoke: POST /v1/search?mode=fast ────────────────────────────────────


class TestE2ESearchHybrid:
    def test_search_fast_timings_hybrid(self, monkeypatch):
        import api.v1 as v1
        import main as app_module
        from fastapi.testclient import TestClient

        _stub_live_pipeline(monkeypatch)

        async def fake_hybrid(query):
            return _hybrid_result()

        monkeypatch.setattr(v1, "_hybrid_retrieve", fake_hybrid)
        monkeypatch.setattr(v1.settings, "hybrid_retrieval_enabled", True)

        client = TestClient(app_module.app)
        r = client.post("/v1/search", json={"query": "x", "mode": "fast"})
        assert r.status_code == 200
        data = r.json()

        # Pre-existing response contract untouched.
        for key in (
            "query",
            "type",
            "mode",
            "answer",
            "confidence",
            "sources",
            "search",
            "verification",
            "timings",
            "understanding",
            "results",
            "count",
        ):
            assert key in data, f"missing key: {key}"

        hybrid = data["timings"].get("hybrid")
        assert hybrid is not None, "timings.hybrid missing"
        assert hybrid["os_hits"] == 1
        assert hybrid["qdrant_hits"] == 0
        assert hybrid["fused"] == 1
        assert hybrid["degraded"] is False
        assert hybrid["merged"] == 1
