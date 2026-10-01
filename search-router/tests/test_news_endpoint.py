"""Regression tests for POST /v1/news — P0 correctness.

The /v1/news endpoint must:
1. Pass req.query (str) to orchestrator._search_query — NOT ProviderSearchQuery
2. Force categories=[SearchCategory.news] via overrides
3. Pass req.max_results via overrides
4. Return {query, results, count} with correct shape
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import api.v1 as v1
from models import SearchCategory


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
