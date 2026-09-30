"""VN Google News lanes (P3) — scoped query parsing, registry wiring.

HTTP is mocked via ``patch(httpx.AsyncClient.get)`` — no real network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import providers.vn_gnews as _gnews
from core.provider_registry import (
    PROVIDER_SPECS,
    ProviderRegistry,
    ProviderSearchQuery,
    VnGNewsProvider,
    _spec_from_mapping,
    create_default_registry,
)
from core.query_understanding import QueryProfile
from core.source_router import VERY_HIGH, SourceRouter
from providers.base import SourceType

_GNEWS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item>
  <title>TOÀN VĂN: Nghị định 254/2026/NĐ-CP về hóa đơn điện tử - xaydungchinhsach.chinhphu.vn</title>
  <link>https://news.google.com/rss/articles/CBMiXwAAA</link>
  <description>&lt;a href="https://chinhphu.vn/nghi-dinh-254"&gt;Nghị định 254/2026/NĐ-CP&lt;/a&gt; quy định về hóa đơn.</description>
  <source url="https://chinhphu.vn">xaydungchinhsach.chinhphu.vn</source>
  <pubDate>Thu, 24 Sep 2026 01:00:00 GMT</pubDate>
</item>
<item>
  <title>Hộ kinh doanh kiến nghị miễn xuất hóa đơn - tuoitre.vn</title>
  <link>https://news.google.com/rss/articles/CBMiXwAAB</link>
  <description>&lt;a href="https://tuoitre.vn/ho-kinh-doanh"&gt;Kiến nghị từ hộ kinh doanh&lt;/a&gt;.</description>
  <source url="https://tuoitre.vn">tuoitre.vn</source>
  <pubDate>Wed, 23 Sep 2026 09:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


class _Resp:
    def __init__(self, status_code: int = 200, text: str = _GNEWS):
        self.status_code = status_code
        self.text = text


def _run(coro):
    return asyncio.run(coro)


def _fake_get(text: str = _GNEWS, status: int = 200):
    async def _get(self, url, **kw):
        _get.last_url = str(url)
        return _Resp(status, text)

    _get.last_url = ""
    return _get


class TestGNewsSearch:
    def test_url_builds_site_scope(self):
        url = _gnews.gnews_url("gnews_vn_legal", "hóa đơn điện tử")
        assert url is not None
        assert "site%3Avbpl.vn" in url
        assert "hl=vi" in url and "ceid=VN%3Avi" in url
        assert _gnews.gnews_url("gnews_vn", "x") is not None
        assert _gnews.gnews_url("bogus", "x") is None

    def test_parse_items_publisher_and_desc(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            items = _run(_gnews.gnews_search("gnews_vn", "hóa đơn"))
        assert len(items) == 2
        first = items[0]
        assert first.engine == "xaydungchinhsach.chinhphu.vn"
        assert not first.title.endswith("xaydungchinhsach.chinhphu.vn")
        assert "Nghị định 254" in first.title
        assert first.published_date == "2026-09-24"
        assert "<a" not in (first.description or "")

    def test_publisher_identity_in_metadata(self):
        """<source> element → publisher_name + publisher_domain; url stays the
        gnews discovery wrapper and the homepage is never canonical."""
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            items = _run(_gnews.gnews_search("gnews_vn", "hóa đơn"))
        first = items[0]
        assert first.url.startswith("https://news.google.com/")
        assert first.metadata["publisher_name"] == "xaydungchinhsach.chinhphu.vn"
        assert first.metadata["publisher_domain"] == "chinhphu.vn"
        # Bare-domain <source url> attrs normalize the same way.
        assert _gnews._publisher_domain("tuoitre.vn") == "tuoitre.vn"
        assert _gnews._publisher_domain("") == ""

    def test_orchestrator_domain_prefers_publisher_domain(self):
        """Authority/dedup must see the real publisher, not news.google.com."""
        from core.orchestrator import SearchOrchestrator
        from models import SearchResultItem

        item = SearchResultItem(
            url="https://news.google.com/rss/articles/CBMiXwAAA",
            title="T",
            metadata={"publisher_domain": "chinhphu.vn"},
        )
        orchestrator = SearchOrchestrator.__new__(SearchOrchestrator)
        sources = orchestrator._normalize([("gnews_vn", item)])
        assert sources[0].domain == "chinhphu.vn"
        assert sources[0].url.startswith("https://news.google.com/")
        # Without publisher identity, netloc fallback is unchanged.
        plain = SearchResultItem(url="https://news.google.com/rss/articles/ZZZ", title="T")
        fallback = orchestrator._normalize([("gnews_vn", plain)])
        assert fallback[0].domain == "news.google.com"

    def test_result_domain_helper(self):
        """Shared domain chooser: publisher identity wins; netloc fallback."""
        from providers.base import result_domain

        gnews = "https://news.google.com/rss/articles/CBMiXwAAA"
        assert result_domain(gnews, {"publisher_domain": "chinhphu.vn"}) == "chinhphu.vn"
        assert result_domain(gnews, {}) == "news.google.com"
        assert result_domain(gnews, None) == "news.google.com"
        assert result_domain(gnews, {"publisher_domain": ""}) == "news.google.com"
        # Non-wrapper URLs normalize like the old netloc logic.
        assert result_domain("https://WWW.Tuoitre.vn/a-b") == "tuoitre.vn"

    def test_query_scoped_per_lane(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()) as p:
            _run(_gnews.gnews_search("gnews_vn_gov", "nghị định"))
        assert "site%3Achinhphu.vn" in p.last_url

    def test_empty_query_no_request(self):
        with patch("httpx.AsyncClient.get", new=_fake_get(status=500)):
            assert _run(_gnews.gnews_search("gnews_vn", "  ")) == []

    def test_http_error_is_call_error_not_empty(self):
        with patch("httpx.AsyncClient.get", new=_fake_get(status=503)):
            sink: list[str] = []
            assert _run(_gnews.gnews_search("gnews_vn", "x", call_error=sink)) == []
        assert sink == ["HTTP 503"]

    def test_transport_error(self):
        async def _boom(self, url, **kw):
            raise httpx.TimeoutException("slow")

        with patch("httpx.AsyncClient.get", new=_boom):
            sink: list[str] = []
            assert _run(_gnews.gnews_search("gnews_vn", "x", call_error=sink)) == []
        assert sink and "slow" in sink[0]

    def test_max_results(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            assert len(_run(_gnews.gnews_search("gnews_vn", "x", max_results=1))) == 1


class TestGNewsWiring:
    def test_specs_cover_lanes(self):
        for lane in _gnews.GNEWS_LANES:
            assert lane in PROVIDER_SPECS
            spec = PROVIDER_SPECS[lane]
            assert spec["countries"] == ["VN"]
            assert spec["languages"] == ["vi"]

    def test_default_registry_includes_gnews(self):
        reg = create_default_registry()
        names = set(dict(reg.all()))
        assert "gnews_vn" in names
        assert "gnews_vn_legal" in names
        assert "gnews_vn_gov" in names

    def test_legal_query_picks_legal_lane(self):
        reg = ProviderRegistry()
        reg.register(
            "gnews_vn_legal",
            VnGNewsProvider("gnews_vn_legal"),
            _spec_from_mapping("gnews_vn_legal", PROVIDER_SPECS["gnews_vn_legal"]),
        )
        plan = SourceRouter().plan(
            "nghị định hóa đơn điện tử mới nhất",
            QueryProfile(language="vi"),
            reg,
        )
        assert "gnews_vn_legal" in plan.provider_names
        assert plan.lane_weights[SourceType.legal] == VERY_HIGH
        assert plan.lane_weights[SourceType.government] == VERY_HIGH

    def test_gov_lane_picked_on_gov_query(self):
        reg = ProviderRegistry()
        reg.register(
            "gnews_vn_gov",
            VnGNewsProvider("gnews_vn_gov"),
            _spec_from_mapping("gnews_vn_gov", PROVIDER_SPECS["gnews_vn_gov"]),
        )
        plan = SourceRouter().plan("nghị định mới nhất", QueryProfile(language="vi"), reg)
        assert "gnews_vn_gov" in plan.provider_names

    def test_vi_lanes_skipped_for_en_query(self):
        reg = ProviderRegistry()
        for lane in _gnews.GNEWS_LANES:
            reg.register(
                lane, VnGNewsProvider(lane), _spec_from_mapping(lane, PROVIDER_SPECS[lane])
            )
        plan = SourceRouter().plan("latest iphone news", QueryProfile(language="en"), reg)
        assert not set(plan.provider_names) & set(_gnews.GNEWS_LANES)


class TestGNewsAdapter:
    def test_provider_result_contract(self):
        reg = ProviderRegistry()
        reg.register(
            "gnews_vn",
            VnGNewsProvider("gnews_vn"),
            _spec_from_mapping("gnews_vn", PROVIDER_SPECS["gnews_vn"]),
        )
        provider = reg.get("gnews_vn")
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            results = _run(provider.search(ProviderSearchQuery(query="hóa đơn")))
        assert results
        for r in results:
            assert r.source == "gnews_vn"
            assert r.language == "vi"
            assert r.country == "VN"
        # Publisher identity survives the SearchResultItem → ProviderResult hop.
        assert results[0].metadata["publisher_domain"] == "chinhphu.vn"
        assert results[0].metadata["publisher_name"] == "xaydungchinhsach.chinhphu.vn"

    def test_health_true_and_false(self):
        with patch("httpx.AsyncClient.get", new=_fake_get()):
            assert _run(_gnews.gnews_health("gnews_vn")) is True
        with patch("httpx.AsyncClient.get", new=_fake_get(status=500)):
            assert _run(_gnews.gnews_health("gnews_vn")) is False
        assert _run(_gnews.gnews_health("bogus")) is False
