"""Synthesizer — Tổng hợp câu trả lời cuối.

Reuse ``pipeline.rag.synthesize_research_answer`` (LLM synthesis + citation
validation + Sources section) thay vì tự viết LLM client. Confidence do code
tính từ tỷ lệ claim đã verify — LLM không tự chấm confidence.
"""

from urllib.parse import urlparse

from models import Source
from pipeline.rag import stream_research_answer, synthesize_research_answer
from research_models.research_state import Claim, EvidenceItem


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except (ValueError, TypeError, AttributeError):
        return ""


def _evidence_to_sources(evidence: list[EvidenceItem]) -> list[Source]:
    """Deduplicate evidence by URL → Source list for RAG synthesis."""
    seen: set[str] = set()
    sources: list[Source] = []
    for ev in evidence:
        if not ev.url or ev.url in seen:
            continue
        seen.add(ev.url)
        sources.append(
            Source(
                source_id=ev.source_id,
                url=ev.url,
                title=ev.title or "",
                domain=_domain_from_url(ev.url),
                description=ev.quote[:300] if ev.quote else "",
                content=ev.quote,
                score=ev.support,
                content_provider="reader",
            )
        )
    return sources


async def synthesize_answer(
    query: str,
    claims: list[Claim],
    evidence: list[EvidenceItem],
    *,
    on_delta=None,
) -> tuple[str, float]:
    """Synthesize final answer from verified claims and evidence.

    Returns (answer_text, confidence). Confidence is deterministic:
    ratio of verified claims, boosted by average evidence support.

    ``on_delta`` (optional async callable) receives each LLM token as it
    is produced — used by the SSE lane for real answer streaming.
    """
    if not claims and not evidence:
        msg = "Không tìm thấy thông tin đủ để trả lời câu hỏi."
        if on_delta is not None:
            await on_delta(msg)
        return msg, 0.0

    sources = _evidence_to_sources(evidence)

    # Reuse the mature RAG synthesis (LLM + citation validation + fallback).
    if on_delta is not None:
        answer = await stream_research_answer(query, sources, on_delta=on_delta)
    else:
        answer = await synthesize_research_answer(query, sources)

    # Deterministic confidence — code quyết định, không LLM.
    if claims:
        verified_count = sum(1 for c in claims if c.verified)
        base_conf = verified_count / len(claims)
    else:
        base_conf = 0.5 if evidence else 0.0

    if evidence:
        avg_support = sum(e.support for e in evidence) / len(evidence)
        confidence = min(base_conf * (1 + avg_support), 1.0)
    else:
        confidence = base_conf

    return answer, round(confidence, 3)
