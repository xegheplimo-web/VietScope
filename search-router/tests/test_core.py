import asyncio
from unittest.mock import AsyncMock, patch

import config
import pytest
from core.budget import BudgetExceeded, SearchBudget
from core.orchestrator import SearchOrchestrator
from core.provider_registry import (
    ProviderRegistry,
    ProviderSearchQuery,
    SearXNGProvider,
    create_default_registry,
)
from core.query_understanding import QueryUnderstanding
from models import EvidencePack, SearchResultItem
from pipeline.reader import ReadResult


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")


class TestProviderRegistry:
    def test_register_get_all(self):
        reg = ProviderRegistry()

        class DummyProvider:
            async def search(self, q):
                return []

            async def health(self):
                return True

        reg.register("dummy", DummyProvider())
        assert reg.get("dummy") is not None
        assert len(reg.all()) == 1
        assert reg.get("missing") is None

    def test_create_default_registry_has_searxng(self):
        reg = create_default_registry()
        names = [n for n, _ in reg.all()]
        assert "searxng" in names

    def test_searxng_provider_search(self):
        dummy = [
            SearchResultItem(
                url="http://example.com",
                title="Example",
                description="An example",
                score=1.0,
            )
        ]

        async def _run():
            with patch("providers.searxng.searxng_search", new=AsyncMock(return_value=dummy)):
                provider = SearXNGProvider()
                res = await provider.search(ProviderSearchQuery(query="test"))
                assert len(res) == len(dummy)
                r = res[0]
                assert (r.url, r.title, r.snippet, r.score) == (
                    "http://example.com",
                    "Example",
                    "An example",
                    1.0,
                )
                assert r.source == "searxng"

        asyncio.run(_run())

    def test_registry_health(self):
        reg = ProviderRegistry()

        class GoodProvider:
            async def search(self, q):
                return []

            async def health(self):
                return True

        class BadProvider:
            async def search(self, q):
                return []

            async def health(self):
                raise RuntimeError("down")

        reg.register("good", GoodProvider())
        reg.register("bad", BadProvider())

        async def _run():
            status = await reg.health()
            assert status["good"] is True
            assert status["bad"] is False

        asyncio.run(_run())


class TestBudget:
    def test_fast_limits(self):
        b = SearchBudget.for_mode("fast")
        assert b.max_queries == 2
        assert b.max_fetches == 3
        assert b.max_followups == 0

    def test_normal_limits(self):
        b = SearchBudget.for_mode("normal")
        assert b.max_queries == 5
        assert b.max_fetches == 8
        assert b.max_followups == 1

    def test_deep_limits(self):
        b = SearchBudget.for_mode("deep")
        assert b.max_queries == 12
        assert b.max_fetches == 20
        assert b.max_followups == 3

    def test_exceed_raises(self):
        b = SearchBudget.for_mode("fast")
        b.use_query()
        b.use_query()
        with pytest.raises(BudgetExceeded):
            b.use_query()

    def test_budget_used_tracking(self):
        b = SearchBudget.for_mode("normal")
        b.use_query(2)
        b.use_fetch(3)
        b.use_followup(1)
        b.use_tokens(100)
        assert b.budget_used["queries"] == 2
        assert b.budget_used["fetches"] == 3
        assert b.budget_used["followups"] == 1
        assert b.budget_used["tokens"] == 100
        assert b.elapsed() >= 0


class TestQueryUnderstanding:
    def test_gpt_moi_nhat(self):
        qu = QueryUnderstanding()
        p = qu.analyze("GPT mới nhất là gì?")
        assert p.intent == "current_fact"
        assert p.freshness_required is True
        assert p.max_age == "24h"
        assert p.language == "vi"

    def test_react_moi(self):
        qu = QueryUnderstanding()
        p = qu.analyze("React có gì mới?")
        assert p.intent == "current_fact"

    def test_gia_vang(self):
        qu = QueryUnderstanding()
        p = qu.analyze("giá vàng hôm nay")
        assert p.intent == "current_fact"
        assert p.freshness_required is True
        assert p.max_age == "24h"


class TestOrchestrator:
    def test_fast_search_returns_evidence_pack(self):
        class DummyProvider:
            async def search(self, q):
                return [
                    SearchResultItem(
                        url="http://example.com/page",
                        title="Example page",
                        description="description",
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
                    return_value=[
                        ReadResult(
                            url="http://example.com/page",
                            success=True,
                            text="# content",
                            tier="http",
                        )
                    ]
                ),
            ):
                orch = SearchOrchestrator(reg)
                pack = await orch.search("test query", "fast")
                assert isinstance(pack, EvidencePack)
                assert len(pack.sources) > 0
                assert pack.coverage is not None
                assert pack.confidence is not None
                assert pack.coverage >= 0
                assert pack.confidence >= 0

        asyncio.run(_run())
