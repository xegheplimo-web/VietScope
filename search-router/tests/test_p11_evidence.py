"""P11.1 — /v1/evidence service tests (``pipeline.evidence_fetch``).

Covers the contract Hermes cites against: ``source_id`` and search-side
provenance preserved end to end, ``passage_id`` = ``{source_id}:p{NNN}``,
canonical dedup, SSRF-blocked and unreadable sources → error items (never
a failed batch), bounded-parallel reads, and max caps.
"""

from __future__ import annotations

import asyncio

import pipeline.evidence_fetch as ef
import pytest
from pipeline.reader import ReadResult


def _run(coro):
    return asyncio.run(coro)


def _ok_read(url: str, text: str = "", tier: str = "http") -> ReadResult:
    return ReadResult(
        url=url,
        success=True,
        text=text or f"extracted text for {url} with enough words to score",
        title=f"title {url}",
        tier=tier,
        word_count=10,
    )


def _fail_read(url: str, error: str = "boom") -> ReadResult:
    return ReadResult(url=url, success=False, error=error, tier="http")


def _reader_with_text(text: str):
    async def _read(urls, **kwargs):
        return [_ok_read(u, text) for u in urls]

    return _read


async def fake_reader(urls, **kwargs):
    """Batch seam: one call, all URLs, records kwargs for assertions."""
    fake_reader.calls.append((list(urls), kwargs))
    return [_ok_read(u) for u in urls]


fake_reader.calls = []


@pytest.fixture(autouse=True)
def _reset():
    fake_reader.calls.clear()
    yield
    fake_reader.calls.clear()


class TestProvenance:
    def test_source_id_and_provenance_preserved(self):
        sources = [
            {
                "source_id": "src_004",
                "url": "https://example.com/article",
                "canonical_url": "https://example.com/article",
                "title": "Từ search",
                "domain": "example.com",
                "published_at": "2026-01-15T00:00:00Z",
                "search_provider": "ddgs",
                "score": 0.89,
            }
        ]
        out = _run(ef.build_evidence(sources, "query", read_urls=fake_reader))
        assert out["count"] >= 1
        item = out["evidence"][0]
        assert item["source_id"] == "src_004"
        assert item["passage_id"].startswith("src_004:p")
        assert item["canonical_url"] == "https://example.com/article"
        assert item["published_at"] == "2026-01-15T00:00:00Z"
        assert item["search_provider"] == "ddgs"
        assert item["domain"] == "example.com"
        assert item["title"] == "Từ search"
        assert item["retrieved_at"]
        assert item["content_provider"] == "http"
        for field in ("url", "text", "quote", "score"):
            assert field in item

    def test_bare_url_string_gets_generated_source_id(self):
        out = _run(ef.build_evidence(["https://a.com/x"], "q", read_urls=fake_reader))
        item = out["evidence"][0]
        assert item["source_id"] == "src_000"
        assert item["url"] == "https://a.com/x"
        assert item["canonical_url"] == "https://a.com/x"

    def test_published_at_falls_back_to_reader_metadata(self):
        async def reader_with_date(urls, **kwargs):
            return [
                ReadResult(
                    url=u,
                    success=True,
                    text="text body",
                    tier="http",
                    metadata={"published_at": "2026-02-01T00:00:00Z"},
                )
                for u in urls
            ]

        out = _run(ef.build_evidence(["https://a.com/x"], "q", read_urls=reader_with_date))
        assert out["evidence"][0]["published_at"] == "2026-02-01T00:00:00Z"


class TestDedup:
    def test_canonical_dedup_keeps_first(self):
        """Same canonical identity across different raw URLs dedupes — one
        read, one source of evidence."""
        sources = [
            {"source_id": "s1", "url": "https://a.com/x/"},
            {"source_id": "s2", "url": "https://A.COM/x"},
        ]
        out = _run(ef.build_evidence(sources, "q", read_urls=fake_reader))
        assert fake_reader.calls[0][0] == ["https://a.com/x/"]
        assert {e["source_id"] for e in out["evidence"]} == {"s1"}


class TestSSRF:
    def test_literal_private_ip_blocked_before_read(self):
        out = _run(ef.build_evidence(["http://127.0.0.1/admin"], "q", read_urls=fake_reader))
        assert fake_reader.calls == []
        item = out["evidence"][0]
        assert item["content_provider"] == "ssrf_blocked"
        assert "error" in item
        assert item["text"] == ""

    def test_non_http_scheme_blocked(self):
        out = _run(ef.build_evidence(["ftp://x.com/f"], "q", read_urls=fake_reader))
        assert fake_reader.calls == []
        assert out["evidence"][0]["content_provider"] == "ssrf_blocked"

    def test_blocked_source_does_not_sink_batch(self):
        out = _run(
            ef.build_evidence(
                ["http://169.254.169.254/latest", "https://ok.com/a"],
                "q",
                read_urls=fake_reader,
            )
        )
        assert fake_reader.calls[0][0] == ["https://ok.com/a"]
        kinds = {e["source_id"] for e in out["evidence"]}
        assert len(kinds) == 2
        errors = [e for e in out["evidence"] if e.get("error")]
        assert errors and errors[0]["content_provider"] == "ssrf_blocked"


class TestBoundedParallel:
    def test_single_batch_call_with_concurrency_cap(self):
        urls = [f"https://s{i}.com/p" for i in range(8)]
        _run(ef.build_evidence(urls, "q", read_urls=fake_reader))
        assert len(fake_reader.calls) == 1
        sent, kwargs = fake_reader.calls[0]
        assert sent == urls
        assert kwargs.get("max_concurrent") == 5
        assert kwargs.get("timeout")


class TestFreshness:
    """freshness class → (use_cache, max_age_s) forwarded to the reader."""

    def _sent_kwargs(self, freshness):
        if freshness is None:
            _run(ef.build_evidence(["https://a.com/x"], "q", read_urls=fake_reader))
        else:
            _run(
                ef.build_evidence(
                    ["https://a.com/x"], "q", freshness=freshness, read_urls=fake_reader
                )
            )
        return fake_reader.calls[0][1]

    def test_default_normal_is_one_hour_ttl(self):
        kw = self._sent_kwargs(None)
        assert kw["use_cache"] is True
        assert kw["max_age_s"] == 3600.0

    def test_realtime_bypasses_cache(self):
        kw = self._sent_kwargs("realtime")
        assert kw["use_cache"] is False

    def test_high_is_five_minutes(self):
        kw = self._sent_kwargs("high")
        assert kw["max_age_s"] == 300.0

    def test_static_never_expires(self):
        kw = self._sent_kwargs("static")
        assert kw["use_cache"] is True
        assert kw["max_age_s"] is None

    def test_unknown_class_falls_back_to_normal(self):
        kw = self._sent_kwargs("bogus")
        assert kw["max_age_s"] == 3600.0


class TestFailures:
    def test_failed_read_yields_error_item(self):
        async def reader(urls, **kwargs):
            return [_fail_read(u, "connection refused") for u in urls]

        out = _run(ef.build_evidence(["https://bad.com/x"], "q", read_urls=reader))
        item = out["evidence"][0]
        assert item["error"] == "connection refused"
        assert item["score"] == 0.0
        assert item["text"] == ""

    def test_reranker_exception_never_fails_batch(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("reranker exploded")

        monkeypatch.setattr(ef, "chunk_and_rerank", boom)
        out = _run(ef.build_evidence(["https://a.com/x"], "q", read_urls=fake_reader))
        assert out["count"] == 0
        assert out["evidence"] == []


class TestRoute:
    """POST /v1/evidence — request model + delegation to build_evidence."""

    def _client(self):
        from fastapi.testclient import TestClient
        from main import app

        return TestClient(app)

    def test_route_delegates_and_passes_body(self, monkeypatch):
        seen = {}

        async def fake_build(sources, query, **kwargs):
            seen["sources"] = sources
            seen["query"] = query
            seen.update(kwargs)
            return {"query": query, "evidence": [], "count": 0, "elapsed_seconds": 0.0}

        import pipeline.evidence_fetch

        monkeypatch.setattr(pipeline.evidence_fetch, "build_evidence", fake_build)
        resp = self._client().post(
            "/v1/evidence",
            json={
                "sources": [{"url": "https://a.com/x", "source_id": "s1"}],
                "query": "q",
                "max_passages": 7,
                "freshness": "realtime",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["query"] == "q"
        assert seen["sources"] == [{"url": "https://a.com/x", "source_id": "s1"}]
        assert seen["query"] == "q"
        assert seen["max_passages"] == 7
        assert seen["max_per_source"] == 3
        assert seen["freshness"] == "realtime"

    def test_route_accepts_bare_url_strings(self, monkeypatch):
        async def fake_build(sources, query, **kwargs):
            return {"query": query, "evidence": [], "count": 0, "elapsed_seconds": 0.0}

        import pipeline.evidence_fetch

        monkeypatch.setattr(pipeline.evidence_fetch, "build_evidence", fake_build)
        resp = self._client().post(
            "/v1/evidence",
            json={"sources": ["https://a.com/x"], "query": "q"},
        )
        assert resp.status_code == 200, resp.text

    def test_route_422_on_empty_sources(self):
        resp = self._client().post("/v1/evidence", json={"sources": [], "query": "q"})
        assert resp.status_code == 422

    def test_route_422_on_missing_url(self):
        resp = self._client().post(
            "/v1/evidence",
            json={"sources": [{"title": "no url"}], "query": "q"},
        )
        assert resp.status_code == 422

    def test_route_422_over_hard_ceiling_20_sources(self):
        """Hard API ceiling: >20 sources rejected before any reading.
        Hermes' own caps (8 normal / 15 deep) are enforced at the MCP
        layer — 20 is the server's last line of defense."""
        resp = self._client().post(
            "/v1/evidence",
            json={
                "sources": [f"https://s{i}.com/" for i in range(21)],
                "query": "q",
            },
        )
        assert resp.status_code == 422

    def test_route_allows_20_sources(self, monkeypatch):
        async def fake_build(sources, query, **kwargs):
            return {"query": query, "evidence": [], "count": 0, "elapsed_seconds": 0.0}

        import pipeline.evidence_fetch

        monkeypatch.setattr(pipeline.evidence_fetch, "build_evidence", fake_build)
        resp = self._client().post(
            "/v1/evidence",
            json={
                "sources": [f"https://s{i}.com/" for i in range(20)],
                "query": "q",
            },
        )
        assert resp.status_code == 200, resp.text

    def test_route_422_on_unknown_freshness(self):
        resp = self._client().post(
            "/v1/evidence",
            json={
                "sources": ["https://a.com/x"],
                "query": "q",
                "freshness": "bogus",
            },
        )
        assert resp.status_code == 422


class TestCaps:
    def test_max_per_source_caps_chunks(self):
        text = "relevant words " * 400
        out = _run(
            ef.build_evidence(
                [
                    {
                        "source_id": "s1",
                        "url": "https://a.com/long",
                    }
                ],
                "relevant",
                max_passages=10,
                max_per_source=2,
                read_urls=_reader_with_text(text),
            )
        )
        assert sum(1 for e in out["evidence"] if e["source_id"] == "s1") <= 2

    def test_max_passages_caps_total(self):
        text = "relevant words " * 400
        sources = [{"url": f"https://s{i}.com/p"} for i in range(4)]
        out = _run(
            ef.build_evidence(
                sources,
                "relevant",
                max_passages=3,
                max_per_source=3,
                read_urls=_reader_with_text(text),
            )
        )
        assert out["count"] <= 3

    def test_error_items_survive_full_cap(self):
        """A full passage budget must not drop blocked-source error rows."""
        text = "relevant words " * 400

        async def reader(urls, **kwargs):
            return [_ok_read(u, text) for u in urls]

        out = _run(
            ef.build_evidence(
                [
                    {"url": "http://127.0.0.1/secret", "source_id": "blocked"},
                    {"url": "https://a.com/long", "source_id": "good"},
                ],
                "relevant",
                max_passages=1,
                read_urls=reader,
            )
        )
        kinds = {e["source_id"]: e for e in out["evidence"]}
        assert kinds["blocked"]["content_provider"] == "ssrf_blocked"
        assert len([e for e in out["evidence"] if not e.get("error")]) <= 1
