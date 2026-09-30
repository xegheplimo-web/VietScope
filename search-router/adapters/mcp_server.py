"""Search Hub MCP Server — agent-facing tools via MCP for Hermes.

Thin adapter only: every tool forwards to the Search Router API. Fetch and
passage reranking live server-side on the SSRF-guarded evidence path
(``POST /v1/evidence``) — no retrieval logic is duplicated here.

Default tool surface (Hermes retrieval contract — Hermes synthesizes):
  search(query, type, max_results)      → web/news/image search (deduped + diversified)
  fetch_evidence(sources, query)        → protected-reader fetch + passage rerank
  code_search(query, repo)              → code search via GitHub + grep.app
  search_places(query, lat, lon, ...)   → local places search (rich place cards)

Opt-in tools — registered only when MCP_ENABLE_DEEP_RESEARCH=true:
  fetch(url, mode, limit)               → read a URL (scrape → SSRF-guarded
                                          /v1/read; crawl/map → legacy /fetch)
  research(query, depth)                → multi-hop research (needs research:use
                                          scope beyond the scoped router key)

``answer`` is never registered — synthesis is Hermes' job, not a tool call.

Usage (from search-router/):
    python -m adapters.mcp_server                    # stdio transport (default)
    MCP_TRANSPORT=http python -m adapters.mcp_server # HTTP transport on port 8901

Environment:
    SEARCH_ROUTER_URL     — URL of the Search Router (default: http://localhost:8888)
    SEARCH_HUB_ROUTER_KEY — Bearer key sent to the router; a scoped key
                            (search:read + read:use + places:read) created
                            via manage_keys.py.
                            The admin key is deliberately not a fallback — the
                            normal MCP path must never hold admin credentials.
                            Unset → no Authorization header (dev auth-off mode).
    MCP_HOST              — HTTP bind host (default: 127.0.0.1)
    MCP_PORT              — HTTP bind port (default: 8901)
    MCP_TIMEOUT_S         — httpx read timeout for router calls (default: 360;
                            must exceed the API router's OPENAI_TIMEOUT_S=300
                            so the MCP layer does not disconnect first)
    MCP_ENABLE_DEEP_RESEARCH — truthy enables the opt-in tools (fetch, research)
                            and raises the fetch_evidence source cap 8 → 15
"""

import json
import os
import sys
from typing import Any

import httpx

try:
    from mcp.server.mcpserver import MCPServer as _Server

    _HTTP_BIND_ON_RUN = True
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

    _HTTP_BIND_ON_RUN = False

SEARCH_ROUTER_URL = os.environ.get("SEARCH_ROUTER_URL", "http://localhost:8888")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8901"))

mcp = _Server("search-hub") if _HTTP_BIND_ON_RUN else _Server("search-hub", host=HOST, port=PORT)


def _auth_headers() -> dict:
    """Bearer header for the Search Router, or {} when no key is configured.

    Only the scoped ``SEARCH_HUB_ROUTER_KEY`` is honored — admin keys are
    deliberately not a fallback on the normal MCP path (least privilege).
    """
    key = os.environ.get("SEARCH_HUB_ROUTER_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _post(endpoint: str, body: dict) -> str:
    """Call Search Router endpoint with JSON body."""
    url = f"{SEARCH_ROUTER_URL}{endpoint}"
    timeout_s = int(os.environ.get("MCP_TIMEOUT_S", "360"))
    with httpx.Client(timeout=timeout_s, headers=_auth_headers()) as client:
        resp = client.post(url, json=body)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2, ensure_ascii=False)


def _get(endpoint: str, params: dict | None = None) -> str:
    """Call a Search Router GET endpoint — same auth header as _post."""
    url = f"{SEARCH_ROUTER_URL}{endpoint}"
    timeout_s = int(os.environ.get("MCP_TIMEOUT_S", "360"))
    query = {k: v for k, v in (params or {}).items() if v is not None}
    with httpx.Client(timeout=timeout_s, headers=_auth_headers()) as client:
        resp = client.get(url, params=query)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2, ensure_ascii=False)


def _deep_research_enabled() -> bool:
    return os.environ.get("MCP_ENABLE_DEEP_RESEARCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _diversify_search_results(data: dict, max_per_domain: int = 2) -> dict:
    """Deduplicate URLs and cap results per domain (MCP-layer safeguard).

    - URL deduplication: case-insensitive, skip exact duplicates.
    - Domain diversity: at most ``max_per_domain`` results per domain,
      keeping the highest-scoring ones first.
    """
    results = data.get("results", [])
    # URL dedup (case-insensitive)
    seen_urls: set[str] = set()
    deduped = []
    for r in results:
        url = (r.get("url") or "").lower().strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(r)
    # Domain diversity cap — keep top max_per_domain per domain
    domain_counts: dict[str, int] = {}
    diversified = []
    for r in sorted(deduped, key=lambda x: x.get("score", 0), reverse=True):
        domain = (r.get("domain") or "").lower().strip()
        if domain:
            count = domain_counts.get(domain, 0)
            if count >= max_per_domain:
                continue
            domain_counts[domain] = count + 1
        diversified.append(r)
    data["results"] = diversified
    data["count"] = len(diversified)
    return data


def search(query: str, type: str = "web", max_results: int = 10, lang: str = "auto") -> str:
    """Search the web for information.

    Args:
        query: What to search for
        type: "web" (general), "news" (recent articles), or "image" (images)
        max_results: Number of results to return (default 10)
        lang: Language code — "auto" (default, detects Vietnamese/English), "vi", "en", ...

    Returns ranked sources with URL, title, description, domain, and score.
    Use this for most information-finding queries.

    Post-processing: URL deduplication (case-insensitive) and domain
    diversity safeguard (max 2 results per domain) applied at the MCP layer.
    """
    raw = _post(
        "/search",
        {
            "query": query,
            "type": type,
            "max_results": max_results,
            "lang": lang,
        },
    )
    data = json.loads(raw)
    data = _diversify_search_results(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def fetch(url: str, mode: str = "scrape", limit: int = 10) -> str:
    """Read content from a URL.

    Opt-in tool (MCP_ENABLE_DEEP_RESEARCH) — not on the default Hermes
    surface. Single-page reads go through the SSRF-guarded /v1/read path;
    crawl/map have no /v1 equivalent and still use the legacy endpoint.

    Args:
        url: The URL to read
        mode: "scrape" (single page), "crawl" (multiple pages), or "map" (discover URLs)
        limit: Max pages for crawl/map (default 10)

    Returns chunked passages (scrape), clean Markdown (crawl) or URL list (map).
    """
    if mode == "scrape":
        return _post("/v1/read", {"url": url})
    return _post("/fetch", {"url": url, "mode": mode, "limit": limit})


def fetch_evidence(
    sources: list[dict[str, Any] | str],
    query: str,
    max_passages: int = 5,
    max_per_source: int = 3,
    freshness: str = "normal",
) -> str:
    """Fetch source content and extract reranked evidence passages.

    Delegates to ``POST /v1/evidence``: each source is read through the
    SSRF-guarded tiered reader (not the legacy ``/fetch`` endpoint),
    chunked and reranked by the shared passage pipeline. Prefer passing
    structured ``search`` results — ``source_id``, ``canonical_url``,
    ``title``, ``domain``, ``published_at``, ``search_provider``, ``score``
    are echoed back on every evidence item so citations keep provenance
    (claim → passage_id → source_id → URL). Bare URL strings are also
    accepted.

    Args:
        sources: Sources to read — dicts with at least ``url`` (ideally the
            full search-result row) or plain URL strings. Capped at 8
            sources (15 when MCP_ENABLE_DEEP_RESEARCH) — pick the best
            candidates first; the API itself rejects >20.
        query: The query to rerank passages against.
        max_passages: Maximum total passages to return (default 5).
        max_per_source: Maximum passages from a single source (default 3).
        freshness: Reader-cache policy — "realtime" (bypass cache for
            fresh-content queries), "high" (5 min TTL), "normal" (1 h,
            default), "static" (no expiry).

    Returns JSON ``{query, evidence, count, elapsed_seconds}`` where each
    evidence item carries ``source_id``, ``passage_id``, ``url``,
    ``canonical_url``, ``title``, ``domain``, ``published_at``,
    ``retrieved_at``, ``text``, ``quote``, ``score``, ``search_provider``,
    ``content_provider`` (``error`` on per-source failures).
    """
    max_sources = 15 if _deep_research_enabled() else 8
    if len(sources) > max_sources:
        return json.dumps(
            {
                "error": (
                    f"fetch_evidence accepts at most {max_sources} sources "
                    f"(got {len(sources)}); pick the strongest candidates first"
                ),
                "evidence": [],
                "count": 0,
            },
            indent=2,
            ensure_ascii=False,
        )
    return _post(
        "/v1/evidence",
        {
            "sources": sources,
            "query": query,
            "max_passages": max_passages,
            "max_per_source": max_per_source,
            "freshness": freshness,
        },
    )


def research(query: str, depth: str = "quick") -> str:
    """Research a query using the internal research pipeline.

    Args:
        query: What to research
        depth: "quick" (concise), "normal" (thorough), or "deep" (multi-step)

    Returns AI-synthesized evidence pack. Use this when you need analysis,
    not just raw search results.

    Opt-in tool (MCP_ENABLE_DEEP_RESEARCH) — requires the `research:use`
    scope beyond the scoped router key.
    """
    mode = {"quick": "fast", "normal": "balanced"}.get(depth, "deep")
    return _post("/v1/research", {"query": query, "mode": mode})


def code_search(query: str, max_results: int = 10, repo: str = "") -> str:
    """Search code repositories via GitHub and grep.app.

    Args:
        query: Code search query
        max_results: Number of results (default 10)
        repo: Optional repo filter, e.g. "firecrawl/firecrawl"

    Returns code matches with repository, file path, and URL.
    """
    body = {"query": query, "max_results": max_results}
    if repo:
        body["repo"] = repo
    return _post("/code_search", body)


def search_places(
    query: str,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 10000.0,
    category: str | None = None,
    limit: int = 10,
) -> str:
    """Search nearby local places — rich place cards from the canonical index.

    Use this for nearby/local intent: restaurants, quán ăn, cafe, phở,
    bún, ăn sáng, nhà thuốc/tiệm thuốc, cây xăng, ATM, khách sạn, or
    "gần đây"/"gần tôi" queries. Returns places with name, category,
    coordinates, opening hours, and — when the data carries them —
    rating, review_count, price_level, open_now, map_url, images.

    When fewer than ~3 results come back, the local index likely lacks
    coverage for that area — fall back to ``search`` (web) instead.

    Args:
        query: Place name or category text (e.g. "quán ăn Yên Dũng",
            "cafe", "nhà thuốc")
        lat: Optional user latitude — enables distance ranking
        lon: Optional user longitude — pair with lat
        radius_m: Search radius in meters (default 10000)
        category: Canonical category filter (e.g. "food", "health",
            "retail", "lodging")
        limit: Max results (default 10)
    """
    return _get(
        "/v1/places/search",
        {
            "q": query,
            "lat": lat,
            "lon": lon,
            "radius_m": radius_m,
            "category": category,
            "limit": limit,
        },
    )


def answer(query: str, depth: str = "normal", max_results: int = 10) -> str:
    """Full research pipeline: search → scrape → rerank → synthesize with citations.

    Args:
        query: The question to answer
        depth: "normal" (search + scrape + synthesize) or "deep" (above + follow-ups)
        max_results: Number of sources to search (default 10)

    Returns answer + evidence (sources with metadata + claim-to-source citations).

    Not exposed as an MCP tool — Hermes is the synthesizer; kept callable
    for scripts/ops use.
    """
    return _post(
        "/answer",
        {
            "query": query,
            "depth": depth,
            "max_results": max_results,
        },
    )


def main():
    if not os.environ.get("SEARCH_HUB_ROUTER_KEY") and os.environ.get("HUB_ADMIN_KEY"):
        print(
            "warning: HUB_ADMIN_KEY is ignored on the MCP path — create a scoped "
            "key (search:read,read:use,places:read) via manage_keys.py and set "
            "SEARCH_HUB_ROUTER_KEY instead",
            file=sys.stderr,
        )
    # Default Hermes surface: retrieval + evidence only. `answer` is never
    # registered (Hermes synthesizes); fetch/research are opt-in under
    # MCP_ENABLE_DEEP_RESEARCH.
    for tool in (search, fetch_evidence, code_search, search_places):
        mcp.tool()(tool)
    if _deep_research_enabled():
        mcp.tool()(fetch)
        mcp.tool()(research)

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        sys.exit("ERROR: MCP_TRANSPORT must be 'stdio' or 'http'")

    if transport == "stdio":
        mcp.run()
    elif _HTTP_BIND_ON_RUN:
        mcp.run(transport="streamable-http", host=HOST, port=PORT)
    else:
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
