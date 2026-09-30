"""P9 — VN market providers: gold / FX / stock quote lanes."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from providers import vn_market
from providers.vn_market import MARKET_SOURCES, vn_market_search

WEBGIA_HTML = """
<table>
<tr><td>Hồ Chí Minh</td><td>Vàng SJC 1L, 10L, 1KG</td><td>14.140.000</td><td>14.440.000</td></tr>
<tr><td>Vàng SJC 5 chỉ</td><td>14.140.000</td><td>14.442.000</td></tr>
<tr><td>Vàng nhẫn SJC 99,99% 1 chỉ, 2 chỉ, 5 chỉ</td><td>14.090.000</td><td>14.390.000</td></tr>
<tr><td colspan="4">no numbers here at all — skipped</td></tr>
</table>
"""

ER_JSON = {
    "result": "success",
    "time_last_update_utc": "Thu, 24 Sep 2026 00:00:00 +0000",
    "rates": {"VND": 26000.0, "USD": 1.0, "EUR": 0.88, "JPY": 158.0, "CNY": 6.7, "GBP": 0.74},
}

VNDIRECT_JSON = {
    "t": [1789603200, 1790208000],
    "c": [1800.0, 1836.0],
    "o": [1790.0, 1802.0],
    "h": [1810.0, 1840.0],
    "l": [1785.0, 1795.0],
    "v": [1, 2],
}


class _Resp:
    def __init__(self, status: int, text: str = "", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _run(coro):
    return asyncio.run(coro)


def test_gold_rows_parsed():
    quotes = vn_market._parse_webgia_sjc(WEBGIA_HTML)
    assert len(quotes) == 3
    assert "mua 14.140.000" in quotes[0].title
    assert "bán 14.440.000" in quotes[0].title


@pytest.mark.parametrize("source", list(MARKET_SOURCES))
def test_unknown_source_reports_error(source):
    err = []
    out = _run(vn_market_search("nope", "x", call_error=err))
    assert out == [] and err and "unknown_market_source" in err[0]


def test_gold_search_returns_items():
    get = AsyncMock(return_value=_Resp(200, text=WEBGIA_HTML))
    with patch("httpx.AsyncClient.get", new=get):
        out = _run(vn_market_search("vn_gold", "giá vàng hôm nay", call_error=[]))
    assert len(out) == 3
    assert out[0].engine == "webgia"
    assert out[0].category == "market"
    assert out[0].url == vn_market.WEBGIA_SJC_URL


def test_gold_http_error_degrades():
    get = AsyncMock(return_value=_Resp(403, text="<html>cf</html>"))
    err = []
    with patch("httpx.AsyncClient.get", new=get):
        out = _run(vn_market_search("vn_gold", "vàng", call_error=err))
    assert out == [] and any("403" in e for e in err)


def test_fx_cross_rates_in_vnd():
    get = AsyncMock(return_value=_Resp(200, payload=ER_JSON))
    with patch("httpx.AsyncClient.get", new=get):
        out = _run(vn_market_search("vn_fx", "tỷ giá hôm nay", call_error=[]))
    titles = [i.title for i in out]
    assert any("Đô la Mỹ" in t and "26.000" in t for t in titles)
    # EUR/VND = 26000/0.88 ≈ 29545
    assert any("Euro" in t and "29" in t for t in titles)
    # JPY shown per 100 yên
    assert any("Yên Nhật (×100)" in t for t in titles)


def test_fx_query_filters_rows():
    get = AsyncMock(return_value=_Resp(200, payload=ER_JSON))
    with patch("httpx.AsyncClient.get", new=get):
        out = _run(vn_market_search("vn_fx", "tỷ giá USD", call_error=[]))
    assert len(out) == 1
    assert "Đô la Mỹ" in out[0].title


def test_stock_delta_computed():
    get = AsyncMock(return_value=_Resp(200, payload=VNDIRECT_JSON))
    with patch("httpx.AsyncClient.get", new=get):
        out = _run(vn_market_search("vn_stock", "chứng khoán", call_error=[]))
    vn = next(i for i in out if "VN-Index" in i.title)
    # 1836 vs 1800 → +2.00%
    assert "+2.00%" in vn.title
    assert vn.published_date == "2026-09-24"


def test_stock_partial_failure_keeps_others():
    calls = {"n": 0}

    async def get(*args, **kw):
        calls["n"] += 1
        params = kw.get("params") or {}
        if params.get("symbol") == "VN30":
            return _Resp(500)
        return _Resp(200, payload=VNDIRECT_JSON)

    with patch("httpx.AsyncClient.get", new=get):
        err = []
        out = _run(vn_market_search("vn_stock", "x", call_error=err))
    # 3 good symbols still returned; VN30 failure recorded
    assert len(out) == 3 and any("VN30" in e for e in err)


def test_stock_non_json_body_degrades_per_symbol():
    calls = {"n": 0}

    async def get(*args, **kw):
        calls["n"] += 1
        params = kw.get("params") or {}
        if params.get("symbol") == "VN30":
            return _Resp(200, text="<html>oops</html>")  # json() raises
        return _Resp(200, payload=VNDIRECT_JSON)

    with patch("httpx.AsyncClient.get", new=get):
        err = []
        out = _run(vn_market_search("vn_stock", "x", call_error=err))
    assert len(out) == 3 and any("non-JSON" in e for e in err)


def test_health_unknown_source():
    assert _run(vn_market.vn_market_health("nope")) is False
