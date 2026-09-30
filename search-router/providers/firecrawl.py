"""Firecrawl provider — scrape, crawl, map, and search."""

import asyncio

import httpx
from config import settings
from models import ScrapeResult


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.firecrawl_api_key:
        h["Authorization"] = f"Bearer {settings.firecrawl_api_key}"
    return h


async def firecrawl_scrape(
    url: str,
    formats: list[str] | None = None,
    timeout: int = 60,
) -> ScrapeResult:
    """Scrape a single URL via Firecrawl v2 API."""
    formats = formats or ["markdown"]
    body = {"url": url, "formats": formats}

    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        resp = await client.post(
            f"{settings.firecrawl_url}/v2/scrape",
            json=body,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("success"):
        return ScrapeResult(
            url=url,
            error=data.get("error", "Scrape failed"),
        )

    d = data.get("data", {})
    return ScrapeResult(
        url=url,
        title=d.get("metadata", {}).get("title", ""),
        markdown=d.get("markdown", ""),
        metadata=d.get("metadata", {}),
    )


async def firecrawl_crawl(
    url: str,
    limit: int = 10,
    formats: list[str] | None = None,
    poll_interval: int = 3,
    max_wait: int = 120,
) -> dict:
    """Start a crawl job and poll until completion."""
    formats = formats or ["markdown"]
    body = {"url": url, "limit": limit, "scrapeOptions": {"formats": formats}}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.firecrawl_url}/v2/crawl",
            json=body,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("success"):
        return data

    job_id = data.get("id")
    if not job_id:
        return data

    # Poll for completion
    elapsed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            resp = await client.get(
                f"{settings.firecrawl_url}/v2/crawl/{job_id}",
                headers=_headers(),
            )
            status_data = resp.json()
            if status_data.get("status") in ("completed", "failed"):
                return status_data

    return {"success": False, "error": "Crawl timed out", "id": job_id}


async def firecrawl_map(url: str, limit: int = 20) -> list[str]:
    """Map a website to discover all URLs."""
    body = {"url": url, "limit": limit}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.firecrawl_url}/v2/map",
            json=body,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("success"):
        return []

    links = data.get("links", [])
    if isinstance(links, list):
        if links and isinstance(links[0], dict):
            return [link.get("url", "") for link in links if link.get("url")]
        return [str(link) for link in links if link]
    return []


async def firecrawl_search(
    query: str,
    limit: int = 5,
    scrape: bool = False,
) -> list[dict]:
    """Search via Firecrawl (uses SearXNG backend natively)."""
    body: dict = {"query": query, "limit": limit}
    if scrape:
        body["scrapeOptions"] = {"formats": ["markdown"]}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.firecrawl_url}/v2/search",
            json=body,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("success"):
        return []

    # Server returns `data` as a plain list of {url, title, description};
    # v1-shape {"web": [...]} is tolerated defensively.
    payload = data.get("data", [])
    web = payload.get("web", []) if isinstance(payload, dict) else payload
    if not isinstance(web, list):
        web = []
    return web


async def firecrawl_health() -> bool:
    """Check if Firecrawl API is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.firecrawl_url}/v2/health")
            return resp.status_code in (200, 404)
    except Exception:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.firecrawl_url}/")
                return resp.status_code in (200, 404)
        except Exception:
            return False
