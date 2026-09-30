"""EvidenceBundle v2 — versioned, immutable evidence contract.

Schema version 2.0: adds plan, conflicts, coverage, citation_whitelist,
answer_constraints, and trace.  All downstream consumers (Hermes,
citation-validator, eval) MUST validate against this schema.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "2.0"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SubQuery:
    id: str
    text: str
    covered: bool = False


@dataclass(frozen=True)
class Plan:
    mode: str
    intent: str
    freshness_class: str
    rounds_executed: int
    subqueries: list[SubQuery] = field(default_factory=list)


@dataclass(frozen=True)
class Passage:
    passage_id: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    char_start: int = 0
    char_end: int = 0
    deep_link: str = ""
    bm25_rank: int = 0
    dense_rank: int = 0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    covers_subqueries: list[str] = field(default_factory=list)
    contains_numbers: list[str] = field(default_factory=list)
    contains_dates: list[str] = field(default_factory=list)
    trust: str = "high"  # high | medium | low


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    canonical_url: str
    domain: str
    title: str
    source_type: str  # official | vendor_docs | technical_blog | aggregator | ...
    authority: float
    authority_reason: str = ""
    published_at: str | None = None
    published_confidence: float = 0.0
    crawled_at: str = ""
    lang: str = "en"
    paywalled: bool = False
    passages: list[Passage] = field(default_factory=list)


@dataclass(frozen=True)
class Conflict:
    conflict_id: str
    claim_key: str
    variants: list[dict[str, Any]] = field(default_factory=list)
    instruction_to_llm: str = ""


@dataclass(frozen=True)
class Coverage:
    ratio: float
    uncovered_subqueries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Sufficiency:
    status: str  # sufficient | insufficient | partial_evidence
    score: float


@dataclass(frozen=True)
class AnswerConstraints:
    must_cite_every_factual_sentence: bool = True
    must_surface_conflicts: list[str] = field(default_factory=list)
    must_state_as_of_date: str = ""
    forbid_external_knowledge: bool = True
    answer_lang: str = "vi"
    max_words: int = 400


@dataclass(frozen=True)
class Trace:
    urls_discovered: int = 0
    urls_fetched: int = 0
    passages_indexed: int = 0
    passages_after_rrf: int = 0
    after_rerank: int = 0
    after_gate: int = 0
    latency_ms: dict[str, float] = field(default_factory=dict)
    degraded_stages: list[str] = field(default_factory=list)


@dataclass
class EvidenceBundle:
    """Immutable evidence package passed from Search-Hub to Hermes.

    All fields are validated at construction time.  The bundle is the
    ONLY contract between Search-Hub and Hermes — Hermes never sees
    raw search results, only this structured evidence.
    """

    query_id: str
    query: dict[str, Any]
    plan: Plan
    sources: list[Source]
    conflicts: list[Conflict] = field(default_factory=list)
    coverage: Coverage = field(default_factory=lambda: Coverage(ratio=0.0))
    sufficiency: Sufficiency = field(
        default_factory=lambda: Sufficiency(status="insufficient", score=0.0)
    )
    citation_whitelist: list[str] = field(default_factory=list)
    answer_constraints: AnswerConstraints = field(default_factory=AnswerConstraints)
    trace: Trace = field(default_factory=Trace)
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        return {
            "schema_version": self.schema_version,
            "query_id": self.query_id,
            "query": self.query,
            "plan": {
                "mode": self.plan.mode,
                "intent": self.plan.intent,
                "freshness_class": self.plan.freshness_class,
                "rounds_executed": self.plan.rounds_executed,
                "subqueries": [
                    {"id": sq.id, "text": sq.text, "covered": sq.covered}
                    for sq in self.plan.subqueries
                ],
            },
            "sources": [
                {
                    "source_id": s.source_id,
                    "url": s.url,
                    "canonical_url": s.canonical_url,
                    "domain": s.domain,
                    "title": s.title,
                    "source_type": s.source_type,
                    "authority": s.authority,
                    "authority_reason": s.authority_reason,
                    "published_at": s.published_at,
                    "published_confidence": s.published_confidence,
                    "crawled_at": s.crawled_at,
                    "lang": s.lang,
                    "paywalled": s.paywalled,
                    "passages": [
                        {
                            "passage_id": p.passage_id,
                            "text": p.text,
                            "heading_path": p.heading_path,
                            "char_start": p.char_start,
                            "char_end": p.char_end,
                            "deep_link": p.deep_link,
                            "bm25_rank": p.bm25_rank,
                            "dense_rank": p.dense_rank,
                            "rrf_score": p.rrf_score,
                            "rerank_score": p.rerank_score,
                            "covers_subqueries": p.covers_subqueries,
                            "contains_numbers": p.contains_numbers,
                            "contains_dates": p.contains_dates,
                            "trust": p.trust,
                        }
                        for p in s.passages
                    ],
                }
                for s in self.sources
            ],
            "conflicts": [
                {
                    "conflict_id": c.conflict_id,
                    "claim_key": c.claim_key,
                    "variants": c.variants,
                    "instruction_to_llm": c.instruction_to_llm,
                }
                for c in self.conflicts
            ],
            "coverage": {
                "ratio": self.coverage.ratio,
                "uncovered_subqueries": self.coverage.uncovered_subqueries,
            },
            "sufficiency": {
                "status": self.sufficiency.status,
                "score": self.sufficiency.score,
            },
            "citation_whitelist": self.citation_whitelist,
            "answer_constraints": {
                "must_cite_every_factual_sentence": self.answer_constraints.must_cite_every_factual_sentence,
                "must_surface_conflicts": self.answer_constraints.must_surface_conflicts,
                "must_state_as_of_date": self.answer_constraints.must_state_as_of_date,
                "forbid_external_knowledge": self.answer_constraints.forbid_external_knowledge,
                "answer_lang": self.answer_constraints.answer_lang,
                "max_words": self.answer_constraints.max_words,
            },
            "trace": {
                "urls_discovered": self.trace.urls_discovered,
                "urls_fetched": self.trace.urls_fetched,
                "passages_indexed": self.trace.passages_indexed,
                "passages_after_rrf": self.trace.passages_after_rrf,
                "after_rerank": self.trace.after_rerank,
                "after_gate": self.trace.after_gate,
                "latency_ms": self.trace.latency_ms,
                "degraded_stages": self.trace.degraded_stages,
            },
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def fingerprint(self) -> str:
        """Stable hash for cache key."""
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]
