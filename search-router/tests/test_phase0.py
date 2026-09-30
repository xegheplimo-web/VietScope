"""Tests for Phase 0 modules: evidence_bundle, tracer, search_eval.

(``pipeline.budget.RequestBudget`` was dead code — tests-only — deleted in
phase-0 dedup T1b; the canonical budget is ``core.budget.SearchBudget``.)
"""

import json
import time
from pathlib import Path

import pytest
from eval.search_eval import SearchEval
from pipeline.evidence_bundle import (
    AnswerConstraints,
    Coverage,
    EvidenceBundle,
    Plan,
    Source,
    SubQuery,
    Sufficiency,
    Trace,
)
from telemetry.tracer import SearchTracer

# ─── EvidenceBundle ──────────────────────────────────────────────────────────


class TestEvidenceBundle:
    def _make_bundle(self) -> EvidenceBundle:
        return EvidenceBundle(
            query_id="test_001",
            query={"raw": "test query", "lang": "vi"},
            plan=Plan(
                mode="normal",
                intent="pricing_lookup",
                freshness_class="high",
                rounds_executed=1,
                subqueries=[SubQuery(id="Q1", text="test query", covered=True)],
            ),
            sources=[
                Source(
                    source_id="S01",
                    url="https://example.com",
                    canonical_url="https://example.com",
                    domain="example.com",
                    title="Test Source",
                    source_type="official",
                    authority=0.95,
                    authority_reason="vendor_official_docs",
                    published_at="2026-05-12",
                    published_confidence=0.93,
                    crawled_at="2026-06-14T09:12:03Z",
                    lang="en",
                    paywalled=False,
                )
            ],
            coverage=Coverage(ratio=1.0, uncovered_subqueries=[]),
            sufficiency=Sufficiency(status="sufficient", score=0.88),
            citation_whitelist=["S01"],
            answer_constraints=AnswerConstraints(
                must_cite_every_factual_sentence=True,
                must_surface_conflicts=[],
                must_state_as_of_date="2026-05-12",
                forbid_external_knowledge=True,
                answer_lang="vi",
                max_words=400,
            ),
            trace=Trace(
                urls_discovered=47,
                urls_fetched=11,
                passages_indexed=118,
                passages_after_rrf=42,
                after_rerank=11,
                after_gate=6,
                latency_ms={"plan": 38, "retrieve": 940, "fetch": 2180},
                degraded_stages=[],
            ),
        )

    def test_bundle_creation(self):
        bundle = self._make_bundle()
        assert bundle.schema_version == "2.0"
        assert bundle.query_id == "test_001"
        assert bundle.plan.mode == "normal"
        assert len(bundle.sources) == 1
        assert bundle.sources[0].source_id == "S01"

    def test_bundle_to_dict(self):
        bundle = self._make_bundle()
        data = bundle.to_dict()
        assert data["schema_version"] == "2.0"
        assert data["query_id"] == "test_001"
        assert data["plan"]["mode"] == "normal"
        assert len(data["sources"]) == 1
        assert data["sources"][0]["source_id"] == "S01"
        assert data["coverage"]["ratio"] == 1.0
        assert data["sufficiency"]["status"] == "sufficient"
        assert data["citation_whitelist"] == ["S01"]
        assert data["trace"]["urls_discovered"] == 47

    def test_bundle_to_json(self):
        bundle = self._make_bundle()
        json_str = bundle.to_json()
        data = json.loads(json_str)
        assert data["schema_version"] == "2.0"
        assert data["query_id"] == "test_001"

    def test_bundle_fingerprint(self):
        bundle = self._make_bundle()
        fp = bundle.fingerprint
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_bundle_fingerprint_stable(self):
        bundle = self._make_bundle()
        fp1 = bundle.fingerprint
        fp2 = bundle.fingerprint
        assert fp1 == fp2


# ─── SearchTracer ───────────────────────────────────────────────────────────


class TestSearchTracer:
    def test_tracer_disabled(self):
        tracer = SearchTracer("test_001", enabled=False)
        with tracer.span("test_stage"):
            pass
        assert len(tracer.stages) == 0

    def test_tracer_enabled(self):
        tracer = SearchTracer("test_001", enabled=True)
        with tracer.span("test_stage", key="value"):
            pass
        assert len(tracer.stages) == 1
        assert tracer.stages[0].stage == "test_stage"
        assert tracer.stages[0].status == "ok"
        assert tracer.stages[0].metadata["key"] == "value"

    def test_tracer_error(self):
        tracer = SearchTracer("test_001", enabled=True)
        with pytest.raises(ValueError):
            with tracer.span("test_stage"):
                raise ValueError("test error")
        assert len(tracer.stages) == 1
        assert tracer.stages[0].status == "error"
        assert tracer.stages[0].metadata["error"] == "test error"

    def test_tracer_summary(self):
        tracer = SearchTracer("test_001", enabled=True)
        # 20ms — Windows timer granularity (~15.6ms) can make 1ms sleeps
        # measure as 0.0, which flakes the total_ms > 0 assertion below.
        with tracer.span("stage1"):
            time.sleep(0.02)  # ensure measurable duration
        with tracer.span("stage2"):
            time.sleep(0.02)
        summary = tracer.summary()
        assert summary["request_id"] == "test_001"
        assert len(summary["stages"]) == 2
        assert summary["total_ms"] > 0


# ─── SearchEval ─────────────────────────────────────────────────────────────


class TestSearchEval:
    def test_load_golden_set(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://example.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        assert len(eval.golden_queries) == 1
        assert eval.golden_queries["gq_001"].query_id == "gq_001"

    def test_evaluate_query(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://example.com", "https://example2.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        result = eval.evaluate(
            query_id="gq_001",
            retrieved_urls=["https://example.com", "https://other.com"],
            answer="test answer",
            citations=["S01"],
            latency_ms=100.0,
        )
        assert result.query_id == "gq_001"
        assert result.recall_at_k == 0.5  # 1 of 2 relevant
        assert result.precision_at_k == 0.5  # 1 of 2 retrieved
        assert result.mrr == 1.0  # first result is relevant

    def test_evaluate_unknown_query(self, tmp_path: Path):
        golden_data = {"queries": []}
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        result = eval.evaluate(
            query_id="unknown",
            retrieved_urls=["https://example.com"],
        )
        assert result.query_id == "unknown"
        assert result.recall_at_k == 0.0

    def test_report(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://example.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        eval.evaluate(
            query_id="gq_001",
            retrieved_urls=["https://example.com"],
            answer="test answer",
            citations=["S01"],
            latency_ms=100.0,
        )
        report = eval.report()
        assert report["queries_evaluated"] == 1
        assert report["avg_recall_at_50"] == 1.0
        assert report["avg_precision_at_10"] == 1.0
        assert report["avg_mrr"] == 1.0
        assert report["avg_latency_ms"] == 100.0

    def test_report_empty(self, tmp_path: Path):
        golden_data = {"queries": []}
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        report = eval.report()
        assert "error" in report

    def test_ndcg_perfect(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://a.com", "https://b.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        result = eval.evaluate(
            query_id="gq_001",
            retrieved_urls=["https://a.com", "https://b.com", "https://c.com"],
        )
        assert result.ndcg_at_k == 1.0  # perfect ranking

    def test_mrr_first_position(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://a.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        result = eval.evaluate(
            query_id="gq_001",
            retrieved_urls=["https://a.com", "https://b.com"],
        )
        assert result.mrr == 1.0

    def test_mrr_second_position(self, tmp_path: Path):
        golden_data = {
            "queries": [
                {
                    "query_id": "gq_001",
                    "query": "test query",
                    "lang": "vi",
                    "intent": "pricing_lookup",
                    "relevant_urls": ["https://a.com"],
                    "gold_answer": "test answer",
                    "gold_facts": ["fact1"],
                    "freshness_class": "high",
                    "mode": "normal",
                }
            ]
        }
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")

        eval = SearchEval(golden_file)
        result = eval.evaluate(
            query_id="gq_001",
            retrieved_urls=["https://b.com", "https://a.com"],
        )
        assert result.mrr == 0.5
