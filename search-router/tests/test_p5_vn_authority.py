"""P5-VN — intent-dependent authority + per-vertical freshness.

Authority is vertical-scoped (a forum is weak on law, useful on product
opinions); freshness decays per lane (market=hours, legal=years).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ranking.authority import (
    authority_for,
    authority_score,
    authority_source_meta,
    classify_source_type,
)
from ranking.quality import (
    FRESHNESS_HALFLIFE_DAYS,
    _freshness_score,
    quality_score,
)
from ranking.types import RankedItem


class TestVerticalAuthority:
    def test_forum_weak_on_law_useful_on_product(self):
        # The spec's core rule: one domain_score is wrong.
        assert authority_for("otofun.net", "legal") == 0.1
        assert authority_for("otofun.net", "product") == 0.7
        assert authority_for("otofun.net", "legal") < authority_for("otofun.net", "product")

    def test_legal_first_party_wins_legal_lane(self):
        assert authority_for("vbpl.vn", "legal") == 1.0
        assert authority_for("congbao.chinhphu.vn", "document") == 1.0
        assert authority_for("thuvienphapluat.vn", "legal") > authority_for(
            "thuvienphapluat.vn", "news"
        )

    def test_state_media_stronger_on_government_than_news(self):
        assert authority_for("nhandan.vn", "government") > authority_for("nhandan.vn", "forum")
        assert authority_for("nhandan.vn", "government") >= authority_for("vnexpress.net", "news")

    def test_market_authority_for_gold_and_stocks(self):
        assert authority_for("sjc.com.vn", "market") == 0.95
        assert authority_for("cafef.vn", "market") > authority_for("cafef.vn", "forum")

    def test_unmapped_lane_falls_back_to_base(self):
        # A lane with no override keeps the domain's base score.
        assert authority_for("vnexpress.net", "medical") == authority_score("vnexpress.net")
        assert authority_for("unknown-example.io", "legal") == authority_score("unknown-example.io")

    def test_no_vertical_matches_legacy_score(self):
        for d in ("vnexpress.net", "otofun.net", "vbpl.vn", "x.io"):
            assert authority_for(d) == authority_score(d)

    def test_vi_boost_still_applies_with_vertical(self):
        # .vn TLD earns the vi boost on top of the vertical override.
        assert authority_for("thuvienphapluat.vn", "legal", lang="vi") > authority_for(
            "thuvienphapluat.vn", "legal", lang="en"
        )

    def test_new_domains_classified(self):
        assert classify_source_type("vbpl.vn") == "government"
        assert classify_source_type("otofun.net") == "forum"
        assert classify_source_type("thegioididong.com") == "vendor_website"
        assert classify_source_type("congthuong.vn") == "major_publication"


class TestSourceMeta:
    def test_official_domains_carry_meta(self):
        meta = authority_source_meta("chinhphu.vn")
        assert meta["official"] is True
        assert meta["ownership"] == "state"
        assert meta["geography"] == "VN"
        assert meta["update_frequency"] == "realtime"

    def test_subdomain_inherits_meta(self):
        assert authority_source_meta("xaydungchinhsach.chinhphu.vn")["official"] is True

    def test_unknown_domain_empty_meta(self):
        assert authority_source_meta("random-blog.example") == {}


class TestVerticalFreshness:
    def test_market_decays_within_day(self):
        old = datetime.now(UTC) - timedelta(hours=18)
        fresh = datetime.now(UTC) - timedelta(hours=1)
        assert _freshness_score(fresh, vertical="market") > _freshness_score(old, vertical="market")
        assert _freshness_score(old, vertical="market") < 0.3

    def test_legal_survives_years(self):
        year_old = datetime.now(UTC) - timedelta(days=400)
        # 10y horizon → ~400d old legal doc still scores high.
        assert _freshness_score(year_old, vertical="legal") > 0.9

    def test_news_vs_legal_same_age(self):
        week = datetime.now(UTC) - timedelta(days=7)
        assert _freshness_score(week, vertical="news") < _freshness_score(week, vertical="legal")

    def test_unknown_vertical_keeps_legacy_decay(self):
        day = datetime.now(UTC) - timedelta(days=10)
        now = datetime.now(UTC)
        assert _freshness_score(day, now=now) == _freshness_score(
            day, now=now, vertical="not-a-lane"
        )

    def test_missing_date_neutral_in_any_lane(self):
        assert _freshness_score(None, vertical="market") == 0.5

    def test_halflife_table_covers_core_lanes(self):
        for lane in ("news", "market", "legal", "government", "forum", "places"):
            assert lane in FRESHNESS_HALFLIFE_DAYS
        assert FRESHNESS_HALFLIFE_DAYS["market"] < FRESHNESS_HALFLIFE_DAYS["news"]
        assert FRESHNESS_HALFLIFE_DAYS["news"] < FRESHNESS_HALFLIFE_DAYS["legal"]


class TestQualityWiring:
    def test_item_lane_changes_authority_term(self):
        item_forum = RankedItem(
            url="https://otofun.net/threads/x",
            title="x",
            normalized_score=0.8,
            source_type="forum",
        )
        item_legal = RankedItem(
            url="https://otofun.net/threads/x",
            title="x",
            normalized_score=0.8,
            source_type="legal",
        )
        assert quality_score(item_forum, "q") > quality_score(item_legal, "q")

    def test_context_vertical_overrides_item_lane(self):
        item = RankedItem(
            url="https://vbpl.vn/doc/1",
            title="x",
            normalized_score=0.8,
            source_type="news",
        )
        legal = quality_score(item, "q", context={"vertical": "legal"})
        news = quality_score(item, "q", context={"vertical": "news"})
        assert legal > news

    def test_metadata_source_type_used(self):
        item = RankedItem(
            url="https://otofun.net/t/1",
            title="x",
            normalized_score=0.8,
            metadata={"source_type": "product"},
        )
        assert quality_score(item, "q") > 0.0


class TestOrchestratorLane:
    def _orch(self):
        from core.orchestrator import SearchOrchestrator

        # Bypass __init__ — _rank/_normalize/_freshness_bonus need no services.
        return SearchOrchestrator.__new__(SearchOrchestrator)

    def test_freshness_bonus_lane_graded(self):
        orch = self._orch()
        old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        assert orch._freshness_bonus(old, "news") == 0.0  # > 2×2d half-life
        assert orch._freshness_bonus(old) == 0.15  # legacy weekly bucket
        stale_legal = (datetime.now(UTC) - timedelta(days=800)).isoformat()
        assert orch._freshness_bonus(stale_legal) == 0.0  # legacy: > week → 0
        # legal texts stay current: 800d into a 10y horizon keeps most bonus.
        assert orch._freshness_bonus(stale_legal, "legal") > 0.25
        fresh_market = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        stale_market = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
        assert orch._freshness_bonus(fresh_market, "market") > 0.2
        assert orch._freshness_bonus(stale_market, "market") < 0.05

    def test_rank_uses_source_lane(self):
        from models import Source

        orch = self._orch()
        src_forum = Source(
            url="https://otofun.net/t/1",
            domain="otofun.net",
            title="xe máy review",
            source_lane="product",
        )
        src_legal = Source(
            url="https://otofun.net/t/2",
            domain="otofun.net",
            title="xe máy review",
            source_lane="legal",
        )
        ranked = orch._rank([src_forum, src_legal], "xe máy review")
        assert ranked[0].source_lane == "product"

    def test_normalize_sets_source_lane(self):
        from providers.base import ProviderResult

        orch = self._orch()
        srcs = orch._normalize(
            [
                (
                    "gnews_vn_legal",
                    ProviderResult(url="https://vbpl.vn/x", title="NĐ", source_type="legal"),
                )
            ]
        )
        assert srcs[0].source_lane == "legal"


class TestEvidenceLane:
    def test_label_lane_aware(self):
        from pipeline.evidence_aggregator import _label

        _t1, s_forum, _tr1, _r1 = _label(
            "https://otofun.net/x", ["gnews_vn"], None, None, "product"
        )
        _t2, s_legal, _tr2, _r2 = _label("https://otofun.net/x", ["gnews_vn"], None, None, "legal")
        assert s_forum > s_legal
        _t3, s_vbpl, _tr3, _r3 = _label(
            "https://vbpl.vn/x", ["gnews_vn_legal"], None, None, "legal"
        )
        assert s_vbpl == 1.0
