"""Exa search provider — semantic/AI-native web search (PREMIUM, key-gated).

API docs: https://docs.exa.ai/reference/search
Endpoint: POST https://api.exa.ai/search (JSON body, ``Authorization: Bearer <key>``)

Exa is an OPTIONAL premium provider reserved for deep_research / semantic
intents — it is never part of the default search path. Without ``EXA_API_KEY``
in the environment the provider reports unhealthy (``False``) and every search
returns ``[]`` immediately (no API call, no error).

Results are normalized to ``SearchResultItem`` so they can be merged with
SearXNG/arXiv/HN results by the orchestrator.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from config import settings
from models import SearchResultItem

logger = logging.getLogger(__name__)

_EXA_API_URL = "https://api.exa.ai/search"
_EXA_TIMEOUT = 30.0
_DEFAULT_TEXT_CHARS = 500

# Shared SearXNG-style ``time_range`` literals → Exa ``startPublishedDate``.
TIME_RANGE_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

# Exa category filter — only forward values the API actually understands.
# Anything else (e.g. "general") is omitted to avoid a 400.
_EXA_CATEGORIES = frozenset({"news", "research paper", "company", "github"})


def is_configured() -> bool:
    """Exa is optional: without a key the provider is skipped entirely."""
    return bool(settings.exa_api_key and settings.exa_api_key.strip())


def _map_category(category: str) -> str:
    """Map a SearchCategory value onto an Exa category (or "" to omit)."""
    cat = (category or "").strip()
    return cat if cat in _EXA_CATEGORIES else ""


def _build_payload(
    query: str,
    max_results: int,
    category: str = "",
    time_range: str | None = None,
) -> dict:
    payload: dict = {
        "query": query,
        "numResults": max(1, int(max_results or 1)),
        "type": "auto",
        "contents": {"text": {"maxCharacters": _DEFAULT_TEXT_CHARS}},
    }
    cat = _map_category(category)
    if cat:
        payload["category"] = cat
    days = TIME_RANGE_DAYS.get(time_range or "")
    if days:
        start = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        payload["startPublishedDate"] = start
    return payload


def _parse_results(data: dict, category: str = "") -> list[SearchResultItem]:
    results: list[SearchResultItem] = []
    for r in data.get("results", []) or []:
        url = r.get("url") or ""
        if not url:
            continue
        title = r.get("title") or ""
        text = r.get("text") or r.get("highlight") or ""
        results.append(
            SearchResultItem(
                url=url,
                title=title,
                description=text[:_DEFAULT_TEXT_CHARS] if text else "",
                score=float(r.get("score") or 0.0),
                engine="exa",
                category=_map_category(category),
                published_date=r.get("publishedDate") or None,
                thumbnail="",
            )
        )
    return results


async def exa_search(
    query: str,
    max_results: int = 10,
    time_range: str | None = None,
    category: str = "",
) -> list[SearchResultItem]:
    """Search via the Exa Web Search API (fail-soft: exception → [] + log).

    Args:
        query: Free-text search query.
        max_results: Max number of results to return.
        time_range: Optional freshness window — day | week | month | year
            (mapped to Exa ``startPublishedDate``). Unknown values are ignored.
        category: Optional Exa category filter (news / research paper / ...).

    Returns:
        List of SearchResultItem, or ``[]`` when not configured / on error.
    """
    if not is_configured():
        logger.debug("exa provider not configured — skipping search")
        return []

    payload = _build_payload(query, max_results, category=category, time_range=time_range)
    headers = {
        "Authorization": f"Bearer {settings.exa_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_EXA_TIMEOUT) as client:
            resp = await client.post(_EXA_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("exa search failed: %s", exc)
        return []

    return _parse_results(data, category=category)[:max_results]


async def exa_health() -> bool:
    """Exa health: ``True`` when a key is set and the API answers, else ``False``.

    Mirrors ``searxng_health`` so every provider exposes the same ``bool``
    contract to ``core.provider_registry``.
    """
    if not is_configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _EXA_API_URL,
                json={"query": "test", "numResults": 1, "type": "auto"},
                headers={
                    "Authorization": f"Bearer {settings.exa_api_key}",
                    "Content-Type": "application/json",
                },
            )
            return resp.status_code == 200
    except Exception:
        return False
