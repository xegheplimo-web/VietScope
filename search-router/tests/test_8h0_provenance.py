"""Tests for retrieval provenance instrumentation (TASK-8H-0)."""

import asyncio
import re

import pytest
from canonical.url import canonical_url
from fastapi.testclient import TestClient
from models import RetrievalObservation, SearchResultItem, result_fingerprint


def _run(coro):
    return asyncio.run(coro)


# ── Table-driven canonicalization ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # UTM stripping
        (
            "https://www.Example.COM/page/?utm_source=x&id=1",
            "https://example.com/page?id=1",
        ),
        # Fragment removal
        ("https://Example.COM/path/#frag", "https://example.com/path"),
        # Scheme preserved (fetchable canonical) + host lower + slash
        ("HTTP://WWW.EXAMPLE.COM/", "http://example.com"),
        # Trailing slash normalization
        ("https://example.COM/a/?", "https://example.com/a"),
        # Query parameter sorting, no UTM, no fragment
        (
            "https://example.com?utm_campaign=launch&b=2&a=1#top",
            "https://example.com?a=1&b=2",
        ),
    ],
)
def test_canonical_url_table(raw, expected):
    assert canonical_url(raw) == expected


# ── Fingerprint determinism ──────────────────────────────────────────────────


def test_fingerprint_is_12_hex():
    fp = result_fingerprint("https://example.com", "Title", "Snippet")
    assert len(fp) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", fp) is not None


def test_fingerprint_stable_for_same_input():
    fp1 = result_fingerprint("https://example.com", "Title", "Snippet")
    fp2 = result_fingerprint("https://example.com", "Title", "Snippet")
    assert fp1 == fp2


def test_fingerprint_changes_with_title_or_snippet():
    base = "https://example.com"
    fp1 = result_fingerprint(base, "Title A", "Snippet")
    fp2 = result_fingerprint(base, "Title B", "Snippet")
    fp3 = result_fingerprint(base, "Title A", "Different snippet")
    assert fp1 != fp2
    assert fp1 != fp3


def test_fingerprint_ignores_whitespace_case():
    fp1 = result_fingerprint("https://example.com", "  Title A  ", "Snippet\n")
    fp2 = result_fingerprint("https://example.com", "title a", "snippet")
    assert fp1 == fp2


# ── SearXNG provider provenance ──────────────────────────────────────────────


def test_searxng_parse_result_includes_provenance():
    from providers.searxng import _parse_results

    data = {
        "results": [
            {
                "url": "https://www.example.com/page?utm_source=twitter",
                "title": "Example Page",
                "content": "A description",
                "score": 0.5,
                "engines": ["brave", "google"],
            }
        ]
    }
    results = _parse_results(data)
    assert len(results) == 1
    r = results[0]
    assert r.canonical_url == "https://example.com/page"
    assert len(r.fingerprint) == 12
    assert len(r.retrieval_observations) == 1
    obs = r.retrieval_observations[0]
    assert obs.provider == "searxng"
    assert obs.engine == "brave, google"
    assert obs.rank == 1
    assert obs.retrieved_at


def test_searxng_missing_engines_defaults_to_searxng():
    from providers.searxng import _parse_results

    data = {"results": [{"url": "https://example.com", "title": "T", "content": "D"}]}
    results = _parse_results(data)
    assert results[0].retrieval_observations[0].engine == "searxng"


def test_searxng_parse_rank_ordering():
    from providers.searxng import _parse_results

    data = {
        "results": [
            {"url": "https://a.example", "title": "A", "content": "a"},
            {"url": "https://b.example", "title": "B", "content": "b"},
        ]
    }
    results = _parse_results(data)
    assert [r.retrieval_observations[0].rank for r in results] == [1, 2]


# ── DDGS provider provenance ─────────────────────────────────────────────────


def test_ddgs_parse_text_includes_provenance():
    from providers.ddgs import _parse_text

    r = _parse_text(
        {
            "title": "T",
            "href": "https://example.com?utm_medium=email",
            "body": "B",
        },
        rank=2,
        retrieved_at="2026-08-18T00:00:00Z",
    )
    assert r.canonical_url == "https://example.com"
    assert len(r.fingerprint) == 12
    assert r.retrieval_observations[0].provider == "ddgs"
    assert r.retrieval_observations[0].engine == "ddgs"
    assert r.retrieval_observations[0].rank == 2


def test_ddgs_parse_results_enumerates_rank():
    from providers.ddgs import _parse_results

    raw = [
        {"title": "A", "href": "https://a.example", "body": "one"},
        {"title": "B", "href": "https://b.example", "body": "two"},
    ]
    results = _parse_results(raw, "text")
    assert results[0].retrieval_observations[0].rank == 1
    assert results[1].retrieval_observations[0].rank == 2


def test_ddgs_search_provider_is_ddgs_comment():
    from providers.ddgs import _parse_text

    # DDGS does not expose the upstream engine; we record provider == "ddgs".
    r = _parse_text({"title": "T", "href": "https://x.example", "body": "B"}, rank=1)
    assert r.retrieval_observations[0].engine == "ddgs"


# ── Cross-provider canonical/fingerprint identity ────────────────────────────


def test_same_url_same_canonical_and_fingerprint():
    from providers.ddgs import _parse_text as ddgs_parse
    from providers.searxng import _parse_results as searxng_parse

    url = "https://example.com/page?utm_source=newsletter#section"
    title = "Shared Title"
    body = "Shared body text."

    s_result = searxng_parse({"results": [{"url": url, "title": title, "content": body}]})[0]
    d_result = ddgs_parse({"title": title, "href": url, "body": body}, rank=1)

    assert s_result.canonical_url == d_result.canonical_url
    assert s_result.fingerprint == d_result.fingerprint
    assert s_result.canonical_url == "https://example.com/page"


# ── Orchestrator / v1 search integration ─────────────────────────────────────


def test_orchestrator_normalize_preserves_provenance():
    from core.orchestrator import SearchOrchestrator

    item = SearchResultItem(
        url="https://example.com?utm_source=x",
        canonical_url="https://example.com",
        title="T",
        description="D",
        fingerprint="abc123def456",
        retrieval_observations=[RetrievalObservation(provider="searxng", engine="brave", rank=1)],
    )
    orchestrator = SearchOrchestrator.__new__(SearchOrchestrator)
    sources = orchestrator._normalize([("searxng", item)])
    assert len(sources) == 1
    assert sources[0].canonical_url == "https://example.com"
    assert sources[0].fingerprint == "abc123def456"
    assert sources[0].retrieval_observations[0].provider == "searxng"


def test_orchestrator_normalize_computes_missing_provenance():
    from core.orchestrator import SearchOrchestrator

    item = SearchResultItem(
        url="https://example.com?utm_source=x",
        title="T",
        description="D",
        retrieval_observations=[RetrievalObservation(provider="searxng", engine="brave", rank=1)],
    )
    orchestrator = SearchOrchestrator.__new__(SearchOrchestrator)
    sources = orchestrator._normalize([("searxng", item)])
    assert sources[0].canonical_url == "https://example.com"
    assert len(sources[0].fingerprint) == 12


def _make_mock_orchestrator(result_items):
    from unittest.mock import AsyncMock, MagicMock

    from core.query_understanding import QueryProfile

    mock_provider = MagicMock()
    mock_provider.search = AsyncMock(return_value=result_items)

    def _get(name):
        if name == "searxng":
            return mock_provider
        return None

    mock_registry = MagicMock()
    mock_registry.get = _get
    mock_orchestrator = MagicMock()
    mock_orchestrator.registry = mock_registry
    mock_orchestrator.query_understanding.analyze.return_value = QueryProfile(
        language="en", freshness_required=False
    )
    return mock_orchestrator


def test_v1_endpoint_returns_provenance_via_testclient():
    import api.v1 as api_v1
    import main as app_module

    item = SearchResultItem(
        url="https://example.com?utm_source=x",
        canonical_url="https://example.com",
        title="T",
        description="D",
        fingerprint="abc123def456",
        score=1.0,
        retrieval_observations=[RetrievalObservation(provider="searxng", engine="brave", rank=1)],
    )

    api_v1._orchestrator = _make_mock_orchestrator([item])
    try:
        client = TestClient(app_module.app)
        resp = client.post(
            "/v1/search",
            json={"query": "x", "type": "web", "max_results": 1},
        )
    finally:
        api_v1._orchestrator = None

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["canonical_url"] == "https://example.com"
    assert result["fingerprint"] == "abc123def456"
    assert result["retrieval_observations"][0]["provider"] == "searxng"
    assert result["retrieval_observations"][0]["engine"] == "brave"
    assert result["retrieval_observations"][0]["rank"] == 1


def test_v1_endpoint_strips_utm_from_canonical_url():
    import api.v1 as api_v1
    import main as app_module

    item = SearchResultItem(
        url="https://www.example.com/page?utm_medium=email",
        canonical_url="https://example.com/page",
        title="T",
        description="D",
        fingerprint="abc123def456",
        score=1.0,
        retrieval_observations=[],
    )

    api_v1._orchestrator = _make_mock_orchestrator([item])
    try:
        client = TestClient(app_module.app)
        resp = client.post(
            "/v1/search",
            json={"query": "x", "type": "web", "max_results": 1},
        )
    finally:
        api_v1._orchestrator = None

    assert resp.status_code == 200
    assert resp.json()["results"][0]["canonical_url"] == "https://example.com/page"
