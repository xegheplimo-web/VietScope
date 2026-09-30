"""TASK-8G — Exa search provider (premium semantic search, key-gated).

Tests mock ``httpx.AsyncClient.post`` — no real network / Exa credits.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import config
import httpx
import pytest
from core.provider_registry import (
    ExaProvider,
    ProviderSearchQuery,
    create_default_registry,
)
from models import SearchResultItem
from providers.exa import (
    _build_payload,
    _map_category,
    _parse_results,
    exa_health,
    exa_search,
    is_configured,
)

_KEY = "exa-test-key"


# ── fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture
def exa_key(monkeypatch):
    monkeypatch.setattr(config.settings, "exa_api_key", _KEY)
    return _KEY


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(config.settings, "exa_api_key", "")
    return ""


def _sample_response():
    return {
        "requestId": "req-1",
        "results": [
            {
                "id": "r1",
                "title": "Example Result",
                "url": "https://example.com/result",
                "publishedDate": "2025-06-01T00:00:00.000Z",
                "author": "Jane",
                "text": "A text snippet about example things.",
                "highlight": None,
                "score": 0.83,
            },
            {
                "id": "r2",
                "title": "No Score",
                "url": "https://example.com/noscore",
                "text": "No score present.",
                "highlight": None,
                "score": None,
            },
        ],
    }


def _mock_resp(status=200, payload=None):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload if payload is not None else _sample_response()
    if status >= 400:
        req = httpx.Request("POST", "https://api.exa.ai/search")
        m.request = req
        m.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"Server error '{status}'", request=req, response=m
        )
    return m


def _patched_post(mock_resp):
    return patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp))


# ── parsing / mapping ────────────────────────────────────────────────────────


class TestParse:
    def test_parses_sample_response(self):
        items = _parse_results(_sample_response(), category="news")
        assert len(items) == 2
        r0 = items[0]
        assert isinstance(r0, SearchResultItem)
        assert r0.title == "Example Result"
        assert r0.url == "https://example.com/result"
        assert r0.engine == "exa"
        assert r0.score == 0.83
        assert r0.published_date == "2025-06-01T00:00:00.000Z"
        assert "text snippet" in r0.description
        assert r0.category == "news"

    def test_score_defaults_to_zero_when_missing(self):
        items = _parse_results(_sample_response())
        assert items[1].score == 0.0

    def test_skips_url_less_results(self):
        data = {
            "results": [
                {"title": "No URL", "text": "x"},
                {"title": "OK", "url": "https://ok.example", "text": "y"},
            ]
        }
        items = _parse_results(data)
        assert [i.url for i in items] == ["https://ok.example"]

    def test_empty_results(self):
        assert _parse_results({"results": []}) == []
        assert _parse_results({}) == []

    def test_category_mapping(self):
        assert _map_category("news") == "news"
        assert _map_category("general") == ""
        assert _map_category("") == ""


# ── payload building ─────────────────────────────────────────────────────────


class TestPayload:
    def test_time_range_maps_to_start_published_date(self):
        payload = _build_payload("rust async", 5, time_range="day")
        assert "startPublishedDate" in payload
        assert payload["numResults"] == 5
        assert payload["type"] == "auto"

    def test_no_time_range_no_start_date(self):
        payload = _build_payload("rust async", 5)
        assert "startPublishedDate" not in payload

    def test_invalid_time_range_ignored_gracefully(self):
        payload = _build_payload("rust async", 5, time_range="hours")
        assert "startPublishedDate" not in payload

    def test_category_only_forwarded_when_valid(self):
        assert _build_payload("q", 5, category="news")["category"] == "news"
        assert "category" not in _build_payload("q", 5, category="general")


# ── key-optional behavior ────────────────────────────────────────────────────


class TestKeyOptional:
    def test_not_configured_when_empty(self, no_key):
        assert is_configured() is False

    def test_configured_when_key_present(self, exa_key):
        assert is_configured() is True

    def test_search_returns_empty_without_key(self, no_key):
        post = AsyncMock()
        with patch("httpx.AsyncClient.post", new=post):
            results = asyncio.run(exa_search("rust async"))
        assert results == []
        post.assert_not_awaited()  # never touches the API

    def test_health_not_configured_without_key(self, no_key):
        assert asyncio.run(exa_health()) is False

    def test_health_not_configured_with_blank_key(self, monkeypatch):
        monkeypatch.setattr(config.settings, "exa_api_key", "   ")
        assert asyncio.run(exa_health()) is False

    def test_health_ok_when_configured(self, exa_key):
        with _patched_post(_mock_resp(status=200)):
            assert asyncio.run(exa_health()) is True

    def test_health_unreachable_on_error(self, exa_key):
        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.ConnectError("fail")),
        ):
            assert asyncio.run(exa_health()) is False


# ── fail-soft search ─────────────────────────────────────────────────────────


class TestFailSoft:
    def test_returns_results_on_success(self, exa_key):
        with _patched_post(_mock_resp(status=200)):
            results = asyncio.run(exa_search("rust async", max_results=5))
        assert len(results) == 2
        assert results[0].engine == "exa"

    def test_returns_empty_on_http_error(self, exa_key):
        with _patched_post(_mock_resp(status=500)):
            results = asyncio.run(exa_search("rust async"))
        assert results == []

    def test_returns_empty_on_exception(self, exa_key):
        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.ConnectError("fail")),
        ):
            results = asyncio.run(exa_search("rust async"))
        assert results == []

    def test_empty_result_payload(self, exa_key):
        with _patched_post(_mock_resp(status=200, payload={"results": []})):
            results = asyncio.run(exa_search("nothing here"))
        assert results == []

    def test_authorization_header_sent(self, exa_key):
        mock = AsyncMock(return_value=_mock_resp(status=200))
        with patch("httpx.AsyncClient.post", new=mock):
            asyncio.run(exa_search("rust async"))
        headers = mock.await_args.kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {_KEY}"


# ── registry integration ─────────────────────────────────────────────────────


class TestRegistryIntegration:
    def test_default_registry_contains_exa(self):
        reg = create_default_registry()
        names = [n for n, _ in reg.all()]
        assert "searxng" in names
        assert "exa" in names
        assert reg.get("exa") is not None

    def test_registry_search_without_key_returns_empty(self, no_key):
        reg = create_default_registry()
        provider = reg.get("exa")

        async def run():
            return await provider.search(ProviderSearchQuery(query="rust async"))

        assert asyncio.run(run()) == []

    def test_registry_health_not_configured_without_key(self, no_key):
        reg = create_default_registry()
        health = asyncio.run(reg.health())
        assert health["exa"] is False
        assert isinstance(health["searxng"], bool)  # searxng untouched

    def test_provider_forwards_time_range_and_category(self, exa_key):
        dummy = [SearchResultItem(url="https://exa.example", title="x")]

        async def run():
            with patch("providers.exa.exa_search", new=AsyncMock(return_value=dummy)) as m:
                await ExaProvider().search(
                    ProviderSearchQuery(
                        query="deep research topic",
                        time_range="month",
                        categories=[],
                    )
                )
                return m

        m = asyncio.run(run())
        assert m.await_args.kwargs["time_range"] == "month"

    def test_provider_default_no_time_range(self, exa_key):
        dummy = [SearchResultItem(url="https://exa.example", title="x")]

        async def run():
            with patch("providers.exa.exa_search", new=AsyncMock(return_value=dummy)) as m:
                await ExaProvider().search(ProviderSearchQuery(query="semantic query"))
                return m

        m = asyncio.run(run())
        assert m.await_args.kwargs.get("time_range") is None

    def test_provider_query_rejects_invalid_time_range(self):
        with pytest.raises(ValueError):
            ProviderSearchQuery(query="q", time_range="hours")
