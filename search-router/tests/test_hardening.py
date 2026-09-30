"""Regression tests for issues found during the independent v3 review."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import core.provider_registry as provider_registry
import models
from core.budget import BudgetExceeded, SearchBudget
from core.orchestrator import SearchOrchestrator
from core.provider_registry import ProviderRegistry, ProviderSearchQuery
from core.query_understanding import QueryUnderstanding
from evidence.citation import build_passages
from evidence.claims import Claim, keywords
from evidence.pack import build_clusters
from evidence.verifier import verify_claims
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import EvidenceCluster, EvidencePack, SearchResultItem, Source
from pipeline.reader import ReadResult
from storage.lock import MemoryLock
from storage.rate_limiter import RedisRateLimiter


def test_provider_query_name_does_not_shadow_v2_contract():
    assert not hasattr(provider_registry, "SearchQuery")
    assert ProviderSearchQuery is not models.SearchQuery


def test_budget_rejection_does_not_corrupt_usage():
    budget = SearchBudget.for_mode("fast")
    budget.use_query(2)

    try:
        budget.use_query()
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("expected BudgetExceeded")

    assert budget.budget_used["queries"] == 2
    assert budget.remaining_queries == 0


def test_orchestrator_fetches_only_remaining_allowance():
    budget = SearchBudget.for_mode("fast")
    budget.use_fetch(2)
    sources = [
        Source(source_id="s1", url="https://one.example"),
        Source(source_id="s2", url="https://two.example"),
    ]
    registry = ProviderRegistry()
    orchestrator = SearchOrchestrator(registry)

    async def run():
        with patch(
            "pipeline.reader.read_batch",
            new=AsyncMock(
                return_value=[
                    ReadResult(url="https://one.example", success=True, text="content", tier="http")
                ]
            ),
        ) as scrape:
            fetched = await orchestrator._fetch_top(sources, budget)
        return fetched, scrape

    fetched, scrape = asyncio.run(run())
    assert [source.source_id for source in fetched] == ["s1"]
    assert budget.budget_used["fetches"] == 3
    assert scrape.await_args.args[0] == ["https://one.example"]


def test_canonical_url_keeps_semantic_query_and_drops_tracking():
    canonical = SearchOrchestrator._canonical(
        "https://www.example.com/watch?v=abc&utm_source=test&b=2&a=1"
    )
    assert canonical == "example.com/watch?a=1&b=2&v=abc"


def test_authority_rules_do_not_trust_substring_spoofs():
    assert SearchOrchestrator._authority("reuters.com") == 0.9
    assert SearchOrchestrator._authority("reuters.com.evil.example") == 0.3
    assert SearchOrchestrator._authority("official-scam.example") == 0.3


def test_comparison_intent_takes_precedence_over_freshness():
    profile = QueryUnderstanding().analyze("So sánh GPT mới nhất và Claude")
    assert profile.intent == "comparison"
    assert profile.freshness_required is True


def test_orchestrator_returns_v2_pack_with_actual_budget_usage():
    class DummyProvider:
        async def search(self, query):
            return [
                SearchResultItem(
                    url="https://example.com/article",
                    title="Example",
                    description="test query",
                )
            ]

        async def health(self):
            return True

    registry = ProviderRegistry()
    registry.register("dummy", DummyProvider())

    async def run():
        with patch(
            "pipeline.reader.read_batch",
            new=AsyncMock(
                return_value=[
                    ReadResult(
                        url="https://example.com/article",
                        success=True,
                        text="evidence",
                        tier="http",
                    )
                ]
            ),
        ):
            return await SearchOrchestrator(registry).search("test query", "fast")

    pack = asyncio.run(run())
    assert isinstance(pack, EvidencePack)
    assert pack.answer == ""
    assert pack.budget_used.queries_used == 1
    assert pack.budget_used.fetches_used == 1


def test_memory_lock_requires_owner_token():
    async def run():
        lock = MemoryLock()
        token = await lock.acquire("job")
        wrong = await lock.release("job", "wrong")
        still_locked = await lock.acquire("job")
        right = await lock.release("job", token)
        return token, wrong, still_locked, right

    token, wrong, still_locked, right = asyncio.run(run())
    assert token
    assert wrong is False
    assert still_locked is None
    assert right is True


def test_redis_rate_limiter_uses_atomic_script():
    class AtomicFakeRedis:
        def __init__(self):
            self.count = 0
            self.eval_calls = 0
            self.lock = asyncio.Lock()

        async def eval(self, script, numkeys, *args):
            self.eval_calls += 1
            async with self.lock:
                limit = int(args[2])
                if self.count >= limit:
                    return 0
                await asyncio.sleep(0)
                self.count += 1
                return 1

    async def run():
        client = AtomicFakeRedis()
        limiter = RedisRateLimiter(client)
        results = await asyncio.gather(*(limiter.allow("provider", 1, 60) for _ in range(20)))
        return client, results

    client, results = asyncio.run(run())
    assert sum(results) == 1
    assert client.eval_calls == 20


def test_pipeline_cache_keeps_write_through_fallback(monkeypatch):
    from pipeline import cache as cache_module

    class FailingRedis:
        fail_reads = False

        def __init__(self):
            self.values = {}

        async def set(self, key, value, ex=None):
            self.values[key] = value

        async def get(self, key):
            if self.fail_reads:
                raise ConnectionError("redis went down")
            return self.values.get(key)

    client = FailingRedis()

    async def get_client():
        return client

    monkeypatch.setattr(cache_module, "get_redis", get_client)
    cache = cache_module.Cache("hardening")

    async def run():
        await cache.set("key", {"value": 1}, 60)
        client.fail_reads = True
        return await cache.get("key")

    assert asyncio.run(run()) == {"value": 1}


def test_build_passages_rejects_non_advancing_chunks():
    source = Source(source_id="s1", url="https://example.com", content="abcdef")
    for chunk_size, overlap in ((0, 0), (10, 10), (10, 11), (10, -1)):
        try:
            build_passages(source, chunk_size=chunk_size, chunk_overlap=overlap)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid chunk settings must be rejected")


def test_read_endpoint_rejects_non_advancing_chunks():
    from api.v1 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post(
        "/v1/read",
        json={"url": "https://example.com", "chunk_size": 100, "chunk_overlap": 100},
    )
    assert response.status_code == 422


def test_empty_sources_do_not_collapse_into_one_cluster():
    clusters = build_clusters(
        [
            Source(source_id="s1", url="https://one.example"),
            Source(source_id="s2", url="https://two.example"),
        ]
    )
    assert len(clusters) == 2


def test_iso_offset_dates_participate_in_freshness_check():
    claim = Claim(
        claim_id="c1",
        text="OpenAI released GPT-5",
        keywords=keywords("OpenAI released GPT-5"),
    )
    source = Source(
        source_id="s1",
        url="https://example.com",
        content="OpenAI released GPT-5",
        published_at="2020-01-01T00:00:00+07:00",
    )
    result = verify_claims(
        [claim],
        [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)],
        sources=[source],
        max_age_days=365,
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert result[0].status == "outdated"
