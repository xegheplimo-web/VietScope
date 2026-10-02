import hashlib
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_text(text: str) -> str:
    """Collapse whitespace and lower-case for stable hashing."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.strip().lower()).strip()


def _snippet_hash(snippet: str) -> str:
    """Stable 64-hex SHA-256 of a normalized snippet."""
    return hashlib.sha256(_normalize_text(snippet).encode("utf-8")).hexdigest()


def result_fingerprint(canonical_url: str, title: str, snippet: str) -> str:
    """Return a 12-hex SHA-256 fingerprint of a normalized result.

    The input is (canonical_url + normalized title + snippet-hash) so the
    fingerprint is deterministic and stable against minor whitespace changes.
    """
    parts = [
        canonical_url or "",
        _normalize_text(title),
        _snippet_hash(snippet),
    ]
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


class RetrievalObservation(BaseModel):
    """A single retrieval provenance record for a normalized search result."""

    provider: str = ""
    engine: str = ""
    rank: int = 0
    retrieved_at: str = Field(default_factory=_utc_now_iso)


class SearchCategory(str, Enum):
    general = "general"
    images = "images"
    news = "news"
    videos = "videos"
    it = "it"
    science = "science"
    files = "files"
    social_media = "social_media"


class SearchType(str, Enum):
    web = "web"
    news = "news"
    image = "image"
    video = "video"


class FetchMode(str, Enum):
    scrape = "scrape"
    crawl = "crawl"
    map = "map"


class ResearchDepth(str, Enum):
    quick = "quick"
    normal = "normal"
    deep = "deep"


# ─── Citation / Evidence Models ──────────────────────────────────────────────


class Source(BaseModel):
    """A retrieved source with full provenance metadata."""

    source_id: str = ""
    url: str
    canonical_url: str = ""
    title: str = ""
    fingerprint: str = ""
    domain: str = ""
    description: str = ""
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=_utc_now_iso)
    content: str = ""
    score: float = 0.0
    search_provider: str = ""  # searxng | github | grep | ddgs | hn | arxiv
    source_lane: str | None = None  # SourceType lane that produced this source (P5-VN)
    content_provider: str = (
        ""  # reader tier (http|firecrawl|playwright|cache) | firecrawl | direct | none
    )
    content_length: int = 0
    error: str | None = None
    # Evidence-aggregator trust fields (optional — back-compat, empty = not labeled)
    authority_type: str | None = (
        None  # official|government|research_paper|major_publication|vendor_website|specialist_blog|forum|reddit|unknown_seo_site
    )
    authority_score: float | None = None  # 0.0-1.0 (+vn boost when vi)
    trust: str | None = None  # high|medium|low|unverified
    trust_reasons: list[str] = []  # why this label (domain class, provider agreement)
    # Provenance fields (TASK-8H-0) — which providers/engines saw this result
    retrieval_observations: list[RetrievalObservation] = []


class SourceTrust(str, Enum):
    """Reliability label assigned by the evidence aggregator."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNVERIFIED = "unverified"


class EvidenceAggregationStats(BaseModel):
    """Aggregator summary — what happened across providers before returning."""

    total_candidates: int = 0  # raw results collected from all providers
    providers_used: list[str] = []  # providers that returned results
    deduplicated: int = 0  # duplicates removed (same canonical URL)
    independent_sources: int = 0  # distinct domains kept
    trust_distribution: dict[str, int] = {}  # {"high": n, "medium": n, ...}
    coverage: float = 0.0  # 0.0-1.0 how many max_results slots are filled


class EvidencePackage(BaseModel):
    """Evidence aggregator output — collect → dedupe → rank → label trust.

    Hermes (the caller) still does the analysis and conclusion; this package
    only provides ranked, deduplicated, trust-labeled sources to reason over.
    """

    query: str
    sources: list[Source] = []
    aggregation: EvidenceAggregationStats = Field(default_factory=EvidenceAggregationStats)


class Citation(BaseModel):
    """A citation linking a claim to source(s)."""

    claim: str = ""
    source_ids: list[str] = []


class EvidenceResult(BaseModel):
    """Evidence package returned with every answer."""

    sources: list[Source] = []
    citations: list[Citation] = []


# ─── Agent-Facing Request Models (5 tools) ───────────────────────────────────


class SearchToolRequest(BaseModel):
    """search() — find information on the web."""

    query: str
    type: SearchType = SearchType.web
    max_results: int = 10
    lang: str = "en"


class FetchToolRequest(BaseModel):
    """fetch() — read content from URL(s)."""

    url: str
    mode: FetchMode = FetchMode.scrape
    limit: int = 10  # for crawl/map modes
    formats: str = "markdown"


class CodeSearchToolRequest(BaseModel):
    """code_search() — search code repositories."""

    query: str
    max_results: int = 10
    repo: str | None = None  # filter by repo, e.g. "firecrawl/firecrawl"


class AnswerToolRequest(BaseModel):
    """answer() — full pipeline: search → scrape → rerank → synthesize with citations."""

    query: str
    depth: ResearchDepth = ResearchDepth.normal
    max_results: int = 10
    scrape_top_n: int = 5
    lang: str = "en"


# ─── Agent-Facing Response Models ────────────────────────────────────────────


class SearchToolResponse(BaseModel):
    query: str
    type: str
    results: list[Source] = []
    elapsed_seconds: float = 0.0


class FetchToolResponse(BaseModel):
    url: str
    mode: str
    content: str = ""
    title: str = ""
    metadata: dict[str, Any] = {}
    pages: list[dict[str, Any]] | None = None  # for crawl
    urls: list[str] | None = None  # for map
    error: str | None = None
    elapsed_seconds: float = 0.0


class CodeSearchToolResponse(BaseModel):
    query: str
    results: list[dict[str, Any]] = []
    elapsed_seconds: float = 0.0


class AnswerToolResponse(BaseModel):
    query: str
    answer: str = ""
    evidence: EvidenceResult = Field(default_factory=EvidenceResult)
    follow_up_questions: list[str] = []
    elapsed_seconds: float = 0.0


# ─── Legacy Models (kept for alias endpoints) ────────────────────────────────


class SearchRequest(BaseModel):
    query: str
    categories: list[SearchCategory] = Field(default=[SearchCategory.general])
    max_results: int = 10
    lang: str = "en"
    safe: bool = False


class SearchResultItem(BaseModel):
    url: str
    canonical_url: str = ""
    title: str = ""
    description: str | None = ""
    fingerprint: str = ""
    score: float = 0.0
    engine: str = ""
    category: str = ""
    published_date: str | None = None
    thumbnail: str | None = ""
    retrieval_observations: list[RetrievalObservation] = []
    # Provider-specific extras (e.g. gnews publisher identity) consumed by
    # internal scoring — excluded from the wire contract on purpose.
    metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ScrapeRequest(BaseModel):
    url: str
    formats: list[str] = Field(default=["markdown"])
    timeout: int = 60


class CrawlRequest(BaseModel):
    url: str
    limit: int = 10
    formats: list[str] = Field(default=["markdown"])


class MapRequest(BaseModel):
    url: str
    limit: int = 20


class AnswerRequest(BaseModel):
    query: str
    max_results: int = 10
    scrape_top_n: int = 5
    categories: list[SearchCategory] = Field(default=[SearchCategory.general])
    lang: str = "en"
    synthesize: bool = True
    follow_up_questions: bool = True


class ScrapeResult(BaseModel):
    url: str
    title: str = ""
    markdown: str = ""
    metadata: dict[str, Any] = {}
    error: str | None = None


class AnswerResult(BaseModel):
    query: str
    search_results: list[SearchResultItem] = []
    scraped_content: list[ScrapeResult] = []
    reranked_sources: list[dict[str, Any]] = []
    answer: str = ""
    follow_up_questions: list[str] = []
    elapsed_seconds: float = 0.0


# ─── New Models (v2 from SPEC-v3) ─────────────────────────────────────────────


class SourceAuthority(str, Enum):
    OFFICIAL = "official"
    GOVERNMENT = "government"
    RESEARCH_PAPER = "research_paper"
    MAJOR_PUBLICATION = "major_publication"
    VENDOR_WEBSITE = "vendor_website"
    SPECIALIST_BLOG = "specialist_blog"
    FORUM = "forum"
    REDDIT = "reddit"
    UNKNOWN_SEO_SITE = "unknown_seo_site"


class SearchMode(str, Enum):
    FAST = "fast"
    NORMAL = "normal"
    DEEP = "deep"


class QueryIntent(str, Enum):
    CURRENT_FACT = "current_fact"
    HISTORICAL = "historical"
    PREDICTIVE = "predictive"
    COMPARE = "compare"
    ANALYSIS = "analysis"
    OPINION = "opinion"


class ProviderStatus(BaseModel):
    name: str = Field(..., description="Provider name")
    status: str = Field(default="unknown", description="Provider status")
    last_check: str | None = Field(default=None, description="Last health check timestamp")


class VerdictStatus(str, Enum):
    """Verification verdict states (SPEC-v3 §9)."""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OUTDATED = "outdated"
    SOURCE_CONFLICT = "source_conflict"


class HealthStatus(BaseModel):
    """Back-compat: provider health summary (used by GET /health)."""

    searxng: str = Field(default="unknown", description="SearXNG status: ok/unreachable")
    firecrawl: str = Field(default="unknown", description="Firecrawl status: ok/unreachable")
    llm: str = Field(default="not configured", description="LLM status: configured/not configured")


class SearchBudget(BaseModel):
    max_queries: int = Field(default=5, description="Maximum number of search queries allowed")
    max_results: int = Field(default=10, description="Maximum number of results per query")
    max_fetches: int = Field(default=8, description="Maximum number of URLs to fetch and process")
    max_tokens: int | None = Field(
        default=None, description="Maximum tokens in answer (None = unlimited)"
    )
    max_duration: int | None = Field(
        default=None,
        description="Maximum processing duration in seconds (None = unlimited)",
    )
    max_followups: int = Field(
        default=1,
        description="Maximum number of follow-up searches for missing evidence",
    )
    max_cost: float | None = Field(
        default=None, description="Maximum cost in USD (None = unlimited)"
    )
    cost_used: float = Field(default=0.0, description="Current cost consumed")
    queries_used: int = Field(default=0, ge=0, description="Search queries consumed")
    fetches_used: int = Field(default=0, ge=0, description="URL fetches consumed")
    followups_used: int = Field(default=0, ge=0, description="Follow-up searches consumed")
    tokens_used: int = Field(default=0, ge=0, description="Inference tokens consumed")
    duration_used: float = Field(
        default=0.0, ge=0.0, description="Elapsed processing time in seconds"
    )


class SearchQuery(BaseModel):
    intent: QueryIntent = Field(..., description="Primary query intent")
    entities: list[str] = Field(default=[], description="Named entities mentioned")
    language: str = Field(default="en", description="Language code (ISO 639-1)")
    geography: str | None = Field(default=None, description="Geographic scope")
    time_sensitive: bool = Field(
        default=False, description="Whether query requires current information"
    )
    freshness_required: bool = Field(default=False, description="Whether freshness is a priority")
    max_age: str | None = Field(
        default=None,
        description="Maximum age of acceptable sources (e.g., '24h', '1y')",
    )
    preferred_sources: list[SourceAuthority] = Field(
        default=[], description="Preferred source authority types"
    )
    original_query: str = Field(..., description="User's original query text")


class SearchResult(BaseModel):
    result_id: str = Field(..., description="Unique result identifier")
    url: str = Field(..., description="Result URL")
    title: str = Field(default="", description="Result title")
    snippet: str = Field(default="", description="Short description/preview")
    source: str = Field(..., description="Provider name (searxng, github, arxiv, etc.)")
    source_type: SourceAuthority = Field(
        default=SourceAuthority.UNKNOWN_SEO_SITE, description="Source authority type"
    )
    authority_score: float = Field(default=0.0, description="Authority score from 0.0 to 1.0")
    freshness_score: float = Field(default=0.0, description="Freshness score from 0.0 to 1.0")
    category: str = Field(default="", description="Result category")
    published_date: str | None = Field(default=None, description="Publication date")
    language: str = Field(default="en", description="Language code")
    image_url: str | None = Field(default=None, description="Thumbnail image URL")
    score: float = Field(default=0.0, description="Composite relevance score")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class Passage(BaseModel):
    passage_id: str = Field(..., description="Unique passage identifier")
    source_id: str = Field(..., description="Source identifier")
    text: str = Field(..., description="Passage text content")
    quote_start: int = Field(default=-1, description="Start position in source document")
    quote_end: int = Field(default=-1, description="End position in source document")
    retrieved_at: str = Field(default_factory=_utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Passage metadata")


class EvidenceCluster(BaseModel):
    cluster_id: str = Field(..., description="Unique cluster identifier")
    sources: list[str] = Field(default=[], description="List of source identifiers in this cluster")
    is_independent: bool = Field(default=True, description="Whether sources are independent")
    deduplication_method: str = Field(default="", description="Method used for deduplication")


class ClaimVerification(BaseModel):
    claim_id: str = Field(..., description="Unique claim identifier")
    claim_text: str = Field(..., description="The claim being verified")
    status: str = Field(..., description="Verification status")
    evidence: list[str] = Field(
        default=[], description="List of evidence identifiers supporting the claim"
    )
    verdict: str = Field(default="", description="Detailed verdict text")
    confidence: float = Field(default=0.0, description="Confidence score (0.0-1.0)")
    contradictions: list[str] = Field(default=[], description="List of contradictions found")
    sources_conflict: bool = Field(default=False, description="Whether source conflict detected")
    timestamp: str = Field(default_factory=_utc_now_iso)


class CitationV2(BaseModel):
    claim_id: str = Field(..., description="Claim identifier")
    evidence: list[dict] = Field(default=[], description="Evidence references")
    citation_text: str = Field(default="", description="Rendered citation text")


class EvidencePack(BaseModel):
    answer: str = Field(..., description="Generated answer")
    claims: list[ClaimVerification] = Field(default=[], description="Claims extracted from answer")
    citations: list[CitationV2] = Field(default=[], description="Citations for claims")
    sources: list[Source] = Field(default=[], description="Source identifiers used")
    coverage: float = Field(default=0.0, description="Coverage score (0.0-1.0)")
    confidence: float = Field(default=0.0, description="Confidence score (0.0-1.0)")
    budget_used: SearchBudget = Field(default_factory=SearchBudget, description="Budget consumed")


class CapabilitiesResponse(BaseModel):
    modes: list[SearchMode] = Field(default=[], description="Supported search modes")
    providers: list[ProviderStatus] = Field(default=[], description="Provider status information")
    features: dict[str, Any] = Field(default_factory=dict, description="Available features")


class BusinessEntity(BaseModel):
    """A local business extracted from web content or stored in the geo database."""

    name: str = Field(..., description="Business name")
    category: str = Field(default="", description="Business category")
    address: str = Field(default="", description="Street address")
    phone: str | None = Field(default=None, description="Phone number")
    hours: str | None = Field(default=None, description="Opening hours")
    rating: float | None = Field(default=None, ge=0.0, le=5.0, description="Rating out of 5")
    price_level: str | None = Field(default=None, description="Price range or level")
    website: str | None = Field(default=None, description="Business website URL")
    lat: float | None = Field(default=None, description="Latitude (WGS84)")
    lon: float | None = Field(default=None, description="Longitude (WGS84)")
    admin_unit_id: int | None = Field(
        default=None,
        description="Canonical administrative_units.unit_id (P14A) — "
        "administrative identity lives here, not in name strings",
    )
    description: str = Field(default="", description="Short description or snippet")
    source_url: str = Field(default="", description="URL this entity was extracted from")
    # LOCAL-1 output contract — additive, defaults keep every existing
    # constructor call and wire consumer working.
    origin: str = Field(
        default="",
        description="Lane that produced the entity: canonical | osm_local | osm_live | web_discovery",
    )
    verified: bool = Field(
        default=False, description="True only for entities from the canonical lane"
    )
    location_precision: str = Field(
        default="unknown", description="exact | street | area | unknown"
    )
    supporting_source_count: int = Field(
        default=1, description="Distinct source URLs merged into this entity"
    )
