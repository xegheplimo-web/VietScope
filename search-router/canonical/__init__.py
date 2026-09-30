"""Canonicalization, content deduplication, and freshness utilities."""

from .content import content_fingerprint, near_duplicate
from .freshness import freshness_score, query_ttl_hint
from .url import canonical_identity, canonical_url, cluster_sources

__all__ = [
    "canonical_identity",
    "canonical_url",
    "cluster_sources",
    "content_fingerprint",
    "freshness_score",
    "near_duplicate",
    "query_ttl_hint",
]
