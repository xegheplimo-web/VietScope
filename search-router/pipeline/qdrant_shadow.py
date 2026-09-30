"""Qdrant Dense Shadow Mode — run Qdrant retrieval in background.

For every normal query:
1. Serve existing OpenSearch result
2. In background: run Qdrant dense retrieval
3. Log comparison (Recall, nDCG, latency, candidate overlap)

Does NOT serve Qdrant ranking yet — only collects metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from qdrant.client import QdrantClient

logger = logging.getLogger(__name__)


@dataclass
class ShadowComparison:
    """Comparison between OpenSearch and Qdrant results."""

    query: str
    query_id: str
    opensearch_results: list[str] = field(default_factory=list)  # doc_ids
    qdrant_results: list[str] = field(default_factory=list)  # doc_ids
    opensearch_latency_ms: float = 0.0
    qdrant_latency_ms: float = 0.0
    recall_at_50: float = 0.0
    ndcg_at_10: float = 0.0
    candidate_overlap: float = 0.0
    timestamp: float = field(default_factory=time.time)


class QdrantShadowMode:
    """Shadow mode runner for Qdrant dense retrieval."""

    def __init__(
        self,
        qdrant_client: QdrantClient,
        collection: str = "web_passages_v1",
        top_k: int = 60,
    ):
        self.qdrant = qdrant_client
        self.collection = collection
        self.top_k = top_k
        self._comparisons: list[ShadowComparison] = []

    async def run_shadow(
        self,
        query: str,
        query_id: str,
        opensearch_results: list[dict[str, Any]],
        query_vector: list[float],
    ) -> ShadowComparison:
        """Run Qdrant dense retrieval in background and compare."""
        start = time.monotonic()

        # Run Qdrant dense search
        qdrant_results = await self.qdrant.search_dense(
            collection=self.collection,
            query_vector=query_vector,
            top_k=self.top_k,
        )

        elapsed_ms = (time.monotonic() - start) * 1000

        # Extract doc_ids
        os_ids = [r.get("doc_id", r.get("passage_id", "")) for r in opensearch_results]
        qdrant_ids = [r.point_id for r in qdrant_results]

        # Compute metrics
        recall = self._compute_recall(os_ids, qdrant_ids)
        overlap = self._compute_overlap(os_ids, qdrant_ids)

        comparison = ShadowComparison(
            query=query,
            query_id=query_id,
            opensearch_results=os_ids,
            qdrant_results=qdrant_ids,
            qdrant_latency_ms=elapsed_ms,
            recall_at_50=recall,
            candidate_overlap=overlap,
        )

        self._comparisons.append(comparison)
        logger.info(
            "Shadow comparison: query=%s recall=%.3f overlap=%.3f qdrant_ms=%.1f",
            query_id,
            recall,
            overlap,
            elapsed_ms,
        )

        return comparison

    def _compute_recall(
        self,
        opensearch_ids: list[str],
        qdrant_ids: list[str],
    ) -> float:
        """Compute recall@50: fraction of OpenSearch results found in Qdrant."""
        if not opensearch_ids:
            return 0.0
        qdrant_set = set(qdrant_ids)
        hits = sum(1 for pid in opensearch_ids if pid in qdrant_set)
        return hits / len(opensearch_ids)

    def _compute_overlap(
        self,
        opensearch_ids: list[str],
        qdrant_ids: list[str],
    ) -> float:
        """Compute candidate overlap: Jaccard similarity."""
        if not opensearch_ids or not qdrant_ids:
            return 0.0
        os_set = set(opensearch_ids)
        qdrant_set = set(qdrant_ids)
        intersection = len(os_set & qdrant_set)
        union = len(os_set | qdrant_set)
        return intersection / union if union > 0 else 0.0

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics of all shadow comparisons."""
        if not self._comparisons:
            return {"total": 0}

        total = len(self._comparisons)
        avg_recall = sum(c.recall_at_50 for c in self._comparisons) / total
        avg_overlap = sum(c.candidate_overlap for c in self._comparisons) / total
        avg_qdrant_ms = sum(c.qdrant_latency_ms for c in self._comparisons) / total

        return {
            "total": total,
            "avg_recall_at_50": avg_recall,
            "avg_candidate_overlap": avg_overlap,
            "avg_qdrant_latency_ms": avg_qdrant_ms,
        }
