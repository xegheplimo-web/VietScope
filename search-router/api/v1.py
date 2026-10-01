"""Search Hub v1 API router (SPEC-v3 §2).

Exposes the evidence-driven endpoints without touching the legacy ``main.py``.
To mount these routes, add ``app.include_router(api.v1.router)`` in ``main.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from agent.orchestrator import run_research
from config import settings
from core.budget import SearchBudget
from core.business_entity import extract_business_batch, osm_tag_for, osm_tag_kv
from core.conversation import get_conversation_manager, resolve_request_query
from core.engine_health import get_engine_health_manager
from core.entity_resolver import resolve_entities
from core.orchestrator import SearchOrchestrator
from core.provider_registry import create_default_registry
from evidence.citation import build_passages
from evidence.claims import Claim, extract_claims_with_llm, keywords
from evidence.pack import build_evidence_pack
from evidence.verifier import verify_claims
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from models import (
    BusinessEntity,
    CapabilitiesResponse,
    ClaimVerification,
    EvidenceCluster,
    EvidencePack,
    ProviderStatus,
    SearchMode,
    Source,
    VerdictStatus,
)
from pipeline.rag import stream_research_answer, synthesize_research_answer
from providers.searxng import MIN_RESULTS_BEFORE_WIDENING
from pydantic import BaseModel, Field, model_validator
from research_models.research_state import ResearchContext
from security.apikeys import require_api_key
from services.admin import admin_anchor
from services.geo import GeoPoint, geocode, overpass_amenities
from services.geo_postgis import osm_pois_nearby
from storage.business_store import BusinessStore

from observability.prometheus import observe_degraded, observe_search

if TYPE_CHECKING:
    from pipeline.federated_retrieval import FederatedResult

router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(require_api_key)])


# ─── Request models ──────────────────────────────────────────────────────────


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: SearchMode = SearchMode.FAST
    max_hops: int | None = Field(default=None, ge=1, le=4)


class VerifyRequest(BaseModel):
    claims: list[str] = Field(default_factory=list)
    clusters: list[EvidenceCluster] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    max_age_days: int | None = None


class ReadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    chunk_size: int = Field(default=800, ge=1, le=20000)
    chunk_overlap: int = Field(default=200, ge=0, le=19999)

    @model_validator(mode="after")
    def validate_chunking(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class EvidenceSourceIn(BaseModel):
    """One ``/v1/evidence`` source — the structured ``search`` contract.

    Only ``url`` is required; the other fields carry search-side
    provenance straight through to every emitted evidence item so
    citations can map claim → passage → source → URL.
    """

    url: str = Field(min_length=1, max_length=8192)
    source_id: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    domain: str | None = None
    published_at: str | None = None
    search_provider: str | None = None
    score: float | None = None


class EvidenceRequest(BaseModel):
    """POST /v1/evidence — batch evidence fetch + passage rerank (P11.1).

    ``sources`` caps at 20 server-side — Hermes contract is 8 (normal) /
    15 (deep), enforced at the MCP layer; 20 is the hard API ceiling so a
    runaway batch cannot outlive the request budget.
    """

    sources: list[EvidenceSourceIn | str] = Field(min_length=1, max_length=20)
    query: str = Field(min_length=1, max_length=2000)
    max_passages: int = Field(default=5, ge=1, le=50)
    max_per_source: int = Field(default=3, ge=1, le=10)
    freshness: Literal["realtime", "high", "normal", "static"] = "normal"


class SearchRequest(BaseModel):
    """POST /v1/search — unified search (SPEC-v3 §2 + research engine).

    Without ``mode``: legacy raw multi-provider search returning
    ``results`` + ``understanding``.  With ``mode``
    (``fast`` | ``balanced`` | ``deep``, aliases ``quick``/``normal``/``auto``
    accepted): the full research pipeline runs — LLM query planning,
    parallel SearXNG search, RRF fusion, AI reranking, Firecrawl scraping,
    passage reranking, gap-driven follow-ups, synthesis, and claim
    verification — and the response adds ``answer``, ``sources``,
    ``search`` stats and ``timings`` while keeping ``results`` and
    ``understanding`` as compatibility keys.

    Conversation context (P4): ``session_id`` loads the stored session
    state (Redis ``convctx:*``, in-memory fallback); ``history``
    (``[role, text]`` turns) supplies stateless context. When both are
    present they merge — the stored state wins on conflicting fields.
    On the research lanes a short/anaphoric follow-up is rewritten into
    a standalone query before the pipeline runs, and the response adds
    ``session_id`` (echo) + ``followup_resolved``/``resolved_query``.
    """

    query: str = Field(min_length=1, max_length=2000)
    type: str = Field(default="web", pattern="^(web|news|image)$")
    max_results: int = Field(default=10, ge=1, le=50)
    # "research" is the canonical v2.1 name for the deep/agentic mode and is
    # what the Vane adapter sends for its "quality" mode.
    mode: str | None = Field(
        default=None, pattern="^(fast|balanced|deep|quick|normal|auto|research)$"
    )
    language: str | None = Field(default=None, max_length=8)
    freshness: str | None = Field(default=None, pattern="^(any|day|week|month)$")
    citations: bool = True
    # P4 — conversation session key; charset is restricted so it is always
    # safe inside the ``convctx:`` Redis key.
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # Client compatibility fields (v2.1 §17) — accepted here so the adapter
    # contract holds; ``sources`` steers lane preferences (P8), ``history`` is
    # conversational context, ``stream`` switches /v1/answer to SSE.
    sources: list[str] | None = None
    history: list | None = None
    stream: bool = False


class BusinessSearchRequest(BaseModel):
    """POST /v1/business/search — local business search by location.

    ``lat``/``lon`` may both be omitted: the anchor is then resolved from
    the query itself (entity resolution → Nominatim geocode, or the text
    following "gần|near|ở|tại|quanh" phrases — P7).
    """

    query: str = Field(min_length=1, max_length=2000)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    radius_km: float = Field(default=2.0, ge=0.1, le=50.0)
    category: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _coords_pair(self) -> BusinessSearchRequest:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be provided together")
        return self


class GeoAnchor(BaseModel):
    """The resolved geo anchor when lat/lon came from the query text."""

    name: str
    lat: float
    lon: float
    display_name: str = ""
    resolved_from: str = "entity"  # entity | phrase


class BusinessSearchResponse(BaseModel):
    query: str
    lat: float | None
    lon: float | None
    radius_km: float
    provider: str
    anchor: GeoAnchor | None = None
    entities: list[BusinessEntity]
    evidence: list[str]
    count: int


class AuthCheckResponse(BaseModel):
    """GET /v1/auth/check — self-verification probe for client startup.

    ``authenticated`` means a Bearer key was presented AND resolved to a
    live (non-revoked) key row; ``auth_enabled`` reports the deployment's
    API_AUTH_ENABLED so dev stacks can skip verification entirely.
    """

    authenticated: bool
    auth_enabled: bool
    tenant: str | None = None
    scopes: list[str] = Field(default_factory=list)


class AdminUnitOut(BaseModel):
    """One administrative unit (P14A), keyed by era-prefixed key."""

    key: str = Field(description="Era-prefixed key, e.g. 'new:24' / 'old:221'")
    unit_id: int | None = None
    code: str
    name: str
    type: str
    admin_level: int
    status: str
    valid_from: str | None = None
    valid_to: str | None = None
    source: str = ""


class AdminRelationOut(BaseModel):
    from_key: str
    to_key: str
    relation_type: str
    effective_date: str | None = None
    source: str = ""


class AdminResolveResponse(BaseModel):
    """GET /v1/admin/resolve — address text → canonical admin graph (P14A)."""

    query: str
    status: str = Field(description="resolved | ambiguous | not_found")
    confidence: float
    matched: list[AdminUnitOut] = Field(
        description="Units matched in the input (any era, may be historical)"
    )
    current: list[AdminUnitOut] = Field(
        description="Current-era units resolved via the transition graph"
    )
    path: list[AdminRelationOut] = Field(description="Transition edges walked to reach ``current``")
    ambiguity: list[AdminUnitOut] = Field(
        default_factory=list,
        description="Competing candidates when status == 'ambiguous'",
    )


# ─── Singletons ──────────────────────────────────────────────────────────────

_registry = None
_orchestrator: SearchOrchestrator | None = None


def _get_orchestrator() -> SearchOrchestrator:
    global _registry, _orchestrator
    if _registry is None:
        _registry = create_default_registry()
    if _orchestrator is None:
        _orchestrator = SearchOrchestrator(_registry)
    return _orchestrator


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _conversation_owner(request: Request) -> str:
    """P4 session-owner principal — binds ``convctx:*`` state to the
    authenticated API key so sessions cannot leak across keys. Uses the
    same identifier the auth dependency derives (``key_id``); the constant
    ``anonymous`` when auth is disabled (local/dev). The manager hashes it
    into the key — raw key material is never stored."""
    ctx = getattr(request.state, "api_key", None)
    principal = getattr(ctx, "key_id", "") if ctx is not None else ""
    return principal or "anonymous"


async def _resolve_conversation_query(req: SearchRequest, owner: str):
    """P4: rewrite anaphoric follow-ups against session/history context.

    Runs before ``ResearchContext`` creation so the pipeline executes the
    standalone query; fail-open — the raw query passes through untouched
    when no context exists or the resolver cannot help.
    """
    return await resolve_request_query(
        req.query, session_id=req.session_id, history=req.history, owner=owner
    )


def _conversation_fields(req: SearchRequest, resolution) -> dict:
    """Response keys added when conversation context could engage (P4).

    ``session_id`` requests always echo ``session_id`` +
    ``followup_resolved``. ``history``-only requests surface
    ``followup_resolved``/``resolved_query`` ONLY when a rewrite actually
    happened — a standalone question carrying history must stay
    byte-identical to a context-free request (zero-diff contract).
    """
    fields: dict = {}
    if req.session_id is not None:
        fields["session_id"] = req.session_id
        fields["followup_resolved"] = resolution.resolved
        if resolution.resolved:
            fields["resolved_query"] = resolution.query
    elif resolution.resolved:
        fields["followup_resolved"] = True
        fields["resolved_query"] = resolution.query
    return fields


async def _persist_conversation_turn(
    req: SearchRequest, resolution, result: dict, owner: str
) -> None:
    """Store the completed exchange under ``convctx:{owner}:{session_id}`` (fail-open)."""
    if req.session_id is None or not settings.conversation_context_enabled:
        return
    with contextlib.suppress(Exception):  # persistence must never break the request
        await get_conversation_manager().record_turn(
            req.session_id,
            owner=owner,
            user_text=req.query,
            resolved_query=resolution.query,
            answer=(result or {}).get("answer") or "",
            sources=(result or {}).get("sources") or [],
            resolution=resolution.resolution,
        )


async def _synthesize_answer(query: str, sources: list[Source]) -> str:
    return await synthesize_research_answer(query, sources)


async def _provider_statuses() -> list[ProviderStatus]:
    from providers.firecrawl import firecrawl_health
    from providers.searxng import searxng_health

    now = _now_iso()
    statuses: list[ProviderStatus] = []

    try:
        searxng_ok = await searxng_health()
    except Exception:  # noqa: BLE001 — health probe must not raise
        searxng_ok = False
    statuses.append(
        ProviderStatus(
            name="searxng",
            status="ok" if searxng_ok else "unreachable",
            last_check=now,
        )
    )

    try:
        firecrawl_ok = await firecrawl_health()
    except Exception:  # noqa: BLE001 — health probe must not raise
        firecrawl_ok = False
    statuses.append(
        ProviderStatus(
            name="firecrawl",
            status="ok" if firecrawl_ok else "unreachable",
            last_check=now,
        )
    )

    github_status = "configured" if settings.github_token else "unconfigured"
    statuses.append(ProviderStatus(name="github", status=github_status, last_check=now))

    return statuses


# ─── Endpoints ───────────────────────────────────────────────────────────────


async def _service_statuses() -> dict[str, str]:
    """Aggregate probe of the whole stack (P10). Short timeouts — a down
    dependency must degrade health, never hang it."""
    import asyncio as _aio

    from providers.firecrawl import firecrawl_health
    from providers.searxng import searxng_health

    async def _probe(name: str, coro) -> tuple[str, str]:
        try:
            ok = await _aio.wait_for(coro, timeout=2.0)
            return name, "ok" if ok else "down"
        except Exception:  # noqa: BLE001 — health probe must not raise
            return name, "down"

    async def _os_health() -> bool:
        from opensearch.client import OpenSearchClient

        return await _aio.to_thread(OpenSearchClient().health)

    async def _qdrant_health() -> bool:
        from qdrant.client import QdrantClient

        qc = QdrantClient()
        try:
            return await qc.health()
        finally:
            await qc.close()

    async def _http_health(url: str) -> bool:
        import httpx

        async with httpx.AsyncClient(timeout=1.5) as c:
            return (await c.get(f"{url}/health")).status_code == 200

    probes = [
        _probe("searxng", searxng_health()),
        _probe("firecrawl", firecrawl_health()),
    ]
    if settings.opensearch_enabled:
        probes.append(_probe("opensearch", _os_health()))
    if settings.qdrant_enabled:
        probes.append(_probe("qdrant", _qdrant_health()))
    if getattr(settings, "embedding_service_url", ""):
        probes.append(_probe("embedding", _http_health(settings.embedding_service_url)))
    if getattr(settings, "reranker_service_url", ""):
        probes.append(_probe("reranker", _http_health(settings.reranker_service_url)))

    async def _minio_probe() -> tuple[str, str]:
        try:
            from storage.object_store import ObjectStore

            ok = await _aio.wait_for(ObjectStore().health(), timeout=2.0)
            return "minio", "ok" if ok else "degraded"
        except Exception:  # noqa: BLE001 — health probe must not raise
            return "minio", "degraded"

    if getattr(settings, "minio_endpoint", ""):
        probes.append(_minio_probe())
    return dict(await _aio.gather(*probes))


@router.get("/health")
async def health(request: Request):
    """Public health: minimal shape. Full per-service detail requires
    admin:debug scope (or API_AUTH_ENABLED=false in local/dev)."""
    services = await _service_statuses()
    degraded = any(v != "ok" for v in services.values())

    ctx = getattr(request.state, "api_key", None)
    detailed = not settings.api_auth_enabled or (ctx is not None and ctx.has_scope("admin:debug"))
    if not detailed:
        return {
            "status": "degraded" if degraded else "ok",
            "version": "3.0.0",
        }
    return {
        "status": "degraded" if degraded else "ok",
        "service": "search-hub",
        "version": "3.0.0",
        "llm": "configured" if settings.llm_api_key else "not configured",
        "services": services,
    }


@router.get("/providers", response_model=list[ProviderStatus])
async def providers():
    return await _provider_statuses()


@router.get("/providers/health")
async def providers_health():
    """Provider health scores + circuit state (Phase 2 federation layer).

    Each entry: health_score, status (healthy/degraded/unhealthy), circuit
    (closed/open/half_open), rolling rates (success/timeout/captcha),
    latency percentiles, yield, consecutive_failures.
    """
    orchestrator = _get_orchestrator()
    return {
        "providers": {name: h.to_dict() for name, h in orchestrator.monitor.snapshot_all().items()},
        "engines": {
            name: {
                "total_requests": s.total_requests,
                "failure_rate": round(s.failure_rate, 4),
                "rate_429": round(s.rate_429, 4),
                "captcha_rate": round(s.captcha_rate, 4),
                "disabled": s.is_disabled,
            }
            for name, s in get_engine_health_manager().get_all_stats().items()
        },
    }


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities():
    return CapabilitiesResponse(
        modes=[SearchMode.FAST, SearchMode.NORMAL, SearchMode.DEEP],
        providers=await _provider_statuses(),
        features={
            # Public API surface — lets clients (Vane, Hermes, OpenWebUI)
            # discover what this deployment supports.
            "endpoints": {
                "search": True,
                "answer": True,
                "research": True,
                "news": True,
                "images": settings.qdrant_images_enabled,
                "read_url": True,
                "verify": True,
            },
            "stream": True,
            "aliases": {"research": "deep", "quick": "fast", "auto": "normal"},
            "auth": {
                "enabled": settings.api_auth_enabled,
                "scheme": "bearer",
            },
            "verification": {
                "states": [s.value for s in VerdictStatus],
                "independent_sources": True,
            },
            "citation": {"passage_level": True},
            "streaming": {
                "events": ["planning", "searching", "fetching", "verifying", "answer"],
            },
            "modes": {
                "fast": {"max_queries": 2, "max_fetches": 3, "max_followups": 0},
                "normal": {"max_queries": 5, "max_fetches": 8, "max_followups": 1},
                "deep": {"max_queries": 12, "max_fetches": 20, "max_followups": 3},
            },
        },
    )


@router.get("/auth/check", response_model=AuthCheckResponse)
async def auth_check(request: Request):
    """Credential self-verification probe (P1.2 follow-up).

    Always public — the response body IS the verdict, so it never
    401s/403s: a client (e.g. the MCP startup script) sends its Bearer key
    and learns whether the key resolves and which scopes it holds, without
    needing an admin scope to ask.
    """
    ctx = getattr(request.state, "api_key", None)
    return AuthCheckResponse(
        authenticated=ctx is not None,
        auth_enabled=settings.api_auth_enabled,
        tenant=ctx.tenant_id if ctx is not None else None,
        scopes=sorted(ctx.scopes) if ctx is not None else [],
    )


@router.post("/research", response_model=EvidencePack)
async def research(req: ResearchRequest):
    orchestrator = _get_orchestrator()
    try:
        pack = await orchestrator.research(req.query, req.mode.value, max_hops=req.max_hops)
    except Exception:
        observe_degraded("research_error")
        raise
    sources = list(pack.sources or [])
    observe_search("/v1/research", req.mode.value, len(sources))

    answer = await _synthesize_answer(req.query, sources)
    claims = await extract_claims_with_llm(answer, orchestrator.inference)

    return build_evidence_pack(
        answer=answer,
        claims=claims,
        sources=sources,
        budget=pack.budget_used,
        inference=orchestrator.inference,
    )


@router.post("/research/stream")
async def research_stream(req: ResearchRequest):
    async def event_stream():
        try:
            orchestrator = _get_orchestrator()
            queue: asyncio.Queue = asyncio.Queue()
            yield _sse("init", {"query": req.query, "mode": req.mode.value})

            async def emit(event: str, data: dict) -> None:
                await queue.put((event, data))

            async def run_research():
                try:
                    pack = await orchestrator.research(
                        req.query,
                        req.mode.value,
                        max_hops=req.max_hops,
                        progress=emit,
                    )
                    await queue.put(("_done", pack))
                except Exception as exc:  # noqa: BLE001 — stream must not die
                    await queue.put(("_error", exc))

            task = asyncio.create_task(run_research())
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item[0] == "_done":
                    pack = item[1]
                    sources = list(pack.sources or [])
                    # P3: stream synthesis tokens live; fallback text arrives
                    # as a single delta via stream_research_answer.
                    box: dict = {}
                    async for tok in _answer_tokens(req.query, sources, box):
                        yield _sse("answer.delta", {"text": tok})
                    answer = box.get("answer") or ""

                    claims = await extract_claims_with_llm(answer, orchestrator.inference)
                    yield _sse("verifying", {"claims": len(claims), "sources": len(sources)})
                    yield _sse("evidence", {"claims": len(claims), "sources": len(sources)})
                    pack_v2 = build_evidence_pack(
                        answer=answer,
                        claims=claims,
                        sources=sources,
                        budget=pack.budget_used,
                        inference=orchestrator.inference,
                    )
                    # Canonical §23 tail: source → citation → answer → done
                    for s in sources:
                        yield _sse("source", s.model_dump(mode="json"))
                    yield _sse("answer", {"pack": pack_v2.model_dump(mode="json")})
                    yield _sse(
                        "done",
                        {
                            "claims": len(claims),
                            "sources": len(sources),
                            "coverage": pack.coverage,
                        },
                    )
                    break
                if item[0] == "_error":
                    yield _sse("error", {"message": str(item[1])})
                    yield _sse("warning", {"message": str(item[1])})
                    break
                event, data = item
                yield _sse(event, data)
                canonical = _SSE_EVENT_MAP.get(event)
                if canonical:
                    yield _sse(canonical, data)
            task.cancel()
        except Exception as exc:  # noqa: BLE001 — stream must not die
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/verify", response_model=list[ClaimVerification])
async def verify(req: VerifyRequest):
    claims = [
        Claim(claim_id=f"c{i + 1}", text=text, keywords=keywords(text))
        for i, text in enumerate(req.claims)
    ]
    return verify_claims(
        claims,
        req.clusters,
        sources=req.sources,
        max_age_days=req.max_age_days,
    )


@router.post("/search")
async def search_v1(req: SearchRequest, request: Request):
    """Unified search — raw results by default, research engine when ``mode`` set."""
    orchestrator = _get_orchestrator()

    # Query understanding (intent, freshness, entities...)
    profile = orchestrator.query_understanding.analyze(req.query)

    if req.mode is not None:
        response = await _research_search(req, profile, _conversation_owner(request))
        observe_search("/v1/search", req.mode, len(response.get("results") or []))
        return response

    from dataclasses import asdict

    from models import SearchCategory

    cat_map = {
        "web": [SearchCategory.general],
        "news": [SearchCategory.news],
        "image": [SearchCategory.images],
        "video": [SearchCategory.videos],
    }
    categories = cat_map.get(req.type, [SearchCategory.general])

    from core.provider_registry import ProviderSearchQuery

    sq = ProviderSearchQuery(
        query=req.query,
        categories=categories,
        max_results=req.max_results,
        lang=profile.language,
    )

    # SearXNG primary, DDGS fallback on low recall / error. Every call goes
    # through the federation executor — health scoring + circuit breaker
    # learn from raw searches too.
    from core.federation import FederatedExecutor
    from core.provider_health import Admission
    from providers.base import SearchContext

    executor = FederatedExecutor(orchestrator.monitor)
    ctx = SearchContext(language=profile.language, mode="normal")

    def _num(value, default: float) -> float:
        return float(value) if isinstance(value, (int, float)) else default

    def _admitted(name: str) -> bool:
        admission = orchestrator.monitor.allow(name)
        if admission is Admission.deny:
            return False
        ctx.probe = admission is Admission.probe
        return True

    results_with_provider: list = []
    searxng_count = 0
    searxng = orchestrator.registry.get("searxng")
    if searxng is not None and _admitted("searxng"):
        spec = orchestrator.registry.spec("searxng")
        searxng_results, _report = await executor.call_provider(
            "searxng",
            searxng,
            sq,
            ctx,
            spec_timeout_s=_num(getattr(spec, "timeout_s", None), 20.0),
            target_latency_ms=_num(getattr(spec, "target_latency_ms", None), 3000.0),
        )
        for r in searxng_results:
            results_with_provider.append((r, "searxng"))
        searxng_count = len(searxng_results)

    if not results_with_provider or searxng_count < MIN_RESULTS_BEFORE_WIDENING:
        ddgs = orchestrator.registry.get("ddgs")
        if ddgs is not None and _admitted("ddgs"):
            spec = orchestrator.registry.spec("ddgs")
            ddgs_results, _report = await executor.call_provider(
                "ddgs",
                ddgs,
                sq,
                ctx,
                spec_timeout_s=_num(getattr(spec, "timeout_s", None), 12.0),
                target_latency_ms=_num(getattr(spec, "target_latency_ms", None), 2500.0),
            )
            for r in ddgs_results:
                results_with_provider.append((r, "ddgs"))

    results = [r for r, _ in results_with_provider]

    # Search-level metrics: zero hits from the primary lane is a degradation
    # even when the DDGS fallback covers it.
    if searxng_count == 0:
        observe_degraded("searxng_empty")
    observe_search("/v1/search", "raw", len(results))

    if not results:
        return {"query": req.query, "understanding": asdict(profile), "results": []}

    return {
        "query": req.query,
        "type": req.type,
        "understanding": asdict(profile),
        "results": [
            {
                "url": r.url,
                "canonical_url": r.canonical_url,
                "title": r.title,
                "description": r.snippet or "",
                "fingerprint": r.fingerprint,
                "published_at": r.published_at,
                "engine": r.engine,
                "thumbnail": r.thumbnail or "",
                "retrieval_observations": [obs.model_dump() for obs in r.retrieval_observations],
            }
            for r in results
        ],
        "count": len(results),
    }


async def _embed_query(query: str) -> list[float] | None:
    """Query embedding via the BGE embedding service (``POST /embed``).

    Returns ``None`` when the service is disabled or errors — the dense lane
    then reports degraded instead of failing the request.
    """
    if not settings.embedding_service_enabled:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.embedding_service_url}/embed",
                json={"texts": [query]},
            )
            resp.raise_for_status()
            vectors = resp.json().get("vectors") or []
            return vectors[0] if vectors and isinstance(vectors[0], list) else None
    except Exception:  # noqa: BLE001 — embedding lane is optional
        return None


async def _hybrid_retrieve(query: str) -> FederatedResult:
    """Hybrid index lane (P11-T2): OpenSearch BM25 + Qdrant dense → RRF.

    Runs as an additive candidate lane inside the research pipeline — the
    live-web providers stay authoritative.
    """
    from opensearch.client import OpenSearchClient
    from pipeline.federated_retrieval import FederatedResult, FederatedRetriever
    from qdrant.client import QdrantClient

    dense_enabled = settings.qdrant_enabled and settings.qdrant_dense_enabled
    query_vector = await _embed_query(query) if dense_enabled else None
    retriever = FederatedRetriever(
        OpenSearchClient(),
        QdrantClient(),
        opensearch_index=settings.opensearch_index_passages,
        qdrant_collection=settings.qdrant_collection_passages,
        rrf_k=settings.hybrid_rrf_k,
        top_k=settings.hybrid_top_k,
    )
    try:
        result = await retriever.retrieve(
            query,
            query_vector=query_vector,
            enable_qdrant=dense_enabled,
            enable_opensearch=settings.opensearch_enabled,
        )
    except Exception:  # noqa: BLE001 — defensive: retriever already degrades
        result = FederatedResult(degraded=True, degraded_reason="retriever_error")
    if dense_enabled and not query_vector:
        result.degraded = True
        result.degraded_reason = result.degraded_reason or "embedding_failed"
    return result


async def _research_search(req: SearchRequest, profile, owner: str = "anonymous") -> dict:
    """Run the research-agent pipeline for ``POST /v1/search`` with ``mode``.

    Returns the enhanced response (``answer``/``sources``/``search``/
    ``timings``/``verification``) plus ``results`` + ``understanding``
    compatibility keys for raw-search consumers.
    """
    from dataclasses import asdict
    from functools import partial

    from agent.orchestrator import run_research
    from pipeline.semantic_cache import semantic_cache
    from research_models.research_state import ResearchContext

    # P4: follow-up resolution precedes ResearchContext — the pipeline
    # executes the standalone query, never the raw anaphoric text.
    resolution = await _resolve_conversation_query(req, owner)
    effective_query = resolution.query

    # P5: answer cache — short-circuits before any provider/LLM work. Keyed on
    # the resolved standalone query so context-free repeats hit the same entry.
    fresh_class = _FRESHNESS_TO_CLASS.get(req.freshness or "", "medium")
    cache_suffix = f"ep:search|cit:{int(bool(req.citations))}"
    if settings.semantic_cache_enabled:
        hit = await semantic_cache.get(
            effective_query,
            mode=req.mode or "auto",
            lang=req.language or "en",
            freshness_class=fresh_class,
            key_suffix=cache_suffix,
        )
        if hit is not None:
            cached = dict(hit.value)
            cached["cache"] = {"hit": True, "layer": hit.layer}
            cached.update(_conversation_fields(req, resolution))
            await _persist_conversation_turn(req, resolution, cached, owner)
            return cached

    context = ResearchContext(query=effective_query, mode=req.mode or "auto")

    # P11-T2: hybrid index lane (OpenSearch BM25 + Qdrant dense → RRF) runs
    # in parallel with the live-web providers on the first search round and
    # merges into the candidate pool before reranking.
    hybrid_lane = None
    if settings.hybrid_retrieval_enabled:
        hybrid_lane = partial(_hybrid_retrieve, effective_query)

    result = await run_research(
        context,
        language=req.language,
        freshness=req.freshness,
        hybrid_lane=hybrid_lane,
    )

    results = [
        {
            "url": s.url,
            "title": s.title,
            "description": s.description,
            "published_at": s.published_at,
            "engine": s.engine or "federated",
        }
        for s in context.search_results
    ]

    response = {
        "query": req.query,
        "type": req.type,
        "mode": req.mode,
        "answer": result["answer"],
        "confidence": result["confidence"],
        "sources": result["sources"],
        "search": result["search"],
        "verification": result["verification"],
        "timings": result["timings"],
        # Compatibility keys — same shape as the raw search response.
        "understanding": asdict(profile),
        "results": results,
        "count": len(results),
    }
    if req.citations:
        response["citations"] = result["citations"]
    if settings.semantic_cache_enabled:
        await semantic_cache.set(
            effective_query,
            response,
            mode=req.mode or "auto",
            lang=req.language or "en",
            freshness_class=fresh_class,
            key_suffix=cache_suffix,
        )
    response.update(_conversation_fields(req, resolution))
    await _persist_conversation_turn(req, resolution, result, owner)
    return response


@router.post("/read")
async def read(req: ReadRequest):
    from providers.firecrawl import firecrawl_scrape
    from security.ssrf import SSRFError, check_url

    # SSRF guard — chặn localhost/private IP/DNS rebinding trước khi fetch
    try:
        check_url(req.url, resolve=True)
    except SSRFError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=f"URL blocked by SSRF policy: {exc}") from exc

    result = await firecrawl_scrape(
        req.url,
        formats=["markdown"],
        timeout=settings.scrape_timeout,
    )
    source = Source(
        source_id=f"read:{req.url}",
        url=req.url,
        title=result.title or "",
        content=result.markdown or "",
        content_provider="firecrawl",
        error=result.error,
        retrieved_at=_now_iso(),
    )
    passages = build_passages(
        source,
        chunk_size=req.chunk_size,
        chunk_overlap=req.chunk_overlap,
    )
    return {
        "url": req.url,
        "title": result.title or "",
        "error": result.error,
        "passages": passages,
    }


@router.post("/evidence")
async def evidence(req: EvidenceRequest):
    """POST /v1/evidence — structured sources → reranked cited passages.

    Each source is read through the tiered, SSRF-guarded reader
    (``pipeline.reader`` → ``crawler.netguard``), chunked and reranked by
    the shared ``pipeline.passage_reranker`` — the same evidence path the
    research pipeline uses, not a parallel implementation. Search-side
    provenance (``source_id``/``canonical_url``/``published_at``/
    ``search_provider``) is echoed back on every item so citations map
    claim → passage_id → source_id → URL. Backed by
    ``pipeline.evidence_fetch.build_evidence``.
    """
    from pipeline.evidence_fetch import build_evidence

    return await build_evidence(
        [
            s.model_dump(exclude_none=True) if isinstance(s, EvidenceSourceIn) else s
            for s in req.sources
        ],
        req.query,
        max_passages=req.max_passages,
        max_per_source=req.max_per_source,
        freshness=req.freshness,
    )


# P5: req.freshness (any|day|week|month) → semantic-cache TTL class.
_FRESHNESS_TO_CLASS = {"day": "realtime", "week": "high", "month": "medium"}

# Canonical SSE event names (v2.1 §23) — emitted by /v1/answer?stream=true and
# alongside the legacy names on /v1/research/stream.
_SSE_EVENT_MAP = {
    "planning": "plan",
    "followup": "research.round",
    "verifying": "evidence",
    "error": "warning",
}


def _sse_stream(events):
    """Wrap an async event iterator into a canonical SSE StreamingResponse."""
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_SSE_KEEPALIVE_S = 20.0


async def _answer_tokens(query: str, sources: list, box: dict):
    """Yield LLM synthesis tokens as they stream.

    ``box["answer"]`` holds the final (post-processing) answer text once the
    generator is exhausted; ``box["error"]`` is set if synthesis raised.
    """
    tok_q: asyncio.Queue = asyncio.Queue()

    async def _tok(t: str) -> None:
        await tok_q.put(t)

    async def _synth() -> None:
        try:
            box["answer"] = await stream_research_answer(query, sources, on_delta=_tok)
        except Exception as exc:  # noqa: BLE001 — stream must not die
            box["error"] = exc
        finally:
            await tok_q.put(None)

    task = asyncio.create_task(_synth())
    try:
        while True:
            tok = await tok_q.get()
            if tok is None:
                break
            yield tok
    finally:
        task.cancel()


async def _canonical_answer_events(query: str, req: SearchRequest, owner: str):
    """Canonical SSE stream (v2.1 §23) — live events pumped from run_research.

    The research task emits real state events (planning → search → source →
    source.read → evidence → answer.delta → verified) as they happen; this
    generator just drains the queue.  Citations + ``done`` are emitted at the
    tail because they exist only after verification.
    """
    query_id = f"srch_{uuid.uuid4().hex[:12]}"
    # P4: resolve follow-ups before the pipeline starts — the stream
    # reports the effective query via ``init``/``done`` fields.
    resolution = await _resolve_conversation_query(req, owner)
    init = {"query": query, "query_id": query_id, "mode": req.mode or "balanced"}
    init.update(_conversation_fields(req, resolution))
    yield _sse("init", init)
    context = ResearchContext(query=resolution.query, mode=req.mode or "balanced")
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    async def _run() -> None:
        try:
            result = await run_research(
                context,
                language=req.language,
                freshness=req.freshness,
                emit=emit,
            )
            await queue.put(("_done", result))
        except Exception as exc:  # noqa: BLE001 — stream must not die
            await queue.put(("_error", exc))

    task = asyncio.create_task(_run())
    result: dict = {}
    seen_sources: set[str] = set()
    try:
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"  # SSE comment — keeps proxies from idling out
                continue
            if event == "_done":
                result = data
                break
            if event == "_error":
                yield _sse("warning", {"message": str(data)})
                yield _sse("done", {"query_id": query_id, "verified": False})
                return
            if event == "source":
                seen_sources.add(str((data or {}).get("url") or ""))
            yield _sse(event, data)
    finally:
        task.cancel()  # client disconnect / early return must not leak the run

    # P4: persist as soon as the result exists — a client disconnect
    # after this point still leaves the session state up to date.
    await _persist_conversation_turn(req, resolution, result, owner)

    # Backstop: any result source not announced live still surfaces before
    # the citation tail (covers non-emitting run_research paths/stubs).
    for source in result.get("sources") or []:
        if str(source.get("url") or "") not in seen_sources:
            yield _sse("source", source)

    if req.citations:
        for citation in result.get("citations") or []:
            yield _sse("citation", citation)

    yield _sse(
        "done",
        {
            "query_id": query_id,
            "coverage": _claims_coverage(result),
            "confidence": result.get("confidence", 0.0),
            "verified": _all_claims_verified(result),
            "timings": result.get("timings", {}),
            **_conversation_fields(req, resolution),
        },
    )


def _claims_coverage(result: dict) -> float:
    """Share of extracted claims backed by evidence (verified or partially
    supported) — distinct from ``confidence``, which scores how strong that
    evidence is. Same semantics as ``evidence.pack.coverage``: verified
    claims are exactly the non-``insufficient_evidence`` ones.
    """
    vstats = result.get("verification") or {}
    total = vstats.get("claims_total", 0)
    if not total:
        return 0.0
    return round(vstats.get("claims_verified", 0) / total, 3)


def _all_claims_verified(result: dict) -> bool:
    """``verified`` per §21: every extracted claim passed verification."""
    vstats = result.get("verification") or {}
    total = vstats.get("claims_total", 0)
    return total > 0 and vstats.get("claims_verified", 0) == total


@router.post("/answer")
async def answer(req: SearchRequest, request: Request):
    """POST /v1/answer — retrieval + evidence + Hermes synthesis + citation validation."""
    from pipeline.semantic_cache import semantic_cache

    owner = _conversation_owner(request)
    if req.stream:
        return _sse_stream(_canonical_answer_events(req.query, req, owner))

    # Run research pipeline — P4 resolves follow-ups first so the
    # pipeline executes the standalone query.
    resolution = await _resolve_conversation_query(req, owner)
    query_id = f"srch_{uuid.uuid4().hex[:12]}"

    # P5: answer cache — same layer as /v1/search, namespaced per endpoint
    # (response shapes differ) with a fresh query_id on every response.
    if settings.semantic_cache_enabled:
        hit = await semantic_cache.get(
            resolution.query,
            mode=req.mode or "balanced",
            lang=req.language or "en",
            freshness_class=_FRESHNESS_TO_CLASS.get(req.freshness or "", "medium"),
            key_suffix=f"ep:answer|cit:{int(bool(req.citations))}",
        )
        if hit is not None:
            cached = dict(hit.value)
            cached["query_id"] = query_id
            cached["cache"] = {"hit": True, "layer": hit.layer}
            cached.update(_conversation_fields(req, resolution))
            await _persist_conversation_turn(req, resolution, cached, owner)
            return cached

    context = ResearchContext(query=resolution.query, mode=req.mode or "balanced")
    result = await run_research(context, language=req.language, freshness=req.freshness)

    # Build response (§21 contract)
    response = {
        "query_id": query_id,
        "answer": result["answer"],
        "sources": result["sources"],
        "conflicts": result.get("conflicts", []),
        "coverage": _claims_coverage(result),
        "confidence": result.get("confidence", 0.0),
        "verified": _all_claims_verified(result),
        "timings": result.get("timings", {}),
    }
    if req.citations:
        response["citations"] = result.get("citations", [])
    if settings.semantic_cache_enabled:
        storable = {k: v for k, v in response.items() if k != "query_id"}
        await semantic_cache.set(
            resolution.query,
            storable,
            mode=req.mode or "balanced",
            lang=req.language or "en",
            freshness_class=_FRESHNESS_TO_CLASS.get(req.freshness or "", "medium"),
            key_suffix=f"ep:answer|cit:{int(bool(req.citations))}",
        )
    response.update(_conversation_fields(req, resolution))
    await _persist_conversation_turn(req, resolution, result, owner)
    return response


@router.post("/news")
async def news(req: SearchRequest):
    """POST /v1/news — news-specific search with realtime freshness."""
    orchestrator = _get_orchestrator()
    profile = orchestrator.query_understanding.analyze(req.query)

    # Force news categories
    from core.provider_registry import ProviderSearchQuery
    from models import SearchCategory

    sq = ProviderSearchQuery(
        query=req.query,
        categories=[SearchCategory.news],
        max_results=req.max_results,
        lang=profile.language,
    )

    results = await orchestrator._search_query(
        sq, budget=SearchBudget.for_mode("fast"), profile=profile, mode="fast"
    )
    return {
        "query": req.query,
        "results": [
            {
                "url": r.url,
                "title": r.title,
                "description": r.description,
                "published_at": r.published_at,
            }
            for r in results
        ],
        "count": len(results),
    }


async def _live_image_rows(req: SearchRequest, orchestrator) -> list[dict]:
    """Image lane on live web providers (P11): SearXNG ``categories=images``
    through the federation executor, DDGS as backfill. Same breaker/health
    path as ``/v1/search``; per-provider failure degrades to the other."""
    from core.federation import FederatedExecutor
    from core.provider_health import Admission
    from core.provider_registry import ProviderSearchQuery
    from models import SearchCategory
    from providers.base import SearchContext

    executor = FederatedExecutor(orchestrator.monitor)
    ctx = SearchContext(language=req.language or "vi", mode="normal")
    sq = ProviderSearchQuery(
        query=req.query,
        categories=[SearchCategory.images],
        max_results=req.max_results,
        lang=req.language or "vi",
    )

    rows: list[dict] = []
    for name in ("searxng", "ddgs"):
        provider = orchestrator.registry.get(name)
        if provider is None:
            continue
        admission = orchestrator.monitor.allow(name)
        if admission is Admission.deny:
            continue
        ctx.probe = admission is Admission.probe
        spec = orchestrator.registry.spec(name)
        results, _report = await executor.call_provider(
            name,
            provider,
            sq,
            ctx,
            spec_timeout_s=float(getattr(spec, "timeout_s", None) or 15.0),
            target_latency_ms=float(getattr(spec, "target_latency_ms", None) or 3000.0),
        )
        for r in results:
            src = r.thumbnail or ""
            if not src:
                continue
            rows.append(
                {
                    "image_id": f"web_{r.fingerprint or len(rows)}",
                    "score": r.score or 0.0,
                    "src_url": src,
                    "page_url": r.url,
                    "alt": r.title or "",
                    "caption": r.snippet or "",
                    "title": r.title or "",
                    "source": name,
                }
            )
        if rows:
            break
    return rows


@router.post("/images")
async def images(req: SearchRequest):
    """POST /v1/images — image search: own corpus first, live web backfill.

    Qdrant image collection (when enabled) supplies corpus hits; thin or
    disabled corpus falls through to the live image lane so the endpoint
    still answers before the CLIP collection is populated.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    if settings.qdrant_images_enabled:
        try:
            from pipeline.image_search import ImageSearchService
            from qdrant.client import QdrantClient

            svc = ImageSearchService(
                qdrant_client=QdrantClient(),
                collection=settings.qdrant_collection_images,
            )
            hits = await svc.search_by_text(req.query, top_k=req.max_results)
        except Exception:  # noqa: BLE001 — corpus lane is optional
            hits = []
        for h in hits:
            key = h.src_url or h.page_url
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "image_id": h.image_id,
                    "score": h.score,
                    "src_url": h.src_url,
                    "page_url": h.page_url,
                    "alt": h.alt,
                    "caption": h.caption,
                    "title": h.caption or h.alt,
                    "source": "corpus",
                }
            )

    if len(rows) < req.max_results:
        orchestrator = _get_orchestrator()
        for row in await _live_image_rows(req, orchestrator):
            key = row["src_url"] or row["page_url"]
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= req.max_results:
                break

    out = {"query": req.query, "results": rows, "count": len(rows)}
    if not rows:
        out["note"] = "No image results (corpus empty, live lane unavailable)"
    return out


_ANCHOR_RE = re.compile(
    r"(?:gần|near|quanh|ở|tại|trong|khu vực|around)\s+(.+?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


async def _resolve_geo_anchor(query: str) -> tuple[GeoPoint | None, str]:
    """Resolve a query's location phrase to coordinates (P7).

    Entity resolution first ("Sài Gòn" → canonical name → geocode); then
    the text after proximity markers ("gần chợ Bến Thành" → geocode the
    tail). The local admin gazetteer (P14) answers before Nominatim —
    it never needs the network and carries former-province anchors.
    Returns ``(point, resolved_from)``.
    """
    for ent in resolve_entities(query):
        if ent.id.startswith("loc:"):
            point = await admin_anchor(ent.canonical) or await geocode(ent.canonical)
            if point is not None:
                return point, "entity"
    m = _ANCHOR_RE.search(query or "")
    if m:
        phrase = m.group(1).strip()
        if 1 < len(phrase) <= 120:
            point = await admin_anchor(phrase) or await geocode(phrase)
            if point is not None:
                return point, "phrase"
    return None, ""


@router.post("/business/search", response_model=BusinessSearchResponse)
async def business_search(req: BusinessSearchRequest):
    """Local business search: anchor → PostGIS → OSM → web-extract."""
    store = BusinessStore()
    entities: list[BusinessEntity] = []
    lanes: list[str] = []
    lat, lon = req.lat, req.lon
    anchor: GeoAnchor | None = None

    if lat is None or lon is None:
        point, resolved_from = await _resolve_geo_anchor(req.query)
        if point is not None:
            lat, lon = point.lat, point.lon
            anchor = GeoAnchor(
                name=point.name,
                lat=point.lat,
                lon=point.lon,
                display_name=point.display_name,
                resolved_from=resolved_from,
            )

    if lat is None or lon is None:
        observe_degraded("anchor_unresolved")
    elif not store._available:
        observe_degraded("business_store_unavailable")

    if lat is not None and lon is not None and store._available:
        geo = await store.search_nearby(
            lat,
            lon,
            req.radius_km,
            req.category,
            req.limit,
        )
        if geo:
            lanes.append("geo")
            entities.extend(geo)

    if lat is not None and lon is not None and len(entities) < req.limit:
        local = await osm_pois_nearby(
            lat,
            lon,
            req.radius_km,
            tag_kv=osm_tag_kv(req.category or req.query),
            limit=req.limit - len(entities),
        )
        if local:
            lanes.append("osm_local")
            seen = {(e.name, e.source_url) for e in entities}
            for e in local:
                if (e.name, e.source_url) not in seen and len(entities) < req.limit:
                    entities.append(e)
                    seen.add((e.name, e.source_url))

    if lat is not None and lon is not None and len(entities) < req.limit:
        osm = await overpass_amenities(
            lat,
            lon,
            req.radius_km,
            osm_tag_for(req.category or req.query),
            limit=req.limit - len(entities),
        )
        if osm:
            lanes.append("osm")
            seen = {(e.name, e.source_url) for e in entities}
            for e in osm:
                if (e.name, e.source_url) not in seen and len(entities) < req.limit:
                    entities.append(e)
                    seen.add((e.name, e.source_url))

    needed = max(0, req.limit - len(entities))
    if needed > 0:
        orchestrator = _get_orchestrator()
        pack = await orchestrator.search(req.query, "fast")
        sources = [s for s in pack.sources or [] if s.content]
        contents = [s.content for s in sources]
        urls = [s.url for s in sources]
        extras = await extract_business_batch(
            contents,
            req.query,
            llm=orchestrator.inference,
            source_urls=urls,
        )
        lanes.append("web")
        seen = {(e.name, e.source_url) for e in entities}
        for e in extras:
            if (e.name, e.source_url) not in seen and len(entities) < req.limit:
                entities.append(e)
                seen.add((e.name, e.source_url))

    evidence = [e.source_url for e in entities if e.source_url]
    observe_search("/v1/business/search", "local", len(entities))
    return BusinessSearchResponse(
        query=req.query,
        lat=lat,
        lon=lon,
        radius_km=req.radius_km,
        provider="+".join(lanes) or "none",
        anchor=anchor,
        entities=entities,
        evidence=evidence,
        count=len(entities),
    )


# ─── P14A: Vietnam administrative graph ──────────────────────────────────────

_resolver = None


def _get_admin_resolver():
    """Lazy seed-backed admin resolver (degrades to unresolvable if missing)."""
    global _resolver
    if _resolver is None:
        from core.geo_resolver import GeoResolver, get_resolver
        from storage.admin_store import AdminGraph

        try:
            _resolver = get_resolver()
        except OSError:
            _resolver = GeoResolver(AdminGraph())
    return _resolver


def _unit_out(u) -> AdminUnitOut:
    return AdminUnitOut(
        key=u.key,
        unit_id=u.unit_id,
        code=u.code,
        name=u.name,
        type=u.type,
        admin_level=u.admin_level,
        status=u.status,
        valid_from=u.valid_from,
        valid_to=u.valid_to,
        source=u.source,
    )


@router.get("/admin/resolve", response_model=AdminResolveResponse)
async def admin_resolve(q: str):
    """Resolve an address/place string onto the administrative graph.

    Historical inputs ("Yên Dũng, Bắc Giang") return both the matched
    historical units and the current-era units reached via transition
    edges, with the provenance-bearing path between them.
    """
    res = _get_admin_resolver().resolve(q)
    return AdminResolveResponse(
        query=res.query,
        status=res.status,
        confidence=res.confidence,
        matched=[_unit_out(u) for u in res.matched],
        current=[_unit_out(u) for u in res.current],
        path=[
            AdminRelationOut(
                from_key=e.from_key,
                to_key=e.to_key,
                relation_type=e.relation_type,
                effective_date=e.effective_date,
                source=e.source or "",
            )
            for e in res.path
        ],
        ambiguity=[_unit_out(u) for u in res.ambiguity],
    )


@router.get("/admin/lookup", response_model=list[AdminUnitOut])
async def admin_lookup(lat: float, lon: float):
    """Point → current administrative units (commune first).

    PostGIS ST_Contains when seeded into Postgres; otherwise the bundled
    seed's simplified GeoJSON boundaries (P14B).
    """
    from storage.admin_store import DictAdminStore, PgAdminStore

    units = await PgAdminStore().units_containing(lat, lon)
    if not units:
        units = await DictAdminStore.from_seed().units_containing(lat, lon)
    return [_unit_out(u) for u in units]


# ─── P15: raw source ingestion (staging — never canonical) ──────────────────


class IngestRequest(BaseModel):
    """POST /v1/ingest/{provider} — push provider records into raw staging.

    ``entries`` are verbatim provider records (scraper NDJSON lines decoded,
    or document rows for web_corpus). The adapter contract is batch
    acquisition — files land via scripts/ingest.py; this endpoint is for
    direct record pushes and small uploads.
    """

    entries: list[dict] = Field(min_length=1, max_length=10000)
    parameters: dict = Field(default_factory=dict)


class IngestResult(BaseModel):
    run_id: int | None = None
    provider: str
    status: str | None = None  # done|failed|aborted — non-done means fatal
    resume_of: int | None = None
    adapter_version: str | None = None
    source_dataset: dict = Field(default_factory=dict)
    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    invalid: int = 0
    failed: int = 0
    errors: dict = Field(default_factory=dict)
    available: bool = True


@router.post("/ingest/{provider}", response_model=IngestResult)
async def ingest_provider(provider: str, req: IngestRequest):
    """Ingest raw provider records into ``place_source_records`` staging.

    Runs are recorded in ``ingestion_runs``; invalid records go to the
    dead-letter table instead of aborting the batch. Degrades to
    ``available=false`` when hub-postgres is unconfigured.
    """
    import json as _json

    from fastapi import HTTPException
    from ingestion.adapters.gmaps import GoogleMapsAdapter
    from ingestion.adapters.web_corpus import WebCorpusAdapter
    from ingestion.runner import run_ingestion

    if provider == "google_maps":
        adapter = GoogleMapsAdapter(lines=[_json.dumps(e) for e in req.entries])
    elif provider == "web_corpus":
        adapter = WebCorpusAdapter(docs=req.entries)
    else:
        raise HTTPException(
            status_code=400,
            detail="unsupported provider; use google_maps or web_corpus "
            "(osm ingest is file-based: scripts/ingest.py)",
        )
    result = await run_ingestion(adapter, parameters=req.parameters)
    if result is None:
        return IngestResult(provider=provider, available=False)
    return IngestResult(**result)


class IngestionRunOut(BaseModel):
    run_id: int
    provider: str
    status: str
    parameters: dict = Field(default_factory=dict)
    source_version: str | None = None
    adapter_version: str | None = None
    source_dataset: dict = Field(default_factory=dict)
    resume_of: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    records_seen: int = 0
    records_new: int = 0
    records_changed: int = 0
    records_unchanged: int = 0
    records_invalid: int = 0
    records_failed: int = 0
    checkpoint: dict = Field(default_factory=dict)
    cursor: str | None = None
    error_summary: dict = Field(default_factory=dict)


@router.get("/ingest/runs", response_model=list[IngestionRunOut])
async def ingest_runs(limit: int = 50):
    """Recent ingestion runs (P15 observability)."""
    from storage import pg_client

    pool = await pg_client.get_pool()
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT * FROM ingestion_runs ORDER BY run_id DESC LIMIT $1",
        min(limit, 500),
    )
    return [
        IngestionRunOut(
            run_id=r["run_id"],
            provider=r["provider"],
            status=r["status"],
            parameters=r["parameters"] or {},
            source_version=r["source_version"],
            # P15.1 columns — tolerate a DB that hasn't run migration 008
            adapter_version=dict(r).get("adapter_version"),
            source_dataset=dict(r).get("source_dataset") or {},
            resume_of=dict(r).get("resume_of"),
            started_at=r["started_at"].isoformat() if r["started_at"] else None,
            completed_at=r["completed_at"].isoformat() if r["completed_at"] else None,
            records_seen=r["records_seen"],
            records_new=r["records_new"],
            records_changed=r["records_changed"],
            records_unchanged=r["records_unchanged"],
            records_invalid=r["records_invalid"],
            records_failed=r["records_failed"],
            checkpoint=r["checkpoint"] or {},
            cursor=r["cursor"],
            error_summary=r["error_summary"] or {},
        )
        for r in rows
    ]


# ─── P16: entity resolution → canonical place graph ─────────────────────


class ResolveRequest(BaseModel):
    """POST /v1/resolve — run entity resolution over staged sources."""

    provider: str | None = None
    since_id: int = 0
    batch_size: int = Field(default=200, ge=1, le=5000)
    threshold: float = Field(default=0.62, ge=0.0, le=1.0)
    resume_of: int | None = None  # resolution_runs.run_id


class ResolveResult(BaseModel):
    run_id: int = 0
    status: str
    resume_of: int | None = None
    resolver_version: str | None = None
    cursor: int = 0
    scanned: int = 0
    scored: int = 0
    created: int = 0
    merged: int = 0
    fields_written: int = 0
    errors: dict = Field(default_factory=dict)
    available: bool = True


@router.post("/resolve", response_model=ResolveResult)
async def resolve_sources(req: ResolveRequest):
    """Resolve ``place_source_records`` into ``canonical_places``.

    Merge-or-create per record with field-level provenance; resumable via
    ``resume_of``. Degrades to ``available=false`` without hub-postgres.
    """
    from resolution.runner import run_resolution
    from storage import pg_client

    pool = await pg_client.get_pool()
    if pool is None:
        return ResolveResult(status="unavailable", available=False)
    result = await run_resolution(
        pool,
        provider=req.provider,
        since_id=req.since_id,
        batch_size=req.batch_size,
        threshold=req.threshold,
        resume_of=req.resume_of,
    )
    return ResolveResult(**result)


class PlaceOut(BaseModel):
    place_id: int
    business_id: int | None = None
    name: str = ""
    canonical_name: str
    canonical_category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    opening_hours: dict | None = None
    lat: float | None = None
    lon: float | None = None
    admin_unit_id: int | None = None
    status: str
    confidence: float
    source_count: int
    # P17 serving additions — all optional so the P16 shape stays valid.
    distance_m: float | None = None
    freshness_score: float = 0.0
    last_verified_at: str | None = None
    aliases: list[str] = Field(default_factory=list)
    score_debug: dict | None = None
    # P2.0 rich place-card fields — promoted from source raw_payload at
    # resolution; open_now/map_url are derived at serve time. All nullable.
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    open_now: bool | None = None
    map_url: str | None = None
    primary_image_url: str | None = None
    images: list[str] = Field(default_factory=list)


def _set_place_headers(response: Response | None, meta) -> None:
    """Expose lane/degradation observability without changing the
    ``list[PlaceOut]`` body shape."""
    if response is None:
        return
    response.headers["X-Places-Lanes"] = ",".join(meta.lanes) or "none"
    response.headers["X-Places-Cache"] = "hit" if meta.cache_hit else "miss"
    response.headers["X-Places-Ms"] = f"{meta.total_ms:.1f}"
    if meta.degraded:
        response.headers["X-Places-Degraded"] = ",".join(meta.degraded)


def _get_places_service():
    from serving.places.service import get_place_service

    return get_place_service()


@router.get("/places/search", response_model=list[PlaceOut])
async def places_search(
    response: Response = None,
    q: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 2000.0,
    admin_unit_id: int | None = None,
    category: str | None = None,
    status: str | None = None,
    bbox: str | None = None,
    admin_contains: bool = False,
    debug: bool = False,
    limit: int = 20,
    open_now: bool | None = None,
    min_rating: float | None = None,
    price_level: str | None = None,
    sort: str | None = None,
):
    """Search canonical places (P17 serving path).

    OpenSearch candidates + PostGIS geo precision + weighted fusion; each
    dependency degrades independently (cache → OS → PostGIS fallback).
    ``debug=1`` adds per-result score components. P17.1 rich filters:
    ``open_now`` (request-time verdict), ``min_rating``, ``price_level``,
    and ``sort`` (``distance`` | ``rating`` | ``popularity``).
    """
    rows, meta = await _get_places_service().search(
        q=q,
        lat=lat,
        lon=lon,
        radius_m=radius_m,
        category=category,
        admin_unit_id=admin_unit_id,
        status=status,
        bbox=bbox,
        admin_contains=admin_contains,
        debug=debug,
        limit=limit,
        open_now=open_now,
        min_rating=min_rating,
        price_level=price_level,
        sort=sort,
    )
    _set_place_headers(response, meta)
    return [PlaceOut(**r) for r in rows]


class PlaceSuggestionOut(BaseModel):
    place_id: int
    name: str
    canonical_category: str | None = None
    lat: float | None = None
    lon: float | None = None
    status: str = "unknown"
    distance_m: float | None = None


@router.get("/places/autocomplete", response_model=list[PlaceSuggestionOut])
async def places_autocomplete(
    response: Response = None,
    q: str = "",
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 10,
):
    """Prefix completion over place names/aliases (P17).

    Edge-ngram index fields + optional geo decay; permanently closed
    places never surface.
    """
    rows, meta = await _get_places_service().autocomplete(q=q, lat=lat, lon=lon, limit=limit)
    _set_place_headers(response, meta)
    return [PlaceSuggestionOut(**r) for r in rows]


class FieldProvenanceOut(BaseModel):
    field: str
    provider: str
    value: dict | str | float | int | list | None = None
    weight: float
    observed_at: str | None = None
    chosen: bool


class PlaceDetailOut(PlaceOut):
    sources: list[dict] = Field(default_factory=list)
    provenance: list[FieldProvenanceOut] = Field(default_factory=list)
    degraded: bool = False


@router.get("/places/{place_id}", response_model=PlaceDetailOut)
async def place_detail(place_id: int, response: Response = None):
    """Canonical place + source lineage + per-field provenance (P17).

    Serves the cache → Postgres → index fallback chain. Postgres remains
    the source of truth; when it is down the indexed document answers in
    degraded form (no provenance). When Postgres is unreachable AND the
    index cannot confirm the place, the endpoint answers 503 rather than
    an unverifiable 404.
    """
    from fastapi import HTTPException

    res = await _get_places_service().get_place(place_id)
    _set_place_headers(response, res.meta)
    if res.status == "unavailable":
        raise HTTPException(status_code=503, detail="postgres unavailable")
    if res.status != "ok" or res.payload is None:
        raise HTTPException(status_code=404, detail="place not found")
    return PlaceDetailOut(**res.payload)


class ReindexRequest(BaseModel):
    """POST /v1/places/reindex — synchronize the places read index."""

    mode: str = "incremental"  # incremental | full | reconcile | status
    batch_size: int = Field(default=1000, ge=1, le=20000)
    max_batches: int | None = Field(default=None, ge=1)


class ReindexResult(BaseModel):
    status: str
    mode: str = ""
    scanned: int = 0
    indexed: int = 0
    failed: int = 0
    available: bool = True
    detail: dict = Field(default_factory=dict)


@router.post("/places/reindex", response_model=ReindexResult)
async def places_reindex(req: ReindexRequest):
    """Sync the serving index from the canonical graph (P17).

    ``incremental`` resumes from the durable cursor; ``full`` rebuilds a
    fresh concrete index and swaps the alias atomically; ``reconcile``
    drops index docs whose canonical row no longer exists. Canonical data
    is never mutated.
    """
    from serving.places.indexer import PlaceIndexer
    from storage import pg_client

    pool = await pg_client.get_pool()
    if pool is None:
        return ReindexResult(status="unavailable", available=False)
    indexer = PlaceIndexer(pool)
    try:
        if req.mode == "full":
            res = await indexer.rebuild(batch_size=req.batch_size)
        elif req.mode == "reconcile":
            res = await indexer.reconcile(batch_size=req.batch_size)
        elif req.mode == "status":
            res = await indexer.status()
        else:
            res = await indexer.sync(batch_size=req.batch_size, max_batches=req.max_batches)
    except Exception as exc:  # noqa: BLE001 — report, never corrupt
        return ReindexResult(status="failed", mode=req.mode, detail={"error": str(exc)})
    return ReindexResult(
        status=res.get("status", "done"),
        mode=res.get("mode", req.mode),
        scanned=res.get("scanned", 0),
        indexed=res.get("indexed", 0),
        failed=res.get("failed", 0),
        detail=res,
    )


class ResolutionRunOut(BaseModel):
    run_id: int
    status: str
    parameters: dict = Field(default_factory=dict)
    resolver_version: str | None = None
    resume_of: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    records_scanned: int = 0
    pairs_scored: int = 0
    places_created: int = 0
    places_merged: int = 0
    fields_written: int = 0
    cursor: int | None = None
    error_summary: dict = Field(default_factory=dict)


@router.get("/resolution/runs", response_model=list[ResolutionRunOut])
async def resolution_runs(limit: int = 50):
    """Recent resolution runs (P16 observability)."""
    from storage import pg_client

    pool = await pg_client.get_pool()
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT * FROM resolution_runs ORDER BY run_id DESC LIMIT $1",
        min(limit, 500),
    )
    return [
        ResolutionRunOut(
            run_id=r["run_id"],
            status=r["status"],
            parameters=r["parameters"] or {},
            resolver_version=r["resolver_version"],
            resume_of=r["resume_of"],
            started_at=r["started_at"].isoformat() if r["started_at"] else None,
            completed_at=r["completed_at"].isoformat() if r["completed_at"] else None,
            records_scanned=r["records_scanned"],
            pairs_scored=r["pairs_scored"],
            places_created=r["places_created"],
            places_merged=r["places_merged"],
            fields_written=r["fields_written"],
            cursor=r["cursor"],
            error_summary=r["error_summary"] or {},
        )
        for r in rows
    ]
