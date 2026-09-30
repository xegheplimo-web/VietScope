"""Citation / Evidence layer — builds source metadata and claim-to-source mapping.

DEPRECATED(phase0): canonical evidence handling lives in ``evidence/``
(passage-level citations via ``evidence.citation``, claim extraction via
``evidence.claims``, verification via ``evidence.verifier``).  This module
serves only the legacy ``/answer`` endpoint in ``main.py`` and dies with it.
See ``docs/phase0-dedup-map.md``.

Every answer from the /answer endpoint includes an EvidenceResult with:
- sources: full provenance for each retrieved source
- citations: claim-to-source mapping (when LLM is available)
"""

import re
from urllib.parse import urlparse

from models import Citation, EvidenceResult, ScrapeResult, SearchResultItem, Source


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc or ""
    except Exception:
        return ""


def build_source_from_search(
    item: SearchResultItem,
    index: int,
    search_provider: str = "searxng",
) -> Source:
    """Build a Source object from a SearXNG search result."""
    return Source(
        source_id=f"src_{index:03d}",
        url=item.url,
        title=item.title or "",
        domain=_extract_domain(item.url),
        description=item.description or "",
        published_at=item.published_date,
        score=item.score,
        search_provider=search_provider,
        content_provider="none",
    )


def enrich_source_with_content(
    source: Source,
    scrape: ScrapeResult,
) -> Source:
    """Enrich a Source with fetched content (tiered reader or Firecrawl)."""
    provider = (scrape.metadata or {}).get("reader_tier") or "firecrawl"
    if scrape.error:
        source.error = scrape.error
        source.content_provider = provider
        return source

    source.content = scrape.markdown or ""
    source.content_length = len(scrape.markdown or "")
    source.content_provider = provider
    if scrape.title and not source.title:
        source.title = scrape.title
    return source


def build_sources(
    search_results: list[SearchResultItem],
    scraped: list[ScrapeResult],
    search_provider: str = "searxng",
) -> list[Source]:
    """Build full source list with content enrichment.

    Matches scraped content to search results by URL.
    """
    sources: list[Source] = []
    scrape_by_url: dict[str, ScrapeResult] = {}

    for s in scraped:
        # Normalize URL for matching (strip trailing slash)
        key = s.url.rstrip("/").lower()
        scrape_by_url[key] = s

    for i, item in enumerate(search_results):
        source = build_source_from_search(item, i, search_provider)
        key = item.url.rstrip("/").lower()
        if key in scrape_by_url:
            source = enrich_source_with_content(source, scrape_by_url[key])
        sources.append(source)

    return sources


def extract_citations_from_answer(
    answer: str,
    sources: list[Source],
) -> list[Citation]:
    """Extract citations from an answer text.

    Looks for [1], [2], etc. patterns and maps them to source_ids.
    Also extracts sentences with source references.
    """
    citations: list[Citation] = []
    if not answer or not sources:
        return citations

    # Pattern: [1], [1,2], [1-3], [Source 1]
    ref_pattern = re.compile(r"\[(\d+(?:[-,]\s*\d+)*)\]")

    # Split answer into sentences
    sentences = re.split(r"(?<=[.!?])\s+", answer)

    for sentence in sentences:
        matches = ref_pattern.findall(sentence)
        if not matches:
            continue

        source_ids: list[str] = []
        for match in matches:
            # Handle ranges like "1-3" and lists like "1,2"
            parts = re.split(r"[,\s]+", match)
            for part in parts:
                part = part.strip()
                if "-" in part:
                    # Range like "1-3"
                    try:
                        start, end = part.split("-")
                        for n in range(int(start.strip()), int(end.strip()) + 1):
                            idx = n - 1  # 0-indexed
                            if 0 <= idx < len(sources):
                                source_ids.append(sources[idx].source_id)
                    except (ValueError, IndexError):
                        pass
                else:
                    try:
                        idx = int(part) - 1
                        if 0 <= idx < len(sources):
                            source_ids.append(sources[idx].source_id)
                    except (ValueError, IndexError):
                        pass

        if source_ids:
            # Clean up the claim text
            claim = ref_pattern.sub("", sentence).strip()
            if claim:
                citations.append(Citation(claim=claim, source_ids=source_ids))

    return citations


def build_evidence(
    search_results: list[SearchResultItem],
    scraped: list[ScrapeResult],
    answer: str = "",
    search_provider: str = "searxng",
) -> EvidenceResult:
    """Build complete evidence package."""
    sources = build_sources(search_results, scraped, search_provider)
    citations = extract_citations_from_answer(answer, sources)
    return EvidenceResult(sources=sources, citations=citations)
