"""Runner — execute a dataset against the Search Router and collect raw results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import DEFAULT_SERVER_URL, Result, SearchClient, build_client
from .datasets import EvalQuery, summary
from .metrics import evaluate_query


@dataclass
class RunConfig:
    top_k: int = 10
    server_url: str = DEFAULT_SERVER_URL
    endpoint: str = "legacy"  # 'legacy' | 'v1' | 'answer'
    timeout: float = 60.0
    providers: list[str] = field(default_factory=lambda: ["searxng"])
    search_type: str = "web"
    force_mock: bool = False
    fallback: bool = True
    cost_per_query_usd: float = 0.0
    answer_mode: str = "balanced"  # /v1/answer mode: fast|balanced|deep


@dataclass
class QueryRun:
    """One dataset query executed against the client, with evaluated metrics."""

    query: EvalQuery
    index: int
    results: list[Result] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float | None = None
    error: str | None = None
    used_mock: bool = False
    metrics: dict[str, float] = field(default_factory=dict)
    # Answer-endpoint extras (empty on retrieval runs).
    answer: str = ""
    cited_urls: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "index": self.index,
            "query": self.query.query,
            "category": self.query.category,
            "region": self.query.region,
            "time_sensitive": self.query.time_sensitive,
            "expected_urls": list(self.query.expected_urls),
            "retrieved_urls": [r.url for r in self.results],
            "retrieved_domains": [r.domain or "" for r in self.results],
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "used_mock": self.used_mock,
            "metrics": dict(self.metrics),
        }
        if self.answer or self.cited_urls or self.query.expected_facts:
            out["answer"] = self.answer
            out["cited_urls"] = list(self.cited_urls)
            out["expected_facts"] = list(self.query.expected_facts)
        return out


@dataclass
class RunResult:
    """Collection of per-query runs + config + dataset summary."""

    config: RunConfig
    dataset: list[EvalQuery]
    runs: list[QueryRun] = field(default_factory=list)
    dataset_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok_runs(self) -> list[QueryRun]:
        return [r for r in self.runs if r.ok]


def run_dataset(
    dataset: list[EvalQuery],
    config: RunConfig | None = None,
    client: SearchClient | None = None,
    authority_scorer=None,
) -> RunResult:
    """Run every query in ``dataset`` and evaluate metrics for each.

    ``client`` is injected for tests; otherwise one is built from ``config``.
    """
    config = config or RunConfig()
    client = client or build_client(
        server_url=config.server_url,
        endpoint=config.endpoint,
        timeout=config.timeout,
        force_mock=config.force_mock,
        fallback=config.fallback,
    )

    runs: list[QueryRun] = []
    for i, q in enumerate(dataset):
        resp = client.search(
            q.query,
            top_k=config.top_k,
            search_type=config.search_type,
            expected_urls=q.expected_urls,
        )
        used_mock = bool(resp.raw.get("mock_fallback")) or config.force_mock
        result_urls = [r.url for r in resp.results]
        results_meta = [r.to_dict() for r in resp.results]
        metrics = evaluate_query(
            result_urls=result_urls,
            expected_urls=q.expected_urls,
            results_meta=results_meta,
            top_k=config.top_k,
            authority_scorer=authority_scorer,
        )
        runs.append(
            QueryRun(
                query=q,
                index=i,
                results=resp.results,
                latency_ms=resp.latency_ms,
                cost_usd=resp.cost_usd,
                error=resp.error,
                used_mock=used_mock,
                metrics=metrics,
            )
        )

    return RunResult(
        config=config,
        dataset=dataset,
        runs=runs,
        dataset_summary=summary(dataset),
    )


def run_answer_dataset(
    dataset: list[EvalQuery],
    config: RunConfig | None = None,
    client: SearchClient | None = None,
    authority_scorer=None,
) -> RunResult:
    """Run every query against ``/v1/answer`` and evaluate answer-level metrics.

    The response's ``sources`` double as the retrieval list, so a single run
    produces both the standard retrieval metrics (ndcg/recall/precision over
    expected_urls) and the answer-level set (correctness, citation precision /
    recall, unsupported-claim rate, cited-source coverage).
    """
    from .answer_metrics import evaluate_answer

    config = config or RunConfig(endpoint="answer")
    client = client or build_client(
        server_url=config.server_url,
        endpoint=config.endpoint,
        timeout=config.timeout,
        force_mock=config.force_mock,
        fallback=config.fallback,
    )

    runs: list[QueryRun] = []
    for i, q in enumerate(dataset):
        resp = client.answer(
            q.query,
            mode=config.answer_mode,
            language="vi" if q.region == "vn" else "en",
            expected_urls=q.expected_urls,
            expected_facts=q.expected_facts,
        )
        used_mock = bool(resp.raw.get("mock_fallback")) or config.force_mock
        source_urls = [r.url for r in resp.sources]
        source_domains = [r.domain or "" for r in resp.sources]
        results_meta = [r.to_dict() for r in resp.sources]
        metrics = evaluate_query(
            result_urls=source_urls,
            expected_urls=q.expected_urls,
            results_meta=results_meta,
            top_k=config.top_k,
            authority_scorer=authority_scorer,
        )
        if resp.ok:
            metrics.update(
                evaluate_answer(
                    answer=resp.answer,
                    cited_urls=resp.cited_urls,
                    citations=resp.citations,
                    source_urls=source_urls,
                    source_domains=source_domains,
                    expected_urls=q.expected_urls,
                    expected_facts=q.expected_facts,
                    verified=resp.verified,
                    coverage=resp.coverage,
                    authority_scorer=authority_scorer,
                )
            )
        runs.append(
            QueryRun(
                query=q,
                index=i,
                results=resp.sources,
                latency_ms=resp.latency_ms,
                cost_usd=resp.cost_usd,
                error=resp.error,
                used_mock=used_mock,
                metrics=metrics,
                answer=resp.answer,
                cited_urls=resp.cited_urls,
                citations=resp.citations,
            )
        )

    return RunResult(
        config=config,
        dataset=dataset,
        runs=runs,
        dataset_summary=summary(dataset),
    )


def aggregate_metrics(run_result: RunResult, top_k: int) -> dict[str, Any]:
    """Mean metrics across queries + latency distribution + cost estimate."""
    ok = run_result.ok_runs
    n = len(ok)
    if n == 0:
        return {
            "error_rate": 1.0 if run_result.runs else 0.0,
            "queries_evaluated": 0,
        }

    def mean_metric(key: str) -> float:
        vals = [r.metrics[key] for r in ok]
        return round(sum(vals) / n, 4)

    def mean_present(key: str) -> float:
        """Mean over runs that emitted ``key`` (optional metrics)."""
        vals = [r.metrics[key] for r in ok if key in r.metrics]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    from .metrics import summarize_latencies

    latencies = [r.latency_ms for r in ok]
    # Explicit API cost where present; otherwise charge the config's per-query
    # estimate (default 0 — the self-hosted SearXNG path is free).
    cost_usd = sum(
        (r.cost_usd if r.cost_usd is not None else run_result.config.cost_per_query_usd) for r in ok
    )

    agg = {
        "queries_evaluated": n,
        "total_queries": len(run_result.runs),
        "error_rate": round(len(run_result.runs) - n, 4) / max(1, len(run_result.runs)),
        "mock_used": int(sum(1 for r in run_result.runs if r.used_mock)),
        "ndcg@10": mean_metric("ndcg@10"),
        f"ndcg@{top_k}": mean_metric(f"ndcg@{top_k}"),
        "mrr": mean_metric("mrr"),
        "recall@5": mean_metric("recall@5"),
        "recall@10": mean_metric("recall@10"),
        "recall@20": mean_metric("recall@20"),
        "precision@10": mean_metric("precision@10"),
        "freshness_ok": mean_metric("freshness_ok"),
        "source_diversity": mean_metric("source_diversity"),
        "latency_ms": summarize_latencies(latencies),
        "cost_usd": cost_usd,
    }
    if "authority" in ok[0].metrics:
        agg["authority"] = mean_metric("authority")
    # Optional answer-level metrics: aggregate over the runs that emitted them
    # (queries without expected_facts/citations simply don't produce a value).
    _OPTIONAL = (
        "answer_present",
        "answer_correctness",
        "citation_precision",
        "citation_recall",
        "unsupported_claim_rate",
        "evidence_quote_rate",
        "cited_source_coverage",
        "verified",
        "coverage",
        "sources_count",
        "citations_count",
    )
    for key in _OPTIONAL:
        if any(key in r.metrics for r in ok):
            agg[key] = mean_present(key)
            agg[f"{key}_queries"] = sum(1 for r in ok if key in r.metrics)
    return agg
