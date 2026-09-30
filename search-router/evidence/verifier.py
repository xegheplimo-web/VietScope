"""Claim verification engine.

Deterministic verdict assignment per SPEC-v3 §9:

- >= 2 independent sources corroborate -> SUPPORTED
- 1 independent source -> PARTIALLY_SUPPORTED
- independent contradiction (and no support) -> CONTRADICTED
- independent contradiction AND support -> SOURCE_CONFLICT
- supporting source(s) exist but are all stale -> OUTDATED
- otherwise -> INSUFFICIENT_EVIDENCE

Independence is read from ``EvidenceCluster.is_independent``; raw URL counts are
never used. Text matching is keyword-overlap based, with an optional async
embedding path available through ``semantic_supports``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from models import ClaimVerification, EvidenceCluster, Source, VerdictStatus

from evidence.claims import keywords, tokenize

logger = logging.getLogger(__name__)

NEGATION_TOKENS: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "without",
        "deny",
        "denied",
        "denies",
        "refute",
        "refuted",
        "refutes",
        "false",
        "incorrect",
        "wrong",
        "misleading",
        "fake",
        "untrue",
        "disproven",
        "disproved",
        "contradicts",
        "không",
        "chưa",
        "chẳng",
        "sai",
        "không phải",
        "không có",
        "bác bỏ",
    }
)

_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)


def _claim_id(claim) -> str:
    if isinstance(claim, dict):
        return claim.get("claim_id") or claim.get("id") or ""
    for attr in ("claim_id", "id"):
        if hasattr(claim, attr):
            val = getattr(claim, attr)
            if val:
                return str(val)
    return ""


def _claim_text(claim) -> str:
    if isinstance(claim, dict):
        return str(claim.get("text") or claim.get("claim_text") or "")
    for attr in ("text", "claim_text"):
        if hasattr(claim, attr):
            val = getattr(claim, attr)
            if val:
                return str(val)
    return str(claim)


def _source_text(source) -> str:
    if isinstance(source, Source):
        parts = [source.title, source.description, source.content]
    elif isinstance(source, dict):
        parts = [
            source.get("title", ""),
            source.get("description", ""),
            source.get("content", ""),
        ]
    else:
        parts = [
            getattr(source, "title", ""),
            getattr(source, "description", ""),
            getattr(source, "content", ""),
        ]
    return " ".join(str(p) for p in parts if p)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        pass
    text = text.split(".")[0].replace("Z", "").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    return None


def _overlap(claim_tokens: Iterable[str], source_tokens: Iterable[str]) -> float:
    ct = set(claim_tokens)
    if not ct:
        return 0.0
    st = set(source_tokens)
    return len(ct & st) / len(ct)


def supports(
    claim_text: str,
    source_text: str,
    threshold: float = 0.4,
) -> bool:
    """Return True when ``source_text`` supports ``claim_text`` by keyword overlap."""
    if not claim_text or not source_text:
        return False
    return _overlap(keywords(claim_text), tokenize(source_text)) >= threshold


def contradicts(
    claim_text: str,
    source_text: str,
    threshold: float = 0.4,
) -> bool:
    """Return True when ``source_text`` negates the claim.

    Deterministic heuristic: sufficient keyword overlap plus a negation marker
    in close proximity to a matched keyword.
    """
    if not claim_text or not source_text:
        return False
    ck = set(keywords(claim_text))
    if not ck:
        return False
    src_tokens = tokenize(source_text)
    matched = ck & set(src_tokens)
    if len(matched) / len(ck) < threshold:
        return False

    for i, tok in enumerate(src_tokens):
        if tok not in matched:
            continue
        window = src_tokens[max(0, i - 5) : i + 6]
        if any(n in NEGATION_TOKENS for n in window):
            return True
    return False


async def semantic_supports(
    claim_text: str,
    source_text: str,
    inference,
    threshold: float = 0.4,
) -> bool:
    """Keyword support, upgraded to embedding cosine when ``inference.embed`` works."""
    if inference is None:
        return supports(claim_text, source_text, threshold)
    try:
        vectors = await inference.embed([claim_text, source_text])
    except Exception:
        vectors = None
    if not vectors or len(vectors) < 2 or vectors[0] is None or vectors[1] is None:
        return supports(claim_text, source_text, threshold)

    import math

    a, b = vectors[0], vectors[1]
    if not hasattr(a, "__iter__") or not hasattr(b, "__iter__"):
        return supports(claim_text, source_text, threshold)
    a = list(a)
    b = list(b)
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return supports(claim_text, source_text, threshold)
    return (dot / (na * nb)) >= threshold


def _normalize_sources(sources):
    """Return (source_id -> text, source_id -> published_at)."""
    text_map: dict[str, str] = {}
    date_map: dict[str, str | None] = {}
    if sources is None:
        return text_map, date_map
    if isinstance(sources, dict):
        for sid, text in sources.items():
            text_map[str(sid)] = str(text)
        return text_map, date_map

    for source in sources:
        if isinstance(source, Source):
            sid = source.source_id
            text_map[sid] = _source_text(source)
            date_map[sid] = source.published_at
        elif isinstance(source, dict):
            sid = str(source.get("source_id") or "")
            text_map[sid] = _source_text(source)
            date_map[sid] = source.get("published_at")
        else:
            sid = str(getattr(source, "source_id", ""))
            text_map[sid] = _source_text(source)
            date_map[sid] = getattr(source, "published_at", None)
    return text_map, date_map


def _cluster_evidence_ids(clusters: list[EvidenceCluster]) -> list[str]:
    ids: list[str] = []
    for cluster in clusters:
        ids.extend(cluster.sources)
    return ids


def _best_overlap(claim_text: str, cluster: EvidenceCluster, text_map: dict[str, str]) -> float:
    best = 0.0
    for sid in cluster.sources:
        text = text_map.get(sid, "")
        if not text:
            continue
        best = max(best, _overlap(keywords(claim_text), tokenize(text)))
    return best


def verify_claims(
    claims,
    clusters,
    sources=None,
    inference=None,
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> list[ClaimVerification]:
    """Verify a list of claims against evidence clusters.

    ``clusters`` should be a list of :class:`EvidenceCluster`. ``sources`` is
    optional and supplies document text/date content; when omitted, only
    cluster independence is used for verdict assignment.
    """
    text_map, date_map = _normalize_sources(sources)
    now = now or datetime.now(UTC)
    results: list[ClaimVerification] = []

    for idx, claim in enumerate(claims):
        cid = _claim_id(claim) or f"c{idx + 1}"
        ctext = _claim_text(claim)

        supporting: list[EvidenceCluster] = []
        contradicting: list[EvidenceCluster] = []

        for cluster in clusters:
            cluster_supports = False
            cluster_contradicts = False
            for sid in cluster.sources:
                text = text_map.get(sid, "")
                if not text:
                    continue
                if contradicts(ctext, text):
                    cluster_contradicts = True
                elif supports(ctext, text):
                    cluster_supports = True
            if cluster_supports:
                supporting.append(cluster)
            if cluster_contradicts:
                contradicting.append(cluster)

        indep_support = [c for c in supporting if c.is_independent]
        indep_contra = [c for c in contradicting if c.is_independent]
        support_count = len(indep_support)
        contra_count = len(indep_contra)

        fresh_support_count = 0
        dated_support_count = 0
        if max_age_days is not None and support_count > 0:
            for cluster in indep_support:
                cluster_fresh = False
                cluster_dated = False
                for sid in cluster.sources:
                    dt = _parse_datetime(date_map.get(sid))
                    if dt is None:
                        continue
                    cluster_dated = True
                    if (now - dt).days <= max_age_days:
                        cluster_fresh = True
                        break
                if cluster_fresh:
                    fresh_support_count += 1
                if cluster_dated:
                    dated_support_count += 1

        status = VerdictStatus.INSUFFICIENT_EVIDENCE
        if contra_count > 0:
            status = (
                VerdictStatus.SOURCE_CONFLICT if support_count > 0 else VerdictStatus.CONTRADICTED
            )
        elif (
            support_count > 0
            and max_age_days is not None
            and fresh_support_count == 0
            and dated_support_count > 0
        ):
            status = VerdictStatus.OUTDATED
        elif support_count >= 2:
            status = VerdictStatus.SUPPORTED
        elif support_count == 1:
            status = VerdictStatus.PARTIALLY_SUPPORTED

        confidence = _confidence(status)
        evidence_ids = _cluster_evidence_ids(indep_support)
        contradictions = [
            f"source {sid} contradicts this claim"
            for cluster in indep_contra
            for sid in cluster.sources
        ]

        verdict = _verdict_text(status, support_count, contra_count)

        results.append(
            ClaimVerification(
                claim_id=cid,
                claim_text=ctext,
                status=status.value,
                evidence=evidence_ids,
                verdict=verdict,
                confidence=round(confidence, 2),
                contradictions=contradictions,
                sources_conflict=(status == VerdictStatus.SOURCE_CONFLICT),
            )
        )

    return results


_CONFIDENCE_BY_STATUS = {
    VerdictStatus.SUPPORTED: 0.9,
    VerdictStatus.PARTIALLY_SUPPORTED: 0.55,
    VerdictStatus.CONTRADICTED: 0.2,
    VerdictStatus.SOURCE_CONFLICT: 0.3,
    VerdictStatus.OUTDATED: 0.35,
    VerdictStatus.INSUFFICIENT_EVIDENCE: 0.1,
}


def _confidence(status: VerdictStatus) -> float:
    return _CONFIDENCE_BY_STATUS.get(status, 0.1)


def _verdict_text(status: VerdictStatus, support_count: int, contra_count: int) -> str:
    if status == VerdictStatus.SUPPORTED:
        return f"Supported by {support_count} independent sources."
    if status == VerdictStatus.PARTIALLY_SUPPORTED:
        return "Partially supported by 1 independent source."
    if status == VerdictStatus.CONTRADICTED:
        return "Contradicted by independent evidence."
    if status == VerdictStatus.SOURCE_CONFLICT:
        return f"Source conflict: {support_count} supporting and {contra_count} contradicting independent sources."
    if status == VerdictStatus.OUTDATED:
        return "Supporting sources are outdated relative to freshness requirements."
    return "Insufficient evidence to verify this claim."


def _cluster_snippets(
    clusters: list[EvidenceCluster],
    text_map: dict[str, str],
) -> list[dict]:
    """Build a short snippet list for the LLM prompt."""
    snippets = []
    for cluster in clusters:
        for sid in cluster.sources:
            text = text_map.get(sid, "")
            if not text:
                continue
            snippets.append(
                {
                    "source_id": sid,
                    "independent": cluster.is_independent,
                    "text": text[:500],
                }
            )
    return snippets[:20]


def _normalize_status(raw) -> VerdictStatus:
    raw = str(raw or "").lower().replace(" ", "_").replace("-", "_")
    for status in VerdictStatus:
        if raw == status.value or raw == status.name.lower():
            return status
    return VerdictStatus.INSUFFICIENT_EVIDENCE


async def verify_claims_with_llm(
    claims,
    clusters,
    llm,
    sources=None,
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> list[ClaimVerification]:
    """Verify claims using an LLM when available; otherwise fall back to deterministic.

    This is an **opt-in** path and does not change the default behavior of
    :func:`verify_claims`. When ``llm`` is unconfigured or the LLM call fails,
    it delegates to the deterministic verifier.
    """
    if llm is None or not getattr(llm, "api_key", None):
        return verify_claims(claims, clusters, sources=sources, max_age_days=max_age_days, now=now)

    text_map, date_map = _normalize_sources(sources)
    snippets = _cluster_snippets(clusters, text_map)
    if not snippets:
        return verify_claims(claims, clusters, sources=sources, max_age_days=max_age_days, now=now)

    claim_texts = [
        {
            "claim_id": _claim_id(claim) or f"c{i + 1}",
            "text": _claim_text(claim),
        }
        for i, claim in enumerate(claims)
    ]

    prompt = (
        "You are a careful evidence verifier. For each claim below, evaluate it "
        "against the provided evidence snippets and return ONLY a JSON object with "
        "a 'verdicts' array. Each verdict must include: "
        "claim_id (string), status (one of SUPPORTED, PARTIALLY_SUPPORTED, "
        "CONTRADICTED, SOURCE_CONFLICT, OUTDATED, INSUFFICIENT_EVIDENCE), "
        "verdict (short explanation string), confidence (0.0-1.0 number), "
        "evidence (list of source_ids supporting it), contradictions (list of source_ids contradicting it).\n\n"
        f"Claims:\n{json.dumps(claim_texts, ensure_ascii=False, indent=2)}\n\n"
        f"Evidence snippets:\n{json.dumps(snippets, ensure_ascii=False, indent=2)}"
    )

    try:
        data = await llm.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Return ONLY valid JSON. Do not explain. "
                        "Use the exact status values requested."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            schema_hint='{"verdicts":[{"claim_id":"...","status":"SUPPORTED","verdict":"...","confidence":0.9,"evidence":["s1"],"contradictions":[]}]}',
            max_tokens=1500,
            temperature=0.2,
        )
    except Exception as exc:
        logger.warning("LLM claim verification failed: %s", exc)
        data = None

    if not isinstance(data, dict):
        return verify_claims(claims, clusters, sources=sources, max_age_days=max_age_days, now=now)

    verdicts = data.get("verdicts") or data.get("results") or data.get("verdict")
    if not isinstance(verdicts, list):
        return verify_claims(claims, clusters, sources=sources, max_age_days=max_age_days, now=now)

    verdict_map = {v.get("claim_id"): v for v in verdicts if isinstance(v, dict)}
    results: list[ClaimVerification] = []
    now = now or datetime.now(UTC)
    for idx, claim in enumerate(claims):
        cid = _claim_id(claim) or f"c{idx + 1}"
        ctext = _claim_text(claim)
        v = verdict_map.get(cid) or {}
        status = _normalize_status(v.get("status"))
        evidence = v.get("evidence") or []
        contradictions = v.get("contradictions") or []
        if not isinstance(evidence, list):
            evidence = []
        if not isinstance(contradictions, list):
            contradictions = []
        try:
            confidence = max(0.0, min(1.0, float(v.get("confidence", _confidence(status)))))
        except (ValueError, TypeError):
            confidence = _confidence(status)

        results.append(
            ClaimVerification(
                claim_id=cid,
                claim_text=ctext,
                status=status.value,
                evidence=evidence,
                verdict=v.get("verdict")
                or _verdict_text(status, len(evidence), len(contradictions)),
                confidence=round(confidence, 2),
                contradictions=[f"source {c} contradicts this claim" for c in contradictions],
                sources_conflict=(status == VerdictStatus.SOURCE_CONFLICT),
            )
        )
    return results
