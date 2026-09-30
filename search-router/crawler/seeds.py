"""Seed registry + sitemap/feed parsers for the crawl frontier.

``VN_SEEDS`` is the curated list of high-quality Vietnamese sources whose
domains bound the crawler's scope (link discovery stays inside these
registrable domains). ``parse_sitemap`` / ``parse_feed`` are stdlib-only
(``xml.etree.ElementTree`` — no feedparser), namespace-tolerant, and never
raise on malformed input: bad XML yields an empty list.

Sitemap collection is bounded end to end (H5): each fetch streams through
``netguard.guarded_get`` — per-hop SSRF validation and a hard byte cap
(``SITEMAP_MAX_BYTES``, default 2 MiB) — and the collector stops at
``SITEMAP_MAX_URLS`` total page URLs (default 10 000). XML carrying a
``<!DOCTYPE``/``<!ENTITY`` declaration is rejected outright (entity
expansion); parsed elements are cleared after use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

_MAX_SITEMAP_DEPTH = 3
_SITEMAP_MAX_BYTES = 2 * 1024 * 1024
_SITEMAP_MAX_URLS = 10_000


class _EntityDecl(Exception):
    """Raised from expat callbacks when a DOCTYPE/ENTITY declaration appears."""


def _max_sitemap_bytes() -> int:
    return int(os.getenv("SITEMAP_MAX_BYTES", str(_SITEMAP_MAX_BYTES)))


def _max_sitemap_urls() -> int:
    return int(os.getenv("SITEMAP_MAX_URLS", str(_SITEMAP_MAX_URLS)))


class SitemapTooLargeError(Exception):
    """A fetched sitemap exceeded the byte cap — truncated, never parsed."""


@dataclass
class SitemapURL:
    """One entry from a sitemap or feed."""

    loc: str
    lastmod: str | None = None
    is_index: bool = False  # True → child sitemap inside a <sitemapindex>


# Second-level public suffixes common in Vietnam + a few international ones.
# A naive last-two-labels split would turn ``dantri.com.vn`` into ``com.vn``.
_SECOND_LEVEL_SUFFIXES = {
    # Vietnam
    "com.vn",
    "net.vn",
    "org.vn",
    "gov.vn",
    "edu.vn",
    "ac.vn",
    "info.vn",
    "biz.vn",
    "name.vn",
    "pro.vn",
    "health.vn",
    "int.vn",
    # Common international second-level suffixes
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "co.jp",
    "or.jp",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "com.sg",
    "com.my",
    "co.th",
    "com.tw",
    "co.kr",
    "com.hk",
    "com.cn",
    "gov.cn",
}


def registrable_domain(host: str) -> str:
    """Return the eTLD+1 for ``host`` (naive, suffix-table based).

    ``kinhte.dantri.com.vn`` → ``dantri.com.vn``; ``www.sbv.gov.vn`` →
    ``sbv.gov.vn``. Single-label hosts (``localhost``) return unchanged.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last2 = ".".join(labels[-2:])
    if last2 in _SECOND_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last2


def _local(tag: str) -> str:
    """Strip an XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem: ET.Element, name: str) -> str | None:
    target = name.lower()
    for child in elem:
        if _local(child.tag) == target and child.text and child.text.strip():
            return child.text.strip()
    return None


def _has_entity_decl(xml_bytes: bytes) -> bool:
    """True when the XML carries a DOCTYPE/ENTITY declaration — any encoding.

    The check runs *inside* expat: ``StartDoctypeDeclHandler`` and
    ``EntityDeclHandler`` fire after expat's own encoding detection
    (BOM sniffing + the XML declaration), so a UTF-16/UTF-32 document
    cannot smuggle a ``<!DOCTYPE`` past a byte-level scan the way the
    old ASCII regex could. Any document that can declare entities is
    rejected rather than parsed — entity expansion (billion-laughs) is
    the surface. A document expat cannot even tokenize reports False and
    is left for ``ET.fromstring`` to reject on its own.
    """
    parser = expat.ParserCreate()

    def _reject(*_args) -> None:
        raise _EntityDecl

    parser.StartDoctypeDeclHandler = _reject
    parser.EntityDeclHandler = _reject
    try:
        parser.Parse(xml_bytes, True)
    except _EntityDecl:
        return True
    except expat.ExpatError:
        return False  # malformed — the real parse rejects it downstream
    return False


def parse_sitemap(xml_bytes: bytes) -> list[SitemapURL]:
    """Parse a ``<urlset>`` or ``<sitemapindex>`` document.

    Index entries are returned flagged ``is_index=True`` — fetching and
    recursing into them is ``collect_sitemap_urls``'s job (bounded by
    ``max_depth``). Malformed, entity-bearing, or foreign XML returns [].
    """
    if not xml_bytes or _has_entity_decl(xml_bytes):
        return []
    try:
        # spec: stdlib xml.etree only (no defusedxml dep) — entity-bearing
        # input is rejected above; feeds are bounded upstream by the
        # fetcher's size cap.
        root = ET.fromstring(xml_bytes)  # noqa: S314
    except ET.ParseError:
        return []

    root_name = _local(root.tag)
    out: list[SitemapURL] = []
    if root_name == "urlset":
        for elem in root:
            if _local(elem.tag) != "url":
                continue
            loc = _child_text(elem, "loc")
            if loc:
                out.append(SitemapURL(loc=loc, lastmod=_child_text(elem, "lastmod")))
            elem.clear()  # free parsed subtree as we go
    elif root_name == "sitemapindex":
        for elem in root:
            if _local(elem.tag) != "sitemap":
                continue
            loc = _child_text(elem, "loc")
            if loc:
                out.append(SitemapURL(loc=loc, lastmod=_child_text(elem, "lastmod"), is_index=True))
            elem.clear()
    return out


def parse_feed(xml_bytes: bytes) -> list[SitemapURL]:
    """Parse an RSS 2.0 or Atom feed into SitemapURL entries.

    RSS: ``channel/item`` → ``link`` + ``pubDate``.
    Atom: ``entry`` → ``link[@href]`` (``rel="alternate"`` preferred) +
    ``updated``/``published``. Malformed, entity-bearing, or foreign XML
    returns [].
    """
    if not xml_bytes or _has_entity_decl(xml_bytes):
        return []
    try:
        root = ET.fromstring(xml_bytes)  # noqa: S314 — see parse_sitemap
    except ET.ParseError:
        return []

    out: list[SitemapURL] = []
    root_name = _local(root.tag)
    if root_name == "rss":
        for channel in root:
            if _local(channel.tag) != "channel":
                continue
            for item in channel:
                if _local(item.tag) != "item":
                    continue
                loc = _child_text(item, "link")
                if loc:
                    out.append(
                        SitemapURL(
                            loc=loc,
                            lastmod=_child_text(item, "pubDate") or _child_text(item, "date"),
                        )
                    )
    elif root_name == "feed":  # Atom
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            loc = None
            fallback = None
            for child in entry:
                if _local(child.tag) != "link":
                    continue
                href = child.attrib.get("href", "").strip()
                if not href:
                    continue
                rel = child.attrib.get("rel", "alternate")
                if rel == "alternate":
                    loc = href
                    break
                fallback = fallback or href
            loc = loc or fallback
            if loc:
                out.append(
                    SitemapURL(
                        loc=loc,
                        lastmod=_child_text(entry, "updated") or _child_text(entry, "published"),
                    )
                )
    return out


_GUARD = None


def _netguard():
    """Shared NetGuard for default sitemap fetches (lazy)."""
    global _GUARD
    if _GUARD is None:
        from crawler.netguard import NetGuard

        _GUARD = NetGuard()
    return _GUARD


async def _default_sitemap_fetch(u: str) -> bytes | None:
    """Default sitemap GET — SSRF-validated per hop, streamed, ≤ cap.

    Raises ``SitemapTooLargeError`` past ``SITEMAP_MAX_BYTES`` so an
    oversized sitemap surfaces as a clear error instead of a silently
    truncated parse.
    """
    from crawler.netguard import guarded_client, guarded_get
    from crawler.robots import DEFAULT_USER_AGENT

    try:
        async with guarded_client(_netguard(), timeout=15.0) as client:
            resp = await guarded_get(
                client,
                u,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/xml,text/xml,*/*;q=0.1",
                },
                timeout=15.0,
                max_redirects=5,
                max_bytes=_max_sitemap_bytes(),
                netguard=_netguard(),
            )
            if resp.oversize:
                raise SitemapTooLargeError(
                    f"{u} exceeds sitemap cap ({_max_sitemap_bytes()} bytes)"
                )
            if resp.error or resp.status != 200:
                return None
            return resp.body
    except SitemapTooLargeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("sitemap fetch failed for %s: %r", u, exc)
        return None


async def collect_sitemap_urls(
    url: str,
    *,
    fetch=None,
    max_depth: int = _MAX_SITEMAP_DEPTH,
    max_urls: int | None = None,
    max_fetches: int | None = None,
    _depth: int = 0,
    _seen: set[str] | None = None,
) -> list[SitemapURL]:
    """Fetch ``url`` and return real page URLs, recursing into sitemap indexes.

    ``fetch`` is an injectable ``async (url) -> bytes | None`` — defaults to
    ``_default_sitemap_fetch`` (netguard + 2 MiB cap). Indexes deeper than
    ``max_depth`` are not followed, a URL is never fetched twice
    (cycle-safe), at most ``max_fetches`` sitemap documents are fetched
    (default ``SITEMAP_MAX_FETCHES``=1024), and at most ``max_urls`` page
    URLs are returned (default ``SITEMAP_MAX_URLS``=10 000).
    """
    if fetch is None:
        fetch = _default_sitemap_fetch
    if max_urls is None:
        max_urls = _max_sitemap_urls()
    if max_fetches is None:
        max_fetches = int(os.getenv("SITEMAP_MAX_FETCHES", "1024"))

    seen = _seen if _seen is not None else set()
    if url in seen or len(seen) >= max_fetches:
        return []
    seen.add(url)

    body = await fetch(url)
    if not body:
        return []
    entries = parse_sitemap(body)

    out: list[SitemapURL] = []
    for entry in entries:
        if len(out) >= max_urls:
            break
        if not entry.is_index:
            out.append(entry)
        elif _depth + 1 <= max_depth:
            out.extend(
                await collect_sitemap_urls(
                    entry.loc,
                    fetch=fetch,
                    max_depth=max_depth,
                    max_urls=max_urls - len(out),
                    max_fetches=max_fetches,
                    _depth=_depth + 1,
                    _seen=seen,
                )
            )
    return out[:max_urls]


# ─── VN seed registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeedSite:
    """One seed: homepage URL plus the vertical its corpus belongs to.

    ``lane`` is a SourceType lane name (news/government/legal/market/…)
    and flows into ``documents.metadata.source_lane`` at ingest, giving
    the own-index corpus the same vertical authority/freshness context
    that P5-VN applies to federated results. ``extra`` carries known
    sitemap/feed URLs that are worth probing beyond the defaults.
    """

    url: str
    lane: str = "general"
    extra: tuple[str, ...] = ()


# Seed set — homepage seeds; link discovery expands inside each seed's
# registrable domain, and ``expand_seeds`` can deep-seed from the site's
# sitemaps/feeds. Lanes follow the P1 taxonomy (SourceType names).
VN_SEED_SITES: tuple[SeedSite, ...] = (
    # ── government / administrative ──────────────────────────────────────
    SeedSite("https://chinhphu.vn", "government"),
    SeedSite("https://baochinhphu.vn", "government"),
    SeedSite("https://xaydungchinhsach.chinhphu.vn", "government"),
    SeedSite("https://www.gso.gov.vn", "government"),
    SeedSite("https://mof.gov.vn", "government"),
    SeedSite("https://sbv.gov.vn", "government"),
    SeedSite("https://www.customs.gov.vn", "government"),
    SeedSite("https://moit.gov.vn", "government"),
    SeedSite("https://mic.gov.vn", "government"),
    SeedSite("https://moh.gov.vn", "government"),
    SeedSite("https://moet.gov.vn", "government"),
    SeedSite("https://hanoi.gov.vn", "administrative"),
    SeedSite("https://hochiminhcity.gov.vn", "administrative"),
    SeedSite("https://danang.gov.vn", "administrative"),
    # ── legal (first-party statute + gazette) ────────────────────────────
    SeedSite("https://vbpl.vn", "legal"),
    SeedSite("https://vanban.chinhphu.vn", "legal"),
    SeedSite("https://congbao.chinhphu.vn", "legal"),
    SeedSite("https://thuvienphapluat.vn", "legal"),
    SeedSite("https://luatvietnam.vn", "legal"),
    # ── news ─────────────────────────────────────────────────────────────
    SeedSite("https://vnexpress.net", "news", ("https://vnexpress.net/rss/tin-moi-nhat.rss",)),
    SeedSite("https://tuoitre.vn", "news", ("https://tuoitre.vn/rss/tin-moi-nhat.rss",)),
    SeedSite("https://thanhnien.vn", "news", ("https://thanhnien.vn/rss/home.rss",)),
    SeedSite("https://dantri.com.vn", "news", ("https://dantri.com.vn/rss/home.rss",)),
    SeedSite("https://laodong.vn", "news", ("https://laodong.vn/rss/home.rss",)),
    SeedSite("https://vtv.vn", "news"),
    SeedSite("https://nhandan.vn", "news"),
    SeedSite("https://vietnamplus.vn", "news", ("https://vietnamplus.vn/rss/home.rss",)),
    SeedSite("https://congthuong.vn", "news", ("https://congthuong.vn/rss/home.rss",)),
    # ── market / finance ─────────────────────────────────────────────────
    SeedSite("https://cafef.vn", "market"),
    SeedSite("https://vietstock.vn", "market"),
    SeedSite("https://sjc.com.vn", "market"),
    # ── places (local listings corpus seed) ──────────────────────────────
    SeedSite("https://foody.vn", "places"),
)

# Homepage-only seeds — kept as a plain URL list for compatibility.
VN_SEEDS: list[str] = [site.url for site in VN_SEED_SITES]

# Registrable domains the crawler stays inside (discovery scope).
SEED_DOMAINS: frozenset[str] = frozenset(
    registrable_domain(urlsplit(seed).netloc) for seed in VN_SEEDS
)

# registrable domain → corpus vertical (first seed wins on collisions).
SEED_LANES: dict[str, str] = {}
_SEED_HOST_LANES: dict[str, str] = {}
for _site in VN_SEED_SITES:
    _host = urlsplit(_site.url).netloc.lower()
    _SEED_HOST_LANES.setdefault(_host, _site.lane)
    SEED_LANES.setdefault(registrable_domain(_host), _site.lane)


def seed_lane(url_or_domain: str) -> str | None:
    """Vertical lane for a URL or host when it belongs to a seed site.

    Exact host wins over the registrable domain so chinhphu.vn
    sub-portals keep their own lanes: ``vanban.``/``congbao.`` are
    ``legal`` while the parent ``chinhphu.vn`` stays ``government``.
    """
    host = urlsplit(url_or_domain).netloc if "://" in url_or_domain else url_or_domain
    host = host.lower().removeprefix("www.")
    return _SEED_HOST_LANES.get(host) or SEED_LANES.get(registrable_domain(host))


# ─── Seed expansion via sitemaps/feeds ──────────────────────────────────

# Default probes when robots.txt declares no Sitemap: line and the seed
# carries no explicit ``extra`` candidates.
_SEED_PROBE_PATHS = (
    "sitemap.xml",
    "sitemap_index.xml",
    "sitemap-index.xml",
    "rss",
    "feed",
    "rss.xml",
    "feed.xml",
)

_SEED_EXPAND_CAP = 500  # page URLs per seed site


def _expand_cap() -> int:
    return int(os.getenv("SEED_EXPAND_CAP", str(_SEED_EXPAND_CAP)))


async def expand_seeds(
    frontier,
    *,
    fetch=None,
    robots=None,
    sites: tuple[SeedSite, ...] | None = None,
    per_site_cap: int | None = None,
) -> dict[str, int]:
    """Deep-seed each seed site from its sitemaps and feeds.

    For every site the candidate documents are (1) ``Sitemap:`` lines from
    its robots.txt (when a ``RobotsCache`` is passed), (2) the seed's
    explicit ``extra`` URLs, and (3) a small list of conventional
    sitemap/feed paths. Each candidate is fetched once, parsed as sitemap
    (urlset/index — indexes recurse through ``collect_sitemap_urls``) or
    feed, and the page URLs are enqueued at ``priority=0.6`` with
    ``discovered_from="sitemap:<registrable-domain>"``. URLs outside the
    seed's registrable domain are never enqueued.

    Returns ``{registrable_domain: enqueued_count}``. Every fetch is
    bounded by the caller's ``fetch`` (default: netguard + byte cap); a
    dead or malformed document yields zero URLs, never an error.
    """
    from workers.freshness_worker import RecrawlTask

    if fetch is None:
        fetch = _default_sitemap_fetch
    cap = _expand_cap() if per_site_cap is None else per_site_cap

    counts: dict[str, int] = {}
    for site in sites or VN_SEED_SITES:
        host = urlsplit(site.url).netloc.lower()
        domain = registrable_domain(host)
        if not domain:
            continue

        docs: list[str] = list(site.extra)
        if robots is not None:
            with contextlib.suppress(Exception):  # robots fetch is best-effort
                docs.extend(await robots.sitemaps(host))
        base = site.url.rstrip("/") + "/"
        docs.extend(urljoin(base, path) for path in _SEED_PROBE_PATHS)

        enqueued = 0
        seen_docs: set[str] = set()
        for doc in docs:
            if enqueued >= cap:
                break
            if doc in seen_docs:
                continue
            seen_docs.add(doc)
            try:
                body = await fetch(doc)
            except SitemapTooLargeError:
                logger.info("seed sitemap over cap, skipped: %s", doc)
                continue
            if not body:
                continue
            entries = parse_sitemap(body) or parse_feed(body)
            queue = list(entries)
            while queue and enqueued < cap:
                entry = queue.pop(0)
                if entry.is_index:
                    try:
                        queue.extend(
                            await collect_sitemap_urls(
                                entry.loc,
                                fetch=fetch,
                                max_urls=cap - enqueued,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 — oversize/parse
                        logger.debug("child sitemap %s skipped: %r", entry.loc, exc)
                    continue
                loc = entry.loc
                if registrable_domain(urlsplit(loc).netloc) != domain:
                    continue
                await frontier.enqueue(
                    RecrawlTask(
                        url=loc,
                        priority=0.6,
                        scheduled_at=0.0,
                        discovered_from=f"sitemap:{domain}",
                    )
                )
                enqueued += 1
        counts[domain] = enqueued
        logger.info("seed %s → %d sitemap/feed URLs", domain, enqueued)
    return counts


async def load_seeds(
    frontier,
    seeds: list[str] | None = None,
    *,
    expand: bool = False,
    fetch=None,
    robots=None,
) -> int:
    """Enqueue all seed URLs at max priority, depth 0, ``discovered_from='seed'``.

    ``expand=True`` additionally deep-seeds each seed site from its
    sitemaps/feeds via ``expand_seeds`` — the homepage still crawls at
    depth 0, the discovered article URLs join at their own depth.
    Returns the number of homepage seeds enqueued (expanded URLs are
    counted in ``expand_seeds``' own return).
    """
    from workers.freshness_worker import RecrawlTask

    count = 0
    for url in seeds if seeds is not None else VN_SEEDS:
        await frontier.enqueue(
            RecrawlTask(
                url=url,
                priority=1.0,
                scheduled_at=0.0,
                discovered_from="seed",
            )
        )
        count += 1
    if expand:
        await expand_seeds(frontier, fetch=fetch, robots=robots)
    return count
