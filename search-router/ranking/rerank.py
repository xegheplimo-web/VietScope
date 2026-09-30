from __future__ import annotations

import math
from collections import Counter
from typing import Any

from ranking.quality import quality_score
from ranking.types import RankedItem

EmbeddingReranker: Any | None = None
try:
    from pipeline.embeddings import EmbeddingReranker as _EmbeddingReranker

    EmbeddingReranker = _EmbeddingReranker
except (
    ImportError,
    ModuleNotFoundError,
):  # pragma: no cover - pluggable interface fallback
    pass


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _domain_from_url(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except (ValueError, TypeError, AttributeError):
        return ""


def _default_reranker() -> Any | None:
    """Return a default embedding reranker if the wave6 module is available."""
    if EmbeddingReranker is None:
        return None
    try:
        return EmbeddingReranker()
    except (ValueError, TypeError, OSError, AttributeError):
        return None


def _reranker_available(reranker: Any) -> bool:
    """Check whether a reranker object is configured and ready."""
    if reranker is None:
        return False
    available = getattr(reranker, "available", None)
    if available is None:
        return True
    return bool(available())


def _embed_docs(reranker: Any, query: str, docs: list[str]) -> list[list[float]] | None:
    """Embed query + docs using a pluggable reranker interface."""
    if not docs:
        return None
    try:
        vectors = reranker.embed([query] + docs)
    except (ValueError, TypeError, OSError, AttributeError):
        return None
    if vectors is None or len(vectors) != len(docs) + 1:
        return None
    return vectors


def rerank(
    results: list[RankedItem],
    query: str,
    *,
    reranker: Any | None = None,
    top_k: int = 10,
    use_quality: bool = True,
    semantic_weight: float = 0.5,
) -> list[RankedItem]:
    """Staged rerank: cheap quality scoring, then optional semantic rerank.

    The pipeline first scores every item with ``quality_score`` (cheap fusion),
    sorts, then runs an optional embedding reranker over the top ``top_k * 2``
    candidates.  Semantic scores are blended into ``final_score`` and the list
    is re-sorted before the final ``top_k`` cut.

    Args:
        results: Fused ``RankedItem`` list.
        query: The user query.
        reranker: Pluggable embedding reranker with ``available()`` and
            ``embed(texts)``.  ``None`` falls back to the keyword stage.
        top_k: Number of results to return (default 10).
        use_quality: Run the cheap ``quality_score`` stage.
        semantic_weight: Blend weight for semantic vs. quality scores (0-1).

    Returns:
        Top-``k`` ``RankedItem`` objects sorted by ``final_score`` descending.
    """
    if not results:
        return []

    if reranker is None:
        reranker = _default_reranker()

    # Cheap fusion: compute quality scores for all results.
    if use_quality:
        domain_counts: Counter[str] = Counter(_domain_from_url(r.url) for r in results)
        context = {"domain_counts": domain_counts}
        for item in results:
            item.final_score = quality_score(item, query, context=context)
    else:
        for item in results:
            item.final_score = item.normalized_score

    results = sorted(results, key=lambda x: x.final_score, reverse=True)

    if not _reranker_available(reranker) or top_k <= 0:
        return results[:top_k] if top_k > 0 else results

    # Semantic rerank on the top candidates only (efficient and stable).
    candidates = results[: max(top_k * 2, 1)]
    docs = [f"{item.title} {item.description}".strip() for item in candidates]
    if not docs:
        return results[:top_k]

    vectors = _embed_docs(reranker, query, docs)
    if vectors is None:
        return results[:top_k]

    q_vec = vectors[0]
    for item, doc_vec in zip(candidates, vectors[1:], strict=True):
        semantic = _cosine(q_vec, doc_vec)
        item.final_score = (1.0 - semantic_weight) * item.final_score + semantic_weight * semantic

    results = sorted(results, key=lambda x: x.final_score, reverse=True)
    return results[:top_k]
