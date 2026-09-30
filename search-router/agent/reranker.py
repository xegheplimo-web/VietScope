"""Reranker — AI cross-encoder with deterministic fallback.

The Research Agent's ``rerank_results`` keeps its public signature
(list[SourceResult] → list[SourceResult]) but internally uses
``ranking.engine.rank_search`` (normalize → fuse → quality score → optional
semantic rerank).  On top of that the research engine runs an AI reranker —
``Qwen/Qwen3-Reranker-0.6B`` via sentence-transformers ``CrossEncoder`` —
over the top candidates, blended through the v2 quality formula
(:func:`ranking.quality.final_quality_score`).  The legacy heuristic
``score_source`` / ``get_authority_score`` are retained as the last-resort
fallback so a missing model or dependency never breaks the pipeline.
"""

from __future__ import annotations

import logging
import math

from config import settings
from research_models.research_state import SourceResult

logger = logging.getLogger(__name__)

# Authority scores by domain type (legacy heuristic fallback).
AUTHORITY_SCORES = {
    # Government / Official
    "chinhphu.vn": 0.98,
    "luatvietnam.vn": 0.85,
    "thuvienphapluat.vn": 0.85,
    "congan.bacgiang.gov.vn": 0.95,
    "cand.com.vn": 0.88,
    "baobacgiang.vn": 0.82,
    # Major Press
    "vnexpress.net": 0.80,
    "tuoitre.vn": 0.80,
    "dantri.com.vn": 0.80,
    "thanhnien.vn": 0.78,
    "vietnamnet.vn": 0.78,
    # Tech / Documentation
    "github.com": 0.85,
    "docs.python.org": 0.90,
    "stackoverflow.com": 0.60,
    # Social / Community
    "facebook.com": 0.25,
    "twitter.com": 0.25,
    "reddit.com": 0.40,
    "tiktok.com": 0.20,
    # Unknown
    "unknown": 0.12,
}


def get_authority_score(url: str) -> float:
    """Get authority score for a URL based on domain (legacy heuristic)."""
    from urllib.parse import urlparse

    try:
        domain = urlparse(url).netloc or ""
        domain = domain.removeprefix("www.")
        return AUTHORITY_SCORES.get(domain, 0.12)
    except (ValueError, TypeError, AttributeError):
        return 0.12


def score_source(result: SourceResult, query: str) -> SourceResult:
    """Legacy single-source heuristic scorer (fallback only)."""
    result.authority_score = get_authority_score(result.url)

    query_words = set(query.lower().split())
    title_words = set(result.title.lower().split()) if result.title else set()
    desc_words = set(result.description.lower().split()) if result.description else set()
    title_overlap = len(query_words & title_words) / max(len(query_words), 1)
    desc_overlap = len(query_words & desc_words) / max(len(query_words), 1)
    result.query_match_score = title_overlap * 0.7 + desc_overlap * 0.3

    result.freshness_score = 0.5
    result.corroboration_score = 0.0
    result.relevance_score = result.score

    result.score = (
        result.relevance_score * 0.30
        + result.authority_score * 0.25
        + result.freshness_score * 0.20
        + result.query_match_score * 0.15
        + result.corroboration_score * 0.10
    )
    return result


# ─── AI reranker (Qwen3-Reranker-0.6B, lazy) ─────────────────────────────────


class CrossEncoderReranker:
    """sentence-transformers ``CrossEncoder`` wrapper — loads on first use.

    ``available()`` never raises: missing deps, missing weights or a disabled
    setting all resolve to ``False`` so callers degrade to heuristics.
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.model_name = model_name or settings.reranker_model
        self.device = device if device is not None else (settings.reranker_device or None)
        self.batch_size = batch_size or settings.reranker_batch_size
        self._model = None
        self._load_failed = False
        # BGE service refactor: delegate to the shared BgeRerankerService
        # while keeping the legacy CrossEncoder path as a fallback when the
        # service module is unavailable (e.g. minimal installs).
        self._bge_service = None
        try:
            from services.bge_reranker import get_bge_reranker

            self._bge_service = get_bge_reranker()
        except Exception:  # noqa: BLE001 — optional dependency
            pass

    def _load(self) -> bool:
        if self._model is not None:
            return True
        if self._load_failed or not settings.reranker_enabled:
            return False
        # Try BGE service first (preferred path since Phase 1)
        if self._bge_service is not None and self._bge_service.available():
            # Service manages its own model; mark _load_failed so we don't
            # retry the legacy path every time.
            self._load_failed = True
            return True
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:  # noqa: BLE001 — dep may be absent
            logger.warning("sentence-transformers unavailable: %s", exc)
            self._load_failed = True
            return False
        try:
            kwargs: dict = {}
            if self.device:
                kwargs["device"] = self.device
            self._model = CrossEncoder(self.model_name, **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001 — model download can fail
            logger.warning("reranker model load failed (%s): %s", self.model_name, exc)
            self._load_failed = True
            return False

    def available(self) -> bool:
        if not settings.reranker_enabled:
            return False
        if self._bge_service is not None and self._bge_service.available():
            return True
        return self._load()

    def score(self, query: str, docs: list[str]) -> list[float] | None:
        """Score (query, doc) pairs → ``[0, 1]`` floats, or ``None`` on failure."""
        if not docs:
            return None
        if not settings.reranker_enabled:
            return None
        # An explicitly loaded/assigned model wins over the managed service
        # (deterministic reranking when callers inject their own cross-encoder).
        if self._model is None:
            # Prefer BGE service (Phase 1 path)
            if self._bge_service is not None and self._bge_service.available():
                return self._bge_service.score(query, docs)
            if not self._load():
                return None
        pairs = [(query, doc) for doc in docs]
        try:
            raw = self._model.predict(
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reranker predict failed: %s", exc)
            return None
        scores: list[float] = []
        for value in list(raw):
            s = float(value)
            # Cross-encoders may emit logits — squash to [0,1] when needed.
            if s < 0.0 or s > 1.0:
                s = 1.0 / (1.0 + math.exp(-s))
            scores.append(max(0.0, min(1.0, s)))
        return scores


_DEFAULT_RERANKER: CrossEncoderReranker | None = None


def get_default_reranker() -> CrossEncoderReranker:
    """Process-wide lazy singleton for the configured reranker model."""
    global _DEFAULT_RERANKER
    if _DEFAULT_RERANKER is None:
        _DEFAULT_RERANKER = CrossEncoderReranker()
    return _DEFAULT_RERANKER


def _apply_ai_rerank(
    items: list,
    query: str,
    reranker: CrossEncoderReranker | None,
) -> bool:
    """Score top candidates with the cross-encoder into ``semantic_score``.

    Returns True when semantic scores were written; False on any fallback so
    callers keep the deterministic order.  Only the top ``reranker_top_n``
    candidates are scored — cross-encoding is quadratic-cost per call.
    """
    if reranker is None:
        return False
    try:
        available = reranker.available()
    except Exception:  # noqa: BLE001
        return False
    if not available:
        return False

    candidates = items[: max(1, settings.reranker_top_n)]
    docs = [f"{i.title} {i.description}".strip() for i in candidates]
    try:
        scores = reranker.score(query, docs)
    except Exception:  # noqa: BLE001
        return False
    if not scores or len(scores) != len(docs):
        return False
    for item, s in zip(candidates, scores, strict=True):
        item.metadata["semantic_score"] = s
    return True


def rerank_results(
    results: list[SourceResult],
    query: str,
    *,
    reranker: CrossEncoderReranker | None = None,
) -> list[SourceResult]:
    """Rerank search results via the mature ranking/ package.

    When a cross-encoder ``reranker`` is supplied (or the default model is
    loadable) the top candidates are rescored semantically and reordered by
    the v2 quality formula; otherwise the deterministic ranking output is
    returned.  Falls back to the legacy heuristic scorer if ranking/ is
    unavailable.
    """
    if not results:
        return []

    try:
        return _rerank_via_ranking(results, query, reranker=reranker)
    except Exception:  # noqa: BLE001 — last-resort catch, heuristic fallback
        # Deterministic fallback — never let ranking failure break research.
        scored = [score_source(r, query) for r in results]
        from collections import Counter

        domain_counts = Counter(r.domain for r in scored if r.domain)
        for r in scored:
            r.corroboration_score = min(domain_counts.get(r.domain, 1) / 5.0, 1.0)
            r.score = (
                r.relevance_score * 0.30
                + r.authority_score * 0.25
                + r.freshness_score * 0.20
                + r.query_match_score * 0.15
                + r.corroboration_score * 0.10
            )
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored


def _source_to_ranked(result: SourceResult, query_text: str):
    """Wrap a SourceResult in a RankedItem for RRF fusion + quality scoring."""
    from canonical.url import canonical_url
    from ranking.types import RankedItem

    return RankedItem(
        url=result.url,
        title=result.title or "",
        description=result.description or "",
        provider="searxng",
        raw_score=result.score,
        published_date=result.published_at,
        canonical_url=canonical_url(result.url) or result.url,
        metadata={
            "source_ref": result,
            "query_text": query_text,
            "content_length": len(result.content or ""),
        },
    )


def rerank_multi_query(
    results_by_query: dict[str, list[SourceResult]],
    query: str,
    *,
    reranker: CrossEncoderReranker | None = None,
    top_n: int | None = None,
) -> list[SourceResult]:
    """Fuse + rerank results collected from multiple sub-queries.

    Each sub-query's list is score-sorted, fused with pure RRF
    (:func:`ranking.fusion.fuse_queries`), optionally rescored by the AI
    reranker, then ordered by the v2 quality formula.  Returns the surviving
    ``SourceResult`` objects with refreshed score fields.
    """
    from ranking.fusion import fuse_queries
    from ranking.quality import final_quality_score

    per_query: dict[str, list] = {}
    for qtext, results in results_by_query.items():
        if not results:
            continue
        ordered = sorted(results, key=lambda r: r.score, reverse=True)
        per_query[qtext] = [_source_to_ranked(r, qtext) for r in ordered]

    fused = fuse_queries(per_query)
    if not fused:
        return []

    semantic_ok = _apply_ai_rerank(fused, query, reranker)
    for item in fused:
        item.final_score = final_quality_score(item, query)
    fused.sort(key=lambda x: x.final_score, reverse=True)
    if top_n is not None and top_n > 0:
        fused = fused[:top_n]

    out: list[SourceResult] = []
    for item in fused:
        src = item.metadata.get("source_ref")
        if src is None:
            continue
        src.score = item.final_score
        src.relevance_score = item.normalized_score
        src.authority_score = get_authority_score(item.url)
        src.corroboration_score = min(len(item.metadata.get("matched_queries", [])) / 5.0, 1.0)
        if semantic_ok and "semantic_score" in item.metadata:
            src.query_match_score = float(item.metadata["semantic_score"])
        out.append(src)
    return out


def _rerank_via_ranking(
    results: list[SourceResult],
    query: str,
    *,
    reranker: CrossEncoderReranker | None = None,
) -> list[SourceResult]:
    """Convert to RankedItem, run ranking.engine.rank_search, map back."""
    from ranking.engine import rank_search
    from ranking.quality import final_quality_score

    # SourceResult → dict for rank_search (accepts list[SearchResultItem|dict]).
    # Sort by score descending first: ranking.fusion uses list order as RRF rank,
    # so the input must already be in relevance order for the rank to be meaningful.
    raw = [
        {
            "url": r.url,
            "title": r.title,
            "content": r.description,  # description maps to content for scoring
            "description": r.description,
            "score": r.score,
            "published_date": r.published_at,
            "category": "",
        }
        for r in sorted(results, key=lambda x: x.score, reverse=True)
    ]

    ranked = rank_search({"agent": raw}, query, top_k=len(results))

    # Optional AI stage: cross-encoder rescore → v2 quality formula ordering.
    if _apply_ai_rerank(ranked, query, reranker):
        for item in ranked:
            item.final_score = final_quality_score(item, query)
        ranked = sorted(ranked, key=lambda x: x.final_score, reverse=True)

    # Map RankedItem back onto the original SourceResult objects (by URL).
    by_url = {r.url: r for r in results if r.url}
    ordered: list[SourceResult] = []
    for item in ranked:
        src = by_url.get(item.url)
        if src is None:
            continue
        src.score = item.final_score
        src.relevance_score = item.normalized_score
        src.authority_score = item.authority if item.authority is not None else 0.0
        src.freshness_score = item.freshness if item.freshness is not None else 0.5
        if "semantic_score" in item.metadata:
            src.query_match_score = float(item.metadata["semantic_score"])
        ordered.append(src)
    # Append any results that didn't map (shouldn't happen, but stay safe).
    seen = {r.url for r in ordered}
    for r in results:
        if r.url and r.url not in seen:
            ordered.append(r)
    return ordered
