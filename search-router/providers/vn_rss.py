"""VN RSS/Atom providers — direct feeds from Vietnamese public sources (P2).

Every VN source is a ``VN_FEEDS`` row here plus one ``PROVIDER_SPECS``
entry — no new code per source, same ``ProviderResult`` contract.

RSS feeds aren't queryable: the adapter fetches the feed, then filters and
ranks items client-side against the query — Vietnamese accent-folding on
both sides so "gia vang" matches "giá vàng". A feed with no matching item
reports ``empty`` to the health monitor — honest signal, never an error.
"""

from __future__ import annotations

import email.utils
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx
from models import SearchResultItem

# provider name → feed. The name is what goes in PROVIDER_SPECS and what
# shows up as ProviderResult.source (per-source circuit breaking).
# All URLs verified live 2026-09-24. vietnamnet/baochinhphu/laodong/
# vneconomy were probed and have no machine-readable feed (404 or JS/anti-bot).
VN_FEEDS: dict[str, dict[str, str]] = {
    "vnexpress": {
        "url": "https://vnexpress.net/rss/tin-moi-nhat.rss",
        "label": "VnExpress",
    },
    "tuoitre": {
        "url": "https://tuoitre.vn/home.rss",
        "label": "Tuổi Trẻ",
    },
    "thanhnien": {
        "url": "https://thanhnien.vn/rss/home.rss",
        "label": "Thanh Niên",
    },
    "dantri": {
        "url": "https://dantri.com.vn/rss/home.rss",
        "label": "Dân Trí",
    },
    # State media — authoritative for the government lane.
    "nhandan": {
        "url": "https://nhandan.vn/rss/home.rss",
        "label": "Nhân Dân",
    },
    "vietnamplus": {
        "url": "https://vietnamplus.vn/rss/home.rss",
        "label": "VietnamPlus",
    },
    # Ministry of Industry & Trade's newspaper — business/market coverage.
    "congthuong": {
        "url": "https://congthuong.vn/rss/cong-thuong-24h.rss",
        "label": "Công Thương",
    },
}

_TIMEOUT = 15.0
_UA = "search-hub/3.0 (+vn-feed)"


def _fold(s: str) -> str:
    """Fold Vietnamese text for matching — strips accents AND maps đ→d."""
    s = s.replace("đ", "d").replace("Đ", "d")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s.lower() if not unicodedata.combining(c))


def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"\w+", _fold(query or "")) if len(t) >= 2]


def _parse_date(text: str | None) -> tuple[str | None, float]:
    """RSS pubDate (RFC 822) or Atom updated (ISO) → (iso_date, epoch)."""
    if not text:
        return None, 0.0
    text = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return text or None, 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.date().isoformat(), dt.timestamp()


def _text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _iter_items(root: ET.Element) -> list[tuple[str, str, str, str]]:
    """RSS 2.0 <item> and Atom <entry> → (title, url, desc, date_text)."""
    out = []
    for elem in root.iter():
        name = _strip_ns(elem.tag)
        if name == "item" or (name == "entry" and elem.find("{*}title") is not None):
            title = desc = url = date_text = ""
            for child in elem:
                cname = _strip_ns(child.tag)
                if cname == "title":
                    title = _text(child)
                elif cname == "link":
                    url = child.get("href") or _text(child)
                elif cname in ("description", "summary", "content"):
                    desc = desc or _text(child)
                elif cname in ("pubdate", "published", "updated", "date"):
                    date_text = date_text or _text(child)
            out.append((title, url, desc, date_text))
    return out


def _rank(
    items: list[tuple[str, str, str, str]], terms: list[str], phrase: str
) -> list[tuple[SearchResultItem, float]]:
    scored = []
    for title, url, desc, date_text in items:
        if not url:
            continue
        hay = _fold(f"{title} {desc} {url}")
        matched = sum(1 for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", hay))
        score = matched / len(terms) if terms else 0.0
        if phrase and phrase in hay:
            score = 1.0
        if score < 0.5:
            continue
        pub, ts = _parse_date(date_text)
        scored.append(
            (
                SearchResultItem(
                    url=url,
                    title=title,
                    description=desc[:300],
                    score=round(score, 3),
                    published_date=pub,
                ),
                ts,
            )
        )
    scored.sort(key=lambda x: (-x[0].score, -x[1]))
    return scored


async def vn_feed_search(
    feed: str,
    query: str,
    max_results: int = 10,
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """Fetch a VN feed and return items matching the query (accent-free)."""
    cfg = VN_FEEDS.get(feed)
    if cfg is None:
        if call_error is not None:
            call_error.append(f"unknown_feed:{feed}")
        return []
    terms = _terms(query)
    if not terms:
        return []

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(cfg["url"], headers={"User-Agent": _UA})
    except Exception as exc:
        if call_error is not None:
            call_error.append(f"{type(exc).__name__}: {exc}")
        return []
    if resp.status_code != 200:
        if call_error is not None:
            call_error.append(f"HTTP {resp.status_code}")
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        if call_error is not None:
            call_error.append("parse_error")
        return []

    phrase = _fold(query or "").strip()
    return [it for it, _ts in _rank(_iter_items(root), terms, phrase)[:max_results]]


async def vn_feed_health(feed: str) -> bool:
    cfg = VN_FEEDS.get(feed)
    if cfg is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(cfg["url"], headers={"User-Agent": _UA})
            return resp.status_code == 200
    except Exception:
        return False
