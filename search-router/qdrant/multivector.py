"""Qdrant Sparse/MultiVector retrieval — BGE-M3 sparse, multivector, late interaction.

Phase 7: Add sparse vector and multivector (ColBERT-style) retrieval
to complement dense vectors from Phase 6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qdrant.client import QdrantClient, QdrantPoint, QdrantSearchResult

logger = logging.getLogger(__name__)


@dataclass
class MultiVectorPoint:
    """A point with multiple vectors (ColBERT-style late interaction)."""

    point_id: str
    dense: list[float] | None = None
    sparse: dict[str, float] | None = None
    multivector: list[list[float]] | None = None  # token-level vectors
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class LateInteractionResult:
    """Result from late interaction (MaxSim) scoring."""

    point_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class QdrantMultiVectorRetriever:
    """Multi-vector and sparse retrieval for Qdrant."""

    def __init__(
        self,
        qdrant_client: QdrantClient,
        collection: str = "web_passages_v1",
        top_k: int = 60,
    ):
        self.qdrant = qdrant_client
        self.collection = collection
        self.top_k = top_k

    async def search_sparse(
        self,
        sparse_vector: dict[str, float],
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[QdrantSearchResult]:
        """Search by sparse vector (BGE-M3 lexical weights)."""
        return await self.qdrant.search_sparse(
            collection=self.collection,
            sparse_vector=sparse_vector,
            top_k=top_k or self.top_k,
            filters=filters,
        )

    async def search_multivector(
        self,
        query_vectors: list[list[float]],
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[LateInteractionResult]:
        """Search by multivector (ColBERT-style late interaction).

        For each query token vector, find the best matching document
        token vector (MaxSim), then sum across all query tokens.
        """
        if not query_vectors:
            return []

        # For each query vector, search Qdrant
        all_results: list[QdrantSearchResult] = []
        for q_vec in query_vectors:
            results = await self.qdrant.search_dense(
                collection=self.collection,
                query_vector=q_vec,
                top_k=top_k or self.top_k,
                filters=filters,
            )
            all_results.extend(results)

        # Aggregate by point_id (MaxSim: sum of best scores per query token)
        best_scores: dict[str, float] = {}
        best_payloads: dict[str, dict[str, Any]] = {}

        for result in all_results:
            pid = result.point_id
            if pid not in best_scores or result.score > best_scores[pid]:
                best_scores[pid] = result.score
                best_payloads[pid] = result.payload

        # Sort by score descending
        sorted_ids = sorted(best_scores.keys(), key=lambda x: best_scores[x], reverse=True)
        return [
            LateInteractionResult(
                point_id=pid,
                score=best_scores[pid],
                payload=best_payloads[pid],
            )
            for pid in sorted_ids[: top_k or self.top_k]
        ]

    async def upsert_multivector(
        self,
        points: list[MultiVectorPoint],
    ) -> bool:
        """Upsert points with multivector support."""
        qdrant_points = []
        for p in points:
            vector: dict[str, Any] = {}
            if p.dense is not None:
                vector["dense"] = p.dense
            if p.sparse is not None:
                vector["sparse"] = p.sparse
            if p.multivector is not None:
                vector["multivector"] = p.multivector
            qdrant_points.append(
                QdrantPoint(
                    point_id=p.point_id,
                    dense=p.dense,
                    sparse=p.sparse,
                    payload=p.payload,
                )
            )
        return await self.qdrant.upsert_points(
            collection=self.collection,
            points=qdrant_points,
        )
