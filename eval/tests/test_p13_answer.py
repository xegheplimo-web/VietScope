"""P13 — /v1/answer endpoint metrics + answer run path."""

import pytest

from eval.answer_metrics import (
    citation_precision,
    citation_recall,
    cited_source_coverage,
    evaluate_answer,
    evidence_quote_rate,
    fact_coverage,
    fold,
    unsupported_claim_rate,
)
from eval.client import AnswerResponse, FallbackClient, HTTPClient, MockClient
from eval.datasets import EvalQuery, load_dataset
from eval.runner import RunConfig, aggregate_metrics, run_answer_dataset


def test_fold_strips_accents():
    assert fold("Giá vàng SJC hôm nay") == fold("gia vang sjc hom nay")
    assert fold("Nghị định 254/2026/NĐ-CP") == fold("nghi dinh 254/2026/nd-cp")


def test_fact_coverage_none_without_facts():
    assert fact_coverage("any answer", None) is None
    assert fact_coverage("any answer", []) is None


def test_fact_coverage_accent_folded_match():
    assert fact_coverage("Giá vàng SJC tăng nhẹ hôm nay", ["gia vang", "SJC"]) == 1.0
    assert fact_coverage("Giá vàng tăng", ["usd"]) == 0.0
    assert fact_coverage("Nghị định 254/2026", ["254/2026", "văn bản"]) == 0.5


def test_citation_precision_and_recall():
    cited = ["https://vbpl.vn/doc1", "https://cafef.vn/x", "https://random.blog/y"]
    expected = ["vbpl.vn", "cafef.vn"]
    assert citation_precision(cited, expected) == pytest.approx(2 / 3)
    assert citation_recall(cited, expected) == 1.0
    assert citation_precision([], expected) is None
    assert citation_recall(cited, None) is None


def test_unsupported_claim_and_quote_rates():
    cits = [
        {"claim_id": "a", "evidence": [{"url": "u1", "quote": "q"}]},
        {"claim_id": "b", "evidence": []},
    ]
    assert unsupported_claim_rate(cits) == 0.5
    assert evidence_quote_rate(cits) == 0.5
    assert unsupported_claim_rate([]) is None
    assert unsupported_claim_rate(None) is None


def test_cited_source_coverage_domains():
    srcs = ["https://vbpl.vn/a", "https://cafef.vn/b", "https://x.vn/c"]
    cited = ["https://vbpl.vn/a", "https://www.cafef.vn/other"]
    assert cited_source_coverage(cited, srcs) == pytest.approx(2 / 3)
    assert cited_source_coverage(cited, []) is None


def test_evaluate_answer_optional_keys_absent():
    m = evaluate_answer(
        answer="an answer",
        cited_urls=["https://a.vn/x"],
        citations=[{"evidence": [{"url": "https://a.vn/x", "quote": "q"}]}],
        source_urls=["https://a.vn/x"],
        source_domains=["a.vn"],
        expected_urls=None,
        expected_facts=None,
        verified=True,
        coverage=0.7,
    )
    assert m["answer_present"] == 1.0
    assert m["verified"] == 1.0
    assert m["coverage"] == 0.7
    assert "answer_correctness" not in m
    assert "citation_precision" not in m
    assert "citation_recall" not in m
    assert m["evidence_quote_rate"] == 1.0
    assert m["cited_source_coverage"] == 1.0


def test_evaluate_answer_full_set():
    m = evaluate_answer(
        answer="Giá vàng SJC hôm nay",
        cited_urls=["https://webgia.com/gold", "https://junk.vn/x"],
        citations=[
            {"evidence": [{"url": "https://webgia.com/gold", "quote": "q"}]},
            {"evidence": []},
        ],
        source_urls=["https://webgia.com/gold", "https://junk.vn/x"],
        source_domains=["webgia.com", "junk.vn"],
        expected_urls=["webgia.com"],
        expected_facts=["gia vang", "SJC"],
        verified=False,
        coverage=0.9,
        authority_scorer={"webgia.com": 1.5, "junk.vn": 0.2}.get,
    )
    assert m["answer_correctness"] == 1.0
    assert m["citation_precision"] == 0.5
    assert m["citation_recall"] == 1.0
    assert m["unsupported_claim_rate"] == 0.5
    assert m["verified"] == 0.0
    assert m["authority"] == pytest.approx(0.85)


def test_mock_client_answer_deterministic():
    c = MockClient()
    a = c.answer("test q", expected_urls=["vnexpress.net"], expected_facts=["fact a"])
    b = c.answer("test q", expected_urls=["vnexpress.net"], expected_facts=["fact a"])
    assert isinstance(a, AnswerResponse)
    assert a.answer == b.answer
    assert a.cited_urls == b.cited_urls
    assert a.sources and a.citations


def test_fallback_client_answer_uses_mock_on_error():
    fc = FallbackClient(HTTPClient(server_url="http://127.0.0.1:1", timeout=0.5))
    resp = fc.answer("anything", expected_facts=["f"])
    assert resp.ok
    assert resp.raw.get("mock_fallback") is True


def test_run_answer_dataset_mock():
    ds = [
        EvalQuery(
            query="gia vang",
            expected_urls=["vnexpress.net"],
            expected_facts=["gia vang"],
            category="vn_market",
        ),
        EvalQuery(query="no facts", expected_urls=["dantri.com.vn"], category="vn_news"),
    ]
    res = run_answer_dataset(ds, RunConfig(force_mock=True), client=MockClient())
    assert len(res.runs) == 2
    m0 = res.runs[0].metrics
    assert "answer_correctness" in m0 or m0.get("answer_present") == 1.0
    agg = aggregate_metrics(res, top_k=10)
    assert agg["answer_present"] == 1.0
    assert "citations_count" in agg
    # query without facts doesn't pollute the denominator
    if "answer_correctness" in res.runs[0].metrics:
        assert agg["answer_correctness_queries"] == 1


def test_vietnam_datasets_expected_facts_load():
    qs = load_dataset("vietnam/legal")
    with_facts = [q for q in qs if q.expected_facts]
    assert with_facts, "expected at least one legal row with expected_facts"
    q = with_facts[0]
    assert isinstance(q.expected_facts, list) and all(isinstance(f, str) for f in q.expected_facts)


def test_expected_facts_backward_compatible():
    q = EvalQuery(query="x")
    assert q.expected_facts == []
    assert q.to_dict()["expected_facts"] == []
