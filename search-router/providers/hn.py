"""Hacker News search provider — Algolia HN Search API (public, no key).

API docs: https://hn.algolia.com/api/v1/search
Endpoint: https://hn.algolia.com/api/v1/search?query=<q>&tags=story&hitsPerPage=<n>
          https://hn.algolia.com/api/v1/search_by_date?query=<q>&tags=story

Returns SearchResultItem-compatible results so they can be merged with
SearXNG results by the orchestrator.
"""

from __future__ import annotations

import httpx
from models import SearchResultItem

_HN_BASE = "https://hn.algolia.com/api/v1"


async def hn_search(
    query: str,
    max_results: int = 10,
    sort_by_date: bool = False,
    tags: str = "story",
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """Search Hacker News via the Algolia public API.

    Args:
        query: Search query string.
        max_results: Max number of hits to return.
        sort_by_date: If True, use the ``search_by_date`` endpoint (recent first).
        tags: Algolia tag filter (default "story"; use "comment" for comments).

    Returns:
        List of SearchResultItem with HN story URLs and metadata.
    """
    endpoint = f"{_HN_BASE}/search_by_date" if sort_by_date else f"{_HN_BASE}/search"
    params = {
        "query": query,
        "tags": tags,
        "hitsPerPage": max_results,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(endpoint, params=params)
            if resp.status_code != 200:
                if call_error is not None:
                    call_error.append(f"HTTP {resp.status_code}")
                return []
            data = resp.json()
        except Exception as exc:
            if call_error is not None:
                call_error.append(f"{type(exc).__name__}: {exc}")
            return []

    hits = data.get("hits", [])[:max_results]
    results: list[SearchResultItem] = []
    for hit in hits:
        object_id = hit.get("objectID", "")
        # HN story URL or external URL.
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        title = hit.get("title") or hit.get("story_title") or ""
        points = hit.get("points") or 0
        num_comments = hit.get("num_comments") or 0
        author = hit.get("author") or ""
        created_at = hit.get("created_at") or ""

        # Build a description from HN metadata.
        desc_parts = []
        if points:
            desc_parts.append(f"{points} points")
        if num_comments:
            desc_parts.append(f"{num_comments} comments")
        if author:
            desc_parts.append(f"by {author}")
        description = " | ".join(desc_parts)

        results.append(
            SearchResultItem(
                url=url,
                title=title,
                description=description,
                score=float(points or 0),
                engine="hn",
                category="it",
                published_date=created_at or None,
            )
        )

    return results


async def hn_health() -> bool:
    """Check if the HN Algolia API is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{_HN_BASE}/search", params={"query": "test", "hitsPerPage": 1}
            )
            return resp.status_code == 200
    except Exception:
        return False
