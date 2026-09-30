"""Tests for HN and arXiv providers (PART 3) — mock HTTP, no network."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from providers.arxiv import (
    _build_search_query,
    _parse_arxiv_xml,
    arxiv_health,
    arxiv_search,
)
from providers.hn import hn_health, hn_search

# ── HN provider ───────────────────────────────────────────────────────────────


_HN_SAMPLE_RESPONSE = {
    "hits": [
        {
            "objectID": "12345",
            "title": "Show HN: A new Python async framework",
            "url": "https://github.com/example/async-fw",
            "points": 250,
            "num_comments": 80,
            "author": "alice",
            "created_at": "2025-08-15T10:00:00.000Z",
        },
        {
            "objectID": "67890",
            "title": "Discussion: LLMs and reasoning",
            "url": None,  # HN post without external URL → links to HN item
            "points": 150,
            "num_comments": 200,
            "author": "bob",
            "created_at": "2025-08-14T12:00:00.000Z",
        },
    ]
}


class TestHNSearch:
    def test_returns_results(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _HN_SAMPLE_RESPONSE

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            results = asyncio.run(hn_search("python async", max_results=5))

        assert len(results) == 2
        assert results[0].title == "Show HN: A new Python async framework"
        assert results[0].engine == "hn"
        assert results[0].score == 250.0
        assert "github.com" in results[0].url

    def test_falls_back_to_hn_url_when_no_external(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _HN_SAMPLE_RESPONSE

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            results = asyncio.run(hn_search("llm", max_results=5))

        # Second hit has no external URL → should link to HN item.
        assert "news.ycombinator.com/item?id=67890" in results[1].url

    def test_returns_empty_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            results = asyncio.run(hn_search("test", max_results=5))

        assert results == []

    def test_returns_empty_on_exception(self):
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(side_effect=httpx.ConnectError("fail")),
        ):
            results = asyncio.run(hn_search("test", max_results=5))

        assert results == []


class TestHNHealth:
    def test_healthy(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            assert asyncio.run(hn_health()) is True

    def test_unhealthy(self):
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
        ):
            assert asyncio.run(hn_health()) is False


# ── arXiv provider ────────────────────────────────────────────────────────────


_ARXIV_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>Attention Is All You Need: A Survey</title>
    <summary>We survey transformer architectures and their applications in NLP.</summary>
    <published>2025-01-15T00:00:00Z</published>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <arxiv:doi>10.1000/test</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <title>LLM Reasoning Capabilities</title>
    <summary>An analysis of reasoning in large language models.</summary>
    <published>2025-02-20T00:00:00Z</published>
    <author><name>Carol Lee</name></author>
  </entry>
</feed>
"""


class TestArxivParse:
    def test_parse_two_entries(self):
        results = _parse_arxiv_xml(_ARXIV_SAMPLE_XML, 10)
        assert len(results) == 2
        assert "Attention" in results[0].title
        assert results[0].engine == "arxiv"
        assert results[0].category == "science"
        assert results[0].published_date == "2025-01-15"

    def test_parse_authors_truncated(self):
        results = _parse_arxiv_xml(_ARXIV_SAMPLE_XML, 10)
        # First entry has 2 authors.
        assert "Alice Smith" in results[0].description
        assert "Bob Jones" in results[0].description

    def test_parse_doi_included(self):
        results = _parse_arxiv_xml(_ARXIV_SAMPLE_XML, 10)
        assert "DOI" in results[0].description

    def test_parse_empty_xml(self):
        assert _parse_arxiv_xml("", 10) == []

    def test_parse_invalid_xml(self):
        assert _parse_arxiv_xml("not xml", 10) == []


class TestArxivSearch:
    def test_returns_results(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _ARXIV_SAMPLE_XML

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            results = asyncio.run(arxiv_search("transformer survey", max_results=5))

        assert len(results) == 2
        assert results[0].title.startswith("Attention")

    def test_returns_empty_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = ""

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            results = asyncio.run(arxiv_search("test", max_results=5))

        assert results == []

    def test_returns_empty_on_exception(self):
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(side_effect=httpx.ConnectError("fail")),
        ):
            results = asyncio.run(arxiv_search("test", max_results=5))

        assert results == []

    def test_empty_query_returns_empty(self):
        results = asyncio.run(arxiv_search("", max_results=5))
        assert results == []


class TestArxivHealth:
    def test_healthy(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            assert asyncio.run(arxiv_health()) is True

    def test_unhealthy(self):
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(side_effect=httpx.ConnectError("fail")),
        ):
            assert asyncio.run(arxiv_health()) is False


class TestBuildSearchQuery:
    def test_single_term(self):
        assert _build_search_query("transformer") == "all:transformer"

    def test_multi_term(self):
        result = _build_search_query("transformer survey")
        assert "all:transformer" in result
        assert "all:survey" in result
        assert "AND" in result

    def test_empty(self):
        assert _build_search_query("") == ""
