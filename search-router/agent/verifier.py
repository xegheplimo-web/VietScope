"""Claim Verifier — Kiểm tra claim có nguồn.

LLM tư duy (đánh giá semantic: claim nào được evidence nào hỗ trợ, mức độ
bao nhiêu), code quyết định execution (validate schema, gắn evidence vào
claim, đánh dấu verified). Heuristic word-overlap giữ lại làm fallback.
"""

import json
import re

from core.inference_gateway import ModelRole
from models import VerdictStatus
from pipeline.rag import _SOURCE_HEADER_RE, llm_chat
from research_models.research_state import Claim, EvidenceItem

from agent.query_planner import detect_lang

# Deterministic: a claim is "verified" only if it has >=1 grounded evidence
# with support >= this threshold (LLM-assigned or heuristic).
_SUPPORT_THRESHOLD = 0.3

# Abstention text returned when verification would strip every claim — an
# honest "insufficient evidence" answer beats an unverifiable one.
_ABSTAIN = {
    "vi": "Không tìm thấy đủ bằng chứng đáng tin cậy để trả lời chắc chắn.",
    "en": "Insufficient reliable evidence to answer confidently.",
}


def _abstain_text(answer: str) -> str:
    return _ABSTAIN.get(detect_lang(answer), _ABSTAIN["en"])


def _verdict_status(claim: Claim, score: float) -> VerdictStatus:
    """SPEC-v3 §9 semantics on agent evidence — same vocabulary as
    ``evidence/verifier.py``: the count of *independent* sources (distinct
    URL/source_id) backing the claim decides the verdict, so ``verified``
    means the same thing on /v1/answer and /v1/research.
    """
    n_sources = len({ev.url or ev.source_id for ev in claim.evidence})
    if score >= _SUPPORT_THRESHOLD and n_sources >= 2:
        return VerdictStatus.SUPPORTED
    if score >= _SUPPORT_THRESHOLD and n_sources >= 1:
        return VerdictStatus.PARTIALLY_SUPPORTED
    return VerdictStatus.INSUFFICIENT_EVIDENCE


async def verify_claims(claims: list[Claim], evidence: list[EvidenceItem]) -> list[Claim]:
    """Verify claims against evidence.

    LLM path: asks the model to map each claim to supporting evidence with a
    support score. Deterministic fallback: word-overlap matching.
    """
    if not claims:
        return []

    llm_verified = await _llm_verify_claims(claims, evidence)
    if llm_verified is not None:
        return llm_verified
    return _heuristic_verify_claims(claims, evidence)


async def _llm_verify_claims(
    claims: list[Claim], evidence: list[EvidenceItem]
) -> list[Claim] | None:
    """LLM-driven verification with strict schema validation."""
    if not evidence:
        for c in claims:
            c.evidence = []
            c.verified = False
        return claims

    claim_lines = [f"[C{i}] {c.claim}" for i, c in enumerate(claims)]
    ev_lines = [f"[E{i}] {e.quote[:300]}" for i, e in enumerate(evidence)]

    system_prompt = (
        "You are a claim verifier. For each claim, decide which evidence "
        "items support it and how strongly (0-1). Return ONLY a JSON object:\n"
        '{"verdicts": [{"claim_index": int, "supporting_evidence": [int], '
        '"support_score": number 0-1}]}\n'
        "claim_index / evidence indices are 0-based. No prose."
    )
    user_prompt = "Claims:\n" + "\n".join(claim_lines) + "\n\nEvidence:\n" + "\n".join(ev_lines)

    raw = await llm_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=800,
        json_mode=True,
        role=ModelRole.VERIFIER,
    )
    if not raw:
        return None

    # Schema validation — code quyết định, không tin LLM mù quáng.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        return None

    # Build index → evidence support map.
    support_by_claim: dict[int, tuple[list[int], float]] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            ci = int(v.get("claim_index", -1))
        except (TypeError, ValueError):
            continue
        if ci < 0 or ci >= len(claims):
            continue
        ev_idx = v.get("supporting_evidence")
        if not isinstance(ev_idx, list):
            ev_idx = []
        valid_idx = [i for i in ev_idx if isinstance(i, int) and 0 <= i < len(evidence)]
        try:
            score = float(v.get("support_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        support_by_claim[ci] = (valid_idx, score)

    for i, claim in enumerate(claims):
        claim.evidence = []
        idxs, score = support_by_claim.get(i, ([], 0.0))
        for ei in idxs:
            ev = evidence[ei]
            ev.support = score
            claim.evidence.append(ev)
        claim.status = _verdict_status(claim, score).value
        claim.verified = claim.status != VerdictStatus.INSUFFICIENT_EVIDENCE.value

    return claims


def _heuristic_verify_claims(claims: list[Claim], evidence: list[EvidenceItem]) -> list[Claim]:
    """Deterministic fallback — word-overlap matching."""
    verified_claims = []

    for claim in claims:
        claim.evidence = []
        claim_words = set(claim.claim.lower().split())

        for ev in evidence:
            ev_words = set(ev.quote.lower().split())
            overlap = len(claim_words & ev_words) / max(len(claim_words), 1)
            if overlap > _SUPPORT_THRESHOLD:
                claim.evidence.append(ev)
                ev.support = overlap

        score = max((ev.support for ev in claim.evidence), default=0.0)
        claim.status = _verdict_status(claim, score).value
        claim.verified = claim.status != VerdictStatus.INSUFFICIENT_EVIDENCE.value
        verified_claims.append(claim)

    return verified_claims


async def extract_claims(answer: str) -> list[Claim]:
    """Extract claims from an answer text.

    Simple approach: each sentence is a claim.
    """
    sentences = re.split(r"[.!?]", answer)
    claims = []

    for sent in sentences:
        sent = sent.strip()
        if len(sent) > 20:  # Skip very short fragments
            claims.append(Claim(claim=sent))

    return claims


# Splits an answer into sentences while keeping delimiters attached, so the
# kept parts can be re-joined without reformatting the original text.
_SENT_KEEP_DELIM = re.compile(r"[^.!?]+[.!?]*")


def _normalize_sentence(text: str) -> str:
    """Strip whitespace and trailing sentence delimiters for claim matching."""
    return text.strip().rstrip(".!?").strip()


async def verify_answer(answer: str, evidence: list[EvidenceItem]) -> tuple[str, list[Claim], dict]:
    """Verify a synthesized answer and suppress unsupported claims.

    Claims are extracted from the answer body, verified against ``evidence``
    (LLM path with heuristic fallback via :func:`verify_claims`), and any
    claim marked unverified is removed from the answer text.  A trailing
    ``Sources``/``Nguồn`` section appended by the synthesizer is preserved
    verbatim — its list entries are URLs, not claims.

    If filtering would empty the answer entirely, an honest abstention
    ("insufficient reliable evidence") is returned instead of the original
    unverifiable text, the ``Sources`` tail is preserved, and
    ``stats["all_removed"]`` is set so the outcome stays auditable.

    Returns ``(filtered_answer, claims, stats)``.
    """
    # Split body / sources tail so citation lines are never treated as claims.
    parts = _SOURCE_HEADER_RE.split(answer, maxsplit=1)
    body = parts[0]
    tail = answer[len(body) :] if len(parts) > 1 else ""

    claims = await extract_claims(body)
    stats = {
        "claims_total": len(claims),
        "claims_verified": 0,
        "claims_removed": 0,
        "all_removed": False,
    }
    if not claims:
        return answer, [], stats

    verified = await verify_claims(claims, evidence)
    unsupported = {_normalize_sentence(c.claim) for c in verified if not c.verified}
    stats["claims_verified"] = sum(1 for c in verified if c.verified)
    stats["by_status"] = {
        s.value: sum(1 for c in verified if c.status == s.value)
        for s in (
            VerdictStatus.SUPPORTED,
            VerdictStatus.PARTIALLY_SUPPORTED,
            VerdictStatus.INSUFFICIENT_EVIDENCE,
        )
    }

    if not unsupported:
        return answer, verified, stats

    kept_parts: list[str] = []
    for part in _SENT_KEEP_DELIM.findall(body):
        normalized = _normalize_sentence(part)
        if len(normalized) > 20 and normalized in unsupported:
            stats["claims_removed"] += 1
            continue
        kept_parts.append(part)

    filtered_body = "".join(kept_parts).strip()
    if not filtered_body:
        # Every claim failed verification — abstain rather than serve an
        # answer the evidence could not support. The Sources tail stays so
        # the caller still sees what was found.
        stats["all_removed"] = True
        return _abstain_text(answer) + tail, verified, stats
    filtered = filtered_body + tail
    return filtered, verified, stats
