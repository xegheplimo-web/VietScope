from __future__ import annotations

from ranking.types import RankedItem

# Default provider quality weights.  Higher means more trusted.
DEFAULT_PROVIDER_WEIGHTS: dict[str, float] = {
    "official": 1.20,
    "github": 1.10,
    "arxiv": 1.05,
    "searxng": 1.00,
    "news": 1.00,
    "reddit": 0.85,
    "forum": 0.80,
    "unknown": 0.70,
}


def _provider_weight(provider: str, weights: dict[str, float]) -> float:
    """Look up a provider weight, falling back to the ``unknown`` default."""
    return weights.get(provider, weights.get("unknown", 1.0))


def _canonical_key(item: RankedItem) -> str:
    """Use the explicit canonical URL or the raw URL as fallback."""
    return (item.canonical_url or item.url).strip().lower()


def fusion(
    results_per_provider: dict[str, list[RankedItem]],
    *,
    k: int = 60,
    provider_weights: dict[str, float] | None = None,
) -> list[RankedItem]:
    """Fuse ranked lists from multiple providers with RRF + quality weighting.

    Reciprocal Rank Fusion (RRF) is combined with per-provider weights.
    Results are deduplicated by ``canonical_url`` when available, otherwise
    by the raw URL.  The returned ``RankedItem`` objects have their
    ``raw_score`` set to the RRF score and ``normalized_score`` re-normalized
    to ``[0, 1]`` across the fused set.

    Args:
        results_per_provider: Mapping from provider name to normalized results.
        k: RRF constant (default 60).
        provider_weights: Optional override for provider quality weights.

    Returns:
        Fused, deduplicated ``RankedItem`` list sorted by ``normalized_score``
        descending.
    """
    if not results_per_provider:
        return []

    weights = provider_weights or DEFAULT_PROVIDER_WEIGHTS
    rrf_by_key: dict[str, float] = {}
    seen_keys: set[str] = set()
    merged: dict[str, RankedItem] = {}

    for provider, items in results_per_provider.items():
        w = _provider_weight(provider, weights)
        for rank, item in enumerate(items, start=1):
            key = _canonical_key(item)
            rrf_by_key[key] = rrf_by_key.get(key, 0.0) + w / (k + rank)

            if key not in seen_keys:
                seen_keys.add(key)
                # Preserve the first (best-ranked) representative.
                merged[key] = item

    if not merged:
        return []

    raw_rrf = list(rrf_by_key.values())
    min_score = min(raw_rrf)
    max_score = max(raw_rrf)

    for key, rrf in rrf_by_key.items():
        item = merged[key]
        item.raw_score = rrf
        if max_score == min_score:
            item.normalized_score = 1.0 if rrf > 0.0 else 0.0
        else:
            item.normalized_score = (rrf - min_score) / (max_score - min_score)

    # Apply a small boost for multi-provider agreement and sort descending.
    out = list(merged.values())
    out.sort(key=lambda x: x.normalized_score, reverse=True)
    return out


def fuse_queries(
    results_per_query: dict[str, list[RankedItem]],
    *,
    k: int = 60,
) -> list[RankedItem]:
    """Fuse ranked lists produced by multiple sub-queries with pure RRF.

    Unlike :func:`fusion` (provider-weighted), every sub-query contributes
    equally: ``score(item) = Σ 1 / (k + rank)`` over each query's ranked list.
    Results are deduplicated by canonical URL, and each surviving item records
    which sub-queries surfaced it under ``metadata["matched_queries"]`` and
    ``metadata["query_hits"]`` — the count acts as a recall-agreement signal
    for downstream quality scoring.

    Args:
        results_per_query: Mapping ``sub-query -> ranked results``.
        k: RRF constant (default 60).

    Returns:
        Deduplicated ``RankedItem`` list sorted by normalized RRF score.
    """
    if not results_per_query:
        return []

    rrf_by_key: dict[str, float] = {}
    merged: dict[str, RankedItem] = {}
    queries_by_key: dict[str, list[str]] = {}

    for query, items in results_per_query.items():
        for rank, item in enumerate(items, start=1):
            key = _canonical_key(item)
            rrf_by_key[key] = rrf_by_key.get(key, 0.0) + 1.0 / (k + rank)
            if key not in merged:
                merged[key] = item
            hits = queries_by_key.setdefault(key, [])
            if query not in hits:
                hits.append(query)

    if not merged:
        return []

    scores = list(rrf_by_key.values())
    lo, hi = min(scores), max(scores)
    for key, item in merged.items():
        rrf = rrf_by_key[key]
        item.raw_score = rrf
        item.normalized_score = 1.0 if hi == lo else (rrf - lo) / (hi - lo)
        item.metadata["matched_queries"] = queries_by_key[key]
        item.metadata["query_hits"] = len(queries_by_key[key])

    out = list(merged.values())
    out.sort(key=lambda x: x.normalized_score, reverse=True)
    return out
