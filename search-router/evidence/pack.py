"""Evidence pack assembly.

Builds the v2 ``EvidencePack`` (answer + verified claims + passage citations +
sources + coverage/confidence + budget) from orchestrator output.
"""

from __future__ import annotations

import hashlib
import re

from models import (
    EvidenceCluster,
    EvidencePack,
    SearchBudget,
    Source,
    VerdictStatus,
)

from evidence.citation import build_passage_citations
from evidence.claims import Claim
from evidence.verifier import verify_claims


def _source_text(source: Source) -> str:
    return " ".join(str(p) for p in (source.title, source.description, source.content) if p)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _fingerprint(source: Source) -> str:
    # Content is the strongest near-duplicate signal; fall back to metadata
    # only when the body is empty (syndicated copies often change the title).
    body = source.content or f"{source.title} {source.description}".strip()
    if not body:
        body = source.url or source.source_id
    return hashlib.sha256(_normalize(body)[:2000].encode("utf-8")).hexdigest()


_SHINGLE_N = 3  # 3-gram word windows (calibrated for short syndicated text)
_SHINGLE_LIMIT = 600  # cap shingles per source (performance guard)
_CONTAINMENT_THRESHOLD = 0.25


def _shingles(text: str) -> set[int]:
    """N-gram word-window shingles → set of 32-bit hashes (research note)."""
    words = _normalize(text).split()
    if len(words) < _SHINGLE_N:
        # Short text: use the whole normalized text as one shingle
        digest = hashlib.sha1(" ".join(words).encode("utf-8"), usedforsecurity=False).digest()
        return {int.from_bytes(digest[:4], "big")}
    shingles: set[int] = set()
    for i in range(len(words) - _SHINGLE_N + 1):
        window = " ".join(words[i : i + _SHINGLE_N])
        digest = hashlib.sha1(window.encode("utf-8"), usedforsecurity=False).digest()
        shingles.add(int.from_bytes(digest[:4], "big"))
        if len(shingles) >= _SHINGLE_LIMIT:
            break
    return shingles


def _containment(a: set[int], b: set[int]) -> float:
    """|A∩B| / min(|A|,|B|) — copy detection metric (bài copy chứa phần lớn gốc)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def build_clusters(sources: list[Source]) -> list[EvidenceCluster]:
    """Group near-duplicate sources into independent evidence clusters.

    Two-pass (SPEC-v3 §8, review-codex #5):
      pass 1 — exact normalized SHA-256 fingerprint (fast path)
      pass 2 — shingle/Jaccard near-duplicate merge (syndicated copies
               that changed a few words) with per-source cluster score.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    # Cluster → shingle set + best score, for near-duplicate pass
    cluster_shingles: dict[str, set[int]] = {}
    cluster_best_score: dict[str, float] = {}

    for idx, source in enumerate(sources or []):
        source_id = source.source_id or f"s{idx}"
        body = source.content or f"{source.title} {source.description}".strip()
        if not body:
            body = source.url or source.source_id

        fp = _fingerprint(source)
        if fp in groups:
            if source_id not in groups[fp]["sources"]:
                groups[fp]["sources"].append(source_id)
            continue

        # Near-duplicate pass: compare shingles against existing clusters
        sh = _shingles(body)
        merged = False
        best_cid, best_j = None, 0.0
        for cid, csh in cluster_shingles.items():
            j = _containment(sh, csh)
            if j > best_j:
                best_cid, best_j = cid, j
        if best_cid is not None and best_j >= _CONTAINMENT_THRESHOLD:
            groups[best_cid]["sources"].append(source_id)
            # Keep best observed similarity for the cluster
            cluster_best_score[best_cid] = max(cluster_best_score.get(best_cid, 0.0), best_j)
            merged = True

        if not merged:
            cid = f"clu{len(groups) + 1}"
            groups[cid] = {"sources": [source_id], "cluster_id": cid}
            order.append(cid)
            cluster_shingles[cid] = sh
            cluster_best_score[cid] = 1.0

    clusters: list[EvidenceCluster] = []
    for cid in order:
        g = groups[cid]
        clusters.append(
            EvidenceCluster(
                cluster_id=g["cluster_id"],
                sources=g["sources"],
                is_independent=True,
                deduplication_method="shingle_jaccard"
                if cluster_best_score.get(cid, 1.0) < 1.0
                else "content_fingerprint",
            )
        )
    return clusters


_BUDGET_PRESETS = {
    "fast": (2, 3, 0),
    "normal": (5, 8, 1),
    "deep": (12, 20, 3),
}


def budget_for_mode(mode: str) -> SearchBudget:
    """Return a v2 :class:`SearchBudget` for the given mode."""
    queries, fetches, followups = _BUDGET_PRESETS.get(str(mode).lower(), _BUDGET_PRESETS["normal"])
    return SearchBudget(
        max_queries=queries,
        max_results=10,
        max_fetches=fetches,
        max_tokens=None,
        max_duration=None,
        max_followups=followups,
        max_cost=None,
        cost_used=0.0,
    )


def coverage(verifications) -> float:
    if not verifications:
        return 0.0
    sufficient = sum(
        1 for v in verifications if v.status != VerdictStatus.INSUFFICIENT_EVIDENCE.value
    )
    return round(sufficient / len(verifications), 2)


def confidence(verifications) -> float:
    if not verifications:
        return 0.0
    return round(sum(v.confidence for v in verifications) / len(verifications), 2)


def build_evidence_pack(
    answer: str,
    claims: list[Claim],
    sources: list[Source],
    clusters: list[EvidenceCluster] | None = None,
    budget: SearchBudget | None = None,
    inference=None,
    max_age_days: int | None = None,
) -> EvidencePack:
    """Assemble a v2 ``EvidencePack`` from an answer and retrieved sources."""
    source_list = list(sources or [])
    clusters = clusters if clusters is not None else build_clusters(source_list)

    verifications = verify_claims(
        claims,
        clusters,
        sources=source_list,
        inference=inference,
        max_age_days=max_age_days,
    )
    citations = build_passage_citations(verifications, source_list)
    budget_used = budget if budget is not None else budget_for_mode("normal")

    return EvidencePack(
        answer=answer,
        claims=verifications,
        citations=citations,
        sources=source_list,
        coverage=coverage(verifications),
        confidence=confidence(verifications),
        budget_used=budget_used,
    )
