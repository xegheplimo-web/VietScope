"""Tests for the metrics feedback & evaluation loop (PART 1)."""

import pytest
from metrics.store import MetricsStore, quality_score


@pytest.fixture
def tmp_store(tmp_path):
    """Fresh MetricsStore with a temp DB for isolation."""
    db = tmp_path / "test_metrics.db"
    return MetricsStore(db_path=db)


class TestQualityScore:
    def test_perfect_score(self):
        q, c, r, d = quality_score(
            results_count=10,
            latency_ms=100,
            relevance_scores=[0.9, 0.95, 0.8],
            domains=["a.com", "b.com", "c.com"],
        )
        assert q > 0.8
        assert c == 1.0  # full coverage
        assert r > 0.8
        assert d == 1.0  # all unique domains

    def test_zero_results(self):
        q, c, r, d = quality_score(results_count=0, latency_ms=100)
        assert q < 0.5
        assert c == 0.0

    def test_error_forces_zero(self):
        q, c, r, d = quality_score(
            results_count=10, latency_ms=100, relevance_scores=[0.9], error="boom"
        )
        assert q == 0.0
        assert c == 0.0

    def test_high_latency_penalty(self):
        q_low, _, _, _ = quality_score(results_count=5, latency_ms=100, relevance_scores=[0.5])
        q_high, _, _, _ = quality_score(results_count=5, latency_ms=10000, relevance_scores=[0.5])
        assert q_low > q_high

    def test_dedup_ratio(self):
        _, _, _, d = quality_score(results_count=4, domains=["a.com", "a.com", "a.com", "b.com"])
        assert d == 0.5  # 2 unique out of 4

    def test_no_domains_defaults_to_one(self):
        _, _, _, d = quality_score(results_count=3, domains=None)
        assert d == 1.0


class TestMetricsStore:
    def test_record_and_retrieve(self, tmp_store):
        rid = tmp_store.record(
            query="test",
            endpoint="/answer",
            query_type="answer",
            results_count=5,
            latency_ms=2000,
            providers="searxng",
            quality_score=0.85,
            coverage=1.0,
            relevance=0.7,
            dedup_ratio=0.8,
        )
        assert rid is not None
        recent = tmp_store.recent_queries(10)
        assert len(recent) == 1
        assert recent[0]["query"] == "test"
        assert recent[0]["quality_score"] == 0.85

    def test_quality_overview(self, tmp_store):
        for i in range(3):
            tmp_store.record(
                query=f"q{i}",
                endpoint="/search",
                query_type="web",
                results_count=5,
                latency_ms=1000,
                providers="searxng",
                quality_score=0.5 + i * 0.1,
            )
        ov = tmp_store.quality_overview()
        assert ov["total_queries"] == 3
        assert ov["scored_queries"] == 3
        assert 0.5 < ov["avg_quality"] < 0.8

    def test_quality_trend(self, tmp_store):
        tmp_store.record(
            query="q",
            endpoint="/answer",
            query_type="answer",
            results_count=5,
            latency_ms=1000,
            quality_score=0.7,
        )
        trend = tmp_store.quality_trend(7)
        assert len(trend) == 1
        assert trend[0]["queries"] == 1

    def test_cache_hit_recorded(self, tmp_store):
        tmp_store.record(
            query="cached",
            endpoint="/search",
            query_type="web",
            results_count=5,
            latency_ms=0,
            cache_hit=True,
            quality_score=0.9,
        )
        ov = tmp_store.quality_overview()
        assert ov["cache_hits"] == 1

    def test_error_recorded(self, tmp_store):
        tmp_store.record(
            query="err",
            endpoint="/fetch",
            query_type="scrape",
            results_count=0,
            latency_ms=500,
            error="timeout",
        )
        ov = tmp_store.quality_overview()
        assert ov["errors"] == 1

    def test_record_never_raises(self, tmp_store, monkeypatch):
        """record_query must never raise even if DB fails."""
        # Force a DB error by making the path invalid.
        monkeypatch.setattr(tmp_store, "_db_path", "/nonexistent/path/db.db")
        rid = tmp_store.record(query="x", endpoint="/search")
        assert rid is None  # gracefully returns None


class TestRecordQuery:
    def test_record_query_computes_quality(self, tmp_path):
        db = tmp_path / "rq.db"
        store = MetricsStore(db_path=db)
        # Use the store directly via record() with quality_score computed.
        from metrics.store import quality_score as qs

        q, c, r, d = qs(
            results_count=5,
            latency_ms=2000,
            relevance_scores=[0.8, 0.6],
            domains=["a.com", "b.com"],
        )
        rid = store.record(
            query="test",
            endpoint="/answer",
            query_type="answer",
            results_count=5,
            latency_ms=2000,
            providers="searxng",
            quality_score=q,
            coverage=c,
            relevance=r,
            dedup_ratio=d,
        )
        assert rid is not None
        recent = store.recent_queries(1)
        assert recent[0]["quality_score"] is not None
        assert recent[0]["quality_score"] > 0
