"""Hand-computed metric cases for nDCG, MRR, Recall, Precision, freshness."""

import math

import pytest

from eval.matching import (
    matched_expected_indexes,
    normalize_expected,
    normalize_url,
    relevance_vector,
    result_matches_any,
    result_matches_expected,
)
from eval.metrics import (
    estimate_cost,
    evaluate_query,
    freshness_ok,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    summarize_latencies,
)


def _meta(count: int, retrieved: bool = True) -> list[dict]:
    ts = "2026-08-16T00:00:00Z"
    return [{"retrieved_at": ts if retrieved else ""} for _ in range(count)]


class TestNDCG:
    def test_perfect_ranking_ndcg_is_1(self):
        rel = [1, 1, 0, 0, 0, 0, 0, 0, 0, 0]
        assert ndcg_at_k(rel, 2, 10) == pytest.approx(1.0)

    def test_no_relevant_ndcg_is_0(self):
        assert ndcg_at_k([0, 0, 0, 0], 2, 10) == 0.0

    def test_zero_expected_ndcg_is_0(self):
        assert ndcg_at_k([1, 1], 0, 10) == 0.0

    def test_single_relevant_at_position_3(self):
        # index 2 -> DCG = 1/log2(4) = 0.5, IDCG = 1.0
        rel = [0, 0, 1, 0, 0, 0, 0, 0, 0, 0]
        assert ndcg_at_k(rel, 1, 10) == pytest.approx(0.5, abs=1e-9)

    def test_two_relevant_at_positions_3_and_5(self):
        # DCG = 1/log2(4) + 1/log2(6) = 0.5 + 0.38685 = 0.88685
        # IDCG = 1/log2(2) + 1/log2(3) = 1.63093
        rel = [0, 0, 1, 0, 1, 0, 0, 0, 0, 0]
        expected = (0.5 + 1 / math.log2(6)) / (1 + 1 / math.log2(3))
        assert ndcg_at_k(rel, 2, 10) == pytest.approx(expected, abs=1e-6)

    def test_k_smaller_than_relevant_count(self):
        rel = [1, 1, 1, 0, 0]
        # k=2 with 3 expected -> IDCG over min(3,2)=2 relevant
        assert ndcg_at_k(rel, 3, 2) == pytest.approx(1.0)


class TestMRR:
    def test_first_position(self):
        assert mrr([1, 0, 0]) == 1.0

    def test_third_position(self):
        assert mrr([0, 0, 1, 0]) == pytest.approx(1.0 / 3.0)

    def test_no_relevant(self):
        assert mrr([0, 0, 0]) == 0.0


class TestRecallPrecision:
    def test_recall_at_k(self):
        urls = [
            "https://a.com/1",
            "https://a.com/2",
            "https://b.com/3",
            "https://c.com/4",
        ]
        # 2 expected, both covered by top-5
        assert recall_at_k(urls, ["a.com", "b.com"], 5) == pytest.approx(1.0)
        # 4 expected, only 2 covered
        assert recall_at_k(urls, ["a.com", "b.com", "z.com", "w.com"], 5) == pytest.approx(0.5)
        # only top-1: a.com
        assert recall_at_k(urls, ["a.com", "b.com"], 1) == pytest.approx(0.5)

    def test_recall_never_exceeds_1_on_duplicate_domains(self):
        urls = ["https://a.com/1", "https://a.com/2", "https://a.com/3"]
        assert recall_at_k(urls, ["a.com"], 5) == pytest.approx(1.0)

    def test_recall_zero_expected(self):
        assert recall_at_k(["https://a.com/1"], [], 5) == 0.0

    def test_precision_at_k(self):
        rel = [1, 1, 0, 0, 0, 0, 0, 0, 0, 0]
        assert precision_at_k(rel, 10) == pytest.approx(0.2)
        assert precision_at_k(rel, 5) == pytest.approx(0.4)

    def test_precision_zero_k(self):
        assert precision_at_k([1, 1], 0) == 0.0


class TestFreshness:
    def test_all_fresh(self):
        assert freshness_ok(_meta(5), 5) == pytest.approx(1.0)

    def test_missing_retrieved_at(self):
        meta = _meta(9) + [{"retrieved_at": ""}]
        assert freshness_ok(meta, 10) == pytest.approx(0.9)

    def test_empty_results(self):
        assert freshness_ok([], 10) == 0.0

    def test_k_truncates(self):
        meta = _meta(3, retrieved=True) + _meta(7, retrieved=False)
        assert freshness_ok(meta, 3) == pytest.approx(1.0)


class TestEvaluateQuery:
    def test_perfect_query(self):
        urls = [f"https://a.com/r{i}" for i in range(10)]
        urls[0] = "https://vnexpress.net/a"
        urls[1] = "https://dantri.com.vn/b"
        metrics = evaluate_query(urls, ["vnexpress.net", "dantri.com.vn"], _meta(10), top_k=10)
        assert metrics["ndcg@10"] == pytest.approx(1.0)
        assert metrics["mrr"] == pytest.approx(1.0)
        assert metrics["recall@5"] == pytest.approx(1.0)
        assert metrics["precision@10"] == pytest.approx(0.2)
        assert metrics["freshness_ok"] == pytest.approx(1.0)

    def test_duplicate_domain_counts_once_for_recall(self):
        # Two results hit the same expected domain: recall still 1/1.
        urls = ["https://vnexpress.net/a", "https://vnexpress.net/b", "https://x.net/c"]
        metrics = evaluate_query(urls, ["vnexpress.net"], _meta(3), top_k=10)
        assert metrics["recall@5"] == pytest.approx(1.0)
        assert metrics["precision@10"] == pytest.approx(0.2)

    def test_no_match_query(self):
        urls = ["https://other.com/1", "https://other.com/2"]
        metrics = evaluate_query(urls, ["vnexpress.net"], _meta(2), top_k=10)
        assert metrics["ndcg@10"] == 0.0
        assert metrics["mrr"] == 0.0
        assert metrics["recall@5"] == 0.0
        assert metrics["precision@10"] == 0.0


class TestCost:
    def test_estimate_zero_default(self):
        assert estimate_cost([{}, {}], 0.0) == 0.0

    def test_estimate_per_query(self):
        assert estimate_cost([{}, {}], 0.01) == pytest.approx(0.02)

    def test_explicit_cost_wins(self):
        queries = [{"cost_usd": 0.05}, {}]
        assert estimate_cost(queries, 0.01) == pytest.approx(0.06)


class TestLatencySummary:
    def test_empty(self):
        s = summarize_latencies([])
        assert s["mean_ms"] == 0.0
        assert s["p95_ms"] == 0.0

    def test_mean_and_p95(self):
        s = summarize_latencies([100, 200, 300, 400, 500])
        assert s["mean_ms"] == pytest.approx(300.0)
        assert s["p95_ms"] == pytest.approx(500.0)


class TestMatching:
    def test_domain_match(self):
        assert result_matches_expected("https://www.vnexpress.net/tin-tuc", "vnexpress.net")
        assert not result_matches_expected("https://vnexpress.net.vn/x", "vnexpress.net")

    def test_url_match_normalizes(self):
        assert result_matches_expected("https://WWW.Example.com/Path/", "http://example.com/path")

    def test_query_string_stripped_from_domain_kind(self):
        assert result_matches_expected("https://example.com/?utm_source=x", "example.com")

    def test_matched_indexes_each_expected_once(self):
        urls = [
            "https://a.com/x",
            "https://a.com/y",  # duplicate match on same expected
            "https://b.com/z",
        ]
        idx = matched_expected_indexes(urls, ["a.com", "b.com", "c.com"])
        assert idx == [0, 1]

    def test_relevance_vector(self):
        rel = relevance_vector(
            ["https://a.com/x", "https://x.net/y", "https://a.com/z"],
            ["a.com"],
            10,
        )
        assert rel == [1, 0, 1]

    def test_result_matches_any(self):
        assert result_matches_any("https://a.com/x", ["z.com", "a.com"])
        assert not result_matches_any("https://a.com/x", ["z.com", "b.com"])

    def test_normalize_expected_kinds(self):
        assert normalize_expected("vnexpress.net")["kind"] == "domain"
        assert normalize_expected("https://vnexpress.net/a")["kind"] == "url"

    def test_normalize_url_default_port(self):
        # http/https treated as the same scheme; default ports stripped.
        assert normalize_url("http://example.com:80/path") == "https://example.com/path"
        assert normalize_url("https://example.com:443") == "https://example.com"

    def test_normalize_url_case_and_scheme_insensitive(self):
        assert normalize_url("HTTP://WWW.Example.com/Path/") == "https://example.com/path"
