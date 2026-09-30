from __future__ import annotations

import statistics
from typing import Any

from models import SearchResultItem

from ranking.types import RankedItem


def normalize_scores(
    results: list[SearchResultItem | dict[str, Any]],
    *,
    method: str = "minmax",
    provider: str = "",
) -> list[RankedItem]:
    """Normalize raw provider scores to a common scale.

    Accepts a list of ``SearchResultItem`` or plain dictionaries and returns
    ``RankedItem`` objects carrying both the original ``raw_score`` and the
    ``normalized_score`` in ``[0, 1]`` (for min-max) or z-scores.

    Args:
        results: Raw provider results.
        method: ``"minmax"`` (default) or ``"zscore"``.
        provider: Optional provider name to tag on each item.

    Returns:
        A list of ``RankedItem`` with ``raw_score`` and ``normalized_score``.
    """
    ranked = [RankedItem.from_search_result(result, provider=provider) for result in results]

    if not ranked:
        return ranked

    raw_scores = [item.raw_score for item in ranked]
    min_score = min(raw_scores)
    max_score = max(raw_scores)

    if method == "minmax":
        if max_score == min_score:
            # Edge case: all identical scores.
            if max_score == 0.0:
                normalized = [0.0 for _ in raw_scores]
            else:
                normalized = [1.0 for _ in raw_scores]
        else:
            span = max_score - min_score
            normalized = [(s - min_score) / span for s in raw_scores]
    elif method == "zscore":
        mean = statistics.mean(raw_scores)
        stdev = statistics.pstdev(raw_scores)
        if stdev == 0.0:
            normalized = [0.0 for _ in raw_scores]
        else:
            normalized = [(s - mean) / stdev for s in raw_scores]
    else:
        raise ValueError(f"Unknown normalization method: {method}")

    for item, norm in zip(ranked, normalized, strict=True):
        item.normalized_score = norm

    return ranked
