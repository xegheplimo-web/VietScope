from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from ranking.authority import authority_for
from ranking.types import RankedItem


def _domain_from_url(url: str) -> str:
    """Extract the lowercase, www-stripped netloc from a URL."""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except (ValueError, TypeError, AttributeError):
        return ""


def _query_language(query: str) -> str:
    """Fast heuristic language detection for authority scoring."""
    if not query:
        return "en"
    # Vietnamese commonly uses diacritics in these code points.
    if re.search(
        r"[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]",
        query,
        re.IGNORECASE,
    ):
        return "vi"
    return "en"


def _parse_published(date_value: str | None) -> datetime | None:
    """Best-effort parsing of publication date strings."""
    if not date_value:
        return None
    s = str(date_value).strip()
    # ISO 8601 variants, including trailing 'Z'.
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    # Year-only fallback.
    try:
        if s.isdigit() and len(s) == 4:
            return datetime(int(s), 1, 1, tzinfo=UTC)
    except ValueError:
        pass
    return None


# Per-vertical freshness horizons (P5-VN) — "mở rộng theo vertical".
# Score decays linearly to 0 at 2× the lane's half-life; lanes absent here
# keep the legacy 5-year decay. Market is intra-day; legal/government văn
# bản stay valid for years; a stale news item is worth little in days.
FRESHNESS_HALFLIFE_DAYS: dict[str, float] = {
    "market": 0.25,  # hours — prices, gold, FX, stocks
    "finance": 1.0,
    "news": 2.0,
    "social": 14.0,
    "forum": 30.0,
    "ecommerce": 14.0,
    "product": 45.0,
    "business": 60.0,
    "company": 90.0,
    "places": 180.0,
    "medical": 730.0,
    "document": 1825.0,
    "legal": 3650.0,
    "government": 3650.0,
    "academic": 3650.0,
}


def _freshness_score(
    published_at: datetime | None,
    *,
    now: datetime | None = None,
    vertical: str | None = None,
) -> float:
    """Return a freshness score in [0, 1]; missing date is neutral.

    ``vertical`` is a ``SourceType`` lane name: news/market decay in
    hours-days, legal/government in years (P5-VN). ``None`` keeps the
    legacy 5-year linear decay exactly.
    """
    if published_at is None:
        return 0.5
    now = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    try:
        age_days = max(0.0, (now - published_at).total_seconds() / 86400.0)
    except (ValueError, TypeError, AttributeError):
        return 0.5
    halflife = FRESHNESS_HALFLIFE_DAYS.get(str(vertical or ""))
    if halflife is None:
        # Legacy: decay over 5 years.
        return max(0.0, min(1.0, 1.0 - (age_days / (365.25 * 5.0))))
    return max(0.0, min(1.0, 1.0 - (age_days / (2.0 * halflife))))


def _item_vertical(item: RankedItem, context: dict[str, Any]) -> str | None:
    """Resolve the item's SourceType lane — the vertical for P5-VN scoring.

    Order: explicit ``context["vertical"]`` (query-level routing lane),
    then the item's own lane (federated items carry ``source_type``
    on the model or inside ``metadata``).
    """
    v = context.get("vertical") or item.source_type or ""
    if not v:
        try:
            v = item.metadata.get("source_type", "") or ""
        except (TypeError, AttributeError):
            v = ""
    v = str(v)
    return v or None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in kilometers."""
    r = 6371.0  # Earth radius in km.
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _geo_bonus(
    item: RankedItem,
    query_lat: float | None,
    query_lon: float | None,
    threshold_km: float = 50.0,
) -> float:
    """Bonus for results near the query location."""
    if item.lat is None or item.lon is None:
        return 0.0
    if query_lat is None or query_lon is None:
        return 0.0
    dist = _haversine_km(item.lat, item.lon, query_lat, query_lon)
    return max(0.0, min(1.0, 1.0 - (dist / threshold_km)))


def _diversity_penalty(item: RankedItem, domain_counts: Counter[str], total: int) -> float:
    """Penalty proportional to how crowded the top results are from the same domain."""
    if total <= 1:
        return 0.0
    domain = _domain_from_url(item.url)
    if not domain:
        return 0.0
    share = domain_counts.get(domain, 0) / total
    # Cap at 0.3 so it does not completely dominate other signals.
    return min(share, 1.0) * 0.3


def quality_score(
    item: RankedItem,
    query: str,
    *,
    context: dict[str, Any] | None = None,
) -> float:
    """Compute a composite quality score for a single result.

    Combines relevance, source authority, freshness, domain diversity, and
    geo proximity.  All sub-scores are in ``[0, 1]`` and the final score is
    clamped to ``[0, 1]``.

    Args:
        item: A ranked item with ``normalized_score``, ``url``, and optional
            ``published_date`` / ``lat`` / ``lon``.
        query: The original user query string.
        context: Optional context with ``lang``, ``domain_counts`` (for
            diversity) and ``query_lat`` / ``query_lon`` (for geo).

    Returns:
        A composite quality score in ``[0, 1]``.
    """
    context = context or {}
    lang = context.get("lang") or _query_language(query)
    vertical = _item_vertical(item, context)

    relevance = max(0.0, min(1.0, item.normalized_score))

    domain = _domain_from_url(item.url)
    authority = authority_for(domain, vertical, lang=lang)

    published = _parse_published(item.published_date)
    freshness = _freshness_score(published, vertical=vertical)

    # Diversity penalty uses the pre-computed domain distribution of the set.
    domain_counts = context.get("domain_counts")
    total = sum(domain_counts.values()) if domain_counts else 0
    diversity_penalty = 0.0
    if isinstance(domain_counts, Counter) and total > 0:
        diversity_penalty = _diversity_penalty(item, domain_counts, total)

    geo = _geo_bonus(
        item,
        context.get("query_lat"),
        context.get("query_lon"),
    )

    # Weighted combination.  Weights sum to 1.0 and the diversity term is a
    # small subtraction to down-rank clusters from the same domain.
    final = (
        0.35 * relevance
        + 0.25 * authority
        + 0.25 * freshness
        - 0.10 * diversity_penalty
        + 0.05 * geo
    )

    return max(0.0, min(1.0, final))


# ─── Research-engine quality model (spec_searchhub_001 §6) ───────────────────
#
# The v1 ``quality_score`` above stays the cheap deterministic stage for the
# legacy /search pipeline.  The research engine uses a richer composite that
# blends the cross-encoder semantic score with the RRF position, source
# authority, freshness, query coverage and content quality:
#
#   final = 0.40*semantic + 0.20*rrf + 0.15*source_quality
#         + 0.10*freshness + 0.10*query_coverage + 0.05*content

QUALITY_WEIGHTS_V2: dict[str, float] = {
    "semantic": 0.40,
    "rrf": 0.20,
    "source_quality": 0.15,
    "freshness": 0.10,
    "query_coverage": 0.10,
    "content": 0.05,
}


def _query_coverage(query: str, text: str) -> float:
    """Fraction of significant query tokens present in the text."""
    tokens = {t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1}
    if not tokens:
        return 0.0
    text_tokens = set(re.findall(r"\w+", (text or "").lower()))
    return len(tokens & text_tokens) / len(tokens)


def _content_quality(item: RankedItem) -> float:
    """Content-depth proxy: longer fetched copy scores higher (cap 4k chars)."""
    try:
        length = float(item.metadata.get("content_length", 0) or 0)
    except (TypeError, ValueError):
        length = 0.0
    if length <= 0:
        length = float(len(item.description or ""))
    return max(0.0, min(1.0, length / 4000.0))


def final_quality_score(
    item: RankedItem,
    query: str,
    *,
    semantic_score: float | None = None,
    rrf_score: float | None = None,
    query_coverage: float | None = None,
    content_quality: float | None = None,
    context: dict[str, Any] | None = None,
) -> float:
    """Spec v2 composite: semantic + RRF + authority + freshness + coverage.

    Sub-scores default to signals already on the ``RankedItem`` so the
    function degrades gracefully when the cross-encoder is unavailable:
    ``semantic_score`` falls back to ``metadata["semantic_score"]`` then the
    normalized score, ``rrf_score`` to the fused normalized score, coverage is
    computed from title+description, and content quality from metadata.

    Returns a score clamped to ``[0, 1]``.
    """
    context = context or {}
    lang = context.get("lang") or _query_language(query)
    vertical = _item_vertical(item, context)

    if semantic_score is None:
        try:
            semantic_score = float(item.metadata.get("semantic_score"))
        except (TypeError, ValueError):
            semantic_score = item.normalized_score
    if rrf_score is None:
        rrf_score = item.normalized_score
    if query_coverage is None:
        query_coverage = _query_coverage(query, f"{item.title} {item.description}")
    if content_quality is None:
        content_quality = _content_quality(item)

    source_quality = authority_for(_domain_from_url(item.url), vertical, lang=lang)
    freshness = _freshness_score(_parse_published(item.published_date), vertical=vertical)

    final = (
        QUALITY_WEIGHTS_V2["semantic"] * max(0.0, min(1.0, semantic_score))
        + QUALITY_WEIGHTS_V2["rrf"] * max(0.0, min(1.0, rrf_score))
        + QUALITY_WEIGHTS_V2["source_quality"] * source_quality
        + QUALITY_WEIGHTS_V2["freshness"] * freshness
        + QUALITY_WEIGHTS_V2["query_coverage"] * max(0.0, min(1.0, query_coverage))
        + QUALITY_WEIGHTS_V2["content"] * max(0.0, min(1.0, content_quality))
    )
    return max(0.0, min(1.0, final))
