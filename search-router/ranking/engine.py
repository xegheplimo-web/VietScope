from __future__ import annotations

from typing import Any

from models import SearchResultItem

from ranking.fusion import fusion
from ranking.normalize import normalize_scores
from ranking.rerank import rerank
from ranking.types import RankedItem


def rank_search(
    results_by_provider: dict[str, list[SearchResultItem | dict[str, Any]]],
    query: str,
    *,
    top_k: int = 10,
    reranker: Any | None = None,
    method: str = "minmax",
    provider_weights: dict[str, float] | None = None,
) -> list[RankedItem]:
    """Unified ranking pipeline: normalize, fuse, rerank.

    This is the main entry point.  It accepts raw results per provider,
    normalizes their scores, fuses them with RRF + weighted fusion, and
    returns a top-``k`` list scored by the quality model.

    Args:
        results_by_provider: Mapping ``provider_name -> raw results``.
        query: The user query string.
        top_k: Number of results to return (default 10).
        reranker: Optional semantic embedding reranker.
        method: Normalization method, ``"minmax"`` or ``"zscore"``.
        provider_weights: Optional provider quality weights.

    Returns:
        Ranked top-``k`` ``RankedItem`` list.
    """
    normalized = {
        provider: normalize_scores(results, provider=provider, method=method)
        for provider, results in results_by_provider.items()
    }
    fused = fusion(normalized, provider_weights=provider_weights)
    return rerank(fused, query, reranker=reranker, top_k=top_k)
