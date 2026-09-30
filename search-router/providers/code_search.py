"""Code search provider — GitHub Search API + grep.app."""

import httpx
from config import settings


async def github_search(
    query: str,
    max_results: int = 10,
    search_type: str = "code",  # code | repos | issues
    call_error: list[str] | None = None,
) -> list[dict]:
    """Search GitHub via REST API."""
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    endpoint = f"https://api.github.com/search/{search_type}"
    params = {"q": query, "per_page": max_results}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(endpoint, headers=headers, params=params)

    if resp.status_code != 200:
        if call_error is not None:
            call_error.append(f"HTTP {resp.status_code}")
        return []

    data = resp.json()
    items = data.get("items", [])[:max_results]

    if search_type == "code":
        return [
            {
                "url": item.get("html_url", ""),
                "title": f"{item.get('repository', {}).get('full_name', '')}: {item.get('path', '')}",
                "description": item.get("name", ""),
                "score": item.get("score", 0.0),
                "repository": item.get("repository", {}).get("full_name", ""),
                "path": item.get("path", ""),
            }
            for item in items
        ]
    elif search_type == "repos":
        return [
            {
                "url": item.get("html_url", ""),
                "title": item.get("full_name", ""),
                "description": item.get("description", ""),
                "score": item.get("stargazers_count", 0),
                "stars": item.get("stargazers_count", 0),
                "language": item.get("language", ""),
            }
            for item in items
        ]
    else:
        return [
            {
                "url": item.get("html_url", ""),
                "title": item.get("title", ""),
                "description": item.get("body", "")[:200] if item.get("body") else "",
                "score": item.get("score", 0.0),
                "state": item.get("state", ""),
            }
            for item in items
        ]


async def grep_app_search(
    query: str, max_results: int = 10, call_error: list[str] | None = None
) -> list[dict]:
    """Search via grep.app API (public code search)."""
    # grep.app has a simple JSON API
    params = {"q": query, "case": "false"}

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get("https://grep.app/api/search", params=params)
            if resp.status_code != 200:
                if call_error is not None:
                    call_error.append(f"HTTP {resp.status_code}")
                return []
            data = resp.json()
        except Exception as exc:
            if call_error is not None:
                call_error.append(f"{type(exc).__name__}: {exc}")
            return []

    hits = data.get("hits", {}).get("hits", [])[:max_results]
    return [
        {
            "url": f"https://grep.app/search/{query}",
            "title": f"{h.get('repo', {}).get('raw', '')}/{h.get('path', {}).get('raw', '')}",
            "description": h.get("content", {}).get("snippet", ""),
            "score": h.get("score", 0.0),
            "repository": h.get("repo", {}).get("raw", ""),
            "path": h.get("path", {}).get("raw", ""),
        }
        for h in hits
    ]
