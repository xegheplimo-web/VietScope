"""P17 local ranking — deterministic weighted fusion over lane signals.

Every candidate is scored as a weighted sum of normalized components
(text relevance, category agreement, distance decay, canonical
confidence, freshness, source corroboration, operational status). The
function is pure and side-effect free so results are reproducible and
each component is individually observable (``components`` on the scored
candidate, surfaced by ``debug=1``).

Weights are tunable via ``PLACES_RANK_WEIGHTS`` (JSON object of component
→ weight); unknown keys are rejected so typos cannot silently no-op.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from serving.places.document import (
    STATUS_OPEN,
    STATUS_PERMANENTLY_CLOSED,
    STATUS_TEMPORARILY_CLOSED,
    STATUS_UNKNOWN,
    PlaceDocumentV1,
)

# Distance decay scale — score halves roughly every `tau` meters. Chosen so
# results inside a 2 km default radius still spread meaningfully.
_DISTANCE_TAU_M = 750.0

# source_count saturates at this many corroborating providers.
_SOURCE_SATURATION = 3.0

_STATUS_SCORE = {
    STATUS_OPEN: 1.0,
    STATUS_UNKNOWN: 0.6,
    STATUS_TEMPORARILY_CLOSED: 0.25,
    STATUS_PERMANENTLY_CLOSED: 0.0,
}

# P17.1 — Bayesian rating prior: with no reviews a place scores the
# corpus mean (3.5), each real review pulls it toward the observed
# rating. ``_RATING_PRIOR`` is the pseudo-count (10 reviews of prior).
_RATING_PRIOR = 10.0
_RATING_GLOBAL_AVG = 3.5
_RATING_MAX = 5.0

# log1p popularity saturates at this review count (component hits 1.0).
_REVIEW_SATURATION = 1000.0

# Default component weights — sum ≈ 1.0 so scores stay interpretable.
DEFAULT_WEIGHTS: dict[str, float] = {
    "text": 0.30,
    "distance": 0.20,
    "category": 0.08,
    "confidence": 0.08,
    "freshness": 0.07,
    "sources": 0.06,
    "status": 0.06,
    "rating_quality": 0.06,
    "popularity": 0.04,
    "open_now_boost": 0.05,
}

# Related buckets score partial agreement — kept in sync with the
# canonical ontology's grouping semantics (food inside retail, lodging
# near tourism, ...). Duplicated here on purpose: serving must not import
# the resolver's internals, and the surface is the frozen bucket names.
_RELATED: dict[str, frozenset[str]] = {
    "food": frozenset({"retail"}),
    "retail": frozenset({"food"}),
    "lodging": frozenset({"tourism"}),
    "tourism": frozenset({"lodging", "culture"}),
    "culture": frozenset({"tourism"}),
    "finance": frozenset({"services"}),
    "services": frozenset({"finance"}),
    "office": frozenset({"industrial"}),
    "industrial": frozenset({"office"}),
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters — fallback when PostGIS is absent."""
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass(slots=True)
class RankWeights:
    """Component weights; ``from_env`` merges a JSON override."""

    text: float = DEFAULT_WEIGHTS["text"]
    distance: float = DEFAULT_WEIGHTS["distance"]
    category: float = DEFAULT_WEIGHTS["category"]
    confidence: float = DEFAULT_WEIGHTS["confidence"]
    freshness: float = DEFAULT_WEIGHTS["freshness"]
    sources: float = DEFAULT_WEIGHTS["sources"]
    status: float = DEFAULT_WEIGHTS["status"]
    rating_quality: float = DEFAULT_WEIGHTS["rating_quality"]
    popularity: float = DEFAULT_WEIGHTS["popularity"]
    open_now_boost: float = DEFAULT_WEIGHTS["open_now_boost"]

    @classmethod
    def from_json(cls, raw: str | None) -> RankWeights:
        import json

        if not raw:
            return cls()
        try:
            overrides = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(overrides, dict):
            return cls()
        valid = set(DEFAULT_WEIGHTS)
        w = cls()
        for k, v in overrides.items():
            if k in valid and isinstance(v, (int, float)) and v >= 0:
                setattr(w, k, float(v))
        return w


@dataclass(slots=True)
class Candidate:
    """One place under consideration, with every lane signal attached."""

    doc: PlaceDocumentV1
    os_score: float | None = None  # raw OpenSearch _score (BM25)
    distance_m: float | None = None  # PostGIS-accurate when available
    lane: str = "opensearch"  # which lane produced the candidate
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


def _norm_text_score(os_score: float | None, max_os_score: float) -> float:
    """Squash an unbounded BM25 score into (0,1] relative to the best hit."""
    if os_score is None or os_score <= 0:
        return 0.0
    if max_os_score > 0:
        return min(1.0, os_score / max_os_score)
    return 1.0


def _distance_score(distance_m: float | None) -> float:
    if distance_m is None:
        return 0.5  # neutral when the query has no geo anchor
    return math.exp(-max(0.0, distance_m) / _DISTANCE_TAU_M)


def _category_score(query_category: str | None, doc_categories: list[str]) -> float:
    if not query_category:
        return 0.5  # neutral — caller did not constrain category
    if not doc_categories:
        return 0.3  # unknown doc category under a constrained query
    if query_category in doc_categories:
        return 1.0
    if any(query_category in _RELATED.get(c, frozenset()) for c in doc_categories):
        return 0.6
    return 0.0


def _sources_score(source_count: int) -> float:
    return min(1.0, max(0.0, source_count) / _SOURCE_SATURATION)


def _rating_quality(rating: float | None, review_count: int | None) -> float:
    """Bayesian-smoothed rating normalized to 0..1.

    ``(rating * n + prior * global_avg) / (n + prior)`` — a lone 5.0
    review sits near the corpus mean; 2500 reviews of 4.8 beats it.
    Unrated places keep the prior (0.7), never a guessed zero.
    """
    rc = max(0.0, float(review_count or 0))
    r = float(rating) if rating is not None else _RATING_GLOBAL_AVG
    bayes = (r * rc + _RATING_PRIOR * _RATING_GLOBAL_AVG) / (rc + _RATING_PRIOR)
    return max(0.0, min(1.0, bayes / _RATING_MAX))


def _popularity(review_count: int | None) -> float:
    """log1p(review_count) normalized to saturate at _REVIEW_SATURATION."""
    rc = max(0.0, float(review_count or 0))
    return min(1.0, math.log1p(rc) / math.log1p(_REVIEW_SATURATION))


def _open_now_boost(open_flag: bool | None) -> float:
    """Serve-time open verdict → component: open 1.0, closed 0.0, unknown 0.5."""
    if open_flag is True:
        return 1.0
    if open_flag is False:
        return 0.0
    return 0.5


def score_candidate(
    cand: Candidate,
    *,
    query_category: str | None,
    max_os_score: float,
    weights: RankWeights,
) -> Candidate:
    """Attach the weighted score + observable components to a candidate."""
    doc = cand.doc
    comp = {
        "text": _norm_text_score(cand.os_score, max_os_score),
        "distance": _distance_score(cand.distance_m),
        "category": _category_score(query_category, doc.category_ids),
        "confidence": max(0.0, min(1.0, doc.confidence)),
        "freshness": max(0.0, min(1.0, doc.freshness_score)),
        "sources": _sources_score(doc.source_count),
        "status": _STATUS_SCORE.get(doc.status, _STATUS_SCORE[STATUS_UNKNOWN]),
        "rating_quality": _rating_quality(doc.rating, doc.review_count),
        "popularity": _popularity(doc.review_count),
        "open_now_boost": _open_now_boost(doc.open_now),
    }
    cand.components = comp
    cand.score = (
        weights.text * comp["text"]
        + weights.distance * comp["distance"]
        + weights.category * comp["category"]
        + weights.confidence * comp["confidence"]
        + weights.freshness * comp["freshness"]
        + weights.sources * comp["sources"]
        + weights.status * comp["status"]
        + weights.rating_quality * comp["rating_quality"]
        + weights.popularity * comp["popularity"]
        + weights.open_now_boost * comp["open_now_boost"]
    )
    return cand


def _id_key(c: Candidate) -> int:
    return int(c.doc.place_id) if str(c.doc.place_id).isdigit() else 0


def _dist_key(c: Candidate) -> float:
    return c.distance_m if c.distance_m is not None else float("inf")


def rank(
    candidates: list[Candidate],
    *,
    query_category: str | None,
    weights: RankWeights | None = None,
    limit: int = 20,
    sort: str | None = None,
) -> list[Candidate]:
    """Score every candidate and return the deterministic top-``limit``.

    ``sort`` (P17.1) picks the primary order — ``distance`` asc,
    ``rating``/``popularity`` by their score components desc — with the
    fusion score as tie-break. Absent → fusion score order.

    Default tie-break chain: score desc, distance asc, place_id asc —
    identical input always produces identical output.
    """
    w = weights or RankWeights()
    max_os = max((c.os_score or 0.0) for c in candidates) if candidates else 0.0
    for c in candidates:
        score_candidate(c, query_category=query_category, max_os_score=max_os, weights=w)
    if sort == "distance":
        candidates.sort(key=lambda c: (_dist_key(c), -c.score, _id_key(c)))
    elif sort in ("rating", "popularity"):
        if sort == "popularity":
            # P2.0.4: sort by raw review_count, not the saturated log1p
            # component (which caps at _REVIEW_SATURATION=1000). A 50000-
            # review place must outrank 1001 on the sort axis; saturation
            # only bounds the fusion score weight, not the sort key.
            candidates.sort(
                key=lambda c: (-(c.doc.review_count or 0), -c.score, _dist_key(c), _id_key(c))
            )
        else:
            candidates.sort(
                key=lambda c: (-c.components["rating_quality"], -c.score, _dist_key(c), _id_key(c))
            )
    else:
        candidates.sort(key=lambda c: (-c.score, _dist_key(c), _id_key(c)))
    return candidates[:limit]


def debug_components(cand: Candidate) -> dict[str, Any]:
    """Per-candidate score breakdown for ``debug=1`` responses."""
    return {
        "score": round(cand.score, 6),
        "components": {k: round(v, 6) for k, v in cand.components.items()},
        "os_score": cand.os_score,
        "distance_m": cand.distance_m,
        "lane": cand.lane,
    }
