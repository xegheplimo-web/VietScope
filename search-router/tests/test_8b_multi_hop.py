import asyncio
from unittest.mock import AsyncMock, patch

import config
import pytest
from core.orchestrator import SearchOrchestrator
from core.provider_registry import ProviderRegistry
from core.query_understanding import QueryUnderstanding
from models import EvidencePack, SearchBudget, SearchResultItem, Source
from pipeline.rag import synthesize_research_answer
from pipeline.reader import ReadResult


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")


def test_decompose_simple():
    qu = QueryUnderstanding()
    assert qu.decompose("Python asyncio tutorial") == ["Python asyncio tutorial"]


def test_decompose_vietnamese_rtx():
    qu = QueryUnderstanding()
    result = qu.decompose("RTX 5090 giá và ngày ra mắt 2025")
    assert result == ["RTX 5090 giá", "RTX 5090 ngày ra mắt 2025"]


def test_decompose_respects_max_hops():
    qu = QueryUnderstanding()
    assert qu.decompose("A và B", max_hops=1) == ["A và B"]
    out = qu.decompose("A và B và C", max_hops=2)
    assert len(out) <= 2


def test_synthesize_research_fallback_no_key(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    sources = [Source(url="http://x", title="Title X", content="snippet text")]
    out = asyncio.run(synthesize_research_answer("query", sources))
    assert "LLM synthesis unavailable" in out
    assert "[1]" in out
    assert "http://x" in out


def test_synthesize_research_mock_llm(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "test-key")
    import pipeline.rag as rag

    class _FakeGateway:
        async def complete(self, *a, **k):
            return (
                "Summary: good\n\n"
                "Key points:\n- Price is high [1]\n\n"
                "Sources:\n[1] Example (http://ex)"
            )

    gw = _FakeGateway()
    monkeypatch.setattr(rag, "get_inference_gateway", lambda: gw)
    sources = [Source(url="http://ex", title="Example", content="Price is high")]
    out = asyncio.run(synthesize_research_answer("q", sources))
    assert "[1]" in out


def test_orchestrator_research_multi_hop():
    class DummyProvider:
        async def search(self, q):
            if "giá" in q.query.lower():
                return [
                    SearchResultItem(
                        url="http://price",
                        title="Price",
                        description="price desc",
                        score=1.0,
                    )
                ]
            if "ra mắt" in q.query.lower():
                return [
                    SearchResultItem(
                        url="http://date",
                        title="Date",
                        description="date desc",
                        score=1.0,
                    )
                ]
            return []

        async def health(self):
            return True

    reg = ProviderRegistry()
    reg.register("dummy", DummyProvider())

    async def _run():
        with patch(
            "pipeline.reader.read_batch",
            new=AsyncMock(
                return_value=[
                    ReadResult(url="http://price", success=True, text="# price", tier="http"),
                    ReadResult(url="http://date", success=True, text="# date", tier="http"),
                ]
            ),
        ):
            orch = SearchOrchestrator(reg)
            pack = await orch.research("RTX 5090 giá và ngày ra mắt 2025", "normal")
            urls = {s.url for s in pack.sources}
            assert "http://price" in urls
            assert "http://date" in urls

    asyncio.run(_run())


def test_orchestrator_research_fallback_single():
    calls = []

    class DummyProvider:
        async def search(self, q):
            calls.append(q.query)
            return [
                SearchResultItem(
                    url="http://x",
                    title="X",
                    description="x",
                    score=1.0,
                )
            ]

        async def health(self):
            return True

    reg = ProviderRegistry()
    reg.register("dummy", DummyProvider())

    async def _run():
        with patch(
            "pipeline.reader.read_batch",
            new=AsyncMock(
                return_value=[ReadResult(url="http://x", success=True, text="# x", tier="http")]
            ),
        ):
            orch = SearchOrchestrator(reg)
            pack = await orch.research("giá vàng hôm nay", "fast")
            assert pack.sources
            assert calls == ["giá vàng hôm nay"]

    asyncio.run(_run())


def test_v1_research_endpoint(monkeypatch):
    import api.v1 as v1
    from fastapi.testclient import TestClient
    from main import app

    pack = EvidencePack(
        answer="",
        sources=[],
        coverage=0.0,
        confidence=0.0,
        budget_used=SearchBudget(max_queries=5),
    )

    mock_orch = AsyncMock()
    mock_orch.research = AsyncMock(return_value=pack)

    monkeypatch.setattr(v1, "_get_orchestrator", lambda: mock_orch)
    monkeypatch.setattr(v1, "_synthesize_answer", AsyncMock(return_value="answer"))
    monkeypatch.setattr(v1, "extract_claims_with_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr(v1, "build_evidence_pack", lambda *a, **k: pack)

    client = TestClient(app)
    resp = client.post("/v1/research", json={"query": "test", "mode": "fast"})
    assert resp.status_code == 200
    data = resp.json()
    for field in (
        "answer",
        "sources",
        "claims",
        "citations",
        "coverage",
        "confidence",
        "budget_used",
    ):
        assert field in data
