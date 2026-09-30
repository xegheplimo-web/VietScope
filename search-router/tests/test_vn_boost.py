"""Tests for the Vietnamese ranking boost (ranking.authority + reranker wiring)."""

from models import SearchResultItem
from pipeline.reranker import rerank_search_results
from ranking.authority import (
    VN_FAMOUS_BOOST,
    VN_TLD_BOOST,
    authority_score,
    vn_boost,
)


class TestVnBoost:
    def test_vn_tld_boost(self):
        assert vn_boost("random-unknown-site.vn") == VN_TLD_BOOST

    def test_famous_vn_domain_boost(self):
        assert vn_boost("vnexpress.net") == VN_FAMOUS_BOOST
        assert vn_boost("foody.vn") == VN_TLD_BOOST  # both → larger wins

    def test_known_social_platform(self):
        assert vn_boost("www.facebook.com") == VN_FAMOUS_BOOST
        assert vn_boost("maps.google.com") == VN_FAMOUS_BOOST

    def test_no_boost_for_foreign(self):
        assert vn_boost("reuters.com") == 0.0
        assert vn_boost("github.com") == 0.0

    def test_unknown_no_boost(self):
        assert vn_boost("example.com") == 0.0


class TestAuthorityScoreLang:
    def test_vi_greater_than_en_for_vn_site(self):
        assert authority_score("foody.vn", lang="vi") > authority_score("foody.vn", lang="en")

    def test_backward_compat_default_lang(self):
        assert authority_score("foody.vn") == authority_score("foody.vn", lang="en")

    def test_vi_boost_applied_for_tld(self):
        assert authority_score("random-unknown-site.vn", lang="vi") > authority_score(
            "random-unknown-site.vn", lang="en"
        )

    def test_lang_case_insensitive(self):
        assert authority_score("vnexpress.net", lang="VI") > authority_score(
            "vnexpress.net", lang="en"
        )

    def test_no_boost_for_english_query(self):
        assert authority_score("vnexpress.net", lang="en") == authority_score("vnexpress.net")


class TestRerankerVnBoost:
    def _mk(self, url, title, desc="", score=1.0):
        return SearchResultItem(
            url=url, title=title, description=desc, score=score, engine="searxng"
        )

    def test_vn_domain_ranks_top_for_vietnamese_query(self):
        results = [
            self._mk(
                "https://example.com/1",
                "generic restaurant guide with general content",
                score=2.0,
            ),
            self._mk(
                "https://foody.vn/somewhere",
                "nhà hàng quán ăn địa điểm review",
                score=0.5,
            ),
        ]
        out = rerank_search_results("nhà hàng ngon ở Hà Nội", list(results))
        assert out[0].url == "https://foody.vn/somewhere"

    def test_no_boost_for_english_query(self):
        results = [
            self._mk("https://example.com/1", "restaurant guide content", score=2.0),
            self._mk("https://foody.vn/somewhere", "nhà hàng quán ăn địa điểm", score=0.5),
        ]
        out = rerank_search_results("best restaurant in hanoi", list(results))
        assert out[0].url == "https://example.com/1"
