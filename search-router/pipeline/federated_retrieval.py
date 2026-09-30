"""Federated Retrieval — OpenSearch BM25 + Qdrant dense → RRF fusion.

Runs both retrieval lanes in parallel and fuses results with RRF (k=60).
Controlled by feature flags:
- HYBRID_RETRIEVAL_ENABLED (api/v1.py gate)
- QDRANT_ENABLED / QDRANT_DENSE_ENABLED
- OPENSEARCH_ENABLED
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from opensearch.client import OpenSearchClient
from qdrant.client import QdrantClient, QdrantSearchResult

logger = logging.getLogger(__name__)


@dataclass
class FederatedResult:
    """Result from federated retrieval."""

    doc_ids: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    # Full fused docs (``source`` for OpenSearch hits, ``payload`` for
    # Qdrant hits) — needed to project hits onto SourceResult candidates.
    results: list[dict[str, Any]] = field(default_factory=list)
    opensearch_count: int = 0
    qdrant_count: int = 0
    fused_count: int = 0
    latency_ms: float = 0.0
    degraded: bool = False
    degraded_reason: str = ""

    def to_source_results(self, top_n: int | None = None) -> list:
        """Project fused docs onto ``SourceResult`` objects for the rerank pool.

        One entry per URL (best RRF score kept) — passage-level doc_ids share
        a document URL and downstream fusion dedups by canonical URL anyway.
        """
        from research_models.research_state import SourceResult

        docs = self.results[:top_n] if top_n else self.results
        best_by_url: dict[str, Any] = {}
        for doc in docs:
            src = doc.get("source") or doc.get("payload") or {}
            url = src.get("canonical_url") or src.get("url") or ""
            if not url:
                continue
            score = float(doc.get("score") or 0.0)
            existing = best_by_url.get(url)
            if existing is not None and existing.score >= score:
                continue
            best_by_url[url] = SourceResult(
                source_id=f"hyb_{len(best_by_url):03d}",
                url=url,
                title=src.get("title") or "",
                description=(src.get("text_ctx") or src.get("text") or "")[:400],
                domain=src.get("domain") or (url.split("/")[2] if "://" in url else ""),
                score=score,
                published_at=src.get("published_at"),
            )
        return list(best_by_url.values())


class FederatedRetriever:
    """Federated retrieval: OpenSearch + Qdrant → RRF."""

    def __init__(
        self,
        opensearch_client: OpenSearchClient,
        qdrant_client: QdrantClient,
        opensearch_index: str = "web_passages",
        qdrant_collection: str = "web_passages_v1",
        rrf_k: int = 60,
        top_k: int = 60,
    ):
        self.opensearch = opensearch_client
        self.qdrant = qdrant_client
        self.opensearch_index = opensearch_index
        self.qdrant_collection = qdrant_collection
        self.rrf_k = rrf_k
        self.top_k = top_k

    async def retrieve(
        self,
        query: str,
        query_vector: list[float] | None = None,
        filters: dict[str, Any] | None = None,
        enable_qdrant: bool = True,
        enable_opensearch: bool = True,
    ) -> FederatedResult:
        """Run federated retrieval with RRF fusion."""
        start = time.monotonic()

        # Run both lanes in parallel
        os_task = (
            self._search_opensearch(query, filters)
            if enable_opensearch
            else asyncio.sleep(0, result=[])
        )
        qdrant_task = (
            self._search_qdrant(query_vector, filters)
            if enable_qdrant and query_vector
            else asyncio.sleep(0, result=[])
        )

        os_results, qdrant_results = await asyncio.gather(
            os_task,
            qdrant_task,
            return_exceptions=True,
        )

        # Capture lane failures BEFORE substituting empty lists — the degraded
        # flag below must reflect real errors, not the post-substitution state.
        os_failed = isinstance(os_results, Exception)
        qdrant_failed = isinstance(qdrant_results, Exception)
        if os_failed:
            logger.warning("OpenSearch search failed: %s", os_results)
            os_results = []
        if qdrant_failed:
            logger.warning("Qdrant search failed: %s", qdrant_results)
            qdrant_results = []

        # Fuse with RRF
        fused = self._rrf_fuse(os_results, qdrant_results)

        elapsed_ms = (time.monotonic() - start) * 1000
        top = fused[: self.top_k]

        return FederatedResult(
            doc_ids=[r["doc_id"] for r in top],
            scores=[r["score"] for r in top],
            results=top,
            opensearch_count=len(os_results),
            qdrant_count=len(qdrant_results),
            fused_count=len(fused),
            latency_ms=elapsed_ms,
            degraded=os_failed or qdrant_failed,
            degraded_reason=(
                "opensearch_failed" if os_failed else "qdrant_failed" if qdrant_failed else ""
            ),
        )

    async def _search_opensearch(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search OpenSearch BM25."""
        results = await self.opensearch.search(
            index=self.opensearch_index,
            query=query,
            top_k=self.top_k,
            filters=filters,
        )
        # OpenSearchClient swallows connection errors into [] — probe the lane
        # on empty results so a dead backend surfaces as degraded, not as a
        # legitimate zero-hit response. Clients without a health probe are
        # assumed healthy (empty = genuine zero-hit).
        probe = getattr(self.opensearch, "health", None)
        if not results and probe is not None and not await self._lane_alive(probe):
            raise RuntimeError("opensearch lane unreachable")
        return results

    async def _search_qdrant(
        self,
        query_vector: list[float] | None,
        filters: dict[str, Any] | None = None,
    ) -> list[QdrantSearchResult]:
        """Search Qdrant dense."""
        if not query_vector:
            return []
        results = await self.qdrant.search_dense(
            collection=self.qdrant_collection,
            query_vector=query_vector,
            top_k=self.top_k,
            filters=filters,
        )
        # Same swallowed-error problem as OpenSearch — an unreachable service
        # or missing collection must read as degraded, not zero hits.
        probe = getattr(self.qdrant, "collection_exists", None)
        if (
            not results
            and probe is not None
            and not await self._lane_alive(lambda: probe(self.qdrant_collection))
        ):
            raise RuntimeError("qdrant lane unreachable")
        return results

    @staticmethod
    async def _lane_alive(probe) -> bool:
        """Run a lane health probe (sync or async) without blocking the loop."""
        try:
            result = (
                await probe()
                if asyncio.iscoroutinefunction(probe)
                else await asyncio.to_thread(probe)
            )
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    def _rrf_fuse(
        self,
        opensearch_results: list[dict[str, Any]],
        qdrant_results: list[QdrantSearchResult],
    ) -> list[dict[str, Any]]:
        """Fuse results with RRF (k=60)."""
        rrf_scores: dict[str, float] = {}
        doc_map: dict[str, dict[str, Any]] = {}

        # Score OpenSearch results
        for rank, doc in enumerate(opensearch_results):
            doc_id = doc.get("doc_id", doc.get("passage_id", ""))
            if not doc_id:
                continue
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank + 1)
            doc_map[doc_id] = doc

        # Score Qdrant results
        for rank, result in enumerate(qdrant_results):
            doc_id = result.point_id
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank + 1)
            if doc_id not in doc_map:
                doc_map[doc_id] = {"doc_id": doc_id, "payload": result.payload}

        # Sort by RRF score. Doc fields spread first, then doc_id/score are
        # pinned so the fused ``score`` is always the RRF score (never the
        # raw per-lane score carried inside the doc dict).
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
        return [
            {**doc_map[doc_id], "doc_id": doc_id, "score": rrf_scores[doc_id]}
            for doc_id in sorted_ids
        ]
