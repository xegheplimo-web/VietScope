"""P11/P11.1 — MCP adapter tests (httpx mocked, no network).

The adapter is thin: every tool forwards to the Search Router API and no
retrieval logic lives here. Covered:

- ``Authorization: Bearer`` propagation from the scoped
  ``SEARCH_HUB_ROUTER_KEY`` only — ``HUB_ADMIN_KEY`` is deliberately NOT a
  fallback on the normal MCP path (least privilege for Hermes).
- No header when no key is configured (dev auth-off compatibility).
- ``fetch_evidence`` is a single ``POST /v1/evidence`` — it must never
  touch the SSRF-unguarded legacy ``/fetch`` endpoint, and structured
  sources (``source_id``/provenance) pass through verbatim.
- The tool surface Hermes sees: ``search``/``fetch_evidence``/
  ``code_search`` by default; ``fetch``+``research`` only under
  ``MCP_ENABLE_DEEP_RESEARCH``; ``answer`` is never registered.
- Source budget: 8 normal / 15 deep, enforced before any HTTP call.
- MCP-layer URL dedup + domain diversity in ``search`` are real
  assertions, not placeholders.
- Tool-signature stability so the MCP schema exposed to Hermes is stable.
- ``MCP_TIMEOUT_S`` default exceeds the router's ``OPENAI_TIMEOUT_S=300``.
"""

from __future__ import annotations

import inspect
import json

import pytest

# The MCP adapter is an optional extra — envs without ``mcp`` installed must
# skip this module instead of failing collection on the import below.
pytest.importorskip("mcp")

import adapters.mcp_server as mcp_server  # noqa: E402


class _FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"results": []}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.Client stand-in recording constructor headers + post calls."""

    instances: list[_FakeClient] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        _FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, **kwargs):
        self.posts.append({"url": url, "json": json, "kwargs": kwargs})
        return _FakeResponse()

    def get(self, url, params=None, **kwargs):
        self.gets.append({"url": url, "params": params, "kwargs": kwargs})
        return _FakeResponse([])


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(mcp_server.httpx, "Client", _FakeClient)
    yield _FakeClient
    _FakeClient.instances.clear()


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.delenv("SEARCH_HUB_ROUTER_KEY", raising=False)
    monkeypatch.delenv("HUB_ADMIN_KEY", raising=False)


def _headers(client: _FakeClient) -> dict:
    return dict(client.kwargs.get("headers") or {})


def _last_post() -> dict:
    return _FakeClient.instances[-1].posts[-1]


class TestBearerHeader:
    def test_header_sent_when_router_key_set(self, monkeypatch, no_keys):
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "dsa_live_abc123")
        mcp_server.search("hello")
        assert len(_FakeClient.instances) == 1
        headers = _headers(_FakeClient.instances[0])
        assert headers.get("Authorization") == "Bearer dsa_live_abc123"

    def test_admin_key_ignored_on_mcp_path(self, monkeypatch, no_keys):
        """P11.1: HUB_ADMIN_KEY is NOT a fallback — the normal MCP path must
        never carry admin credentials (least privilege)."""
        monkeypatch.setenv("HUB_ADMIN_KEY", "dsa_live_admin999")
        mcp_server.search("hello")
        headers = _headers(_FakeClient.instances[0])
        assert "Authorization" not in headers

    def test_router_key_wins_over_admin(self, monkeypatch, no_keys):
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "dsa_live_router")
        monkeypatch.setenv("HUB_ADMIN_KEY", "dsa_live_admin")
        mcp_server.search("hello")
        headers = _headers(_FakeClient.instances[0])
        assert headers.get("Authorization") == "Bearer dsa_live_router"

    def test_no_header_when_no_key(self, no_keys):
        mcp_server.search("hello")
        headers = _headers(_FakeClient.instances[0])
        assert "Authorization" not in headers

    def test_empty_string_key_treated_as_absent(self, monkeypatch, no_keys):
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "")
        mcp_server.search("hello")
        headers = _headers(_FakeClient.instances[0])
        assert "Authorization" not in headers

    def test_header_on_every_tool_call(self, monkeypatch, no_keys):
        """The scoped key rides every call — whether or not the tool is on
        the default surface (functions stay callable for scripts/ops)."""
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "dsa_live_abc123")
        mcp_server.search("q")
        mcp_server.fetch("https://example.com")
        mcp_server.fetch_evidence(["https://example.com"], "q")
        mcp_server.research("q")
        mcp_server.code_search("q")
        mcp_server.search_places("q")
        mcp_server.answer("q")
        assert len(_FakeClient.instances) == 7
        for client in _FakeClient.instances:
            assert _headers(client).get("Authorization") == "Bearer dsa_live_abc123"

    def test_endpoints_and_bodies(self, monkeypatch, no_keys):
        """Tool → endpoint mapping; fetch_evidence hits the protected
        /v1/evidence path, never the SSRF-unguarded legacy /fetch."""
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "dsa_live_abc123")
        mcp_server.search("q", type="news", max_results=3, lang="vi")
        mcp_server.fetch("https://example.com")  # scrape → guarded /v1/read
        mcp_server.fetch("https://example.com", mode="crawl", limit=5)
        mcp_server.fetch_evidence(["https://example.com"], "q", max_passages=3, max_per_source=2)
        mcp_server.research("q", depth="deep")
        mcp_server.code_search("q", max_results=7, repo="a/b")
        mcp_server.answer("q", depth="deep", max_results=4)
        posts = [c.posts[0] for c in _FakeClient.instances]
        assert posts[0]["url"].endswith("/search")
        assert posts[0]["json"] == {
            "query": "q",
            "type": "news",
            "max_results": 3,
            "lang": "vi",
        }
        assert posts[1]["url"].endswith("/v1/read")
        assert posts[1]["json"] == {"url": "https://example.com"}
        assert posts[2]["url"].endswith("/fetch")
        assert posts[2]["json"] == {
            "url": "https://example.com",
            "mode": "crawl",
            "limit": 5,
        }
        assert posts[3]["url"].endswith("/v1/evidence")
        assert posts[3]["json"] == {
            "sources": ["https://example.com"],
            "query": "q",
            "max_passages": 3,
            "max_per_source": 2,
            "freshness": "normal",
        }
        assert posts[4]["url"].endswith("/v1/research")
        assert posts[4]["json"] == {"query": "q", "mode": "deep"}
        assert posts[5]["url"].endswith("/code_search")
        assert posts[5]["json"] == {"query": "q", "max_results": 7, "repo": "a/b"}
        assert posts[6]["url"].endswith("/answer")
        assert posts[6]["json"] == {"query": "q", "depth": "deep", "max_results": 4}


class TestToolSchemaUnchanged:
    """Signatures drive the MCP tool schema — they must not drift."""

    EXPECTED = {
        "search": (["query", "type", "max_results", "lang"], ("web", 10, "auto")),
        "fetch": (["url", "mode", "limit"], ("scrape", 10)),
        "fetch_evidence": (
            ["sources", "query", "max_passages", "max_per_source", "freshness"],
            (5, 3, "normal"),
        ),
        "research": (["query", "depth"], ("quick",)),
        "code_search": (["query", "max_results", "repo"], (10, "")),
        "search_places": (
            ["query", "lat", "lon", "radius_m", "category", "limit"],
            (None, None, 10000.0, None, 10),
        ),
        "answer": (["query", "depth", "max_results"], ("normal", 10)),
    }

    @pytest.mark.parametrize("tool", sorted(EXPECTED))
    def test_signature(self, tool):
        names, defaults = self.EXPECTED[tool]
        sig = inspect.signature(getattr(mcp_server, tool))
        params = list(sig.parameters.values())
        assert [p.name for p in params] == names
        required = [p for p in params if p.default is inspect.Parameter.empty]
        optional = [p for p in params if p.default is not inspect.Parameter.empty]
        assert [p.name for p in required] == names[: len(required)]
        assert tuple(p.default for p in optional) == defaults


class TestTimeoutConfig:
    """MCP timeout is configurable via MCP_TIMEOUT_S (default 360).

    The default must exceed the API router's OPENAI_TIMEOUT_S=300 so the
    MCP layer does not disconnect first when the router uses nearly all
    of its budget.
    """

    def test_timeout_defaults_to_360(self, monkeypatch, no_keys):
        monkeypatch.delenv("MCP_TIMEOUT_S", raising=False)
        mcp_server.search("hello")
        client = _FakeClient.instances[0]
        assert client.kwargs.get("timeout") == 360

    def test_timeout_default_exceeds_router_budget(self, monkeypatch, no_keys):
        monkeypatch.delenv("MCP_TIMEOUT_S", raising=False)
        mcp_server.search("hello")
        client = _FakeClient.instances[0]
        assert client.kwargs.get("timeout") > 300

    def test_timeout_env_var_respected(self, monkeypatch, no_keys):
        monkeypatch.setenv("MCP_TIMEOUT_S", "120")
        mcp_server.search("hello")
        client = _FakeClient.instances[0]
        assert client.kwargs.get("timeout") == 120


class TestFetchEvidence:
    """fetch_evidence — thin adapter over POST /v1/evidence.

    Fetch, SSRF guard, chunking and reranking all live server-side in
    ``pipeline.evidence_fetch``; these tests pin the adapter contract only.
    """

    def test_posts_to_evidence_endpoint_never_legacy_fetch(self, monkeypatch, no_keys):
        mcp_server.fetch_evidence(["https://example.com/a"], "q")
        post = _last_post()
        assert post["url"].endswith("/v1/evidence")
        assert not post["url"].endswith("/fetch")

    def test_structured_sources_preserve_source_id(self, monkeypatch, no_keys):
        """Real source_id test: a structured search result is forwarded
        verbatim — provenance (source_id, canonical_url, published_at,
        search_provider, score) reaches /v1/evidence untouched."""
        sources = [
            {
                "source_id": "src_004",
                "url": "https://example.com/article",
                "canonical_url": "https://example.com/article",
                "title": "Article",
                "domain": "example.com",
                "published_at": "2026-01-15T00:00:00Z",
                "search_provider": "ddgs",
                "score": 0.89,
            }
        ]
        mcp_server.fetch_evidence(sources, "thủ đô")
        post = _last_post()
        assert post["json"]["sources"] == sources
        assert post["json"]["sources"][0]["source_id"] == "src_004"

    def test_bare_url_strings_accepted(self, monkeypatch, no_keys):
        mcp_server.fetch_evidence(["https://a.com/1", "https://b.com/2"], "q")
        post = _last_post()
        assert post["json"]["sources"] == ["https://a.com/1", "https://b.com/2"]

    def test_response_passed_through_verbatim(self, monkeypatch, no_keys):
        payload = {
            "query": "q",
            "evidence": [
                {
                    "source_id": "src_004",
                    "passage_id": "src_004:p003",
                    "url": "https://example.com/a",
                    "canonical_url": "https://example.com/a",
                    "title": "T",
                    "domain": "example.com",
                    "published_at": "2026-01-15T00:00:00Z",
                    "retrieved_at": "2026-02-01T00:00:00Z",
                    "text": "passage text",
                    "quote": "passage",
                    "score": 0.93,
                    "search_provider": "ddgs",
                    "content_provider": "trafilatura",
                }
            ],
            "count": 1,
            "elapsed_seconds": 0.5,
        }
        monkeypatch.setattr(
            mcp_server,
            "_post",
            lambda endpoint, body: json.dumps(payload),
        )
        result = json.loads(mcp_server.fetch_evidence(["https://example.com/a"], "q"))
        assert result == payload

    def test_no_llm_called(self, monkeypatch, no_keys):
        """fetch_evidence must not call any generative endpoint."""
        llm_called = []

        def tracking_post(endpoint, body):
            if any(k in endpoint for k in ("answer", "research", "synthesize")):
                llm_called.append(endpoint)
            return json.dumps({"evidence": []})

        monkeypatch.setattr(mcp_server, "_post", tracking_post)
        mcp_server.fetch_evidence(["https://example.com"], "test")
        assert llm_called == []


class TestSearchPlaces:
    """P2.0 — search_places is a thin GET adapter on /v1/places/search."""

    def test_get_endpoint_and_params(self, monkeypatch, no_keys):
        mcp_server.search_places(
            "quán ăn", lat=21.03, lon=105.85, radius_m=5000.0, category="food", limit=5
        )
        call = _FakeClient.instances[-1].gets[-1]
        assert call["url"].endswith("/v1/places/search")
        assert call["params"] == {
            "q": "quán ăn",
            "lat": 21.03,
            "lon": 105.85,
            "radius_m": 5000.0,
            "category": "food",
            "limit": 5,
        }

    def test_none_params_dropped(self, monkeypatch, no_keys):
        """Optional params unset by the caller never reach the query string."""
        mcp_server.search_places("cafe")
        call = _FakeClient.instances[-1].gets[-1]
        assert call["params"] == {"q": "cafe", "radius_m": 10000.0, "limit": 10}

    def test_uses_get_not_post(self, monkeypatch, no_keys):
        mcp_server.search_places("phở")
        client = _FakeClient.instances[-1]
        assert client.gets and not client.posts

    def test_auth_header_sent(self, monkeypatch, no_keys):
        monkeypatch.setenv("SEARCH_HUB_ROUTER_KEY", "dsa_live_abc123")
        mcp_server.search_places("q")
        assert _headers(_FakeClient.instances[-1]).get("Authorization") == (
            "Bearer dsa_live_abc123"
        )


class TestToolRegistration:
    """main() registers exactly the Hermes surface — deep tools are opt-in,
    ``answer`` is never a tool."""

    class _FakeMCP:
        def __init__(self):
            self.registered: list[str] = []

        def tool(self):
            def _reg(fn):
                self.registered.append(fn.__name__)
                return fn

            return _reg

        def run(self, *a, **k):
            return None

    def _register(self, monkeypatch, no_keys, deep: bool):
        fake = self._FakeMCP()
        monkeypatch.setattr(mcp_server, "mcp", fake)
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        if deep:
            monkeypatch.setenv("MCP_ENABLE_DEEP_RESEARCH", "true")
        else:
            monkeypatch.delenv("MCP_ENABLE_DEEP_RESEARCH", raising=False)
        mcp_server.main()
        return fake.registered

    def test_default_surface(self, monkeypatch, no_keys):
        assert self._register(monkeypatch, no_keys, deep=False) == [
            "search",
            "fetch_evidence",
            "code_search",
            "search_places",
        ]

    def test_deep_flag_adds_fetch_and_research(self, monkeypatch, no_keys):
        assert self._register(monkeypatch, no_keys, deep=True) == [
            "search",
            "fetch_evidence",
            "code_search",
            "search_places",
            "fetch",
            "research",
        ]

    def test_answer_never_registered(self, monkeypatch, no_keys):
        for deep in (False, True):
            assert "answer" not in self._register(monkeypatch, no_keys, deep=deep)


class TestSourceBudget:
    """fetch_evidence caps sources before any HTTP call: 8 normal,
    15 under MCP_ENABLE_DEEP_RESEARCH."""

    def test_over_eight_sources_rejected_normal(self, monkeypatch, no_keys):
        monkeypatch.delenv("MCP_ENABLE_DEEP_RESEARCH", raising=False)
        out = json.loads(mcp_server.fetch_evidence([f"https://s{i}.com/" for i in range(9)], "q"))
        assert "error" in out
        assert out["evidence"] == []
        assert _FakeClient.instances == []  # rejected before any HTTP call

    def test_eight_sources_allowed_normal(self, monkeypatch, no_keys):
        monkeypatch.delenv("MCP_ENABLE_DEEP_RESEARCH", raising=False)
        mcp_server.fetch_evidence([f"https://s{i}.com/" for i in range(8)], "q")
        assert _last_post()["url"].endswith("/v1/evidence")

    def test_deep_flag_raises_cap_to_15(self, monkeypatch, no_keys):
        monkeypatch.setenv("MCP_ENABLE_DEEP_RESEARCH", "true")
        mcp_server.fetch_evidence([f"https://s{i}.com/" for i in range(15)], "q")
        assert _last_post()["url"].endswith("/v1/evidence")

    def test_over_fifteen_rejected_even_in_deep(self, monkeypatch, no_keys):
        monkeypatch.setenv("MCP_ENABLE_DEEP_RESEARCH", "true")
        out = json.loads(mcp_server.fetch_evidence([f"https://s{i}.com/" for i in range(16)], "q"))
        assert "error" in out
        assert _FakeClient.instances == []


class TestSearchDiversity:
    """search — MCP-layer URL dedup and per-domain cap (real assertions)."""

    def test_url_deduplication(self, monkeypatch, no_keys):
        """Same URL (incl. case/whitespace variants) collapses to one row."""
        monkeypatch.setattr(
            mcp_server,
            "_post",
            lambda endpoint, body: json.dumps(
                {
                    "query": "test",
                    "results": [
                        {"url": "https://a.com/1", "domain": "a.com", "title": "A1", "score": 0.9},
                        {
                            "url": "https://A.COM/1",
                            "domain": "a.com",
                            "title": "A1-dup",
                            "score": 0.85,
                        },
                        {"url": "https://a.com/2", "domain": "a.com", "title": "A2", "score": 0.8},
                        {"url": "https://b.com/1", "domain": "b.com", "title": "B1", "score": 0.7},
                    ],
                }
            ),
        )
        data = json.loads(mcp_server.search("test", max_results=10))
        urls = [r["url"].lower() for r in data["results"]]
        assert len(urls) == len(set(urls)) == 3
        assert data["count"] == 3

    def test_domain_diversity_cap(self, monkeypatch, no_keys):
        """At most 2 results per domain in search output."""
        monkeypatch.setattr(
            mcp_server,
            "_post",
            lambda endpoint, body: json.dumps(
                {
                    "query": "test",
                    "results": [
                        {"url": "https://a.com/1", "domain": "a.com", "title": "A1", "score": 0.9},
                        {"url": "https://a.com/2", "domain": "a.com", "title": "A2", "score": 0.8},
                        {"url": "https://a.com/3", "domain": "a.com", "title": "A3", "score": 0.7},
                        {"url": "https://b.com/1", "domain": "b.com", "title": "B1", "score": 0.6},
                    ],
                }
            ),
        )
        data = json.loads(mcp_server.search("test", max_results=10))
        from collections import Counter

        domains = Counter(r["domain"] for r in data["results"])
        for d, count in domains.items():
            assert count <= 2, f"Domain {d} has {count} results (max 2 allowed)"
