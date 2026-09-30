"""OpenSearch index provider — internal lexical lane (v2.1 §9).

Queries the local ``web_passages`` BM25 index built by the L17 indexing
worker and returns deduplicated ``SearchResultItem``s keyed by document URL.
Always degrades to ``[]`` when the index is unreachable — the live-web lanes
stay authoritative while the index is cold.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import settings
from models import SearchResultItem
from opensearch.client import OpenSearchClient

logger = logging.getLogger(__name__)

# BM25 scores are unbounded; divide by this to land roughly in [0, 1] so the
# downstream composite ranker can mix them with engine scores.
_SCORE_NORM = 20.0


class OpenSearchIndexProvider:
    """SearchProvider over the internal OpenSearch passage index."""

    name = "opensearch"

    def __init__(self, client: OpenSearchClient | None = None):
        self._client = client

    @property
    def client(self) -> OpenSearchClient:
        if self._client is None:
            self._client = OpenSearchClient()
        return self._client

    async def search(self, query) -> list[SearchResultItem]:
        """BM25 over web_passages, deduplicated to one hit per URL."""
        top_k = min(max(query.max_results * 3, 10), 60)
        hits = await self.client.search(
            index=settings.opensearch_index_passages,
            query=query.query,
            top_k=top_k,
        )
        return self._to_results(hits, query.max_results)

    @staticmethod
    def _to_results(hits: list[dict[str, Any]], max_results: int) -> list[SearchResultItem]:
        best_by_url: dict[str, SearchResultItem] = {}
        for hit in hits:
            src = hit.get("source") or {}
            url = src.get("canonical_url") or src.get("url")
            if not url:
                continue
            score = min(1.0, (hit.get("score") or 0.0) / _SCORE_NORM)
            item = SearchResultItem(
                url=url,
                title=src.get("title") or "",
                description=(src.get("text_ctx") or src.get("text") or "")[:400],
                score=score,
                engine="opensearch",
                published_date=src.get("published_at"),
            )
            existing = best_by_url.get(url)
            if existing is None or item.score > existing.score:
                best_by_url[url] = item
        results = sorted(best_by_url.values(), key=lambda r: r.score, reverse=True)
        return results[:max_results]

    async def health(self) -> bool:
        try:
            return await asyncio.to_thread(self.client.health)
        except Exception:
            return False
