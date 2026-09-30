"""Passage-level citation per SPEC-v3 §10.

For each claim, locate the source passage that best contains the claim
keywords, record the character offsets of the quote, and emit a
``CitationV2`` with ``source_id``, ``passage_id``, ``url``, ``quote_start``,
``quote_end`` and ``retrieved_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from models import CitationV2, Passage, Source

from evidence.claims import keywords

_CONTEXT_BEFORE = 150
_CONTEXT_AFTER = 300


def _claim_id(claim) -> str:
    if isinstance(claim, dict):
        return claim.get("claim_id") or claim.get("id") or ""
    for attr in ("claim_id", "id"):
        if hasattr(claim, attr) and getattr(claim, attr):
            return str(getattr(claim, attr))
    return ""


def _claim_text(claim) -> str:
    if isinstance(claim, dict):
        return str(claim.get("text") or claim.get("claim_text") or claim.get("claim") or "")
    for attr in ("text", "claim_text", "claim"):
        if hasattr(claim, attr) and getattr(claim, attr):
            return str(getattr(claim, attr))
    return str(claim)


def _claim_evidence_ids(claim) -> list[str]:
    if isinstance(claim, dict):
        ev = claim.get("evidence") or []
    elif hasattr(claim, "evidence"):
        ev = claim.evidence or []
    else:
        ev = []
    if isinstance(ev, str):
        return [ev]
    ids: list[str] = []
    for e in ev:
        if not e:
            continue
        if isinstance(e, str):
            ids.append(e)
        elif isinstance(e, dict):
            sid = e.get("source_id") or e.get("id")
            if sid:
                ids.append(str(sid))
        elif hasattr(e, "source_id"):
            # EvidenceItem-style objects carry their own source_id.
            ids.append(str(e.source_id))
        else:
            ids.append(str(e))
    return ids


def _source_text(source) -> str:
    if isinstance(source, Source):
        return source.content or source.description or source.title or ""
    if isinstance(source, dict):
        return str(source.get("content") or source.get("description") or source.get("title") or "")
    return str(
        getattr(source, "content", "")
        or getattr(source, "description", "")
        or getattr(source, "title", "")
        or ""
    )


def _source_attrs(source) -> dict:
    if isinstance(source, Source):
        return {
            "source_id": source.source_id,
            "url": source.url,
            "domain": source.domain,
            "title": source.title,
            "retrieved_at": source.retrieved_at,
        }
    if isinstance(source, dict):
        return {
            "source_id": str(source.get("source_id") or ""),
            "url": str(source.get("url") or ""),
            "domain": str(source.get("domain") or ""),
            "title": str(source.get("title") or ""),
            "retrieved_at": str(source.get("retrieved_at") or _now_iso()),
        }
    return {
        "source_id": str(getattr(source, "source_id", "")),
        "url": str(getattr(source, "url", "")),
        "domain": str(getattr(source, "domain", "")),
        "title": str(getattr(source, "title", "")),
        "retrieved_at": str(getattr(source, "retrieved_at", "") or _now_iso()),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _find_match(content: str, claim_keywords: list[str]):
    """Return (match_start, match_end) for the earliest keyword occurrence."""
    lower = content.lower()
    best: tuple[int, int] | None = None
    # Prefer longer keywords to reduce false positives.
    for kw in sorted(claim_keywords, key=len, reverse=True):
        if len(kw) < 3:
            continue
        pos = lower.find(kw.lower())
        if pos < 0:
            continue
        end = pos + len(kw)
        if best is None or pos < best[0]:
            best = (pos, end)
    return best


def _extract_quote(content: str, match: tuple[int, int] | None) -> tuple[int, int]:
    if match is None:
        # No keyword found — fall back to the document head.
        return 0, min(len(content), _CONTEXT_AFTER)
    start, end = match
    quote_start = max(0, start - _CONTEXT_BEFORE)
    quote_end = min(len(content), end + _CONTEXT_AFTER)
    return quote_start, quote_end


def build_passage_citations(
    claims,
    sources,
    max_evidence_per_claim: int = 3,
) -> list[CitationV2]:
    """Build passage-level citations for each claim.

    ``claims`` may be :class:`Claim` (from extraction) or
    :class:`ClaimVerification` (from verification, whose ``evidence`` source
    ids are used as candidates).
    """
    source_list = list(sources or [])
    source_by_id: dict[str, object] = {}
    for source in source_list:
        sid = _source_attrs(source)["source_id"]
        if sid:
            source_by_id[sid] = source

    citations: list[CitationV2] = []
    passage_counter = 0

    for claim in claims:
        cid = _claim_id(claim)
        ctext = _claim_text(claim)
        if not ctext:
            continue

        kws = keywords(ctext)
        evidence_ids = _claim_evidence_ids(claim)

        candidates: list[object] = []
        if evidence_ids:
            for sid in evidence_ids:
                src = source_by_id.get(sid)
                if src is not None:
                    candidates.append(src)
        if not candidates:
            # Fall back to keyword search over all sources.
            for source in source_list:
                text = _source_text(source)
                if not text:
                    continue
                if any(kw in text.lower() for kw in kws if len(kw) >= 3):
                    candidates.append(source)

        evidence: list[dict] = []
        for source in candidates[:max_evidence_per_claim]:
            attrs = _source_attrs(source)
            content = _source_text(source)
            if not content:
                continue

            match = _find_match(content, kws)
            quote_start, quote_end = _extract_quote(content, match)
            passage_id = f"p{passage_counter}"
            passage_counter += 1
            quote = content[quote_start:quote_end]

            evidence.append(
                {
                    "source_id": attrs["source_id"],
                    "passage_id": passage_id,
                    "url": attrs["url"],
                    "quote_start": quote_start,
                    "quote_end": quote_end,
                    "retrieved_at": attrs["retrieved_at"],
                    "quote": quote,
                }
            )

        if evidence:
            first = evidence[0]
            source_label = first.get("url") or first.get("source_id")
            citations.append(
                CitationV2(
                    claim_id=cid,
                    evidence=evidence,
                    citation_text=f"{cid}: {source_label} [{first.get('quote_start')}:{first.get('quote_end')}]",
                )
            )

    return citations


def build_passages(
    source: Source,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
) -> list[Passage]:
    """Split a source's content into offset-tracked passages for ``/v1/read``."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must satisfy 0 <= overlap < chunk_size")
    content = _source_text(source)
    if not content:
        return []

    attrs = _source_attrs(source)
    passages: list[Passage] = []
    idx = 0
    i = 0
    while i < len(content):
        end = min(len(content), i + chunk_size)
        passages.append(
            Passage(
                passage_id=f"{attrs['source_id']}:p{idx}",
                source_id=attrs["source_id"],
                text=content[i:end],
                quote_start=i,
                quote_end=end,
                retrieved_at=attrs["retrieved_at"],
                metadata={"url": attrs["url"], "title": attrs["title"]},
            )
        )
        idx += 1
        if end >= len(content):
            break
        i = end - chunk_overlap

    return passages
