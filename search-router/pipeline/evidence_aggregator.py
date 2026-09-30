"""Evidence Aggregator — collect → dedupe → rank → label trust → EvidencePackage.

This layer sits between raw search providers and Hermes. It never writes an
answer or draws a conclusion — it only:

1. Collects raw results from one or more providers.
2. Deduplicates on canonical URL (and collapses same-domain spam).
3. Ranks with the unified ranking pipeline (normalize → RRF fusion → rerank).
4. Labels each surviving source with an authority type + trust level.
5. Returns an EvidencePackage (sources + aggregation stats).

Hermes remains the single analysis/conclusion authority.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from models import (
    EvidenceAggregationStats,
    EvidencePackage,
    SearchResultItem,
    Source,
    SourceTrust,
)

# Trust mapping from authority classification (ranking/authority.py labels).
# A source whose domain is unclassified lands in "unverified" unless a later
# signal (multi-provider agreement) upgrades it to "medium".
_TRUST_BY_AUTHORITY: dict[str, str] = {
    "official": SourceTrust.HIGH,
    "government": SourceTrust.HIGH,
    "research_paper": SourceTrust.HIGH,
    "major_publication": SourceTrust.HIGH,
    "vendor_website": SourceTrust.MEDIUM,
    "specialist_blog": SourceTrust.MEDIUM,
    "forum": SourceTrust.MEDIUM,
    "reddit": SourceTrust.LOW,
    "unknown_seo_site": SourceTrust.UNVERIFIED,
}

# Domains that are structurally reliable for code/docs (github raw, docs, stack).
_STRONG_CODE_DOMAINS = {"github.com", "gitlab.com", "bitbucket.org"}


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _canonical_key(url: str) -> str:
    """Normalized identity key: lowercase, scheme-less, trailing-slash-stripped."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower().removeprefix("www.")
        path = p.path.rstrip("/")
        # Strip common tracking/query params for identity (utm, ref, fbclid).
        return f"{host}{path}"
    except Exception:
        return (url or "").strip().lower()


def _authority_for_domain(domain: str, vertical: str | None = None) -> tuple[str, float]:
    """Reuse ranking/authority classification; fall back to structural rules.

    ``vertical`` (a SourceType lane) selects the per-lane score — P5-VN
    intent-dependent authority; ``None`` keeps the legacy scalar.
    """
    try:
        from ranking.authority import authority_for, classify_source_type

        stype = classify_source_type(domain)
        return stype, authority_for(domain, vertical)
    except Exception:
        pass
    if domain in _STRONG_CODE_DOMAINS:
        return "vendor_website", 0.85
    return "unknown_seo_site", 0.3


def _trust_from_authority(authority_type: str) -> str:
    return _TRUST_BY_AUTHORITY.get(authority_type, SourceTrust.UNVERIFIED)


def _label(
    url: str,
    providers: list[str],
    authority_type: str | None,
    authority_score: float | None,
    vertical: str | None = None,
) -> tuple[str | None, float | None, str, list[str]]:
    """Compute (authority_type, authority_score, trust, reasons) for one source.

    Multi-provider agreement is a soft upgrade: a domain seen from 2+
    providers earns at least MEDIUM even if unclassified — the query
    independently surfaced it from multiple engines.
    """
    reasons: list[str] = []
    domain = _domain(url)
    if not domain:
        return None, None, SourceTrust.UNVERIFIED, ["no domain"]

    # Prefer ranking/authority classification when available.
    if authority_type is None or authority_score is None:
        authority_type, authority_score = _authority_for_domain(domain, vertical)

    trust = _trust_from_authority(authority_type)
    reasons.append(f"domain class={authority_type}")

    if providers:
        reasons.append(f"providers={','.join(sorted(set(providers)))}")

    # Soft upgrade on multi-provider agreement (never downgrade HIGH).
    if len(set(providers)) >= 2 and trust in (
        SourceTrust.UNVERIFIED,
        SourceTrust.LOW,
    ):
        trust = SourceTrust.MEDIUM
        reasons.append("multi-provider agreement")

    # Trust penalties for clearly low-value content farms / video/porn domains.
    # Match on the full URL (domain + path) so /bokep-style paths are caught.
    low_value = re.search(
        r"(bokep|porn|xvideo|xxx|hentai|casino|bong88|lode|f88|vaytien|"
        r"taichinh24h|nokiavn|clipnong)",
        (url or ""),
        re.IGNORECASE,
    )
    if low_value:
        trust = SourceTrust.LOW
        reasons.append(f"low-value domain pattern={low_value.group(0)}")

    return authority_type, round(authority_score or 0.0, 3), trust, reasons


def _to_source(
    item,
    index: int,
    provider: str,
    trust_cache: dict[str, tuple],
) -> Source:
    url = item.url or ""
    domain = _domain(url)
    # P5-VN: the item's lane picks the intent-dependent authority score.
    lane = getattr(item, "source_type", "") or ""
    if not lane and isinstance(item, dict):
        lane = str(item.get("source_type") or "")
    if not lane:
        lane = str(getattr(item, "category", "") or "")
    lane = str(lane) if lane else None
    cache_key = (_canonical_key(url) or url) + f"|{lane or ''}"
    cached = trust_cache.get(cache_key)
    if cached:
        atype, ascore, trust, reasons = cached
    else:
        atype, ascore, trust, reasons = _label(url, [provider], None, None, vertical=lane)
        trust_cache[cache_key] = (atype, ascore, trust, reasons)

    # Accept both SearchResultItem (.score) and RankedItem (.final_score).
    raw_score = getattr(item, "score", None)
    if raw_score is None:
        raw_score = getattr(item, "final_score", None) or getattr(item, "normalized_score", 0.0)
    title = getattr(item, "title", "") or ""
    desc = getattr(item, "description", "") or ""
    pub = getattr(item, "published_date", None)
    if pub is None and isinstance(item, dict):
        pub = item.get("published_date")

    return Source(
        source_id=f"src_{index:03d}",
        url=url,
        title=title,
        domain=domain,
        description=desc,
        published_at=pub,
        score=round(float(raw_score or 0.0), 4),
        search_provider=provider,
        content_provider="none",
        authority_type=atype,
        authority_score=ascore,
        trust=trust,
        trust_reasons=reasons,
        source_lane=lane,
    )


def aggregate(
    results_by_provider: dict[str, list[SearchResultItem]],
    query: str,
    max_results: int = 10,
    lang: str = "en",
) -> EvidencePackage:
    """Aggregate multi-provider results into a trust-labeled EvidencePackage.

    Steps: collect → canonical dedupe → multi-provider merge → rank
    (keyword/quality via the unified ranking pipeline when available, else a
    stable score sort) → trust label → stats.

    The ranking pipeline is intentionally *deterministic* (no LLM): this layer
    must stay cheap and predictable; Hermes does the reasoning.
    """
    from ranking.engine import rank_search

    total_candidates = sum(len(v) for v in results_by_provider.values() if v)
    providers_used = [p for p, v in results_by_provider.items() if v]

    if total_candidates == 0:
        return EvidencePackage(
            query=query,
            sources=[],
            aggregation=EvidenceAggregationStats(
                total_candidates=0,
                providers_used=providers_used,
            ),
        )

    # Unified ranking: normalize → RRF fusion (dedupes by canonical URL) → rerank.
    # rank_search returns top_k RankedItem objects (already fused/deduped).
    ranked = rank_search(
        {p: v for p, v in results_by_provider.items() if v},
        query,
        top_k=max_results,
    )

    # Reconstruct the provider each surviving URL came from (first provider wins).
    url_to_providers: dict[str, list[str]] = {}
    for provider, items in results_by_provider.items():
        for it in items:
            if it.url:
                url_to_providers.setdefault(_canonical_key(it.url), []).append(provider)

    trust_cache: dict[str, tuple] = {}
    sources: list[Source] = []
    seen_canonical: set[str] = set()
    deduplicated = max(0, total_candidates - len(ranked))

    for item in ranked:
        key = _canonical_key(item.url)
        if key in seen_canonical:
            continue
        seen_canonical.add(key)
        providers = url_to_providers.get(key) or [item.provider or "unknown"]
        src = _to_source(item, len(sources), item.provider or providers[0], trust_cache)
        # Recompute trust with the *full* provider set for agreement bonuses.
        if len(set(providers)) >= 2 and src.trust in (
            SourceTrust.UNVERIFIED,
            SourceTrust.LOW,
        ):
            src.trust = SourceTrust.MEDIUM
            if "multi-provider agreement" not in src.trust_reasons:
                src.trust_reasons.append("multi-provider agreement")
        # Ensure providers actually contributing are recorded on the source.
        src.search_provider = ",".join(sorted(set(providers))) or src.search_provider
        sources.append(src)

    trust_distribution = Counter(s.trust or "unverified" for s in sources)
    independent = len({_domain(s.url) for s in sources if s.url})

    return EvidencePackage(
        query=query,
        sources=sources,
        aggregation=EvidenceAggregationStats(
            total_candidates=total_candidates,
            providers_used=providers_used,
            deduplicated=deduplicated,
            independent_sources=independent,
            trust_distribution=dict(trust_distribution),
            coverage=round(min(1.0, len(sources) / max(1, max_results)), 3),
        ),
    )
