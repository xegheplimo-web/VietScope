"""VN news metasearch — Google News RSS scoped to Vietnamese sources (P3).

Unlike the direct RSS providers (``vn_rss``), Google News' search RSS is
*queryable* server-side: ``/rss/search?q=...&hl=vi&gl=VN&ceid=VN:vi``
returns VN-locale results across hundreds of Vietnamese outlets. A
``site:`` suffix narrows a lane to official domains, which is how the
legal and government lanes get first-party documents (vbpl.vn,
vanban.chinhphu.vn, congbao.chinhphu.vn, *.gov.vn) without scraping
each portal's bespoke search page.

Three lanes, one adapter — adding a scoped lane is one ``GNEWS_LANES``
row plus one ``PROVIDER_SPECS`` entry, same ``ProviderResult`` contract.
Items carry a Google *discovery* URL — ``news.google.com/rss/articles/...``
links no longer 302 to the publisher, so resolving the canonical article
URL is left to the fetch layer and is never assumed from the publisher's
homepage. Publisher identity (``publisher_name``/``publisher_domain``,
from the RSS ``<source>`` element) rides in ``metadata`` so authority
scoring and dedup see the real domain instead of ``news.google.com``.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

import httpx
from models import SearchResultItem

from providers.vn_rss import _fold, _iter_items, _parse_date, _strip_ns, _text

GNEWS_BASE = "https://news.google.com/rss/search"
GNEWS_PARAMS = {"hl": "vi", "gl": "VN", "ceid": "VN:vi"}

# lane name → site-scope suffix appended to the user query. Names land in
# PROVIDER_SPECS and ProviderResult.source.
GNEWS_LANES: dict[str, dict[str, str]] = {
    "gnews_vn": {"suffix": "", "label": "Google News (Việt Nam)"},
    "gnews_vn_gov": {
        "suffix": " site:chinhphu.vn OR site:gov.vn",
        "label": "Google News (.gov.vn)",
    },
    "gnews_vn_legal": {
        "suffix": (
            " site:vbpl.vn OR site:vanban.chinhphu.vn OR"
            " site:congbao.chinhphu.vn OR site:thuvienphapluat.vn OR"
            " site:luatvietnam.vn"
        ),
        "label": "Google News (văn bản pháp luật)",
    },
}

_TIMEOUT = 15.0
_UA = "search-hub/3.0 (+vn-gnews)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_desc(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _iter_entries(root: ET.Element) -> list[tuple[str, str, str, str, str, str]]:
    """Google News <item> → (title, url, desc, date_text, publisher, domain)."""
    base = _iter_items(root)
    items = [e for e in root.iter() if _strip_ns(e.tag) == "item"]
    out = []
    for i, (title, url, desc, date_text) in enumerate(base):
        publisher = domain = ""
        if i < len(items):
            for child in items[i]:
                if _strip_ns(child.tag) == "source":
                    publisher = _text(child)
                    domain = child.get("url") or ""
        out.append((title, url, desc, date_text, publisher, domain))
    return out


def _strip_publisher_suffix(title: str, publisher: str) -> str:
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(f" - {publisher}")].strip()
    return title


def _publisher_domain(source_url: str) -> str:
    """``<source url="https://chinhphu.vn">`` attr → bare domain (``chinhphu.vn``).

    The attr is the publisher's *homepage*, not the article URL — it only
    ever feeds ``metadata.publisher_domain`` (identity), never
    ``canonical_url``.
    """
    if not source_url:
        return ""
    raw = source_url if "://" in source_url else f"https://{source_url}"
    return urllib.parse.urlparse(raw).netloc.lower().removeprefix("www.")


def _publisher_meta(publisher: str, source_url: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if publisher:
        meta["publisher_name"] = publisher
    domain = _publisher_domain(source_url)
    if domain:
        meta["publisher_domain"] = domain
    return meta


def gnews_url(lane: str, query: str) -> str | None:
    cfg = GNEWS_LANES.get(lane)
    if cfg is None:
        return None
    q = f"{query.strip()}{cfg['suffix']}"
    params = urllib.parse.urlencode({"q": q, **GNEWS_PARAMS})
    return f"{GNEWS_BASE}?{params}"


async def gnews_search(
    lane: str,
    query: str,
    *,
    max_results: int = 10,
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """Query Google News' VN RSS lane. Empty feed → [] (``empty``, not error)."""
    if not _fold(query or "").strip():
        return []
    url = gnews_url(lane, query)
    if url is None:
        if call_error is not None:
            call_error.append(f"unknown_gnews_lane:{lane}")
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
    except httpx.HTTPError as exc:
        if call_error is not None:
            call_error.append(str(exc))
        return []
    if resp.status_code != 200:
        if call_error is not None:
            call_error.append(f"HTTP {resp.status_code}")
        return []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        if call_error is not None:
            call_error.append(f"parse_error:{exc}")
        return []

    out: list[SearchResultItem] = []
    for title, url, desc, date_text, publisher, domain in _iter_entries(root):
        if not url:
            continue
        pub, _ts = _parse_date(date_text)
        out.append(
            SearchResultItem(
                url=url,
                title=_strip_publisher_suffix(title, publisher),
                description=_clean_desc(desc)[:300],
                engine=publisher or domain or "google_news",
                metadata=_publisher_meta(publisher, domain),
                published_date=pub,
                # Google orders by relevance already; keep a gentle decay.
                score=round(max(0.4, 1.0 - len(out) * 0.02), 3),
            )
        )
        if len(out) >= max_results:
            break
    return out


async def gnews_health(lane: str) -> bool:
    url = gnews_url(lane, "tin tức")
    if url is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200
