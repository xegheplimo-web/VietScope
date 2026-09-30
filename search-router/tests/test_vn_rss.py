"""VN RSS providers (P2) — feed parsing, accent-folded filtering, registry wiring.

HTTP is mocked via ``patch(httpx.AsyncClient.get)`` — no real network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import providers.vn_rss as _vn_rss
from core.provider_registry import (
    PROVIDER_SPECS,
    ProviderRegistry,
    ProviderSearchQuery,
    VnRssProvider,
    _spec_from_mapping,
    create_default_registry,
)
from core.query_understanding import QueryProfile
from core.source_router import VERY_HIGH, SourceRouter
from providers.base import SourceType

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title>
<item>
  <title>Giá vàng hôm nay tăng mạnh</title>
  <link>https://vnexpress.net/gia-vang-hom-nay-123.html</link>
  <description>Vàng SJC đạt mốc mới trong phiên sáng.</description>
  <pubDate>Thu, 24 Sep 2026 02:00:00 +0700</pubDate>
</item>
<item>
  <title>Đội tuyển Việt Nam thắng trận giao hữu</title>
  <link>https://vnexpress.net/doi-tuyen-124.html</link>
  <description>Kết quả mới nhất từ sân.</description>
  <pubDate>Thu, 24 Sep 2026 01:00:00 +0700</pubDate>
</item>
</channel></rss>
"""

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>Nghị định mới về hóa đơn điện tử</title>
  <link href="https://nhandan.vn/nghi-dinh-999.htm"/>
  <summary>Chính phủ ban hành nghị định.</summary>
  <published>2026-09-24T01:30:00+07:00</published>
</entry>
</feed>
"""


class _Resp:
    def __init__(self, status_code: int = 200, text: str = _RSS):
        self.status_code = status_code
        self.text = text


def _run(coro):
    return asyncio.run(coro)


def _fake_get(text: str = _RSS, status: int = 200):
    async def get(self, url, **kwargs):
        return _Resp(status, text)

    return get


class TestFeedSearch:
    def test_rss_parses_and_matches(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            items = _run(_vn_rss.vn_feed_search("vnexpress", "giá vàng", 10))
        assert len(items) == 1
        assert items[0].url.endswith("gia-vang-hom-nay-123.html")
        assert items[0].published_date == "2026-09-24"

    def test_accent_folded_query_matches(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            items = _run(_vn_rss.vn_feed_search("vnexpress", "gia vang", 10))
        assert len(items) == 1

    def test_unrelated_query_is_empty_not_error(self):
        sink: list[str] = []
        items = _run(_vn_rss.vn_feed_search("vnexpress", "python traceback debug", 10, sink))
        assert items == []
        assert sink == []  # empty ≠ error for the circuit breaker

    def test_http_error_reports_call_error(self):
        sink: list[str] = []
        with patch("httpx.AsyncClient.get", new=_fake_get(status=503)):
            items = _run(_vn_rss.vn_feed_search("vnexpress", "giá vàng", 10, sink))
        assert items == []
        assert sink == ["HTTP 503"]

    def test_transport_error_reports_call_error(self):
        async def boom(self, url, **kwargs):
            raise TimeoutError("dead")

        sink: list[str] = []
        with patch("httpx.AsyncClient.get", new=boom):
            items = _run(_vn_rss.vn_feed_search("vnexpress", "giá vàng", 10, sink))
        assert items == []
        assert sink and "TimeoutError" in sink[0]

    def test_atom_feed_parses(self):
        sink: list[str] = []
        with patch("httpx.AsyncClient.get", new=_fake_get(text=_ATOM)):
            items = _run(_vn_rss.vn_feed_search("nhandan", "nghị định hóa đơn", 10, sink))
        assert len(items) == 1
        assert "nghi-dinh-999" in items[0].url

    def test_unknown_feed(self):
        sink: list[str] = []
        assert _run(_vn_rss.vn_feed_search("nope", "x", 5, sink)) == []
        assert sink == ["unknown_feed:nope"]

    def test_max_results_caps(self):
        big = _RSS.replace(
            "</channel>",
            "".join(
                f"<item><title>giá vàng {i}</title><link>https://x/{i}</link></item>"
                for i in range(30)
            )
            + "</channel>",
        )
        with patch("httpx.AsyncClient.get", new=_fake_get(text=big)):
            items = _run(_vn_rss.vn_feed_search("vnexpress", "giá vàng", 5))
        assert len(items) == 5


class TestRegistryWiring:
    def test_every_feed_has_a_spec(self):
        for name in _vn_rss.VN_FEEDS:
            spec = _spec_from_mapping(name, PROVIDER_SPECS[name])
            assert spec.countries == ["VN"] and spec.languages == ["vi"]
            assert SourceType.news in spec.source_types

    def test_default_registry_registers_feeds(self):
        reg = create_default_registry()
        names = dict(reg.all())
        for name in _vn_rss.VN_FEEDS:
            assert name in names
            assert isinstance(names[name], VnRssProvider)

    def test_router_picks_vn_feeds_for_vi_news_query(self):
        reg = ProviderRegistry()
        for name in _vn_rss.VN_FEEDS:
            reg.register(name, VnRssProvider(name), _spec_from_mapping(name, PROVIDER_SPECS[name]))
        plan = SourceRouter().plan(
            "tin tức mới nhất", QueryProfile(language="vi", freshness_required=True), reg
        )
        picks = set(plan.provider_names)
        # Fan-out cap drops the lowest-priority overflow — everything else picked.
        assert len(picks) >= 5
        assert picks <= set(_vn_rss.VN_FEEDS)
        dropped = set(_vn_rss.VN_FEEDS) - picks
        assert all(dict(plan.skipped)[n] == "budget_cap" for n in dropped)
        assert plan.lane_weights[SourceType.news] == VERY_HIGH

    def test_vi_feeds_skipped_for_en_query(self):
        reg = ProviderRegistry()
        for name in _vn_rss.VN_FEEDS:
            reg.register(name, VnRssProvider(name), _spec_from_mapping(name, PROVIDER_SPECS[name]))
        plan = SourceRouter().plan("latest tech news", QueryProfile(language="en"), reg)
        assert set(plan.provider_names) == set()
        assert dict(plan.skipped)["vnexpress"] == "locale_mismatch"

    def test_gov_feed_picked_on_legal_query(self):
        reg = ProviderRegistry()
        reg.register(
            "nhandan",
            VnRssProvider("nhandan"),
            _spec_from_mapping("nhandan", PROVIDER_SPECS["nhandan"]),
        )
        plan = SourceRouter().plan("nghị định mới về hóa đơn", QueryProfile(language="vi"), reg)
        assert plan.provider_names == ["nhandan"]
        assert plan.lane_weights[SourceType.government] == VERY_HIGH


class TestAdapterOutput:
    def test_provider_result_contract(self):
        provider = VnRssProvider("vnexpress")
        sq = ProviderSearchQuery(query="giá vàng", lang="vi")
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            results = _run(provider.search(sq))
        assert results
        r = results[0]
        assert r.source == "vnexpress"
        assert r.source_type == SourceType.news.value
        assert r.language == "vi"
        assert r.country == "VN"
        assert r.url and r.title

    def test_health(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            assert _run(VnRssProvider("vnexpress").health()) is True
        with patch("httpx.AsyncClient.get", new=_fake_get(status=500)):
            assert _run(VnRssProvider("vnexpress").health()) is False
