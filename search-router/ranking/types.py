from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import SearchResultItem


@dataclass
class RankedItem:
    """A search result enriched with ranking metadata.

    This is the canonical item that flows through the unified ranking
    pipeline.  It keeps the original raw score, the normalized score, and
    the final composite score in one place without changing the legacy
    ``SearchResultItem`` contract.
    """

    url: str
    title: str = ""
    description: str = ""
    provider: str = ""
    raw_score: float = 0.0
    normalized_score: float = 0.0
    final_score: float = 0.0
    published_date: str | None = None
    canonical_url: str | None = None
    lat: float | None = None
    lon: float | None = None
    source_type: str = ""
    authority: float | None = None
    freshness: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_search_result(
        cls, result: SearchResultItem | dict[str, Any], *, provider: str = ""
    ) -> RankedItem:
        """Build a ``RankedItem`` from a provider result."""
        if isinstance(result, SearchResultItem):
            url = result.url or ""
            title = result.title or ""
            description = result.description or ""
            raw_score = float(result.score or 0.0)
            published_date = result.published_date
            engine = result.engine or ""
            source_type = result.category or ""
        else:
            url = str(result.get("url", ""))
            title = str(result.get("title", ""))
            description = str(result.get("description", "") or "")
            raw_score = float(result.get("score", 0.0) or 0.0)
            published_date = result.get("published_date")
            engine = str(result.get("engine", "") or "")
            source_type = str(result.get("category", "") or "")

        canonical = url
        if isinstance(result, dict) and result.get("canonical_url"):
            canonical = str(result["canonical_url"])

        return cls(
            url=url,
            title=title,
            description=description,
            provider=provider or engine,
            raw_score=raw_score,
            published_date=published_date,
            canonical_url=canonical,
            source_type=source_type,
        )
