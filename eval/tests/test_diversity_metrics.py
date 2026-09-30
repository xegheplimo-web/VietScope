"""Tests for wave-8C additions: source_diversity, domain_overlap, time_sensitive.

Covers the pure metric math, report-level aggregators that must run on OLD
reports (only need per_query[].retrieved_domains), integration into
evaluate_query/aggregate_metrics, dataset loading of ``time_sensitive``, and
the new ``eval report`` CLI subcommand.
"""

import json

import pytest

from eval.cli import main
from eval.datasets import EvalQuery, load_dataset
from eval.metrics import (
    domain_overlap,
    evaluate_query,
    report_domain_overlap,
    report_source_diversity,
    source_diversity,
)
from eval.runner import RunConfig, aggregate_metrics, run_dataset


class TestSourceDiversity:
    def test_all_distinct_is_1(self):
        assert source_diversity(["a.com", "b.com", "c.com"]) == pytest.approx(1.0)

    def test_single_domain_repeated(self):
        assert source_diversity(["a.com", "a.com", "a.com"]) == pytest.approx(1.0 / 3)

    def test_mixed(self):
        assert source_diversity(["a.com", "a.com", "b.com"]) == pytest.approx(2.0 / 3)

    def test_empty_is_zero(self):
        assert source_diversity([]) == 0.0
        assert source_diversity(None) == 0.0

    def test_blank_entries_ignored(self):
        assert source_diversity(["a.com", "  ", "b.com"]) == pytest.approx(1.0)

    def test_www_and_case_normalized(self):
        assert source_diversity(["WWW.A.com", "B.com"]) == pytest.approx(1.0)
        # two entries collapsing to the same domain after normalization
        assert source_diversity(["WWW.A.com", "a.com"]) == pytest.approx(0.5)


class TestDomainOverlap:
    def test_identical_sets(self):
        assert domain_overlap(["a.com", "b.com"], ["b.com", "a.com"]) == pytest.approx(1.0)

    def test_disjoint_sets(self):
        assert domain_overlap(["a.com"], ["b.com"]) == pytest.approx(0.0)

    def test_partial_overlap_jaccard(self):
        # intersection {b,c} = 2, union {a,b,c,d} = 4 -> 0.5
        assert domain_overlap(
            ["a.com", "b.com", "c.com"], ["b.com", "c.com", "d.com"]
        ) == pytest.approx(0.5)

    def test_empty_side_is_zero(self):
        assert domain_overlap([], ["a.com"]) == 0.0
        assert domain_overlap(["a.com"], []) == 0.0
        assert domain_overlap([], []) == 0.0

    def test_www_normalized(self):
        assert domain_overlap(["www.a.com"], ["A.com"]) == pytest.approx(1.0)


class TestReportDiversity:
    @staticmethod
    def _report(rows, metrics=True):
        per_query = []
        for i, domains in rows:
            r = {
                "index": i,
                "query": f"q{i}",
                "retrieved_domains": list(domains),
            }
            if metrics:
                r["metrics"] = {"ndcg@10": 0.5}
            per_query.append(r)
        return {"per_query": per_query}

    def test_report_source_diversity_mean(self):
        report = self._report([(0, ["a.com", "b.com"]), (1, ["c.com"])])
        out = report_source_diversity(report)
        assert out["queries"] == 2
        # per query: 1.0 and 1.0
        assert out["per_query"] == [1.0, 1.0]
        assert out["mean"] == pytest.approx(1.0)

    def test_old_report_without_metrics_still_works(self):
        # A pre-8C report has no "metrics.source_diversity" key — the
        # aggregator must only read retrieved_domains (present since wave 7B).
        report = self._report([(0, ["a.com", "a.com", "b.com"]), (1, ["x.net"])], metrics=False)
        out = report_source_diversity(report)
        assert out["queries"] == 2
        assert out["per_query"] == [
            pytest.approx(round(2.0 / 3, 4)),
            pytest.approx(1.0),
        ]
        assert out["mean"] == pytest.approx(round((round(2.0 / 3, 4) + 1.0) / 2, 4))

    def test_empty_report(self):
        assert report_source_diversity({"per_query": []})["mean"] == 0.0
        assert report_source_diversity({})["queries"] == 0

    def test_report_domain_overlap_aligned_by_index(self):
        a = self._report([(0, ["a.com", "b.com"]), (1, ["c.com"]), (2, ["z.com"])])
        b = self._report([(0, ["b.com", "c.com"]), (1, ["c.com"]), (2, ["q.com"])])
        out = report_domain_overlap(a, b)
        assert out["compared"] == 3
        # q0: {a,b} vs {b,c} -> 1/3 ; q1: {c} vs {c} -> 1.0 ; q2: disjoint -> 0.0
        assert out["per_query"] == [
            pytest.approx(round(1.0 / 3, 4)),
            pytest.approx(1.0),
            pytest.approx(0.0),
        ]
        assert out["mean"] == pytest.approx(round((1.0 / 3 + 1.0 + 0.0) / 3, 4))

    def test_report_domain_overlap_skips_unmatched_indexes(self):
        a = self._report([(0, ["a.com"]), (1, ["b.com"])])
        b = self._report([(0, ["a.com"]), (2, ["x.com"])])
        out = report_domain_overlap(a, b)
        assert out["compared"] == 1
        assert out["per_query"] == [pytest.approx(1.0)]

    def test_report_domain_overlap_empty(self):
        assert report_domain_overlap({}, {})["mean"] == 0.0


class TestEvaluateQueryIntegration:
    def test_evaluate_query_exposes_source_diversity(self):
        urls = [
            "https://a.com/1",
            "https://b.com/2",
            "https://c.com/3",
        ]
        metrics = evaluate_query(urls, ["a.com"], top_k=10)
        assert metrics["source_diversity"] == pytest.approx(1.0)
        assert "source_diversity" in metrics

    def test_aggregate_metrics_has_source_diversity(self):
        ds = load_dataset("multi_source_15q")
        result = run_dataset(ds, RunConfig(top_k=10, force_mock=True))
        agg = aggregate_metrics(result, 10)
        assert "source_diversity" in agg
        assert 0.0 <= agg["source_diversity"] <= 1.0


class TestTimeSensitiveDataset:
    def test_freshness_dataset_all_flag(self):
        ds = load_dataset("freshness_10q")
        assert len(ds) == 10
        assert all(q.time_sensitive for q in ds)

    def test_multi_source_defaults_false(self):
        ds = load_dataset("multi_source_15q")
        assert len(ds) == 15
        assert all(not q.time_sensitive for q in ds)

    def test_old_datasets_default_false(self):
        ds = load_dataset("vi_general")
        assert all(not q.time_sensitive for q in ds)

    def test_string_values_accepted(self, tmp_path):
        p = tmp_path / "ts.jsonl"
        p.write_text(
            '{"query": "a", "expected_urls": ["a.com"], "time_sensitive": "true"}\n'
            '{"query": "b", "expected_urls": ["b.com"], "time_sensitive": "1"}\n'
            '{"query": "c", "expected_urls": ["c.com"], "time_sensitive": "false"}\n',
            encoding="utf-8",
        )
        ds = load_dataset(str(p))
        assert [q.time_sensitive for q in ds] == [True, True, False]

    def test_to_dict_roundtrip(self):
        q = EvalQuery(query="x", expected_urls=["a.com"], time_sensitive=True)
        assert q.to_dict()["time_sensitive"] is True


class TestCLIReport:
    def _write_report(self, tmp_path, name, rows):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "summary": {"ndcg@10": 0.5},
                    "per_query": [
                        {"index": i, "query": f"q{i}", "retrieved_domains": d} for i, d in rows
                    ],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_cli_report_source_diversity(self, tmp_path, capsys):
        f = self._write_report(tmp_path, "a.json", [(0, ["a.com", "b.com"])])
        assert main(["report", "--file", f]) == 0
        out = capsys.readouterr().out
        assert "source_diversity (mean): 1.0000" in out

    def test_cli_report_compare(self, tmp_path, capsys):
        fa = self._write_report(tmp_path, "a.json", [(0, ["a.com", "b.com"])])
        fb = self._write_report(tmp_path, "b.json", [(0, ["b.com", "c.com"])])
        assert main(["report", "--file", fa, "--compare", fb]) == 0
        out = capsys.readouterr().out
        assert "domain_overlap (mean)" in out

    def test_cli_report_missing_file(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            main(["report", "--file", str(tmp_path / "nope.json")])
