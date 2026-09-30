"""Tests for pipeline/tiered_fetch.py — tiered reader fetch behind NetGuard.

The HTTP tier is exercised with a stubbed ``guarded_get`` (the guard
itself is covered by tests/test_netguard.py); Firecrawl/Playwright tiers
get a stubbed ``httpx.AsyncClient`` posting to a fake renderer.
"""

import asyncio

from crawler.netguard import GuardedResponse, NetGuard
from pipeline import tiered_fetch as tf
from pipeline.tiered_fetch import TieredFetcher, _same_host


def _run(coro):
    return asyncio.run(coro)


def _private_guard() -> NetGuard:
    """NetGuard whose DNS always answers a private address."""
    return NetGuard(resolver=lambda host: ["10.1.2.3"])


def _public_guard() -> NetGuard:
    return NetGuard(resolver=lambda host: ["93.184.216.34"])


class _FakeGuardedClient:
    """Async context manager stand-in for ``guarded_client``'s client."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _stub_http_tier(monkeypatch, resp: GuardedResponse | Exception):
    """Replace the guarded HTTP round-trip with a canned response."""
    monkeypatch.setattr(tf, "guarded_client", lambda netguard, **kw: _FakeGuardedClient())
    if isinstance(resp, Exception):

        async def boom(*a, **kw):
            raise resp

        monkeypatch.setattr(tf, "guarded_get", boom)
    else:

        async def fake_get(*a, **kw):
            return resp

        monkeypatch.setattr(tf, "guarded_get", fake_get)


class _FakeResp:
    def __init__(self, status_code: int, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _FakeHttpxClient:
    """Minimal ``httpx.AsyncClient`` stand-in for renderer POST calls.

    ``routes`` maps a substring of the request URL ("/v2/scrape",
    "/render") to a canned response or exception.
    """

    routes: dict = {}
    default: _FakeResp | Exception = _FakeResp(500)

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: dict | None = None):
        self.last_call = (url, json)
        r = next((v for k, v in self.routes.items() if k in url), self.default)
        if isinstance(r, Exception):
            raise r
        return r


def _stub_httpx(monkeypatch, resp: _FakeResp | Exception, **routes) -> type[_FakeHttpxClient]:
    """Stub ``httpx.AsyncClient``: ``resp`` becomes the default answer;
    keyword routes like ``scrape=...``/``render=...`` override per URL."""
    merged = {f"/{k}": v for k, v in routes.items()}
    cls = type("FakeHttpxClient", (_FakeHttpxClient,), {"routes": merged, "default": resp})
    monkeypatch.setattr(tf.httpx, "AsyncClient", cls)
    return cls


# ─── _same_host ──────────────────────────────────────────────────────────────


class TestSameHost:
    def test_same_host_ignores_case_and_path(self):
        assert _same_host("https://A.example/x", "https://a.example/y") is True

    def test_different_host(self):
        assert _same_host("https://a.example/", "https://b.example/") is False

    def test_malformed_never_matches(self):
        assert _same_host("not a url", "https://a.example/") is False

    def test_unparseable_never_matches(self):
        # urlsplit raises on a bad IPv6 literal → treated as no-match.
        assert _same_host("http://[::1", "http://[::1") is False


# ─── fetch_http (guarded tier) ───────────────────────────────────────────────


class TestFetchHttp:
    def test_200_success_maps_guarded_response(self, monkeypatch):
        _stub_http_tier(
            monkeypatch,
            GuardedResponse(
                status=200,
                headers={"content-type": "text/html", "etag": "W/1", "last-modified": "today"},
                body=b"<html>ok</html>",
                final_url="https://a.example/landing",
                redirects=["https://a.example/landing"],
            ),
        )
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert r.success and r.status_code == 200 and r.tier == "http"
        assert r.url == "https://a.example/landing"
        assert r.content == "<html>ok</html>"
        assert r.etag == "W/1" and r.last_modified == "today"
        assert r.metadata["final_url"] == "https://a.example/landing"
        assert r.metadata["redirects"] == ["https://a.example/landing"]
        assert r.metadata["oversize"] is False

    def test_guard_error_is_failure(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(error="too_many_redirects"))
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert not r.success and r.error == "too_many_redirects"

    def test_non_200_is_failure(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=404))
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert not r.success and r.status_code == 404 and r.error == "HTTP 404"

    def test_exception_is_failure(self, monkeypatch):
        _stub_http_tier(monkeypatch, RuntimeError("dial blew up"))
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert not r.success and "dial blew up" in r.error

    def test_oversize_is_failure_not_truncated_doc(self, monkeypatch):
        # A body capped by max_bytes is never a valid document — it must
        # fail so the next tier can try, not get cached as a prefix.
        _stub_http_tier(
            monkeypatch,
            GuardedResponse(status=200, body=b"<html>trun", oversize=True),
        )
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert not r.success and "oversize" in r.error

    def test_decodes_declared_charset(self, monkeypatch):
        # windows-1252 \x93\x94 are smart quotes — utf-8 decode would
        # corrupt them; the declared charset must win.
        _stub_http_tier(
            monkeypatch,
            GuardedResponse(
                status=200,
                headers={"content-type": "text/html; charset=windows-1252"},
                body=b"<p>\x93caf\xe9\x94</p>",
            ),
        )
        r = _run(TieredFetcher(netguard=_public_guard()).fetch_http("https://a.example/"))
        assert r.success and r.content == "<p>\u201ccaf\xe9\u201d</p>"


# ─── egress check (firecrawl / playwright pre-dispatch vetting) ───────────────


class TestEgressCheck:
    def test_firecrawl_refuses_private_url(self):
        r = _run(TieredFetcher(netguard=_private_guard()).fetch_firecrawl("https://a.example/"))
        assert not r.success and r.tier == "firecrawl" and "SSRF blocked" in r.error

    def test_playwright_refuses_private_url(self):
        r = _run(TieredFetcher(netguard=_private_guard()).fetch_playwright("https://a.example/"))
        assert not r.success and r.tier == "playwright" and "SSRF blocked" in r.error

    def test_egress_check_passes_public(self):
        out = _run(
            TieredFetcher(netguard=_public_guard())._egress_check("https://a.example/", "firecrawl")
        )
        assert out is None


# ─── fetch_firecrawl ──────────────────────────────────────────────────────────


class TestFetchFirecrawl:
    def test_200_same_host_success(self, monkeypatch):
        _stub_httpx(
            monkeypatch,
            _FakeResp(
                200,
                {
                    "success": True,
                    "data": {
                        "markdown": "# ok",
                        "metadata": {"sourceURL": "https://a.example/land"},
                    },
                },
            ),
        )
        r = _run(
            TieredFetcher(netguard=_public_guard(), firecrawl_url="http://fc:3002").fetch_firecrawl(
                "https://a.example/"
            )
        )
        assert r.success and r.tier == "firecrawl" and r.content == "# ok"
        assert r.content_type == "text/markdown" and r.metadata["firecrawl"]

    def test_200_off_host_source_url_dropped(self, monkeypatch):
        _stub_httpx(
            monkeypatch,
            _FakeResp(
                200,
                {
                    "success": True,
                    "data": {
                        "markdown": "# ok",
                        "metadata": {"sourceURL": "https://evil.example/"},
                    },
                },
            ),
        )
        r = _run(
            TieredFetcher(netguard=_public_guard(), firecrawl_url="http://fc:3002").fetch_firecrawl(
                "https://a.example/"
            )
        )
        assert not r.success and "off-host" in r.error

    def test_success_false_is_failure(self, monkeypatch):
        _stub_httpx(
            monkeypatch,
            _FakeResp(200, {"success": False, "error": "scrape timed out"}),
        )
        r = _run(
            TieredFetcher(netguard=_public_guard(), firecrawl_url="http://fc:3002").fetch_firecrawl(
                "https://a.example/"
            )
        )
        assert not r.success and r.error == "scrape timed out"

    def test_non_200_is_failure(self, monkeypatch):
        _stub_httpx(monkeypatch, _FakeResp(500))
        r = _run(
            TieredFetcher(netguard=_public_guard(), firecrawl_url="http://fc:3002").fetch_firecrawl(
                "https://a.example/"
            )
        )
        assert not r.success and r.error == "Firecrawl 500"

    def test_exception_is_failure(self, monkeypatch):
        _stub_httpx(monkeypatch, ConnectionError("firecrawl down"))
        r = _run(
            TieredFetcher(netguard=_public_guard(), firecrawl_url="http://fc:3002").fetch_firecrawl(
                "https://a.example/"
            )
        )
        assert not r.success and "firecrawl down" in r.error


# ─── fetch_playwright ─────────────────────────────────────────────────────────


class TestFetchPlaywright:
    def test_200_success(self, monkeypatch):
        _stub_httpx(monkeypatch, _FakeResp(200, {"html": "<html>js</html>"}))
        r = _run(
            TieredFetcher(
                netguard=_public_guard(), playwright_url="http://pw:3000"
            ).fetch_playwright("https://a.example/")
        )
        assert r.success and r.tier == "playwright" and r.content == "<html>js</html>"
        assert r.content_type == "text/html" and r.metadata["playwright"]

    def test_non_200_is_failure(self, monkeypatch):
        _stub_httpx(monkeypatch, _FakeResp(502))
        r = _run(
            TieredFetcher(
                netguard=_public_guard(), playwright_url="http://pw:3000"
            ).fetch_playwright("https://a.example/")
        )
        assert not r.success and r.error == "Playwright 502"

    def test_exception_is_failure(self, monkeypatch):
        _stub_httpx(monkeypatch, TimeoutError("render timeout"))
        r = _run(
            TieredFetcher(
                netguard=_public_guard(), playwright_url="http://pw:3000"
            ).fetch_playwright("https://a.example/")
        )
        assert not r.success and "render timeout" in r.error


# ─── fetch() chain + batch ────────────────────────────────────────────────────


class TestFetchChain:
    def test_http_success_short_circuits(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=200, body=b"ok"))
        f = TieredFetcher(netguard=_public_guard())
        r = _run(f.fetch("https://a.example/"))
        assert r.success and r.tier == "http"
        # Cached: second fetch returns the same stored result.
        r2 = _run(f.fetch("https://a.example/"))
        assert r2 is r

    def test_falls_to_firecrawl_on_http_failure(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=404))
        _stub_httpx(
            monkeypatch,
            _FakeResp(500),
            v2=_FakeResp(
                200,
                {
                    "success": True,
                    "data": {
                        "markdown": "# fc",
                        "metadata": {"sourceURL": "https://a.example/"},
                    },
                },
            ),
        )
        r = _run(TieredFetcher(netguard=_public_guard()).fetch("https://a.example/"))
        assert r.success and r.tier == "firecrawl"

    def test_falls_to_playwright_on_renderers_only(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=404))
        _stub_httpx(
            monkeypatch,
            _FakeResp(500),
            render=_FakeResp(200, {"html": "<html>js</html>"}),
        )
        r = _run(TieredFetcher(netguard=_public_guard()).fetch("https://a.example/"))
        assert r.success and r.tier == "playwright"

    def test_falls_through_all_tiers(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=404))
        _stub_httpx(monkeypatch, _FakeResp(500))
        r = _run(TieredFetcher(netguard=_public_guard()).fetch("https://a.example/"))
        assert not r.success and r.error == "All fetch tiers failed"

    def test_fetch_batch(self, monkeypatch):
        _stub_http_tier(monkeypatch, GuardedResponse(status=200, body=b"ok"))
        rs = _run(
            TieredFetcher(netguard=_public_guard()).fetch_batch(
                ["https://a.example/", "https://b.example/"]
            )
        )
        assert len(rs) == 2 and all(r.success for r in rs)
