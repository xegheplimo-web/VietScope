"""Tests for crawler/seeds.py — sitemap/feed parsing, VN seed registry."""

from __future__ import annotations

import asyncio

import pytest
from crawler import seeds
from crawler.seeds import (
    SEED_DOMAINS,
    VN_SEEDS,
    SitemapTooLargeError,
    SitemapURL,
    collect_sitemap_urls,
    load_seeds,
    parse_feed,
    parse_sitemap,
    registrable_domain,
)

URLSET = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc><lastmod>2026-09-01</lastmod></url>
  <url><loc>https://example.com/b</loc></url>
  <url><loc>https://example.com/c</loc><lastmod>2026-09-20T10:00:00+07:00</lastmod></url>
</urlset>
"""

SITEMAPINDEX = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sm1.xml</loc><lastmod>2026-09-01</lastmod></sitemap>
  <sitemap><loc>https://example.com/sm2.xml</loc></sitemap>
</sitemapindex>
"""

CHILD_SITEMAP = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/child-page</loc><lastmod>2026-09-02</lastmod></url>
</urlset>
"""

RSS2 = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Story 1</title>
      <link>https://example.com/story-1</link>
      <pubDate>Mon, 21 Sep 2026 08:00:00 +0700</pubDate>
    </item>
    <item>
      <link>https://example.com/story-2</link>
    </item>
  </channel>
</rss>
"""

ATOM = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Entry 1</title>
    <link rel="alternate" href="https://example.com/entry-1"/>
    <updated>2026-09-21T08:00:00Z</updated>
  </entry>
  <entry>
    <link href="https://example.com/entry-2"/>
    <published>2026-09-20T08:00:00Z</published>
  </entry>
</feed>
"""


def _run(coro):
    return asyncio.run(coro)


# ─── Sitemap parsing ─────────────────────────────────────────────────────


def test_parse_urlset():
    urls = parse_sitemap(URLSET)
    assert [u.loc for u in urls] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert urls[0].lastmod == "2026-09-01"
    assert urls[1].lastmod is None
    assert all(not u.is_index for u in urls)


def test_parse_sitemapindex_flags_children():
    urls = parse_sitemap(SITEMAPINDEX)
    assert [u.loc for u in urls] == [
        "https://example.com/sm1.xml",
        "https://example.com/sm2.xml",
    ]
    assert all(u.is_index for u in urls)
    assert urls[0].lastmod == "2026-09-01"


def test_parse_malformed_returns_empty():
    assert parse_sitemap(b"not xml at all") == []
    assert parse_sitemap(b"") == []
    assert parse_sitemap(b"<html><body>nope</body></html>") == []


def test_collect_sitemap_urls_recurses_index():
    payloads = {
        "https://example.com/root.xml": SITEMAPINDEX,
        "https://example.com/sm1.xml": CHILD_SITEMAP,
        "https://example.com/sm2.xml": URLSET,
    }

    async def fake_fetch(url: str) -> bytes | None:
        return payloads.get(url)

    urls = _run(collect_sitemap_urls("https://example.com/root.xml", fetch=fake_fetch))
    locs = [u.loc for u in urls]
    assert "https://example.com/child-page" in locs
    assert "https://example.com/a" in locs
    assert not any(u.is_index for u in urls)


def test_collect_sitemap_urls_respects_max_depth():
    # Chain of indexes: each level points one level deeper.
    payloads = {f"https://example.com/sm{i}.xml": None for i in range(10)}

    def index_xml(i: int) -> bytes:
        return (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>https://example.com/sm{i + 1}.xml</loc></sitemap>"
            "</sitemapindex>"
        ).encode()

    for i in range(10):
        payloads[f"https://example.com/sm{i}.xml"] = index_xml(i)

    async def fake_fetch(url: str) -> bytes | None:
        return payloads.get(url)

    # max_depth=3 → indexes deeper than 3 are not followed, no real URLs found.
    urls = _run(collect_sitemap_urls("https://example.com/sm0.xml", fetch=fake_fetch, max_depth=3))
    assert urls == []


def test_collect_sitemap_fetch_failure_returns_empty():
    async def fake_fetch(url: str) -> bytes | None:
        return None

    assert _run(collect_sitemap_urls("https://x.example/s.xml", fetch=fake_fetch)) == []


# ─── H5: bounded collection ──────────────────────────────────────────────


def test_collect_sitemap_urls_caps_total_urls():
    entries = "".join(f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(15_000))
    big = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + "</urlset>"
    ).encode()

    async def fake_fetch(url: str) -> bytes | None:
        return big

    urls = _run(collect_sitemap_urls("https://example.com/s.xml", fetch=fake_fetch))
    assert len(urls) == 10_000  # SITEMAP_MAX_URLS default


def test_collect_sitemap_urls_env_url_cap(monkeypatch):
    monkeypatch.setenv("SITEMAP_MAX_URLS", "5")
    entries = "".join(f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(100))
    big = f"<urlset>{entries}</urlset>".encode()

    async def fake_fetch(url: str) -> bytes | None:
        return big

    urls = _run(collect_sitemap_urls("https://example.com/s.xml", fetch=fake_fetch))
    assert len(urls) == 5


def test_parse_sitemap_rejects_doctype_and_entity():
    evil = b"""\
<?xml version="1.0"?>
<!DOCTYPE urlset [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
</urlset>
"""
    assert parse_sitemap(evil) == []
    assert parse_feed(evil) == []


def test_parse_sitemap_rejects_utf16_entity_decl():
    # F8: UTF-16 spreads `<!DOCTYPE` across NUL-interleaved bytes — an
    # ASCII byte-regex misses it, but expat's own encoding sniffing does
    # not. The declaration must be caught before ElementTree can expand
    # the entity.
    doc = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE urlset [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.com/a</loc></url></urlset>"
    )
    for enc in ("utf-16", "utf-16-le", "utf-16-be"):
        assert parse_sitemap(doc.encode(enc)) == [], enc
        assert parse_feed(doc.encode(enc)) == [], enc


def test_utf16_sitemap_without_doctype_parses():
    # Encoding alone is not the bug — a clean UTF-16 sitemap still works.
    doc = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.com/a</loc></url></urlset>"
    )
    urls = parse_sitemap(doc.encode("utf-16"))
    assert [u.loc for u in urls] == ["https://example.com/a"]


def test_parse_sitemap_rejects_doctype_system_id():
    # External DTD reference — no internal subset, still a DOCTYPE.
    evil = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE urlset SYSTEM "http://evil.example/dtd">'
        b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    )
    assert parse_sitemap(evil) == []


def test_default_fetch_oversize_raises_clear_error(monkeypatch):
    from crawler.netguard import GuardedResponse

    async def fake_guarded(*a, **kw):
        return GuardedResponse(status=200, oversize=True)

    monkeypatch.setattr("crawler.netguard.guarded_get", fake_guarded)
    with pytest.raises(SitemapTooLargeError):
        _run(seeds._default_sitemap_fetch("https://x.example/big.xml"))


def test_collect_sitemap_urls_bounded_fetches():
    # A fan-out index must not fetch unboundedly: max_fetches bounds it.
    def index_xml(i: int) -> bytes:
        return (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(
                f"<sitemap><loc>https://example.com/c{i}_{j}.xml</loc></sitemap>" for j in range(50)
            )
            + "</sitemapindex>"
        ).encode()

    def child_xml(i: int, j: int) -> bytes:
        return (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>https://example.com/p{i}_{j}</loc></url></urlset>"
        ).encode()

    fetched: list[str] = []

    async def fake_fetch(url: str) -> bytes | None:
        fetched.append(url)
        if url.endswith("root.xml"):
            return index_xml(0)
        parts = url.rsplit("/c", 1)[-1].rstrip(".xml").split("_")
        return child_xml(int(parts[0]), int(parts[1]))

    _run(collect_sitemap_urls("https://example.com/root.xml", fetch=fake_fetch, max_fetches=10))
    assert len(fetched) <= 10


# ─── Feed parsing ────────────────────────────────────────────────────────


def test_parse_rss2():
    urls = parse_feed(RSS2)
    assert [u.loc for u in urls] == [
        "https://example.com/story-1",
        "https://example.com/story-2",
    ]
    assert urls[0].lastmod == "Mon, 21 Sep 2026 08:00:00 +0700"
    assert urls[1].lastmod is None


def test_parse_atom():
    urls = parse_feed(ATOM)
    assert [u.loc for u in urls] == [
        "https://example.com/entry-1",
        "https://example.com/entry-2",
    ]
    assert urls[0].lastmod == "2026-09-21T08:00:00Z"
    assert urls[1].lastmod == "2026-09-20T08:00:00Z"


def test_parse_feed_malformed():
    assert parse_feed(b"junk") == []
    assert parse_feed(b"") == []


# ─── Registrable domain ──────────────────────────────────────────────────


def test_registrable_domain_vn_suffixes():
    assert registrable_domain("vnexpress.net") == "vnexpress.net"
    assert registrable_domain("dantri.com.vn") == "dantri.com.vn"
    assert registrable_domain("www.sbv.gov.vn") == "sbv.gov.vn"
    assert registrable_domain("kinhte.dantri.com.vn") == "dantri.com.vn"
    assert registrable_domain("sub.hanoi.gov.vn") == "hanoi.gov.vn"


def test_registrable_domain_generic():
    assert registrable_domain("example.com") == "example.com"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert registrable_domain("www.example.org") == "example.org"
    assert registrable_domain("") == ""
    assert registrable_domain("localhost") == "localhost"


# ─── VN seeds ────────────────────────────────────────────────────────────


def test_vn_seeds_cover_required_sites():
    joined = " ".join(VN_SEEDS)
    for needle in [
        "chinhphu.vn",
        "tuoitre.vn",
        "vnexpress.net",
        "thanhnien.vn",
        "dantri.com.vn",
        "laodong.vn",
        "vtv.vn",
        "baochinhphu.vn",
        "thuvienphapluat.vn",
        "gso.gov.vn",
        "mof.gov.vn",
        "sbv.gov.vn",
        "customs.gov.vn",
        "moit.gov.vn",
        "mic.gov.vn",
        "moh.gov.vn",
        "moet.gov.vn",
        "hanoi.gov.vn",
        "danang.gov.vn",
    ]:
        assert needle in joined, needle
    assert 18 <= len(VN_SEEDS) <= 40
    # Seeds are homepages — no deep paths.
    for seed in VN_SEEDS:
        assert seed.startswith("https://")
        assert seed.rstrip("/").count("/") == 2, seed


def test_seed_domains_derived():
    assert "vnexpress.net" in SEED_DOMAINS
    assert "dantri.com.vn" in SEED_DOMAINS
    assert "sbv.gov.vn" in SEED_DOMAINS


# ─── load_seeds ──────────────────────────────────────────────────────────


class _FakeFrontier:
    def __init__(self):
        self.tasks = []

    async def enqueue(self, task):
        self.tasks.append(task)
        return "db"


def test_load_seeds_enqueues_high_priority():
    frontier = _FakeFrontier()
    count = _run(load_seeds(frontier, seeds=["https://a.vn", "https://b.vn"]))
    assert count == 2
    for task in frontier.tasks:
        assert task.priority == 1.0
        assert task.discovered_from == "seed"
        assert task.scheduled_at == 0.0  # due immediately
    assert [t.url for t in frontier.tasks] == ["https://a.vn", "https://b.vn"]


def test_load_seeds_default_is_vn_seeds():
    frontier = _FakeFrontier()
    assert _run(load_seeds(frontier)) == len(VN_SEEDS)


def test_sitemap_url_shape():
    entry = SitemapURL(loc="https://x.example", lastmod="2026-01-01", is_index=True)
    assert entry.is_index is True
    # defaults
    assert SitemapURL(loc="https://y.example").is_index is False


# ─── seed sites, lanes, expansion (P4 VN corpus) ────────────────────────


def test_seed_sites_have_lanes():
    from crawler.seeds import SEED_LANES, VN_SEED_SITES, seed_lane

    assert len(VN_SEED_SITES) == len(VN_SEEDS)
    for site in VN_SEED_SITES:
        assert site.lane, site.url  # every seed declared a vertical
    # Lanes key on registrable domain for scope; host-level override keeps
    # chinhphu.vn sub-portals distinct (vanban/congbao = legal).
    assert SEED_LANES["vbpl.vn"] == "legal"
    assert SEED_LANES["chinhphu.vn"] == "government"
    assert SEED_LANES["gso.gov.vn"] == "government"
    assert SEED_LANES["hanoi.gov.vn"] == "administrative"
    assert SEED_LANES["vnexpress.net"] == "news"
    assert SEED_LANES["cafef.vn"] == "market"
    assert SEED_LANES["foody.vn"] == "places"
    # seed_lane accepts URLs and bare hosts, incl. www-prefixed hosts.
    assert seed_lane("https://www.vbpl.vn/vanban/x") == "legal"
    assert seed_lane("vanban.chinhphu.vn") == "legal"
    assert seed_lane("congbao.chinhphu.vn") == "legal"
    assert seed_lane("chinhphu.vn") == "government"  # parent keeps its lane
    assert seed_lane("hanoi.gov.vn") == "administrative"
    assert seed_lane("https://unseeded.example/article") is None


def test_vn_seeds_cover_legal_first_party():
    # P4 corpus needs first-party legal + gazette domains as seeds.
    joined = " ".join(VN_SEEDS)
    for needle in ["vbpl.vn", "vanban.chinhphu.vn", "congbao.chinhphu.vn"]:
        assert needle in joined, needle


class _FakeRobots:
    def __init__(self, sitemaps=None):
        self._sitemaps = sitemaps or {}

    async def sitemaps(self, host):
        return list(self._sitemaps.get(host, []))


def test_expand_seeds_feeds_and_sitemaps():
    from crawler.seeds import SeedSite, expand_seeds

    site = SeedSite("https://example.com", "news", ("https://example.com/custom.feed",))
    bodies = {
        "https://example.com/robots.xml": SITEMAPINDEX,  # robots-declared
        "https://example.com/custom.feed": RSS2,  # explicit extra
        "https://example.com/sitemap.xml": URLSET,  # default probe
        "https://example.com/sm1.xml": CHILD_SITEMAP,  # index child
    }

    async def fake_fetch(url):
        return bodies.get(url)

    robots = _FakeRobots({"example.com": ["https://example.com/robots.xml"]})
    frontier = _FakeFrontier()
    counts = _run(
        expand_seeds(
            frontier,
            fetch=fake_fetch,
            robots=robots,
            sites=(site,),
            per_site_cap=50,
        )
    )
    enqueued = {t.url: t for t in frontier.tasks}
    # 3 urlset pages + 2 feed items + 1 recursed index child.
    assert counts == {"example.com": 6}
    assert "https://example.com/a" in enqueued
    assert "https://example.com/story-1" in enqueued
    assert "https://example.com/story-2" in enqueued
    assert "https://example.com/child-page" in enqueued
    for task in frontier.tasks:
        assert task.discovered_from == "sitemap:example.com"
        assert task.priority == 0.6


FOREIGN_URLSET = b"""\
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://other.example/x</loc></url>
</urlset>"""


def test_expand_seeds_respects_domain_scope_and_cap():
    from crawler.seeds import SeedSite, expand_seeds

    site = SeedSite("https://example.com", "general")
    bodies = {
        "https://example.com/sitemap.xml": URLSET,  # 3 same-domain pages
        "https://example.com/sitemap_index.xml": FOREIGN_URLSET,  # 1 off-domain
    }

    async def fake_fetch(url):
        return bodies.get(url)

    frontier = _FakeFrontier()
    counts = _run(expand_seeds(frontier, fetch=fake_fetch, sites=(site,), per_site_cap=10))
    # Foreign URL from the second doc is scope-filtered, not enqueued.
    assert counts["example.com"] == 3
    for task in frontier.tasks:
        assert task.url.startswith("https://example.com")

    frontier = _FakeFrontier()
    counts = _run(expand_seeds(frontier, fetch=fake_fetch, sites=(site,), per_site_cap=1))
    assert counts["example.com"] == 1


def test_expand_seeds_empty_when_nothing_serves():
    from crawler.seeds import SeedSite, expand_seeds

    async def fake_fetch(url):
        return None

    frontier = _FakeFrontier()
    counts = _run(expand_seeds(frontier, fetch=fake_fetch, sites=(SeedSite("https://x.example"),)))
    assert counts == {"x.example": 0}
    assert frontier.tasks == []


RSS_VNE = b"""\
<rss version="2.0"><channel>
<item><link>https://vnexpress.net/bai-viet-1</link></item>
<item><link>https://vnexpress.net/bai-viet-2</link></item>
</channel></rss>"""


def test_load_seeds_expand_deep_seeds():
    frontier = _FakeFrontier()

    async def fake_fetch(url):
        # Only vnexpress' declared extra feed answers; all probes 404.
        return RSS_VNE if url == "https://vnexpress.net/rss/tin-moi-nhat.rss" else None

    count = _run(load_seeds(frontier, expand=True, fetch=fake_fetch))
    assert count == len(VN_SEEDS)
    seed_tasks = [t for t in frontier.tasks if t.discovered_from == "seed"]
    assert len(seed_tasks) == len(VN_SEEDS)
    sitemap_tasks = [t for t in frontier.tasks if t.discovered_from.startswith("sitemap:")]
    assert len(sitemap_tasks) == 2
    for t in sitemap_tasks:
        assert t.discovered_from == "sitemap:vnexpress.net"
        assert t.url.startswith("https://vnexpress.net")


def test_module_has_no_extra_deps():
    # seeds must stay stdlib-only (xml.etree, no feedparser).
    assert "feedparser" not in vars(seeds)
