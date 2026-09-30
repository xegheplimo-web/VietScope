"""Tests for DDGS provider (Wave 8G) — fully mocked, no network."""

import asyncio
from unittest.mock import patch

import pytest
from models import SearchCategory
from providers.ddgs import _parse_results, ddgs_health, ddgs_search


class _MockClient:
    def __init__(self, results_by_category, raise_on=None):
        self.results_by_category = results_by_category
        self.raise_on = raise_on or {}
        self.last_kwargs = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _call(self, category, **kwargs):
        self.last_kwargs[category] = kwargs
        if category in self.raise_on:
            raise self.raise_on[category]
        return self.results_by_category.get(category, [])

    def text(self, **kwargs):
        return self._call("text", **kwargs)

    def news(self, **kwargs):
        return self._call("news", **kwargs)

    def images(self, **kwargs):
        return self._call("images", **kwargs)

    def videos(self, **kwargs):
        return self._call("videos", **kwargs)


def _make_mock_ddgs(results_by_category, raise_on=None):
    client = _MockClient(results_by_category, raise_on=raise_on)
    return lambda *a, **k: client, client


def _run(coro):
    return asyncio.run(coro)


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_parse_text_result():
    raw = [
        {
            "title": "Welcome to Python",
            "href": "https://www.python.org/",
            "body": "Python is a programming language.",
        }
    ]
    results = _parse_results(raw, "text")
    assert len(results) == 1
    assert results[0].url == "https://www.python.org/"
    assert results[0].title == "Welcome to Python"
    assert results[0].description == "Python is a programming language."
    assert results[0].category == "text"
    assert results[0].engine == "ddgs"


def test_parse_news_result():
    raw = [
        {
            "title": "Python challenge winner",
            "body": "Winner snags 96 snakes.",
            "url": "https://example.com/news",
            "date": "2026-08-05T14:46:00+00:00",
            "image": "https://example.com/img.jpg",
            "source": "USA TODAY",
        }
    ]
    results = _parse_results(raw, "news")
    assert results[0].url == "https://example.com/news"
    assert results[0].published_date == "2026-08-05T14:46:00+00:00"
    assert results[0].thumbnail == "https://example.com/img.jpg"
    assert results[0].category == "news"
    assert results[0].engine == "USA TODAY"


def test_parse_images_result():
    raw = [
        {
            "title": "Python logo",
            "url": "https://example.com/logo.png",
            "thumbnail": "https://example.com/thumb.png",
            "source": "example.com",
        }
    ]
    results = _parse_results(raw, "images")
    assert results[0].url == "https://example.com/logo.png"
    assert results[0].thumbnail == "https://example.com/thumb.png"
    assert results[0].category == "images"


def test_parse_ignores_empty_rows():
    raw = [{}, {"title": "Real", "href": "https://real.example"}]
    results = _parse_results(raw, "text")
    assert len(results) == 1
    assert results[0].url == "https://real.example"


# ── Search & integration ──────────────────────────────────────────────────────


def test_search_returns_text_results():
    factory, client = _make_mock_ddgs(
        {"text": [{"title": "A", "href": "https://a.example", "body": "body"}]}
    )

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(ddgs_search("test"))

    assert len(results) == 1
    assert results[0].url == "https://a.example"
    assert client.last_kwargs["text"]["query"] == "test"
    assert client.last_kwargs["text"]["safesearch"] == "off"


def test_search_safe_search_on():
    factory, client = _make_mock_ddgs({"text": []})

    with patch("providers.ddgs.DDGS", new=factory):
        _run(ddgs_search("test", safe=True))

    assert client.last_kwargs["text"]["safesearch"] == "on"


def test_search_maps_time_range():
    factory, client = _make_mock_ddgs({"text": []})

    with patch("providers.ddgs.DDGS", new=factory):
        _run(ddgs_search("test", time_range="week"))

    assert client.last_kwargs["text"]["timelimit"] == "w"


def test_search_maps_categories():
    factory, client = _make_mock_ddgs(
        {
            "text": [{"title": "A", "href": "https://a.example", "body": ""}],
            "news": [{"title": "B", "url": "https://b.example", "body": ""}],
        }
    )

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(
            ddgs_search(
                "test",
                categories=[SearchCategory.news, SearchCategory.general],
                max_results=5,
            )
        )

    assert "text" in client.last_kwargs
    assert "news" in client.last_kwargs
    assert len(results) == 2


def test_search_image_category():
    factory, client = _make_mock_ddgs(
        {
            "images": [
                {
                    "title": "Logo",
                    "url": "https://img.example/1.png",
                    "thumbnail": "https://img.example/t.png",
                    "source": "img.example",
                }
            ]
        }
    )

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(ddgs_search("test", categories=[SearchCategory.images], max_results=3))

    assert client.last_kwargs["images"]["max_results"] == 3
    assert len(results) == 1
    assert results[0].category == "images"


def test_search_dedupes_and_slices_max():
    factory, client = _make_mock_ddgs(
        {
            "text": [
                {"title": "A", "href": "https://dup.example", "body": "one"},
                {"title": "B", "href": "https://dup.example", "body": "two"},
                {"title": "C", "href": "https://c.example", "body": "three"},
            ]
        }
    )

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(ddgs_search("test", max_results=2))

    assert len(results) == 2
    urls = {r.url for r in results}
    assert "https://dup.example" in urls
    assert "https://c.example" in urls


def test_search_fail_soft_on_exception():
    factory, client = _make_mock_ddgs({"text": []}, raise_on={"text": RuntimeError("rate limited")})

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(ddgs_search("test"))

    assert results == []


def test_search_empty_query_returns_empty():
    results = _run(ddgs_search(""))
    assert results == []


def test_search_empty_result():
    factory, client = _make_mock_ddgs({"text": []})

    with patch("providers.ddgs.DDGS", new=factory):
        results = _run(ddgs_search("test"))

    assert results == []


def test_invalid_time_range_raises():
    with pytest.raises(ValueError, match="invalid time_range"):
        _run(ddgs_search("test", time_range="century"))


# ── Health ────────────────────────────────────────────────────────────────────


def test_health():
    with patch("providers.ddgs._DDGS_AVAILABLE", True):
        assert _run(ddgs_health()) is True


def test_health_false_when_not_available():
    with patch("providers.ddgs._DDGS_AVAILABLE", False):
        assert _run(ddgs_health()) is False
