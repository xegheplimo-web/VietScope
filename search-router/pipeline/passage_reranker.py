"""Passage reranker — second-stage rerank over scraped page chunks.

Sits between Firecrawl scraping and LLM synthesis: pages are chunked
(≈800 chars, overlapping), scored, and only the top ``top_n`` passages reach
the answer synthesizer.  The AI cross-encoder (Qwen3-Reranker-0.6B via
``agent.reranker``) is used when available; otherwise the deterministic
keyword/BM25/phrase heuristic from :mod:`pipeline.reranker` applies — the
module never hard-fails on a missing model.
"""

from __future__ import annotations

import logging
from typing import Any

from models import ScrapeResult

from pipeline.reranker import (
    _bm25_score,
    _chunk_text,
    _keyword_overlap_score,
    _phrase_bonus,
    _tokenize,
    _tokenize_set,
)

logger = logging.getLogger(__name__)

# Blend weights when the AI reranker supplies semantic scores.
_SEMANTIC_WEIGHT = 0.7
_HEURISTIC_WEIGHT = 0.3


def chunk_pages(
    scraped: list[ScrapeResult],
    *,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
    max_chunks_per_source: int = 8,
) -> list[dict[str, Any]]:
    """Split scraped pages into overlapping passage dicts.

    Returns ``[{source_url, title, chunk_index, chunk_text, score}]`` with
    ``score`` unset (0.0) — scoring happens in :func:`rerank_passages`.
    Errored or empty pages are skipped.
    """
    chunks: list[dict[str, Any]] = []
    for doc in scraped:
        if getattr(doc, "error", None) or not getattr(doc, "markdown", ""):
            continue
        pieces = _chunk_text(doc.markdown, chunk_size, chunk_overlap)
        for i, piece in enumerate(pieces[:max_chunks_per_source]):
            chunks.append(
                {
                    "source_url": doc.url,
                    "title": doc.title or "",
                    "chunk_index": i,
                    "chunk_text": piece,
                    "score": 0.0,
                }
            )
    return chunks


def _heuristic_score(
    query_terms: list[str],
    query_tokens: set[str],
    chunk_text: str,
    avg_len: float,
) -> float:
    """Keyword + BM25 + phrase score, mirroring rerank_scraped_content."""
    kw = _keyword_overlap_score(query_tokens, chunk_text)
    bm25 = _bm25_score(query_terms, _tokenize(chunk_text), avg_len)
    bm25_norm = min(bm25 / 3.0, 1.0)
    phrase = _phrase_bonus(" ".join(query_terms), chunk_text)
    return 0.5 * kw + 0.3 * bm25_norm + 0.2 * phrase


def rerank_passages(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    top_n: int = 16,
    reranker: Any | None = None,
) -> list[dict[str, Any]]:
    """Score passages and keep the top ``top_n``.

    With an available cross-encoder ``reranker``, scores are
    ``0.7 * semantic + 0.3 * heuristic``; otherwise heuristic-only.  Input
    dicts are mutated with their final ``score``.
    """
    if not chunks:
        return []

    query_terms = _tokenize(query)
    query_tokens = _tokenize_set(query)
    avg_len = sum(len(_tokenize(c["chunk_text"])) for c in chunks) / len(chunks) if chunks else 1.0

    for chunk in chunks:
        chunk["score"] = _heuristic_score(query_terms, query_tokens, chunk["chunk_text"], avg_len)

    # AI stage — semantic rescore blended over the heuristic baseline.
    semantic_scores: list[float] | None = None
    if reranker is not None:
        try:
            if reranker.available():
                semantic_scores = reranker.score(query, [c["chunk_text"] for c in chunks])
        except Exception as exc:  # noqa: BLE001 — degrade to heuristic
            logger.warning("passage reranker semantic stage failed: %s", exc)
            semantic_scores = None
    if semantic_scores and len(semantic_scores) == len(chunks):
        for chunk, sem in zip(chunks, semantic_scores, strict=True):
            chunk["semantic_score"] = sem
            chunk["score"] = _SEMANTIC_WEIGHT * sem + _HEURISTIC_WEIGHT * chunk["score"]

    chunks.sort(key=lambda c: c["score"], reverse=True)
    return chunks[:top_n] if top_n > 0 else chunks


def chunk_and_rerank(
    query: str,
    scraped: list[ScrapeResult],
    *,
    top_n: int = 16,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
    max_chunks_per_source: int = 8,
    reranker: Any | None = None,
) -> list[dict[str, Any]]:
    """One-shot helper: chunk pages then rerank passages."""
    chunks = chunk_pages(
        scraped,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        max_chunks_per_source=max_chunks_per_source,
    )
    return rerank_passages(query, chunks, top_n=top_n, reranker=reranker)
