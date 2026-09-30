"""Tests for the unified ranking engine (wave7)."""

from collections import Counter
from datetime import UTC

import pytest
from models import SearchResultItem
from ranking import (
    RankedItem,
    fusion,
    normalize_scores,
    quality_score,
    rank_search,
    rerank,
)


def _item(
    url: str,
    title: str = "",
    desc: str = "",
    score: float = 0.0,
    engine: str = "searxng",
    published_date: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> SearchResultItem:
    return SearchResultItem(
        url=url,
        title=title,
        description=desc,
        score=score,
        engine=engine,
        published_date=published_date,
    )


class TestNormalizeScores:
    def test_empty(self):
        assert normalize_scores([]) == []

    def test_single_result_normalizes_to_one(self):
        results = [_item("https://a.com", score=5.0)]
        out = normalize_scores(results)
        assert len(out) == 1
        assert out[0].raw_score == 5.0
        assert out[0].normalized_score == 1.0

    def test_zero_scores(self):
        results = [
            _item("https://a.com", score=0.0),
            _item("https://b.com", score=0.0),
        ]
        out = normalize_scores(results)
        assert [o.normalized_score for o in out] == [0.0, 0.0]

    def test_minmax_range(self):
        results = [
            _item("https://a.com", score=10.0),
            _item("https://b.com", score=5.0),
            _item("https://c.com", score=0.0),
        ]
        out = normalize_scores(results)
        scores = {o.url: o.normalized_score for o in out}
        assert scores["https://a.com"] == 1.0
        assert scores["https://b.com"] == 0.5
        assert scores["https://c.com"] == 0.0

    def test_zscore_zero_variance(self):
        results = [
            _item("https://a.com", score=3.0),
            _item("https://b.com", score=3.0),
        ]
        out = normalize_scores(results, method="zscore")
        assert [o.normalized_score for o in out] == [0.0, 0.0]

    def test_zscore_monotonic(self):
        results = [
            _item("https://a.com", score=0.0),
            _item("https://b.com", score=5.0),
            _item("https://c.com", score=10.0),
        ]
        out = normalize_scores(results, method="zscore")
        # Z-scores should be ascending with the same order as raw scores.
        assert out[0].normalized_score < out[1].normalized_score < out[2].normalized_score

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError):
            normalize_scores([_item("https://a.com")], method="unknown")

    def test_dict_input(self):
        out = normalize_scores([{"url": "https://a.com", "score": 10.0}])
        assert out[0].raw_score == 10.0


class TestFusion:
    def test_empty(self):
        assert fusion({}) == []

    def test_single_provider(self):
        results = {
            "searxng": [
                RankedItem(url="https://a.com", raw_score=1.0, normalized_score=1.0),
                RankedItem(url="https://b.com", raw_score=0.5, normalized_score=0.5),
            ]
        }
        out = fusion(results)
        assert len(out) == 2
        # Top score should keep a/b order.
        assert out[0].url == "https://a.com"

    def test_duplicate_canonical_url(self):
        results = {
            "searxng": [
                RankedItem(
                    url="https://A.COM/",
                    canonical_url="https://a.com/",
                    normalized_score=1.0,
                ),
            ],
            "github": [
                RankedItem(
                    url="https://a.com",
                    canonical_url="https://a.com/",
                    normalized_score=0.6,
                ),
            ],
        }
        out = fusion(results)
        assert len(out) == 1

    def test_multi_provider_agreement_boosts(self):
        a = RankedItem(url="https://a.com", raw_score=1.0, normalized_score=1.0)
        b = RankedItem(url="https://b.com", raw_score=1.0, normalized_score=1.0)
        c = RankedItem(url="https://c.com", raw_score=1.0, normalized_score=0.9)
        results = {
            "searxng": [a, b, c],
            "github": [a, b],
        }
        out = fusion(results)
        # a and b are seen by both providers and should outrank c.
        assert out[0].url in ("https://a.com", "https://b.com")

    def test_provider_weights_included(self):
        a = RankedItem(url="https://a.com", normalized_score=1.0)
        b = RankedItem(url="https://b.com", normalized_score=1.0)
        results = {
            "official": [a],
            "unknown": [b],
        }
        out = fusion(results)
        # official has a higher default weight.
        assert out[0].url == "https://a.com"


class TestQualityScore:
    def test_empty_item(self):
        item = RankedItem(url="https://a.com")
        score = quality_score(item, "test")
        assert 0.0 <= score <= 1.0

    def test_authority_boost(self):
        low = RankedItem(url="https://some-unknown-blog.xyz", normalized_score=0.5)
        high = RankedItem(url="https://github.com/python", normalized_score=0.5)
        assert quality_score(high, "python") > quality_score(low, "python")

    def test_freshness(self):
        from datetime import datetime

        now = datetime.now(UTC)
        fresh = RankedItem(
            url="https://a.com",
            normalized_score=0.5,
            published_date=now.isoformat(),
        )
        old = RankedItem(
            url="https://b.com",
            normalized_score=0.5,
            published_date="2010-01-01",
        )
        assert quality_score(fresh, "news") > quality_score(old, "news")

    def test_diversity_penalty(self):
        item = RankedItem(url="https://same.com/1", normalized_score=1.0)
        counts = Counter({"same.com": 5})
        with_penalty = quality_score(item, "test", context={"domain_counts": counts, "lang": "en"})
        without = quality_score(item, "test", context={"domain_counts": Counter()})
        assert with_penalty < without

    def test_geo_bonus(self):
        hanoi = RankedItem(
            url="https://a.com",
            normalized_score=0.5,
            lat=21.0285,
            lon=105.8542,
        )
        context = {"query_lat": 21.0285, "query_lon": 105.8542, "lang": "en"}
        score = quality_score(hanoi, "hanoi", context=context)
        assert score > quality_score(RankedItem(url="https://b.com", normalized_score=0.5), "hanoi")

    def test_vietnamese_authority(self):
        vn = RankedItem(url="https://vnexpress.net/news", normalized_score=0.5)
        en = RankedItem(url="https://example.com/news", normalized_score=0.5)
        assert quality_score(vn, "tin tức") > quality_score(en, "tin tức")


class TestRerank:
    def test_empty(self):
        assert rerank([], "test") == []

    def test_top_k(self):
        results = [
            RankedItem(url=f"https://x{i}.com", normalized_score=i / 10.0) for i in range(10)
        ]
        out = rerank(results, "test", top_k=3)
        assert len(out) == 3
        assert [r.url for r in out] == [f"https://x{i}.com" for i in [9, 8, 7]]

    def test_fake_reranker(self):
        class Fake:
            def available(self):
                return True

            def embed(self, texts):
                # Every doc becomes perfectly similar to the query.
                return [[1.0, 0.0] for _ in texts]

        results = [
            RankedItem(url="https://a.com", title="a", description="a", normalized_score=0.9),
            RankedItem(url="https://b.com", title="b", description="b", normalized_score=0.1),
        ]
        out = rerank(results, "test", reranker=Fake(), top_k=2)
        assert len(out) == 2

    def test_unavailable_reranker(self):
        class Fake:
            def available(self):
                return False

            def embed(self, texts):
                return None

        results = [
            RankedItem(url="https://a.com", normalized_score=0.9),
            RankedItem(url="https://b.com", normalized_score=0.1),
        ]
        out = rerank(results, "test", reranker=Fake(), top_k=2)
        assert out[0].url == "https://a.com"


class TestRankSearch:
    def test_sample_pipeline(self):
        by_provider = {
            "searxng": [
                _item("https://a.com", "Python async", "Learn python async", 10.0),
                _item("https://b.com", "Java guide", "Java only", 5.0),
            ],
            "github": [
                _item("https://a.com", "python/async repo", "Python async code", 8.0),
            ],
        }
        out = rank_search(by_provider, "python async", top_k=2)
        assert len(out) == 2
        # The a.com URL appears in both providers and should win.
        assert out[0].url == "https://a.com"

    def test_zero_scores(self):
        by_provider = {
            "searxng": [
                _item("https://a.com", score=0.0),
                _item("https://b.com", score=0.0),
            ]
        }
        out = rank_search(by_provider, "test", top_k=1)
        assert len(out) == 1
        assert out[0].final_score >= 0.0

    def test_empty_providers(self):
        out = rank_search({}, "test", top_k=10)
        assert out == []


class TestPublicExports:
    def test_all_exports(self):
        from ranking import rank_search

        assert callable(rank_search)
