"""TASK-8D — SearXNG provider freshness threading + coverage widening."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.provider_registry import ProviderSearchQuery, SearXNGProvider
from models import SearchResultItem
from providers.searxng import _dedupe, searxng_search


def _item(url, title="T"):
    return {
        "url": url,
        "title": title,
        "content": "c",
        "score": 0.5,
        "engines": ["e"],
        "category": "general",
        "publishedDate": None,
        "thumbnail": "",
    }


def _resp(items):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"results": items}
    return m


def _many(n):
    return [_item(f"https://e{i}.com") for i in range(n)]


class TestTimeRangeThreading:
    def test_time_range_week_passed_for_numeric(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price"))
        assert len(results) == 6
        assert get.await_count == 1
        params = get.await_args.kwargs["params"]
        assert params["time_range"] == "week"
        assert "news" in params["categories"]

    def test_no_time_range_for_normal_query(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            asyncio.run(searxng_search("cách làm bánh mì"))
        params = get.await_args.kwargs["params"]
        assert "time_range" not in params

    def test_no_time_range_for_evergreen_query(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            asyncio.run(searxng_search("what is bitcoin"))
        params = get.await_args.kwargs["params"]
        assert "time_range" not in params

    def test_day_for_now_marker(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            asyncio.run(searxng_search("bitcoin price today"))
        assert get.await_args.kwargs["params"]["time_range"] == "day"

    def test_explicit_time_range_overrides_signal(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            asyncio.run(searxng_search("bitcoin price today", time_range="month"))
        assert get.await_args.kwargs["params"]["time_range"] == "month"


class TestTimeRangeValidation:
    """MAJOR r2: direct searxng_search must not forward invalid time_range."""

    def test_direct_searxng_search_rejects_invalid_time_range(self):
        with pytest.raises(ValueError):
            asyncio.run(searxng_search("bitcoin price", time_range="hours"))

    def test_direct_searxng_search_accepts_valid_time_range(self):
        get = AsyncMock(side_effect=[_resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price", time_range="year"))
        assert len(results) == 6
        assert get.await_args.kwargs["params"]["time_range"] == "year"


class TestCoverageWidening:
    def test_widening_triggers_on_thin_results(self):
        get = AsyncMock(side_effect=[_resp([]), _resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price"))
        assert len(results) == 6
        assert get.await_count == 2
        first_q = get.await_args_list[0].kwargs["params"]["q"]
        second_q = get.await_args_list[1].kwargs["params"]["q"]
        assert first_q == "bitcoin price"
        assert second_q == "bitcoin price rate"

    def test_no_widening_when_enough_results(self):
        get = AsyncMock(side_effect=[_resp(_many(5))])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price"))
        assert len(results) == 5
        assert get.await_count == 1

    def test_no_widening_for_normal_query_even_if_thin(self):
        get = AsyncMock(side_effect=[_resp([])])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("cách làm bánh mì"))
        assert results == []
        assert get.await_count == 1


class TestZeroCoverageFallback:
    """CRITICAL finding: never retry the same failing time_range constraint."""

    def test_falls_back_to_original_query_without_time_range(self):
        get = AsyncMock(side_effect=[_resp([]), _resp([]), _resp(_many(6))])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price"))
        assert len(results) == 6
        assert get.await_count == 3

        first = get.await_args_list[0].kwargs["params"]
        second = get.await_args_list[1].kwargs["params"]
        third = get.await_args_list[2].kwargs["params"]

        assert first["q"] == "bitcoin price"
        assert first["time_range"] == "week"

        assert second["q"] == "bitcoin price rate"
        assert second["time_range"] == "week"

        assert third["q"] == "bitcoin price"
        assert "time_range" not in third

    def test_fallback_is_single_retry_only(self):
        get = AsyncMock(side_effect=[_resp([]), _resp([]), _resp([])])
        with patch("httpx.AsyncClient.get", new=get):
            results = asyncio.run(searxng_search("bitcoin price"))
        assert results == []
        assert get.await_count == 3


class TestDedupe:
    def test_drops_url_less_results(self):
        items = [
            SearchResultItem(url="", title="a"),
            SearchResultItem(url="https://x.com", title="b"),
            SearchResultItem(url="https://x.com", title="c"),
            SearchResultItem(url="", title="d"),
            SearchResultItem(url="https://y.com", title="e"),
        ]
        out = _dedupe(items)
        assert [r.url for r in out] == ["https://x.com", "https://y.com"]


class TestProviderForwarding:
    def test_provider_query_forwards_time_range(self):
        dummy = [SearchResultItem(url="https://x.com", title="x")]

        async def run():
            with patch(
                "providers.searxng.searxng_search",
                new=AsyncMock(return_value=dummy),
            ) as m:
                await SearXNGProvider().search(
                    ProviderSearchQuery(query="bitcoin price", time_range="week")
                )
                return m

        m = asyncio.run(run())
        assert m.await_args.kwargs["time_range"] == "week"

    def test_provider_query_default_no_time_range(self):
        dummy = [SearchResultItem(url="https://x.com", title="x")]

        async def run():
            with patch(
                "providers.searxng.searxng_search",
                new=AsyncMock(return_value=dummy),
            ) as m:
                await SearXNGProvider().search(ProviderSearchQuery(query="cách làm bánh mì"))
                return m

        m = asyncio.run(run())
        assert m.await_args.kwargs.get("time_range") is None

    def test_provider_query_rejects_invalid_time_range(self):
        with pytest.raises(ValueError):
            ProviderSearchQuery(query="bitcoin price", time_range="hours")

    def test_provider_query_accepts_valid_time_ranges(self):
        for tr in ["day", "week", "month", "year", None]:
            q = ProviderSearchQuery(query="bitcoin price", time_range=tr)
            assert q.time_range == tr
