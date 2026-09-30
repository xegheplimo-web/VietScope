"""Tests for crawler/robots.py — RobotsCache.

All HTTP is mocked via ``httpx.MockTransport`` — no network. The handler
serves per-host robots.txt bodies (or raises / returns status codes), so
parsing, caching, and RFC 9309 degrade rules are exercised for real.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from crawler.netguard import NetGuard
from crawler.robots import (
    DEFAULT_USER_AGENT,
    VERDICT_ALLOWED,
    VERDICT_UNAVAILABLE,
    RobotsCache,
    parse_robots_text,
)

ROBOTS_BASIC = b"""\
User-agent: *
Disallow: /admin/
Disallow: /private
Crawl-delay: 5
Sitemap: https://example.com/sitemap.xml
"""

ROBOTS_BOT_SPECIFIC = b"""\
User-agent: Googlebot
Disallow: /

User-agent: SearchHubBot
Disallow: /tmp/
"""

# Real-world shape (tuoitre.vn): UTF-8 BOM + `Allow: /` ahead of Disallows.
# stdlib robotparser first-match-wins would allow everything; RFC 9309
# longest-match must honor the Disallows.
ROBOTS_ALLOW_SHADOW_BOM = (
    b"\xef\xbb\xbfUser-agent: *\nAllow: /\nDisallow: /tim-kiem.htm\nDisallow: /print/\n"
)

ROBOTS_WILDCARD = b"""\
User-agent: *
Disallow: /news-*.htm$
Disallow: /tmp/*
Allow: /tmp/public
"""

# §2.2.1: two groups matching the same product token merge their rules.
ROBOTS_MERGE_GROUPS = b"""\
User-agent: SearchHubBot
Disallow: /foo

User-agent: Googlebot
Disallow: /secret

User-agent: SearchHubBot
Disallow: /bar
"""

# §2.2.1: specific group wins over `*`.
ROBOTS_SPECIFIC_VS_STAR = b"""\
User-agent: *
Disallow: /

User-agent: SearchHubBot
Allow: /
Disallow: /tmp/
"""

# §2.2.2: equivalent-length allow beats disallow regardless of order.
ROBOTS_TIE_ALLOW_FIRST = b"User-agent: *\nAllow: /x\nDisallow: /x\n"
ROBOTS_TIE_DISALLOW_FIRST = b"User-agent: *\nDisallow: /x\nAllow: /x\n"

# §2.2.2/§2.2.3: %2A is a literal `*`, not a wildcard; %24 a literal `$`.
ROBOTS_PCT = b"""\
User-agent: *
Disallow: /file%2A
Disallow: /dol%24
Disallow: /star*
"""

ROBOTS_BIG = b"User-agent: *\nDisallow: /a\n" + (b"# " + b"x" * 100 + b"\n") * 6000


class FakeRobotsServer:
    """Serves robots.txt per host; records request count per host."""

    def __init__(self, bodies: dict[str, bytes | int | Exception] | None = None):
        self.bodies = bodies or {}
        self.counts: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        self.counts[host] = self.counts.get(host, 0) + 1
        body = self.bodies.get(host, 404)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})


def _cache(server: FakeRobotsServer, **kw) -> RobotsCache:
    # Deterministic DNS — the fake hosts never hit a real resolver
    # (netguard refuses unresolvable hosts since F1).
    kw.setdefault("netguard", NetGuard(resolver=lambda host: ["93.184.216.34"]))
    return RobotsCache(
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)), **kw
    )


def _run(coro):
    return asyncio.run(coro)


# ─── Parsing + rules ─────────────────────────────────────────────────────


def test_disallow_rules_applied():
    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server)
    assert _run(robots.allowed("https://example.com/")) is True
    assert _run(robots.allowed("https://example.com/news/article-1")) is True
    assert _run(robots.allowed("https://example.com/admin/")) is False
    assert _run(robots.allowed("https://example.com/admin/panel")) is False
    assert _run(robots.allowed("https://example.com/private")) is False


def test_crawl_delay_and_sitemaps_parsed():
    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server)
    assert _run(robots.crawl_delay("example.com")) == 5.0
    assert _run(robots.sitemaps("example.com")) == ["https://example.com/sitemap.xml"]


def test_allow_does_not_shadow_disallow():
    server = FakeRobotsServer({"example.com": ROBOTS_ALLOW_SHADOW_BOM})
    robots = _cache(server)
    assert _run(robots.allowed("https://example.com/")) is True
    assert _run(robots.allowed("https://example.com/tim-kiem.htm")) is False
    assert _run(robots.allowed("https://example.com/print/x")) is False


def test_wildcard_and_anchor_rules():
    server = FakeRobotsServer({"example.com": ROBOTS_WILDCARD})
    robots = _cache(server)
    assert _run(robots.allowed("https://example.com/news-abc.htm")) is False
    assert _run(robots.allowed("https://example.com/news-abc.html")) is True
    assert _run(robots.allowed("https://example.com/tmp/a/b")) is False
    # /tmp/public (len 11) is longer than /tmp/* (len 6) → Allow wins.
    assert _run(robots.allowed("https://example.com/tmp/public/x")) is True


def test_bot_specific_group_wins():
    # SearchHubBot has its own group — the Googlebot disallow-all must not leak.
    server = FakeRobotsServer({"example.com": ROBOTS_BOT_SPECIFIC})
    robots = _cache(server)
    assert _run(robots.allowed("https://example.com/")) is True
    assert _run(robots.allowed("https://example.com/tmp/x")) is False


def test_missing_robots_allows_all():
    server = FakeRobotsServer({})  # every host → 404
    robots = _cache(server)
    assert _run(robots.allowed("https://nowhere.example/x")) is True
    assert _run(robots.crawl_delay("nowhere.example")) is None
    assert _run(robots.sitemaps("nowhere.example")) == []


def test_timeout_is_unavailable_not_allowed():
    # RFC 9309 §2.3.1.4: unreachable robots = complete disallow, retry —
    # never silently allow-all.
    server = FakeRobotsServer({"slow.example": httpx.ConnectTimeout("boom")})
    robots = _cache(server)
    assert _run(robots.check("https://slow.example/")) == VERDICT_UNAVAILABLE
    assert _run(robots.allowed("https://slow.example/")) is False
    assert _run(robots.crawl_delay("slow.example")) is None


def test_5xx_is_unavailable():
    # RFC 9309 §2.3.1.4: server errors = unavailable → disallow, short TTL.
    server = FakeRobotsServer({"broken.example": 500})
    robots = _cache(server)
    assert _run(robots.check("https://broken.example/")) == VERDICT_UNAVAILABLE
    assert _run(robots.allowed("https://broken.example/")) is False


def test_429_is_unavailable():
    server = FakeRobotsServer({"busy.example": 429})
    robots = _cache(server)
    assert _run(robots.check("https://busy.example/")) == VERDICT_UNAVAILABLE


def test_401_403_allow_all():
    # RFC 9309 §2.3.1.3: 4xx client errors (except 429) → unrestricted.
    server = FakeRobotsServer({"a.example": 401, "b.example": 403})
    robots = _cache(server)
    assert _run(robots.check("https://a.example/")) == VERDICT_ALLOWED
    assert _run(robots.allowed("https://a.example/")) is True
    assert _run(robots.allowed("https://b.example/")) is True


def test_204_is_success_allow_all():
    # RFC 9309 §2.3.1.1: ANY 2xx is a successful retrieval — a 204's empty
    # body parses to zero groups → unrestricted, not "unavailable" (F7).
    server = FakeRobotsServer({"empty.example": 204})
    robots = _cache(server)
    assert _run(robots.check("https://empty.example/")) == VERDICT_ALLOWED
    assert _run(robots.allowed("https://empty.example/x")) is True
    assert server.counts["empty.example"] == 1  # cached, not retried


def test_201_also_success():
    server = FakeRobotsServer({"odd.example": 201})
    robots = _cache(server)
    assert _run(robots.check("https://odd.example/")) == VERDICT_ALLOWED


def test_oversized_robots_truncated_and_parsed():
    server = FakeRobotsServer({"big.example": ROBOTS_BIG})
    robots = _cache(server)
    # Cap is 512 KiB (RFC §2.5 ≥500 KiB) — leading Disallow still parsed.
    assert len(ROBOTS_BIG) > 512 * 1024
    assert _run(robots.allowed("https://big.example/a")) is False
    assert _run(robots.allowed("https://big.example/other")) is True


# ─── RFC 9309 matching (M6) ──────────────────────────────────────────────


def test_equal_length_allow_wins_either_order():
    for body in (ROBOTS_TIE_ALLOW_FIRST, ROBOTS_TIE_DISALLOW_FIRST):
        server = FakeRobotsServer({"example.com": body})
        robots = _cache(server)
        assert _run(robots.allowed("https://example.com/x")) is True
        assert _run(robots.allowed("https://example.com/y")) is True


def test_matching_groups_merge_rules():
    # §2.2.1: every group matching the product token contributes rules.
    server = FakeRobotsServer({"example.com": ROBOTS_MERGE_GROUPS})
    robots = _cache(server)
    assert _run(robots.allowed("https://example.com/foo")) is False
    assert _run(robots.allowed("https://example.com/bar")) is False
    assert _run(robots.allowed("https://example.com/secret")) is True
    assert _run(robots.allowed("https://example.com/other")) is True


def test_specific_group_overrides_star_group():
    server = FakeRobotsServer({"example.com": ROBOTS_SPECIFIC_VS_STAR})
    robots = _cache(server)
    # Without the specific group, `Disallow: /` would block everything.
    assert _run(robots.allowed("https://example.com/")) is True
    assert _run(robots.allowed("https://example.com/page")) is True
    assert _run(robots.allowed("https://example.com/tmp/x")) is False


def test_pct2a_is_literal_not_wildcard():
    server = FakeRobotsServer({"example.com": ROBOTS_PCT})
    robots = _cache(server)
    # `/file%2A` matches the literal `*` path — encoded or raw.
    assert _run(robots.allowed("https://example.com/file%2A")) is False
    assert _run(robots.allowed("https://example.com/file*")) is False
    # …but is NOT a wildcard — this is the stdlib bug M6 fixes.
    assert _run(robots.allowed("https://example.com/fileabc")) is True
    # `%24` matches a literal `$`, not an end anchor.
    assert _run(robots.allowed("https://example.com/dol$")) is False
    assert _run(robots.allowed("https://example.com/dolX")) is True
    # A real `*` wildcard still works.
    assert _run(robots.allowed("https://example.com/starfish")) is False


def test_pct_unreserved_decodes():
    # `%62` = `b` — unreserved octet decodes before comparison.
    parsed = parse_robots_text("User-agent: *\nDisallow: /%62an\n")
    assert parsed.allows("searchhubbot", "https://x/ban") is False
    assert parsed.allows("searchhubbot", "https://x/banana") is False
    assert parsed.allows("searchhubbot", "https://x/can") is True


def test_robots_txt_path_implicitly_allowed():
    parsed = parse_robots_text("User-agent: *\nDisallow: /\n")
    assert parsed.allows("searchhubbot", "https://x/robots.txt") is True


def test_rules_before_first_useragent_ignored():
    parsed = parse_robots_text("Disallow: /\nUser-agent: *\nAllow: /\n")
    assert parsed.allows("searchhubbot", "https://x/anything") is True


# ─── F6: empty UA tokens + Crawl-delay group boundary ────────────────────


def test_empty_user_agent_group_dropped():
    # `User-agent:` with no value must not match every token — `"" in
    # token` is True, which would let a UA-less group swallow all rules.
    parsed = parse_robots_text(
        "User-agent: *\nDisallow: /\nUser-agent:\nCrawl-delay: 10\nAllow: /x\n"
    )
    assert len(parsed.groups) == 1
    assert parsed.groups[0].uas == ["*"]
    assert parsed.allows("searchhubbot", "https://x/anything") is False
    # The orphaned `Allow: /x` died with the UA-less group.
    assert parsed.allows("searchhubbot", "https://x/x") is False


def test_empty_user_agent_does_not_swallow_other_groups():
    parsed = parse_robots_text(
        "User-agent:\nDisallow: /secret\n\nUser-agent: *\nDisallow: /admin\n"
    )
    # The UA-less group's Disallow must not leak into the * group.
    assert parsed.allows("searchhubbot", "https://x/secret") is True
    assert parsed.allows("searchhubbot", "https://x/admin/p") is False


def test_crawl_delay_does_not_close_group():
    # Crawl-delay is an extension record of the *current* group — a
    # following User-agent line joins the group instead of opening a new
    # one (only a UA line after actual rules splits).
    parsed = parse_robots_text(
        "User-agent: SearchHubBot\nCrawl-delay: 5\nUser-agent: OtherBot\nDisallow: /x\n"
    )
    assert len(parsed.groups) == 1
    assert set(parsed.groups[0].uas) == {"SearchHubBot", "OtherBot"}
    assert parsed.groups[0].crawl_delay == 5.0
    assert parsed.allows("otherbot", "https://x/x") is False
    assert parsed.allows("searchhubbot", "https://x/x") is False


def test_crawl_delay_after_rules_still_ends_group():
    # A real rule before the next User-agent still closes the group.
    parsed = parse_robots_text(
        "User-agent: A\nDisallow: /x\nCrawl-delay: 5\nUser-agent: B\nDisallow: /y\n"
    )
    assert len(parsed.groups) == 2
    assert parsed.groups[0].crawl_delay == 5.0
    assert parsed.allows("b", "https://x/y") is False
    assert parsed.allows("b", "https://x/x") is True


def test_sitemap_record_does_not_terminate_group():
    parsed = parse_robots_text("User-agent: *\nSitemap: https://x/s.xml\nDisallow: /admin\n")
    assert parsed.sitemaps == ["https://x/s.xml"]
    assert parsed.allows("searchhubbot", "https://x/admin/p") is False


def test_empty_disallow_is_no_rule():
    parsed = parse_robots_text("User-agent: *\nDisallow:\n")
    assert parsed.allows("searchhubbot", "https://x/anything") is True


def test_utf8_octets_compare():
    # A raw UTF-8 rule matches its percent-encoded URI form (Fig. 4).
    parsed = parse_robots_text("User-agent: *\nDisallow: /foo/ツ\n")
    assert parsed.allows("searchhubbot", "https://x/foo/%E3%83%84") is False
    assert parsed.allows("searchhubbot", "https://x/foo/other") is True


def test_default_user_agent_string():
    assert "SearchHubBot" in DEFAULT_USER_AGENT


def test_fetch_locks_bounded():
    # L12: per-host fetch locks must not grow without bound.
    server = FakeRobotsServer({"default": ROBOTS_BASIC})
    robots = _cache(server)

    async def drive():
        for i in range(1500):
            await robots.allowed(f"https://h{i}.example/")

    _run(drive())
    assert len(robots._locks) <= 1024  # _MAX_LOCKS bound
    assert len(robots._cache) <= 500  # _MAX_ENTRIES LRU still applies


def test_queued_waiter_lock_not_evicted():
    # F5: Lock.locked() is False for a scheduled-but-not-yet-holding
    # waiter — eviction must use refs (holders + waiters), or a second
    # lock is minted for the same origin and robots.txt double-fetches.
    bodies = {f"h{i}.example": 404 for i in range(1100)}
    bodies["a.example"] = ROBOTS_BASIC
    server = FakeRobotsServer(bodies)
    robots = _cache(server)

    async def main():
        gate = asyncio.Event()
        real_fetch = robots._fetch

        async def slow_fetch(origin):
            if origin == "https://a.example":
                await gate.wait()
            return await real_fetch(origin)

        robots._fetch = slow_fetch

        t1 = asyncio.create_task(robots.allowed("https://a.example/1"))
        await asyncio.sleep(0.02)  # t1 holds a.example's lease
        t2 = asyncio.create_task(robots.allowed("https://a.example/2"))
        await asyncio.sleep(0.02)  # t2 queued on the same lease

        # Push the lock map past _MAX_LOCKS — a.example is busy + queued
        # and must survive eviction.
        for i in range(1100):
            await robots.allowed(f"https://h{i}.example/")

        assert "https://a.example" in robots._locks
        assert robots._locks["https://a.example"].refs == 2
        gate.set()
        await asyncio.gather(t1, t2)
        # Serialization held — one robots fetch served both callers.
        assert server.counts["a.example"] == 1

    _run(main())


# ─── Caching ─────────────────────────────────────────────────────────────


def test_robots_fetched_once_per_domain():
    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server)
    _run(robots.allowed("https://example.com/a"))
    _run(robots.allowed("https://example.com/b"))
    _run(robots.crawl_delay("example.com"))
    _run(robots.sitemaps("example.com"))
    assert server.counts["example.com"] == 1


def test_lru_evicts_oldest_domain():
    server = FakeRobotsServer({f"h{i}.example": 404 for i in range(5)})
    robots = _cache(server, max_entries=3)
    for i in range(4):
        _run(robots.allowed(f"https://h{i}.example/"))
    # h0 evicted → refetch on next access; h1..h3 still cached.
    _run(robots.allowed("https://h0.example/"))
    assert server.counts["h0.example"] == 2
    assert server.counts["h1.example"] == 1


def test_ttl_expiry_refetches(monkeypatch):
    import crawler.robots as robots_mod

    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server, ttl_seconds=100.0)
    _run(robots.allowed("https://example.com/a"))
    assert server.counts["example.com"] == 1

    now = [robots_mod.time.monotonic()]
    monkeypatch.setattr(robots_mod.time, "monotonic", lambda: now[0])
    now[0] += 50  # within TTL — still cached
    _run(robots.allowed("https://example.com/b"))
    assert server.counts["example.com"] == 1
    now[0] += 60  # past TTL — refetch
    _run(robots.allowed("https://example.com/c"))
    assert server.counts["example.com"] == 2


def test_domain_arg_forms():
    # crawl_delay/sitemaps accept bare domains and full URLs alike.
    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server)
    assert _run(robots.crawl_delay("example.com")) == 5.0
    assert _run(robots.crawl_delay("https://example.com/some/page")) == 5.0


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_scheme_respected(scheme):
    server = FakeRobotsServer({"example.com": ROBOTS_BASIC})
    robots = _cache(server)
    assert _run(robots.allowed(f"{scheme}://example.com/admin/")) is False
