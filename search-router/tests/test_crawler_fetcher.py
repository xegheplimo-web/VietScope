"""Tests for crawler/fetcher.py — conditional GET, JS-heavy fallback, caps."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from crawler.fetcher import Fetcher, FetchResult
from crawler.netguard import NetGuard
from models import ScrapeResult

HTML_NORMAL = (
    b"<html><head><title>Trang chu</title></head><body>"
    + b"<p>"
    + b"Noi dung bai viet day du, nhieu chu cai de vuot nguong. " * 8
    + b"</p>"
    + b"</body></html>"
)

# Practically empty visible text — classic JS-shell page.
HTML_JS_HEAVY = (
    b'<html><head><title>app</title></head><body><div id="root"></div>'
    b'<script src="/bundle.js"></script></body></html>'
)

# Thin static page — under the JS text threshold but carrying real links
# and no script: must NOT be parked as js_required (R2).
HTML_THIN_LINKED = (
    b"<html><body><nav>"
    b'<a href="/tin-tuc/1">Bai 1</a> <a href="/tin-tuc/2">Bai 2</a> '
    b'<a href="/tin-tuc/3">Bai 3</a>'
    b"</nav></body></html>"
)

# Thin page with a script bundle AND links — link presence still wins (R2).
HTML_THIN_LINKED_SCRIPT = (
    b'<html><body><a href="/x">x</a><script src="/analytics.js"></script></body></html>'
)

# Thin page, no links, no JS markers — an empty static stub, not a shell.
HTML_THIN_EMPTY = b"<html><body><p>hi</p></body></html>"


class FakeWeb:
    """MockTransport handler table: url → (status, body, headers) | Exception."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        if isinstance(route, Exception):
            raise route
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers)


def _fetcher(web: FakeWeb, **kw) -> Fetcher:
    # Deterministic DNS: every fake host resolves public — tests never
    # depend on real DNS (netguard is fail-closed on resolution errors).
    kw.setdefault("netguard", NetGuard(resolver=lambda host: ["93.184.216.34"]))
    return Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(web.handler)), **kw)


def _run(coro):
    return asyncio.run(coro)


# ─── Basic fetch ─────────────────────────────────────────────────────────


def test_fetch_200_html():
    web = FakeWeb(
        {"https://a.vn/page": (200, HTML_NORMAL, {"content-type": "text/html; charset=utf-8"})}
    )
    result = _run(_fetcher(web).fetch("https://a.vn/page"))
    assert result.ok is True
    assert result.status == 200
    assert result.content == HTML_NORMAL
    assert result.mime == "text/html"
    assert result.via == "http"
    assert result.final_url == "https://a.vn/page"
    assert result.elapsed_ms >= 0
    assert result.not_modified is False


def test_fetch_follows_redirects():
    web = FakeWeb(
        {
            "https://a.vn/old": (301, b"", {"location": "https://a.vn/new"}),
            "https://a.vn/new": (200, HTML_NORMAL, {"content-type": "text/html"}),
        }
    )
    result = _run(_fetcher(web).fetch("https://a.vn/old"))
    assert result.ok is True
    assert result.final_url == "https://a.vn/new"


def test_fetch_error_status_not_ok():
    web = FakeWeb({"https://a.vn/gone": (410, b"", {})})
    result = _run(_fetcher(web).fetch("https://a.vn/gone"))
    assert result.ok is False
    assert result.status == 410


def test_fetch_network_error_returns_not_ok():
    web = FakeWeb({"https://dead.vn/": httpx.ConnectError("refused")})
    result = _run(_fetcher(web).fetch("https://dead.vn/"))
    assert result.ok is False
    assert result.status == 0
    assert result.error


def test_fetch_user_agent_header():
    web = FakeWeb({"https://a.vn/": (200, HTML_NORMAL, {"content-type": "text/html"})})
    _run(_fetcher(web).fetch("https://a.vn/"))
    ua = web.requests[0].headers.get("user-agent", "")
    assert "SearchHubBot" in ua


# ─── Conditional GET ─────────────────────────────────────────────────────


def test_conditional_headers_sent():
    web = FakeWeb({"https://a.vn/": (200, HTML_NORMAL, {"content-type": "text/html"})})
    _run(
        _fetcher(web).fetch(
            "https://a.vn/", etag='"v1"', last_modified="Mon, 21 Sep 2026 00:00:00 GMT"
        )
    )
    headers = web.requests[0].headers
    assert headers.get("if-none-match") == '"v1"'
    assert headers.get("if-modified-since") == "Mon, 21 Sep 2026 00:00:00 GMT"


def test_304_not_modified():
    web = FakeWeb({"https://a.vn/": (304, b"", {})})
    result = _run(_fetcher(web).fetch("https://a.vn/", etag='"v1"'))
    assert result.ok is True
    assert result.not_modified is True
    assert result.content == b""
    assert result.status == 304


def test_no_conditional_headers_when_absent():
    web = FakeWeb({"https://a.vn/": (200, HTML_NORMAL, {"content-type": "text/html"})})
    _run(_fetcher(web).fetch("https://a.vn/"))
    headers = web.requests[0].headers
    assert "if-none-match" not in headers
    assert "if-modified-since" not in headers


# ─── JS-heavy Firecrawl fallback (opt-in: CRAWLER_FIRECRAWL_FALLBACK) ────


def _enable_firecrawl(monkeypatch):
    """Opt the fetcher into the Firecrawl render lane (G4 — off by default)."""
    monkeypatch.setenv("CRAWLER_FIRECRAWL_FALLBACK", "true")


def test_js_heavy_marks_js_required_when_fallback_disabled(monkeypatch):
    # G4: default-off — a JS shell is reported as js_required and the
    # Firecrawl lane is never invoked, even when a callable is injected.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})
    calls = []

    async def spy_scrape(url: str, **kw) -> ScrapeResult:
        calls.append(url)
        return ScrapeResult(url=url, markdown="# Rendered")

    result = _run(_fetcher(web, firecrawl=spy_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True
    assert result.via == "http"
    assert calls == []


# ─── R2: thin static HTML must not be parked as js_required ──────────────


def test_thin_static_html_with_links_not_js_required(monkeypatch):
    # R2: a short static navigation page (<200 visible chars, 3 anchors,
    # no script) is a normal fetch — parked js_required would lose both
    # persistence and link discovery for 30 days.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    web = FakeWeb({"https://nav.vn/": (200, HTML_THIN_LINKED, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False
    assert result.content == HTML_THIN_LINKED


def test_thin_html_links_win_over_script_marker(monkeypatch):
    # R2: even with a <script> present, discoverable anchors mean the page
    # is not a pure shell — never park it.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    web = FakeWeb(
        {"https://nav.vn/": (200, HTML_THIN_LINKED_SCRIPT, {"content-type": "text/html"})}
    )
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


def test_thin_html_without_markers_not_js_required(monkeypatch):
    # R2: no positive JS evidence at all — an empty static stub flows
    # through as a normal document rather than a fake js_required park.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    web = FakeWeb({"https://empty.vn/": (200, HTML_THIN_EMPTY, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://empty.vn/"))
    assert result.ok is True
    assert result.js_required is False


# ─── R5: non-discoverable anchors must not rescue a JS shell ────────────


@pytest.mark.parametrize(
    "body",
    [
        # javascript: pseudo-link — discovery drops non-http(s) schemes,
        # so this anchor leads nowhere and cannot rescue the shell.
        (
            b'<html><body><div id="root">'
            b'<a href="javascript:void(0)">Loading</a></div>'
            b'<script src="/app.js"></script></body></html>'
        ),
        # Anchor inside an HTML comment — not markup discovery can see.
        (
            b'<html><body><div id="root"></div>'
            b'<!-- <a href="/about">About</a> -->'
            b'<script src="/app.js"></script></body></html>'
        ),
        # Empty href — urljoin resolves it onto the page itself; skipped.
        (
            b'<html><body><div id="root"><a href="">Loading</a></div>'
            b'<script src="/app.js"></script></body></html>'
        ),
        # Fragment-only href — collapses onto the page itself; skipped.
        (
            b'<html><body><div id="root"><a href="#section">Loading</a></div>'
            b'<script src="/app.js"></script></body></html>'
        ),
    ],
    ids=["javascript_href", "commented_anchor", "empty_href", "fragment_href"],
)
def test_js_shell_dead_anchor_still_js_required(monkeypatch, body):
    # R5: an anchor discovery cannot follow is not a rescue — the page
    # is still a pure shell and must park as js_required.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    web = FakeWeb({"https://spa.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True


def test_thin_script_page_with_usable_link_still_rescued(monkeypatch):
    # R5: the rescue survives for anchors discovery can actually follow —
    # here a root-relative href on a thin page carrying a script marker.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><a href="/about">About</a><script src="/analytics.js"></script></body></html>'
    )
    web = FakeWeb({"https://nav.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


# ─── R6: markup-looking bytes inside script/style bodies are not markup ──


def test_script_string_comment_marker_does_not_swallow_anchor(monkeypatch):
    # R6: "<!--" inside a JS string is script data, not an HTML comment.
    # The old regex comment-stripper treated it as one and ate the rest of
    # the document — including the real anchor — falsely parking the page.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><script>const marker = "<!--";</script>'
        b'<a href="/about">About</a></body></html>'
    )
    web = FakeWeb({"https://nav.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


def test_style_body_comment_marker_does_not_swallow_anchor(monkeypatch):
    # R6: same hole through a CSS comment — the "<!--" inside <style> is
    # style data and must not swallow the following anchor. (The trailing
    # <script> supplies the JS marker so the anchor check actually runs.)
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><style>/* <!-- */</style><a href="/x">X</a>'
        b'<script src="/app.js"></script></body></html>'
    )
    web = FakeWeb({"https://nav.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


def test_anchor_inside_real_comment_still_js_required(monkeypatch):
    # R6: a real comment node is still skipped by the parser — an anchor
    # that only exists inside one cannot rescue the shell.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><div id="root"></div><!-- <a href="/y">Y</a> -->'
        b'<script src="/app.js"></script></body></html>'
    )
    web = FakeWeb({"https://spa.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True


def test_anchor_inside_noscript_does_not_rescue(monkeypatch):
    # R6: <noscript> content is fallback markup — the parser scans it in
    # skip mode, so an anchor there is not discoverable and cannot rescue.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><noscript><a href="/z">Enable JS</noscript>'
        b'<script src="/app.js"></script></body></html>'
    )
    web = FakeWeb({"https://spa.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True


# ─── R7: noscript raw-text + self-closing syntax on skip elements ───────


def test_malformed_noscript_close_does_not_swallow_anchor(monkeypatch):
    # R7: noscript content is raw text — a literal </noscript> always ends
    # it, even when the end tag arrives while inner markup is unclosed.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><div id="root"></div><noscript><div></noscript>'
        b'<a href="/real">Real</a><script src="/app.js"></script></body></html>'
    )
    web = FakeWeb({"https://nav.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


def test_skip_tag_inside_noscript_does_not_hold_scan_open(monkeypatch):
    # R7: the real swallow — a <script> opened inside <noscript> pushed a
    # second frame onto the skip stack and put the parser in script CDATA,
    # so the literal </noscript> was consumed as script text and the scan
    # skipped the rest of the document, anchor included.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><div id="root"></div><noscript><script></noscript>'
        b'<a href="/real">Real</a></body></html>'
    )
    web = FakeWeb({"https://nav.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://nav.vn/"))
    assert result.ok is True
    assert result.js_required is False


def test_self_closing_template_does_not_unlock_skip(monkeypatch):
    # R7: "<template/>" is not self-closing in text/html — the slash is
    # ignored, so the anchor after it sits inside the still-open template
    # and stays inert: it must not rescue the shell.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = (
        b'<html><body><div id="root"></div><template />'
        b'<a href="/fake">Fake</a></template>'
        b'<script src="/app.js"></script></body></html>'
    )
    web = FakeWeb({"https://spa.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True


def test_self_closing_script_keeps_skip(monkeypatch):
    # R7: "<script/>" likewise opens the element — everything after is raw
    # script text until a literal </script>, so a trailing anchor cannot
    # rescue the shell.
    monkeypatch.delenv("CRAWLER_FIRECRAWL_FALLBACK", raising=False)
    body = b'<html><body><div id="root"></div><script /><a href="/fake">Fake</a></body></html>'
    web = FakeWeb({"https://spa.vn/": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.js_required is True


def test_thin_html_without_markers_skips_firecrawl(monkeypatch):
    # No JS evidence → the render lane isn't worth a call either.
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://empty.vn/": (200, HTML_THIN_EMPTY, {"content-type": "text/html"})})
    calls = []

    async def spy_scrape(url: str, **kw) -> ScrapeResult:
        calls.append(url)
        return ScrapeResult(url=url, markdown="x")

    result = _run(_fetcher(web, firecrawl=spy_scrape).fetch("https://empty.vn/"))
    assert result.ok is True
    assert result.via == "http"
    assert calls == []


def test_js_heavy_falls_back_to_firecrawl(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def fake_scrape(url: str, **kw) -> ScrapeResult:
        return ScrapeResult(
            url=url,
            title="Rendered",
            markdown="# Rendered\n\nFull rendered text here.",
            metadata={"sourceURL": url},
        )

    result = _run(_fetcher(web, firecrawl=fake_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "firecrawl"
    assert b"Rendered" in result.content


def test_js_heavy_firecrawl_failure_keeps_http_result(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def failing_scrape(url: str, **kw) -> ScrapeResult:
        return ScrapeResult(url=url, error="playwright down")

    result = _run(_fetcher(web, firecrawl=failing_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "http"
    assert result.content == HTML_JS_HEAVY


def test_js_heavy_firecrawl_exception_keeps_http_result(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def exploding_scrape(url: str, **kw) -> ScrapeResult:
        raise RuntimeError("firecrawl boom")

    result = _run(_fetcher(web, firecrawl=exploding_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "http"


def test_normal_html_does_not_call_firecrawl(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://a.vn/": (200, HTML_NORMAL, {"content-type": "text/html"})})
    calls = []

    async def spy_scrape(url: str, **kw) -> ScrapeResult:
        calls.append(url)
        return ScrapeResult(url=url, markdown="x")

    _run(_fetcher(web, firecrawl=spy_scrape).fetch("https://a.vn/"))
    assert calls == []


def test_non_html_never_calls_firecrawl(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb(
        {"https://a.vn/doc.pdf": (200, b"%PDF-1.4 tiny", {"content-type": "application/pdf"})}
    )
    calls = []

    async def spy_scrape(url: str, **kw) -> ScrapeResult:
        calls.append(url)
        return ScrapeResult(url=url, markdown="x")

    result = _run(_fetcher(web, firecrawl=spy_scrape).fetch("https://a.vn/doc.pdf"))
    assert result.ok is True
    assert result.via == "http"
    assert calls == []


# ─── Size cap ────────────────────────────────────────────────────────────


def test_body_capped_at_max_bytes():
    big = b"x" * (150 * 1024)
    web = FakeWeb({"https://a.vn/big": (200, big, {"content-type": "text/html"})})
    result = _run(_fetcher(web, max_bytes=64 * 1024).fetch("https://a.vn/big"))
    assert result.ok is True
    # A capped body is marked oversize — never silently truncated into a
    # success document (M8).
    assert result.oversize is True
    assert len(result.content) <= 64 * 1024


def test_exact_cap_not_oversize():
    body = b"y" * (32 * 1024)
    web = FakeWeb({"https://a.vn/exact": (200, body, {"content-type": "text/html"})})
    result = _run(_fetcher(web, max_bytes=32 * 1024).fetch("https://a.vn/exact"))
    assert result.ok is True
    assert result.oversize is False
    assert result.content == body


def test_firecrawl_markdown_capped(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def big_scrape(url: str, **kw) -> ScrapeResult:
        return ScrapeResult(url=url, markdown="x" * (15 * 1024), metadata={})

    result = _run(_fetcher(web, firecrawl=big_scrape, max_bytes=8 * 1024).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "firecrawl"
    assert result.oversize is True
    assert result.content == b""


# ─── Redirect edge cases (M11) ───────────────────────────────────────────


def test_redirect_without_location_is_not_ok():
    web = FakeWeb({"https://a.vn/r": (302, b"", {})})
    result = _run(_fetcher(web).fetch("https://a.vn/r"))
    assert result.ok is False
    assert result.status == 302
    assert result.error == "redirect_no_location"


def test_too_many_redirects_is_not_ok():
    routes = {
        f"https://a.vn/{i}": (301, b"", {"location": f"https://a.vn/{i + 1}"}) for i in range(15)
    }
    result = _run(_fetcher(FakeWeb(routes), max_redirects=4).fetch("https://a.vn/0"))
    assert result.ok is False
    assert result.error == "too_many_redirects"


def test_redirect_chain_recorded():
    web = FakeWeb(
        {
            "https://a.vn/0": (301, b"", {"location": "/1"}),
            "https://a.vn/1": (301, b"", {"location": "https://a.vn/2"}),
            "https://a.vn/2": (200, HTML_NORMAL, {"content-type": "text/html"}),
        }
    )
    result = _run(_fetcher(web).fetch("https://a.vn/0"))
    assert result.ok is True
    assert result.final_url == "https://a.vn/2"
    assert result.redirects == ["https://a.vn/1", "https://a.vn/2"]


def test_non_followed_3xx_is_not_ok():
    # 300 Multiple Choices — no Location handling → not a valid document.
    web = FakeWeb({"https://a.vn/mc": (300, b"pick one", {})})
    result = _run(_fetcher(web).fetch("https://a.vn/mc"))
    assert result.ok is False
    assert result.status == 300


# ─── F3: per-hop policy enforcement ──────────────────────────────────────


def test_policy_check_blocks_redirect_hop_before_request():
    web = FakeWeb(
        {
            "https://a.vn/r": (301, b"", {"location": "https://b.vn/x"}),
            "https://b.vn/x": (200, HTML_NORMAL, {"content-type": "text/html"}),
        }
    )

    async def policy(target: str):
        return "skipped_robots" if "b.vn" in target else None

    result = _run(_fetcher(web).fetch("https://a.vn/r", policy_check=policy))
    assert result.ok is False
    assert result.error == "skipped_robots"
    # B was never requested — policy refused before the hop was sent.
    assert [str(r.url) for r in web.requests] == ["https://a.vn/r"]


def test_policy_check_passed_to_every_hop():
    web = FakeWeb(
        {
            "https://a.vn/0": (301, b"", {"location": "https://a.vn/1"}),
            "https://a.vn/1": (301, b"", {"location": "https://b.vn/2"}),
            "https://b.vn/2": (200, HTML_NORMAL, {"content-type": "text/html"}),
        }
    )
    seen: list[str] = []

    async def policy(target: str):
        seen.append(target)
        return None

    result = _run(_fetcher(web).fetch("https://a.vn/0", policy_check=policy))
    assert result.ok is True
    # Origin + every redirect hop passed through the callback.
    assert seen == ["https://a.vn/0", "https://a.vn/1", "https://b.vn/2"]


# ─── F1: Firecrawl server-side redirect is dropped ───────────────────────


def test_firecrawl_off_host_source_url_dropped(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def redirect_scrape(url: str, **kw) -> ScrapeResult:
        # Firecrawl followed a server-side redirect to an unvetted host.
        return ScrapeResult(
            url=url,
            markdown="# Rendered elsewhere",
            metadata={"sourceURL": "https://evil.example/x"},
        )

    result = _run(_fetcher(web, firecrawl=redirect_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "http"  # off-host render dropped → httpx body kept
    assert result.content == HTML_JS_HEAVY


def test_firecrawl_same_host_source_url_kept(monkeypatch):
    _enable_firecrawl(monkeypatch)
    web = FakeWeb({"https://spa.vn/": (200, HTML_JS_HEAVY, {"content-type": "text/html"})})

    async def same_host_scrape(url: str, **kw) -> ScrapeResult:
        return ScrapeResult(
            url=url,
            markdown="# Rendered",
            metadata={"sourceURL": "https://spa.vn/landing"},
        )

    result = _run(_fetcher(web, firecrawl=same_host_scrape).fetch("https://spa.vn/"))
    assert result.ok is True
    assert result.via == "firecrawl"
    assert result.final_url == "https://spa.vn/landing"


# ─── FetchResult shape ───────────────────────────────────────────────────


def test_fetch_result_defaults():
    r = FetchResult(ok=True)
    assert r.status == 0
    assert r.content == b""
    assert r.via == "http"
    assert r.not_modified is False
    assert r.oversize is False
    assert r.redirects == []
    assert r.error is None
