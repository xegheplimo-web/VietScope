"""Evidence Extractor — Firecrawl + quote extraction.

Two entry points:

* ``extract_evidence`` — legacy paragraph-based extraction over raw scraped
  pages (kept for callers that do not run the passage reranker).
* ``extract_evidence_from_passages`` — turns passage-reranker chunks into
  ``EvidenceItem`` objects, preserving URL/title/chunk metadata so citations
  stay traceable to the exact passage.
"""

from research_models.research_state import EvidenceItem, SourceResult


async def extract_evidence(
    scraped_content: list, results: list[SourceResult]
) -> list[EvidenceItem]:
    """Extract evidence from scraped content.

    Returns list of EvidenceItem with quotes and support scores.
    """
    evidence = []

    for i, content in enumerate(scraped_content):
        if not content:
            continue

        # Handle both dict and Pydantic objects
        if isinstance(content, dict):
            markdown = content.get("markdown", "")
            url = content.get("url", "")
            title = content.get("title", "")
        else:
            markdown = getattr(content, "markdown", "") or ""
            url = getattr(content, "url", "") or ""
            title = getattr(content, "title", "") or ""

        if not markdown:
            continue

        # Split into chunks (simple paragraph split)
        paragraphs = [p.strip() for p in markdown.split("\n\n") if p.strip()]

        # Take top 3 most relevant paragraphs
        for j, para in enumerate(paragraphs[:3]):
            if len(para) < 50:  # Skip very short paragraphs
                continue

            evidence.append(
                EvidenceItem(
                    source_id=f"src_{i:03d}_{j}",
                    url=url,
                    title=title,
                    quote=para[:500],  # Limit quote length
                    support=0.5,  # Will be updated by verifier
                )
            )

    return evidence


async def extract_evidence_from_passages(
    passages: list[dict],
) -> list[EvidenceItem]:
    """Build evidence items from passage-reranker output.

    Each passage dict carries ``source_url`` / ``title`` / ``chunk_index`` /
    ``chunk_text`` / ``score``.  The passage score seeds ``EvidenceItem.support``
    (the verifier may refine it later), so weakly-relevant passages start with
    a proportionally lower support.
    """
    evidence: list[EvidenceItem] = []
    for i, passage in enumerate(passages):
        text = (passage.get("chunk_text") or "").strip()
        if len(text) < 50:
            continue
        try:
            support = float(passage.get("score", 0.5))
        except (TypeError, ValueError):
            support = 0.5
        evidence.append(
            EvidenceItem(
                source_id=f"psg_{i:03d}_{passage.get('chunk_index', 0)}",
                url=passage.get("source_url", "") or "",
                title=passage.get("title", "") or "",
                quote=text[:800],
                support=max(0.0, min(1.0, support)),
            )
        )
    return evidence
