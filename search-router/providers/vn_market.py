"""VN market lanes — gold, FX, and index quotes as provider rows (P9).

Keyless public endpoints, one ``SearchResultItem`` per quote row so the
federation layer treats a market tick exactly like any other result
(same contract, same circuit breaker — a walled or dead source trips
its own provider, never the lane):

- ``vn_gold``  — webgia.com SJC board (sjc.com.vn itself is
  Cloudflare-walled; webgia mirrors the SJC table: ``label buy sell``).
- ``vn_fx``    — open.er-api.com USD base → VND plus computed crosses.
- ``vn_stock`` — VNDirect dchart history for VN-INDEX/VN30/HNX/UPCOM.

Query relevance is a soft keyword filter: a query mentioning "vàng"/"SJC"
keeps gold rows, "USD"/"tỷ giá" keeps FX rows; unmatched queries get the
whole quote set (broad queries like "thị trường hôm nay" want it all).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx
from models import SearchResultItem

from providers.vn_rss import _fold

_UA = {"User-Agent": "Mozilla/5.0 (search-hub/3.0; market lane)"}
_TIMEOUT = 15.0

WEBGIA_SJC_URL = "https://webgia.com/gia-vang/sjc/"
ER_API_URL = "https://open.er-api.com/v6/latest/USD"
VNDIRECT_URL = "https://dchart-api.vndirect.com.vn/dchart/history"

_VND_NUM_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3})+)")


@dataclass(frozen=True)
class _Quote:
    title: str
    description: str
    url: str
    engine: str
    keywords: frozenset[str]
    published_date: str | None = None


# ─── gold (webgia SJC table) ────────────────────────────────────────────────


def _parse_webgia_sjc(html: str) -> list[_Quote]:
    """Table rows like ``'Vàng SJC 1L, 10L, 1KG 14.140.000 14.440.000'``."""
    out: list[_Quote] = []
    for row in re.findall(r"<tr[^>]*>(.{20,1500}?)</tr>", html, re.S | re.I):
        cells = re.sub(r"<[^>]+>", " ", row)
        cells = re.sub(r"\s+", " ", cells).strip()
        nums = _VND_NUM_RE.findall(cells)
        if len(nums) < 2:
            continue
        label = _VND_NUM_RE.sub("", cells).strip(" .")
        if not label:
            continue
        buy, sell = nums[0], nums[1]
        kw = frozenset(_fold(label).split()) | {"vang", "sjc", "gold"}
        out.append(
            _Quote(
                title=f"{label}: mua {buy} · bán {sell} ₫/lượng",
                description=f"Giá vàng {label} — mua {buy} đ, bán {sell} đ trên lượng.",
                url=WEBGIA_SJC_URL,
                engine="webgia",
                keywords=kw,
            )
        )
    return out


async def _fetch_gold(call_error: list[str]) -> list[_Quote]:
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_UA, follow_redirects=True
        ) as client:
            resp = await client.get(WEBGIA_SJC_URL)
        if resp.status_code != 200:
            call_error.append(f"gold HTTP {resp.status_code}")
            return []
    except httpx.HTTPError as exc:
        call_error.append(str(exc))
        return []
    rows = _parse_webgia_sjc(resp.text)
    if not rows:
        call_error.append("gold parse: no rows")
    return rows


# ─── FX (open.er-api) ────────────────────────────────────────────────────────

_FX_PAIRS = (
    ("USD", "Đô la Mỹ", "usd|dollar|do la|ngoai te"),
    ("EUR", "Euro", "eur|euro"),
    ("JPY", "Yên Nhật (×100)", "jpy|yen|nhat"),
    ("CNY", "Nhân dân tệ", "cny|rmb|trung quoc|nhan dan te"),
    ("GBP", "Bảng Anh", "gbp|bang anh|pound"),
    ("AUD", "Đô la Úc", "aud|uc"),
)


async def _fetch_fx(call_error: list[str]) -> list[_Quote]:
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_UA, follow_redirects=True
        ) as client:
            resp = await client.get(ER_API_URL)
        if resp.status_code != 200:
            call_error.append(f"fx HTTP {resp.status_code}")
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        call_error.append(str(exc))
        return []
    rates = data.get("rates") or {}
    vnd = rates.get("VND")
    if not vnd:
        call_error.append("fx: no VND rate")
        return []
    as_of = data.get("time_last_update_utc") or ""
    out: list[_Quote] = []
    for code, label, kw_raw in _FX_PAIRS:
        rate = rates.get(code)
        if not rate:
            continue
        per_vnd = vnd / rate  # one unit of `code` in VND
        shown = per_vnd * 100 if code == "JPY" else per_vnd
        kw = frozenset(kw_raw.split("|")) | {"ty gia", "exchange", "forex"}
        out.append(
            _Quote(
                title=f"{label}: {shown:,.0f} ₫".replace(",", "."),
                description=(
                    f"Tỷ giá {label} ({code}) — khoảng {shown:,.0f} VND"
                    f"{' (100 yên)' if code == 'JPY' else ''}."
                ).replace(",", "."),
                url="https://www.exchangerate-api.com",
                engine="er-api",
                keywords=kw | {code.lower()},
                published_date=as_of or None,
            )
        )
    return out


# ─── indices (VNDirect dchart) ───────────────────────────────────────────────

_STOCK_SYMBOLS = (
    ("VNINDEX", "VN-Index", "vn-index|vnindex|chung khoan|hose"),
    ("VN30", "VN30", "vn30"),
    ("HNXINDEX", "HNX-Index", "hnx|hnxindex|hnx-index"),
    ("UPCOM", "UPCOM-Index", "upcom"),
)


async def _fetch_stock(call_error: list[str]) -> list[_Quote]:
    now = int(time.time())
    out: list[_Quote] = []
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_UA, follow_redirects=True
        ) as client:
            for symbol, label, kw_raw in _STOCK_SYMBOLS:
                resp = await client.get(
                    VNDIRECT_URL,
                    params={
                        "symbol": symbol,
                        "resolution": "D",
                        "from": now - 86400 * 10,
                        "to": now,
                    },
                )
                if resp.status_code != 200:
                    call_error.append(f"{symbol} HTTP {resp.status_code}")
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    call_error.append(f"{symbol}: non-JSON body")
                    continue
                closes = data.get("c") or []
                times = data.get("t") or []
                if not closes:
                    call_error.append(f"{symbol}: no candles")
                    continue
                last = closes[-1]
                prev = closes[-2] if len(closes) > 1 else last
                delta = (last - prev) / prev * 100 if prev else 0.0
                sign = "+" if delta >= 0 else ""
                as_of = time.strftime("%Y-%m-%d", time.gmtime(times[-1])) if times else None
                kw = frozenset(kw_raw.split("|")) | {
                    "chung khoan",
                    "co phieu",
                    "thanh khoan",
                }
                out.append(
                    _Quote(
                        title=f"{label}: {last:,.2f} ({sign}{delta:.2f}%)",
                        description=(
                            f"Chỉ số {label} ({symbol}) đóng cửa "
                            f"{last:,.2f} điểm, {sign}{delta:.2f}% so với "
                            "phiên trước."
                        ),
                        url=f"https://dstock.vndirect.com.vn/{symbol.lower()}",
                        engine="vndirect",
                        keywords=kw | {symbol.lower()},
                        published_date=as_of,
                    )
                )
    except httpx.HTTPError as exc:
        call_error.append(str(exc))
    return out


# ─── lane dispatcher ─────────────────────────────────────────────────────────

MARKET_SOURCES: dict[str, dict] = {
    "vn_gold": {"fetch": _fetch_gold, "label": "Giá vàng (webgia/SJC)"},
    "vn_fx": {"fetch": _fetch_fx, "label": "Tỷ giá ngoại tệ (ER-API)"},
    "vn_stock": {"fetch": _fetch_stock, "label": "Chứng khoán (VNDirect)"},
}


def _relevant(query: str, quotes: list[_Quote]) -> list[_Quote]:
    """Keep quotes whose keywords intersect the folded query; else all."""
    tokens = set(_fold(query or "").replace("|", " ").split())
    if not tokens:
        return quotes
    matched = [q for q in quotes if q.keywords & tokens]
    return matched or quotes


async def vn_market_search(
    source: str,
    query: str,
    *,
    max_results: int = 10,
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """One call shape for every market source — same contract as vn_rss."""
    cfg = MARKET_SOURCES.get(source)
    if cfg is None:
        if call_error is not None:
            call_error.append(f"unknown_market_source:{source}")
        return []
    sink: list[str] = [] if call_error is None else call_error
    quotes = _relevant(query, await cfg["fetch"](sink))
    out: list[SearchResultItem] = []
    for i, q in enumerate(quotes[:max_results]):
        out.append(
            SearchResultItem(
                url=q.url,
                title=q.title,
                description=q.description,
                engine=q.engine,
                category="market",
                published_date=q.published_date,
                score=round(max(0.6, 1.0 - i * 0.03), 3),
            )
        )
    return out


async def vn_market_health(source: str) -> bool:
    """Probe the source's endpoint once; cheap, no parse."""
    cfg = MARKET_SOURCES.get(source)
    if cfg is None:
        return False
    url = {
        "vn_gold": WEBGIA_SJC_URL,
        "vn_fx": ER_API_URL,
        "vn_stock": f"{VNDIRECT_URL}?symbol=VNINDEX&resolution=D",
    }[source]
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=_UA, follow_redirects=True) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
