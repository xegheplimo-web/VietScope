"""Regression tests for POST /v1/news — P0 correctness.

The /v1/news endpoint must:
1. Pass req.query (str) to orchestrator._search_query — NOT ProviderSearchQuery
2. Force categories=[SearchCategory.news] via overrides
3. Pass req.max_results via overrides
4. Return {query, results, count} with correct shape
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import api.v1 as v1
from core.orchestrator import SearchOrchestrator
from core.provider_registry import ProviderRegistry, _categories_for
from fastapi.testclient import TestClient
from models import SearchCategory
from providers.base import ProviderResult, ProviderSpec, SourceType


class _FakeQueryUnderstanding:
    def analyze(self, query):
        return SimpleNamespace(
            intent="news",
            language="vi",
            freshness_required=True,
            categories=[SearchCategory.news],
        )


def _make_orchestrator():
    """Build a mock orchestrator with _search_query AsyncMock."""
    orch = MagicMock()
    orch.query_understanding = _FakeQueryUnderstanding()
    orch._search_query = AsyncMock(return_value=[])
    return orch


def _post_news(client, query="AI", max_results=5):
    return client.post(
        "/v1/news",
        json={"query": query, "max_results": max_results},
    )


def _client():
    import main as app_module

    return TestClient(app_module.app)


def test_news_passes_query_string_not_provider_search_query(monkeypatch):
    """CRITICAL: _search_query must receive req.query (str), not ProviderSearchQuery.

    The P0 bug was passing ProviderSearchQuery to _search_query which expects str.
    """
    orch = _make_orchestrator()
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client, query="tin tức mới", max_results=3)

    assert resp.status_code == 200
    orch._search_query.assert_called_once()
    call_args = orch._search_query.call_args
    # First positional arg must be the query string
    assert call_args[0][0] == "tin tức mới"
    assert isinstance(call_args[0][0], str)


def test_news_forces_news_category_via_overrides(monkeypatch):
    """_search_query must receive overrides with categories=[SearchCategory.news]."""
    orch = _make_orchestrator()
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client)

    assert resp.status_code == 200
    call_kwargs = orch._search_query.call_args[1]
    overrides = call_kwargs.get("overrides")
    assert overrides is not None
    assert "categories" in overrides
    assert overrides["categories"] == [SearchCategory.news]


def test_news_passes_max_results_via_overrides(monkeypatch):
    """_search_query must receive overrides with max_results=req.max_results."""
    orch = _make_orchestrator()
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client, max_results=7)

    assert resp.status_code == 200
    call_kwargs = orch._search_query.call_args[1]
    overrides = call_kwargs.get("overrides")
    assert overrides is not None
    assert "max_results" in overrides
    assert overrides["max_results"] == 7


def test_news_returns_correct_response_shape(monkeypatch):
    """Response must be {query, results: [{url, title, description, published_at}], count}."""
    orch = _make_orchestrator()

    # Mock _search_query to return fake sources
    fake_sources = [
        SimpleNamespace(
            url="https://example.com/news1",
            title="News 1",
            description="Desc 1",
            published_at="2026-10-01",
        ),
        SimpleNamespace(
            url="https://example.com/news2",
            title="News 2",
            description="Desc 2",
            published_at=None,
        ),
    ]
    orch._search_query = AsyncMock(return_value=fake_sources)
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "AI"
    assert data["count"] == 2
    assert len(data["results"]) == 2
    assert data["results"][0]["url"] == "https://example.com/news1"
    assert data["results"][0]["title"] == "News 1"
    assert data["results"][0]["description"] == "Desc 1"
    assert data["results"][0]["published_at"] == "2026-10-01"
    assert data["results"][1]["published_at"] is None


def test_news_empty_results(monkeypatch):
    """Empty results should return count=0 and empty list."""
    orch = _make_orchestrator()
    orch._search_query = AsyncMock(return_value=[])
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "AI"
    assert data["count"] == 0
    assert data["results"] == []


def test_news_uses_fast_mode(monkeypatch):
    """_search_query must be called with mode='fast'."""
    orch = _make_orchestrator()
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    client = _client()
    resp = _post_news(client)

    assert resp.status_code == 200
    call_kwargs = orch._search_query.call_args[1]
    assert call_kwargs.get("mode") == "fast"


# ─── R5 regression tests — real orchestrator + fake providers ────────────────


class _CapturingProvider:
    """Phase-2 provider stub — records the (sq, ctx) the executor passes in."""

    def __init__(self, name, items=()):
        self.name = name
        self.items = list(items)
        self.calls = []

    async def search(self, sq, ctx=None):
        self.calls.append((sq, ctx))
        return list(self.items)

    async def health(self):
        return True


def _news_orchestrator(providers):
    """Real SearchOrchestrator over fakes that serve the news+general lanes.

    Mirroring production specs (vnexpress, searxng), each fake serves both
    general_web and news — so the router's always-on general_web backbone
    lands in every pick's served_types.
    """
    reg = ProviderRegistry()
    for name, provider in providers.items():
        reg.register(
            name,
            provider,
            ProviderSpec(
                name=name,
                source_types=[SourceType.general_web, SourceType.news],
            ),
        )
    return SearchOrchestrator(reg)


def test_news_provider_categories_are_exclusive(monkeypatch):
    """Provider-visible categories for /v1/news must be exactly [news].

    _categories_for unions sq.categories with every routed lane in
    ctx.source_types; without exclusivity the general_web backbone re-adds
    SearchCategory.general and generic web results leak into the news feed.
    """
    prov = _CapturingProvider("fakevn")
    orch = _news_orchestrator({"fakevn": prov})
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    resp = _post_news(_client(), query="tin tức AI", max_results=3)

    assert resp.status_code == 200
    assert prov.calls, "provider was never searched"
    for sq, ctx in prov.calls:
        assert _categories_for(sq, ctx) == [SearchCategory.news]


def test_news_max_results_caps_merged_response(monkeypatch):
    """max_results bounds the merged+ranked response, not each provider."""
    top = [
        ProviderResult(
            url=f"https://news-hub.example.com/story-{i}",
            title=f"tin tức AI bản {i}",
            snippet="tin tức AI",
            score=score,
        )
        for i, score in enumerate((0.9, 0.8, 0.7))
    ]
    tail = [
        ProviderResult(
            url=f"https://filler-site.example.com/filler-{i}",
            title=f"zzz qqq {i}",
            snippet="zzz qqq",
            score=0.1,
        )
        for i in range(3)
    ]
    orch = _news_orchestrator(
        {
            "alpha": _CapturingProvider("alpha", top),
            "zeta": _CapturingProvider("zeta", tail),
        }
    )
    monkeypatch.setattr(v1, "_get_orchestrator", lambda: orch)

    resp = _post_news(_client(), query="tin tức AI", max_results=3)

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert len(data["results"]) == 3
    assert [r["url"] for r in data["results"]] == [r.url for r in top]
