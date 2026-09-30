"""Search-Eval framework — golden set + offline evaluation.

Stores golden queries with expected URLs, answers, and facts.
Runs offline evaluation to measure nDCG, MRR, citation_precision,
and other metrics before any ranking change reaches production.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GoldenQuery:
    """Single golden query with ground truth."""

    query_id: str
    query: str
    lang: str = "en"
    intent: str = ""
    relevant_urls: list[str] = field(default_factory=list)
    gold_answer: str = ""
    gold_facts: list[str] = field(default_factory=list)
    freshness_class: str = "medium"
    mode: str = "normal"


@dataclass
class EvalResult:
    """Result of one evaluation run."""

    query_id: str
    retrieved_urls: list[str]
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    degraded: bool = False

    # Metrics (computed by evaluator)
    recall_at_k: float = 0.0
    precision_at_k: float = 0.0
    ndcg_at_k: float = 0.0
    mrr: float = 0.0
    citation_precision: float = 0.0
    citation_recall: float = 0.0


class SearchEval:
    """Offline evaluator for Search-Hub ranking changes.

    Usage:
        eval = SearchEval("eval/golden_set.json")
        result = eval.evaluate(query_id, retrieved_urls, answer, citations)
        report = eval.report()  # aggregate metrics
    """

    def __init__(self, golden_set_path: str | Path):
        self.golden_set_path = Path(golden_set_path)
        self.golden_queries: dict[str, GoldenQuery] = {}
        self.results: list[EvalResult] = []
        self._load()

    def _load(self) -> None:
        """Load golden set from JSON file."""
        if not self.golden_set_path.exists():
            logger.warning("golden set not found: %s", self.golden_set_path)
            return

        data = json.loads(self.golden_set_path.read_text(encoding="utf-8"))
        for item in data.get("queries", []):
            gq = GoldenQuery(**item)
            self.golden_queries[gq.query_id] = gq

        logger.info("loaded %d golden queries", len(self.golden_queries))

    def evaluate(
        self,
        query_id: str,
        retrieved_urls: list[str],
        answer: str = "",
        citations: list[str] | None = None,
        latency_ms: float = 0.0,
        degraded: bool = False,
    ) -> EvalResult:
        """Evaluate a single query result against golden data."""
        gq = self.golden_queries.get(query_id)
        if not gq:
            logger.warning("query_id %s not in golden set", query_id)
            return EvalResult(query_id=query_id, retrieved_urls=retrieved_urls)

        result = EvalResult(
            query_id=query_id,
            retrieved_urls=retrieved_urls,
            answer=answer,
            citations=citations or [],
            latency_ms=latency_ms,
            degraded=degraded,
        )

        # Compute metrics
        if gq.relevant_urls:
            result.recall_at_k = self._recall_at_k(retrieved_urls, gq.relevant_urls, k=50)
            result.precision_at_k = self._precision_at_k(retrieved_urls, gq.relevant_urls, k=10)
            result.ndcg_at_k = self._ndcg_at_k(retrieved_urls, gq.relevant_urls, k=20)
            result.mrr = self._mrr(retrieved_urls, gq.relevant_urls)

        self.results.append(result)
        return result

    def report(self) -> dict[str, Any]:
        """Aggregate metrics across all evaluated queries."""
        if not self.results:
            return {"error": "no results"}

        n = len(self.results)
        return {
            "queries_evaluated": n,
            "avg_recall_at_50": round(sum(r.recall_at_k for r in self.results) / n, 4),
            "avg_precision_at_10": round(sum(r.precision_at_k for r in self.results) / n, 4),
            "avg_ndcg_at_20": round(sum(r.ndcg_at_k for r in self.results) / n, 4),
            "avg_mrr": round(sum(r.mrr for r in self.results) / n, 4),
            "avg_citation_precision": round(sum(r.citation_precision for r in self.results) / n, 4),
            "avg_latency_ms": round(sum(r.latency_ms for r in self.results) / n, 1),
            "degraded_count": sum(1 for r in self.results if r.degraded),
        }

    @staticmethod
    def _recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
        if not relevant:
            return 0.0
        top_k = retrieved[:k]
        hits = sum(1 for url in relevant if url in top_k)
        return hits / len(relevant)

    @staticmethod
    def _precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
        if not retrieved:
            return 0.0
        top_k = retrieved[:k]
        hits = sum(1 for url in top_k if url in relevant)
        return hits / len(top_k)

    @staticmethod
    def _ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
        if not relevant:
            return 0.0
        top_k = retrieved[:k]
        dcg = 0.0
        for i, url in enumerate(top_k):
            if url in relevant:
                dcg += 1.0 / math.log2(i + 2)
        ideal = [1.0 / math.log2(i + 2) for i in range(min(len(relevant), k))]
        idcg = sum(ideal)
        return dcg / idcg if idcg > 0 else 0.0

    @staticmethod
    def _mrr(retrieved: list[str], relevant: list[str]) -> float:
        for i, url in enumerate(retrieved):
            if url in relevant:
                return 1.0 / (i + 1)
        return 0.0
