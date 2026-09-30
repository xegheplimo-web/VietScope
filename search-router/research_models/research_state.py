"""Research Agent — State Machine Models."""

from enum import Enum
from typing import Literal

from models import ScrapeResult
from pydantic import BaseModel, Field


class ResearchState(str, Enum):
    """Research state machine states."""

    START = "START"
    ANALYZE = "ANALYZE"
    PLAN = "PLAN"
    SEARCH = "SEARCH"
    RERANK = "RERANK"
    SCRAPE = "SCRAPE"
    EXTRACT_EVIDENCE = "EXTRACT_EVIDENCE"
    CHECK_GAPS = "CHECK_GAPS"
    REFINE_QUERY = "REFINE_QUERY"
    VERIFY = "VERIFY"
    SYNTHESIZE = "SYNTHESIZE"
    END = "END"


class SearchIntent(BaseModel):
    """Analyzed search intent."""

    needs_web: bool = True
    depth: Literal["none", "quick", "normal", "deep"] = "normal"
    freshness: Literal["any", "day", "week", "month"] = "any"
    categories: list[str] = Field(default_factory=lambda: ["general"])
    preferred_domains: list[str] = Field(default_factory=list)
    official_first: bool = False


class EvidenceItem(BaseModel):
    """Single piece of evidence."""

    source_id: str
    url: str
    title: str
    quote: str
    support: float = 0.0


class Claim(BaseModel):
    """Verified claim.

    ``status`` carries the SPEC-v3 §9 verdict vocabulary shared with
    ``evidence/verifier.py`` (``supported`` | ``partially_supported`` |
    ``insufficient_evidence`` | ...); ``verified`` is True iff status is
    not ``insufficient_evidence``.
    """

    claim: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    verified: bool = False
    status: str = "insufficient_evidence"


class SourceResult(BaseModel):
    """Search result with scoring."""

    source_id: str
    url: str
    title: str
    description: str
    domain: str
    score: float = 0.0
    authority_score: float = 0.0
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    query_match_score: float = 0.0
    corroboration_score: float = 0.0
    content: str = ""
    published_at: str | None = None
    engine: str = ""  # provider that surfaced this result (searxng, ddgs, os, …)


class GapResult(BaseModel):
    """Gap analysis result."""

    known: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    need_more_search: bool = False


class ResearchContext(BaseModel):
    """Full research context/state."""

    query: str
    # Legacy depth names (auto/quick/deep) plus canonical search-mode names
    # (fast/balanced/normal); all resolve via pipeline.search_modes aliases.
    mode: Literal["auto", "quick", "deep", "fast", "balanced", "normal", "research"] = "auto"
    state: ResearchState = ResearchState.START
    intent: SearchIntent | None = None
    search_plan: list[str] = Field(default_factory=list)
    search_results: list[SourceResult] = Field(default_factory=list)
    scraped_content: list[ScrapeResult] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    answer: str | None = None
    confidence: float = 0.0
    search_round: int = 0
    max_rounds: int = 3
    queries_used: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
