"""L6 URL Pre-Ranking — cheap scoring before fetch.

Scores URLs based on entity match, authority, freshness, and diversity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.url_normalize import NormalizedURL

# Domain authority scores (expandable)
DOMAIN_AUTHORITY: dict[str, float] = {
    # Official/vendor docs
    "help.aliyun.com": 0.97,
    "docs.python.org": 0.97,
    "openai.com": 0.97,
    "platform.openai.com": 0.97,
    "anthropic.com": 0.97,
    "docs.anthropic.com": 0.97,
    "huggingface.co": 0.95,
    "github.com": 0.93,
    "docs.docker.com": 0.96,
    "kubernetes.io": 0.96,
    "react.dev": 0.95,
    "vuejs.org": 0.95,
    "wikipedia.org": 0.90,
    "arxiv.org": 0.92,
    "stackoverflow.com": 0.88,
    "reddit.com": 0.75,
    "medium.com": 0.70,
    "dev.to": 0.72,
}

# Intent-specific domain multipliers
INTENT_DOMAIN_MULTIPLIERS: dict[str, dict[str, float]] = {
    "pricing_lookup": {
        "official": 1.5,
        "vendor_docs": 1.4,
        "technical_blog": 0.8,
        "aggregator": 0.6,
        "seo_farm": 0.3,
    },
    "product_opinion": {
        "reddit": 1.4,
        "forum": 1.3,
        "review": 1.2,
        "vendor_marketing": 0.6,
        "blog": 0.8,
    },
    "api_reference": {
        "official_docs": 1.5,
        "github": 1.3,
        "tutorial": 0.9,
        "aggregator": 0.5,
    },
    "news_event": {
        "wire": 1.5,
        "news": 1.3,
        "aggregator": 0.7,
        "blog": 0.6,
    },
}


@dataclass
class PreRankedURL:
    """URL with pre-ranking score."""

    url: NormalizedURL
    score: float
    entity_match: float = 0.0
    authority: float = 0.0
    freshness_fit: float = 0.0
    diversity_penalty: float = 0.0


class URLPreRanker:
    """Cheap pre-ranking of URLs before fetch."""

    def __init__(self, intent: str = "current_fact", entities: list[str] | None = None):
        self.intent = intent
        self.entities = entities or []
        self._domain_counts: dict[str, int] = {}

    def rank(self, urls: list[NormalizedURL], top_n: int = 15) -> list[PreRankedURL]:
        """Rank URLs and return top N."""
        scored: list[PreRankedURL] = []

        for url in urls:
            # Skip blocked URLs
            if url.is_private or url.is_spam:
                continue

            # Calculate score components
            entity_match = self._entity_match(url)
            authority = self._authority_score(url)
            freshness_fit = self._freshness_fit(url)
            diversity_penalty = self._diversity_penalty(url)

            # Final score
            score = (
                0.30 * entity_match
                + 0.25 * authority
                + 0.15 * freshness_fit
                + 0.10 * (1.0 - diversity_penalty)
                - 0.20 * url.spam_score
            )

            scored.append(
                PreRankedURL(
                    url=url,
                    score=score,
                    entity_match=entity_match,
                    authority=authority,
                    freshness_fit=freshness_fit,
                    diversity_penalty=diversity_penalty,
                )
            )

            # Track domain count for diversity
            self._domain_counts[url.domain] = self._domain_counts.get(url.domain, 0) + 1

        # Sort by score descending
        scored.sort(key=lambda x: x.score, reverse=True)

        # Apply diversity quota
        result: list[PreRankedURL] = []
        domain_counts: dict[str, int] = {}
        max_per_domain = 2 if self.intent != "research" else 3

        for item in scored:
            domain = item.url.domain
            if domain_counts.get(domain, 0) < max_per_domain:
                result.append(item)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
            if len(result) >= top_n:
                break

        return result

    def _entity_match(self, url: NormalizedURL) -> float:
        """Check if URL matches query entities."""
        if not self.entities:
            return 0.5  # neutral

        url_text = f"{url.domain} {url.path}".lower()
        matches = 0
        for entity in self.entities:
            if entity.lower() in url_text:
                matches += 1

        return min(matches / len(self.entities), 1.0) if self.entities else 0.5

    def _authority_score(self, url: NormalizedURL) -> float:
        """Calculate authority score for URL."""
        # Base domain score
        base_score = DOMAIN_AUTHORITY.get(url.domain, 0.5)

        # Apply intent multiplier
        intent_multipliers = INTENT_DOMAIN_MULTIPLIERS.get(self.intent, {})
        source_type = self._classify_source_type(url)
        multiplier = intent_multipliers.get(source_type, 1.0)

        return min(base_score * multiplier, 1.0)

    def _freshness_fit(self, url: NormalizedURL) -> float:
        """Estimate freshness fit (without actual content)."""
        # Check URL patterns for date indicators
        date_patterns = [
            r"/20\d{2}/",  # /2024/
            r"/20\d{2}-\d{2}/",  # /2024-01/
            r"/latest/",
            r"/new/",
            r"/release/",
        ]

        for pattern in date_patterns:
            if re.search(pattern, url.path, re.IGNORECASE):
                return 0.8

        return 0.5  # neutral

    def _diversity_penalty(self, url: NormalizedURL) -> float:
        """Penalty for duplicate domains."""
        count = self._domain_counts.get(url.domain, 0)
        return min(count * 0.2, 1.0)

    def _classify_source_type(self, url: NormalizedURL) -> str:
        """Classify URL source type."""
        domain = url.domain

        if any(d in domain for d in ["help.", "docs.", "documentation"]):
            return "official_docs"
        if "github.com" in domain:
            return "github"
        if "reddit.com" in domain:
            return "reddit"
        if "stackoverflow.com" in domain:
            return "stackoverflow"
        if "medium.com" in domain or "dev.to" in domain:
            return "blog"
        if "news" in domain or "vnexpress" in domain or "tuoitre" in domain:
            return "news"

        return "general"
