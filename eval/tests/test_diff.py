"""Baseline diff logic tests (+/-/= classification)."""

from eval.diff import (
    comparable_metrics,
    delta_class,
    diff_summary,
    format_diff,
    format_per_query_diffs,
)


def _summary(ndcg=0.5, mrr=0.4, lat=1200.0, cost=0.01):
    return {
        "ndcg@10": ndcg,
        "mrr": mrr,
        "recall@5": 0.3,
        "recall@10": 0.4,
        "recall@20": 0.5,
        "precision@10": 0.1,
        "freshness_ok": 1.0,
        "latency_ms": {"mean_ms": lat, "p50_ms": lat, "p95_ms": lat, "p99_ms": lat},
        "cost_usd": cost,
        "error_rate": 0.0,
    }


class TestDeltaClass:
    def test_higher_better_improved(self):
        assert delta_class("ndcg@10", 0.6, 0.4) == "+"
        assert delta_class("recall@5", 0.4, 0.3) == "+"

    def test_higher_better_regressed(self):
        assert delta_class("mrr", 0.3, 0.5) == "-"

    def test_lower_better_improved(self):
        assert delta_class("latency_ms.p95_ms", 800.0, 1200.0) == "+"
        assert delta_class("cost_usd", 0.005, 0.01) == "+"

    def test_lower_better_regressed(self):
        assert delta_class("latency_ms.mean_ms", 1500.0, 900.0) == "-"

    def test_unchanged(self):
        assert delta_class("ndcg@10", 0.5, 0.5) == "="
        assert delta_class("ndcg@10", 0.5, 0.5 + 1e-12) == "="

    def test_non_numeric_is_equal(self):
        assert delta_class("ndcg@10", "x", 0.5) == "="


class TestComparable:
    def test_pairs_intersection(self):
        new = _summary()
        base = _summary(ndcg=0.4)
        pairs = comparable_metrics(new, base)
        keys = {p[0] for p in pairs}
        assert "ndcg@10" in keys
        assert "latency_ms.p95_ms" in keys
        assert "cost_usd" in keys
        # metric missing from baseline is excluded
        new_only = dict(_summary())
        base = _summary()
        base.pop("mrr")
        assert "mrr" not in {p[0] for p in comparable_metrics(new_only, base)}

    def test_diff_summary_machine_readable(self):
        d = diff_summary(_summary(ndcg=0.6), _summary(ndcg=0.4))
        assert d["ndcg@10"] == "+"

    def test_format_diff_contains_all_symbols(self):
        new = _summary(ndcg=0.6, mrr=0.3, lat=800.0, cost=0.005)
        base = _summary(ndcg=0.4, mrr=0.5, lat=1200.0, cost=0.01)
        text = format_diff(new, base)
        assert "+" in text
        assert "-" in text
        assert "=" in text

    def test_format_diff_empty(self):
        assert "No comparable" in format_diff({"x": 1}, {"y": 2})


class TestPerQueryDiff:
    def _report(self, ndcgs):
        return {
            "per_query": [
                {
                    "index": i,
                    "query": f"q{i}",
                    "metrics": {"ndcg@10": v},
                }
                for i, v in enumerate(ndcgs)
            ]
        }

    def test_lines_aligned_by_index(self):
        new = self._report([0.5, 0.8])
        base = self._report([0.4, 0.9])
        text = format_per_query_diffs(new, base)
        assert "0: 0.4000 -> 0.5000" in text
        assert "1: 0.9000 -> 0.8000" in text
        assert "+" in text and "-" in text

    def test_mismatched_indexes_ignored(self):
        new = self._report([0.5, 0.6, 0.7])
        base = self._report([0.4])  # only index 0 in common
        text = format_per_query_diffs(new, base)
        assert "0:" in text
        assert "1:" not in text
