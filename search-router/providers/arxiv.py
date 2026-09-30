"""arXiv search provider — arXiv API (public, no key).

API docs: https://info.arxiv.org/help/api/index.html
Endpoint: http://export.arxiv.org/api/query?search_query=<q>&max_results=<n>&sortBy=relevance

Returns SearchResultItem-compatible results so they can be merged with
SearXNG results by the orchestrator.

Uses Atom XML feeds (parsed with xml.etree.ElementTree from stdlib).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from models import SearchResultItem

_ARXIV_BASE = "http://export.arxiv.org/api/query"

# Atom XML namespaces used by arXiv.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


def _build_search_query(query: str) -> str:
    """Build an arXiv search_query string from a free-text query.

    Searches all fields (all) by default. Quotes multi-word terms.
    """
    terms = re.findall(r"\S+", query.strip())
    if not terms:
        return ""
    return "+AND+".join(f"all:{t}" for t in terms)


def _parse_date(date_str: str) -> str | None:
    """Parse an arXiv date (ISO 8601) into a normalized string."""
    if not date_str:
        return None
    try:
        # arXiv uses e.g. 2025-01-15T00:00:00Z
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return date_str or None


async def arxiv_search(
    query: str,
    max_results: int = 10,
    sort_by: str = "relevance",  # relevance | lastUpdatedDate | submittedDate
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """Search arXiv via the public Atom API.

    Args:
        query: Free-text search query.
        max_results: Max number of papers to return.
        sort_by: Sort criterion — "relevance", "lastUpdatedDate", or "submittedDate".

    Returns:
        List of SearchResultItem with arXiv paper URLs and metadata.
    """
    search_query = _build_search_query(query)
    if not search_query:
        return []

    params = {
        "search_query": search_query,
        "max_results": str(max_results),
        "sortBy": sort_by,
        "sortOrder": "descending",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(_ARXIV_BASE, params=params)
            if resp.status_code != 200:
                if call_error is not None:
                    call_error.append(f"HTTP {resp.status_code}")
                return []
            xml_text = resp.text
        except Exception as exc:
            if call_error is not None:
                call_error.append(f"{type(exc).__name__}: {exc}")
            return []

    return _parse_arxiv_xml(xml_text, max_results)


def _parse_arxiv_xml(xml_text: str, max_results: int) -> list[SearchResultItem]:
    """Parse an arXiv Atom feed into SearchResultItem list."""
    results: list[SearchResultItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results

    for entry in root.findall("atom:entry", _NS)[:max_results]:
        # Primary URL = abs page; PDF link is in a <link> with title="pdf".
        id_elem = entry.find("atom:id", _NS)
        url = (id_elem.text or "").strip() if id_elem is not None else ""

        title_elem = entry.find("atom:title", _NS)
        title = (title_elem.text or "").strip() if title_elem is not None else ""
        # arXiv titles often have excess whitespace.
        title = re.sub(r"\s+", " ", title)

        summary_elem = entry.find("atom:summary", _NS)
        summary = (summary_elem.text or "").strip() if summary_elem is not None else ""
        summary = re.sub(r"\s+", " ", summary)[:300]

        published_elem = entry.find("atom:published", _NS)
        published = _parse_date(published_elem.text or "") if published_elem is not None else None

        # Authors.
        authors = []
        for author_elem in entry.findall("atom:author", _NS):
            name_elem = author_elem.find("atom:name", _NS)
            if name_elem is not None and name_elem.text:
                authors.append(name_elem.text.strip())
        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += " et al."

        # DOI / journal ref if present.
        doi_elem = entry.find("arxiv:doi", _NS)
        doi = (doi_elem.text or "").strip() if doi_elem is not None else ""

        desc_parts = []
        if author_str:
            desc_parts.append(author_str)
        if doi:
            desc_parts.append(f"DOI: {doi}")
        if summary:
            desc_parts.append(summary[:200])
        description = " | ".join(desc_parts)

        results.append(
            SearchResultItem(
                url=url,
                title=title,
                description=description,
                score=1.0,  # arXiv doesn't return a relevance score; use neutral.
                engine="arxiv",
                category="science",
                published_date=published,
            )
        )

    return results


async def arxiv_health() -> bool:
    """Check if the arXiv API is reachable."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _ARXIV_BASE, params={"search_query": "all:test", "max_results": 1}
            )
            return resp.status_code == 200
    except Exception:
        return False
