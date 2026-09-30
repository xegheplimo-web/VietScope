"""Integration tests: dataset loading, mock-runner end-to-end, report shape."""

import json

import pytest

from eval.client import MockClient
from eval.datasets import (
    VALID_CATEGORIES,
    EvalQuery,
    list_datasets,
    load_dataset,
    summary,
)
from eval.report import build_report, format_per_query, format_summary
from eval.runner import RunConfig, RunResult, aggregate_metrics, run_dataset


def test_list_datasets_contains_samples():
    names = list_datasets()
    assert "vi_general" in names
    assert "current_events" in names


def test_vi_general_dataset_shape():
    ds = load_dataset("vi_general")
    assert 10 <= len(ds) <= 25
    for q in ds:
        assert q.query.strip()
        assert q.expected_urls, "every query must have ground truth"
        assert q.category in VALID_CATEGORIES
        assert q.region in ("vn", "global")


def test_current_events_dataset_shape():
    ds = load_dataset("current_events")
    assert 5 <= len(ds) <= 15
    for q in ds:
        assert q.category == "current_events"
        assert q.expected_urls


def test_dataset_summary():
    ds = load_dataset("vi_general")
    s = summary(ds)
    assert s["queries"] == len(ds)
    assert s["by_category"]["vi_general"] == len(ds)


def test_load_dataset_rejects_invalid_category(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(
        '{"query": "x", "expected_urls": ["a.com"], "category": "bogus"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="category"):
        load_dataset(str(p))


def test_load_dataset_rejects_missing_query(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"expected_urls": ["a.com"]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="query"):
        load_dataset(str(p))


def test_load_dataset_skips_blank_and_comments(tmp_path):
    p = tmp_path / "ok.jsonl"
    p.write_text(
        '# a comment\n\n{"query": "a", "expected_urls": ["a.com"]}\n',
        encoding="utf-8",
    )
    ds = load_dataset(str(p))
    assert len(ds) == 1


class TestMockRun:
    def test_run_dataset_mock_end_to_end(self):
        ds = load_dataset("vi_general")
        config = RunConfig(top_k=5, force_mock=True, providers=["searxng"])
        result = run_dataset(ds, config)
        assert isinstance(result, RunResult)
        assert len(result.runs) == len(ds)
        for run in result.runs:
            assert run.ok
            assert run.used_mock
            assert len(run.results) <= 5
            assert 0.0 <= run.metrics["ndcg@10"] <= 1.0
            assert 0.0 <= run.metrics["mrr"] <= 1.0

    def test_mock_is_deterministic(self):
        ds = load_dataset("current_events")
        cfg = RunConfig(top_k=10, force_mock=True)
        r1 = run_dataset(ds, cfg)
        r2 = run_dataset(ds, cfg)
        assert [r.results[0].url for r in r1.runs] == [r.results[0].url for r in r2.runs]

    def test_mock_client_respects_expected_domains(self):
        client = MockClient(seed_offset=0)
        q = EvalQuery(
            query="cach nau pho bo",
            expected_urls=["vnexpress.net"],
            category="vi_general",
            region="vn",
        )
        found = False
        for _ in range(10):
            resp = client.search(q.query, 20, "web", expected_urls=q.expected_urls)
            if any(r.domain == "vnexpress.net" for r in resp.results):
                found = True
                break
        assert found, "mock should surface expected domains for some seeds"

    def test_aggregate_metrics_and_report_shape(self):
        ds = load_dataset("vi_general")
        result = run_dataset(ds, RunConfig(top_k=10, force_mock=True))
        agg = aggregate_metrics(result, 10)
        assert agg["queries_evaluated"] == len(ds)
        assert 0.0 <= agg["ndcg@10"] <= 1.0
        assert agg["error_rate"] == 0.0
        assert set(agg["latency_ms"]) == {"mean_ms", "p50_ms", "p95_ms", "p99_ms"}

        report = build_report(result, "vi_general", note="test")
        assert report["meta"]["dataset"] == "vi_general"
        assert report["meta"]["top_k"] == 10
        assert report["summary"]["queries_evaluated"] == len(ds)
        assert len(report["per_query"]) == len(ds)
        assert set(report["by_category"]) == {"vi_general"}
        # JSON-serializable (ensure_ascii=False round trip)
        json.dumps(report, ensure_ascii=False)

    def test_report_has_expected_urls_and_retrieved(self):
        ds = load_dataset("vi_general")
        result = run_dataset(ds, RunConfig(top_k=10, force_mock=True))
        report = build_report(result, "vi_general")
        first = report["per_query"][0]
        assert first["expected_urls"]
        assert "retrieved_urls" in first
        assert "retrieved_domains" in first
        assert "latency_ms" in first
        assert "metrics" in first

    def test_format_functions_do_not_crash(self):
        ds = load_dataset("current_events")
        result = run_dataset(ds, RunConfig(top_k=5, force_mock=True))
        report = build_report(result, "current_events")
        assert format_summary(report)
        assert format_per_query(report)


def test_run_with_injected_client_marks_error():
    class FailingClient:
        def search(self, query, top_k, search_type="web", expected_urls=None):
            from eval.client import SearchResponse

            return SearchResponse(query=query, error="boom", latency_ms=0.0)

    ds = load_dataset("vi_general")
    result = run_dataset(ds, RunConfig(top_k=5), client=FailingClient())
    assert all(not r.ok for r in result.runs)
    agg = aggregate_metrics(result, 5)
    assert agg["error_rate"] == 1.0
