"""Gap Analyzer — Phân tích thiếu thông tin.

LLM tư duy (đánh giá semantic: facts đã có / còn thiếu / mâu thuẫn /
truy vấn đề xuất + confidence), code quyết định execution (validate schema,
áp policy: confidence < threshold HOẶC missing_facts != [] → search lại).
Heuristic giữ lại làm fallback.
"""

import json

from core.inference_gateway import ModelRole
from pipeline.rag import llm_chat
from research_models.research_state import EvidenceItem, GapResult

# Deterministic policy thresholds — LLM không tự quyết việc loop.
_CONFIDENCE_THRESHOLD = 0.7
_MIN_EVIDENCE_FOR_CONFIDENT = 3


async def analyze_gaps(evidence: list[EvidenceItem], query: str) -> GapResult:
    """Analyze what information is missing.

    LLM path returns structured facts_supported / missing_facts /
    conflicting_facts / proposed_queries / confidence. Heuristic fallback
    below when LLM is unavailable.
    """
    if not evidence:
        return GapResult(
            known=[],
            missing=[query],
            confidence=0.0,
            need_more_search=True,
        )

    llm_result = await _llm_analyze_gaps(query, evidence)
    if llm_result is not None:
        return llm_result
    return _heuristic_analyze_gaps(evidence, query)


async def _llm_analyze_gaps(query: str, evidence: list[EvidenceItem]) -> GapResult | None:
    """LLM-driven gap analysis with strict schema validation + policy."""
    # Compact evidence context (cap tokens).
    ev_lines = []
    for i, e in enumerate(evidence[:12], 1):
        quote = (e.quote or "").strip().replace("\n", " ")[:400]
        ev_lines.append(f"[E{i}] ({e.url}): {quote}")
    ev_block = "\n".join(ev_lines)

    system_prompt = (
        "You are a research gap analyzer. Given a question and the evidence "
        "gathered so far, decide what is known, what is missing, what "
        "conflicts, and what to search next. Return ONLY a JSON object:\n"
        '{"facts_supported": [str], "missing_facts": [str], '
        '"conflicting_facts": [str], "proposed_queries": [str], '
        '"confidence": number 0-1}\n'
        "No prose. confidence is your confidence the question is fully "
        "answerable from the evidence (0=none, 1=complete)."
    )
    user_prompt = f"Question: {query}\n\nEvidence:\n{ev_block}"

    raw = await llm_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=800,
        json_mode=True,
        role=ModelRole.PLANNER,
    )
    if not raw:
        return None

    # Schema validation — code quyết định, không tin LLM mù quáng.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def _str_list(key: str) -> list[str]:
        val = data.get(key)
        if not isinstance(val, list):
            return []
        return [str(x).strip() for x in val if isinstance(x, (str, int, float)) and str(x).strip()]

    facts_supported = _str_list("facts_supported")[:8]
    missing_facts = _str_list("missing_facts")[:8]
    conflicting_facts = _str_list("conflicting_facts")[:8]
    proposed_queries = _str_list("proposed_queries")[:8]

    # Confidence: validate range, default low if absent/invalid.
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # Deterministic policy — LLM không tự quyết việc search lại.
    need_more = bool(missing_facts) or bool(conflicting_facts) or confidence < _CONFIDENCE_THRESHOLD

    known = facts_supported
    # proposed_queries feed the follow-up loop — they're the LLM's suggested
    # searches for the missing facts, so they ride along as gaps.
    missing = missing_facts + conflicting_facts + proposed_queries

    return GapResult(
        known=known,
        missing=missing,
        confidence=confidence,
        need_more_search=need_more,
    )


def _heuristic_analyze_gaps(evidence: list[EvidenceItem], query: str) -> GapResult:
    """Heuristic fallback — keyword coverage + evidence count."""
    # Extract known topics from evidence
    known = []
    for item in evidence:
        words = item.quote.split()[:10]
        if words:
            known.append(" ".join(words))

    # Simple heuristic: if we have less than 3 evidence pieces, we need more
    need_more = len(evidence) < _MIN_EVIDENCE_FOR_CONFIDENT

    # Calculate confidence based on evidence count and support scores
    if evidence:
        avg_support = sum(e.support for e in evidence) / len(evidence)
        confidence = min((len(evidence) / 5.0) * avg_support, 1.0)
    else:
        confidence = 0.0

    # Determine missing topics (simplified)
    missing = []
    query_words = query.lower().split()
    for word in query_words:
        if len(word) > 3:  # Skip short words
            word_found = any(word in e.quote.lower() for e in evidence)
            if not word_found:
                missing.append(word)

    missing = missing[:5]

    return GapResult(
        known=known[:5],
        missing=missing,
        confidence=confidence,
        need_more_search=need_more or confidence < _CONFIDENCE_THRESHOLD,
    )
