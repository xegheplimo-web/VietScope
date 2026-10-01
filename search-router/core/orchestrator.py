"""SearchOrchestrator — canonical *search* orchestration for the /v1 API.

Provider-registry fan-out with per-request ``SearchBudget`` and
``QueryUnderstanding``, producing ``EvidencePack`` results for
``/v1/search`` (raw), ``/v1/research``, ``/v1/news`` and
``/v1/business/search``.

Distinct from ``agent.orchestrator.run_research`` — the research-agent
state machine (intent → plan → multi-query search → rerank → scrape →
evidence → gap loop → synthesize → verify) behind ``/v1/search?mode=`` and
``/v1/answer``.  ``pipeline.router`` is the deprecated legacy-path
orchestrator.  See ``docs/phase0-dedup-map.md``.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse

from canonical.url import canonical_url as canonicalize_url
from config import settings
from models import (
    EvidencePack,
    SearchCategory,
    Source,
    result_fingerprint,
)
from pipeline import reader
from pipeline.reader import ReadResult
from providers.base import result_domain
from ranking.authority import authority_for, authority_score
from ranking.quality import FRESHNESS_HALFLIFE_DAYS

from core.budget import SearchBudget
from core.federation import FanOutResult, FederatedExecutor
from core.inference_gateway import InferenceGateway, get_inference_gateway
from core.provider_health import get_provider_health_monitor
from core.provider_registry import (
    ProviderRegistry,
    ProviderSearchQuery,
    ProviderSearchResult,
)
from core.query_understanding import QueryUnderstanding
from core.source_router import SourceRouter


class SearchOrchestrator:
    def __init__(
        self,
        registry: ProviderRegistry,
        inference: InferenceGateway | None = None,
    ) -> None:
        self.registry = registry
        self.inference = inference or get_inference_gateway()
        self.query_understanding = QueryUnderstanding()
        # Phase 2: provider health monitor + adaptive source router.
        self.monitor = get_provider_health_monitor()
        self.router = SourceRouter(self.monitor)

    @staticmethod
    def _canonical(url: str) -> str:
        p = urlparse(url)
        netloc = p.netloc.lower().removeprefix("www.")
        path = p.path or "/"
        if path != "/":
            path = path.rstrip("/")
        tracking = {"fbclid", "gclid", "ref", "source"}
        query = urlencode(
            sorted(
                (key, value)
                for key, value in parse_qsl(p.query, keep_blank_values=True)
                if not key.lower().startswith("utm_") and key.lower() not in tracking
            ),
            doseq=True,
        )
        return f"{netloc}{path}" + (f"?{query}" if query else "")

    @staticmethod
    def _authority(domain: str) -> float:
        return authority_score(domain)

    @staticmethod
    def _freshness_bonus(published_at: str | None, vertical: str | None = None) -> float:
        if not published_at:
            return 0.0
        text = str(published_at).strip()
        dt = None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - dt.astimezone(UTC)).total_seconds()
        except ValueError:
            text2 = text.split(".")[0].replace("Z", "")
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    dt = datetime.strptime(text2, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return 0.0
            age = (datetime.now(UTC).replace(tzinfo=None) - dt).total_seconds()
        if age < 0:
            return 0.0
        # P5-VN: per-vertical freshness — a graded decay (0.3→0 at 2× the
        # lane's half-life) for known lanes. A day-old market price is
        # stale; an 800-day-old nghị định still carries a freshness bonus
        # because legal texts stay current for years. Unknown lanes keep
        # the legacy day/week buckets exactly.
        halflife = FRESHNESS_HALFLIFE_DAYS.get(str(vertical or ""))
        if halflife is not None:
            return round(0.3 * max(0.0, 1.0 - age / (2.0 * halflife * 86400.0)), 4)
        if age < 86400:
            return 0.3
        if age < 604800:
            return 0.15
        return 0.0

    def _normalize(self, results: list[tuple[str, ProviderSearchResult]]) -> list[Source]:
        # ``ProviderSearchResult`` is the Phase-2 ``ProviderResult`` schema:
        # ``snippet``/``published_at`` (with legacy field names tolerated for
        # any provider still emitting SearchResultItem-shaped payloads).
        sources = []
        for pname, r in results:
            snippet = getattr(r, "snippet", None) or getattr(r, "description", "") or ""
            published = getattr(r, "published_at", None) or getattr(r, "published_date", None)
            # Metasearch wrappers (gnews) carry the true publisher domain in
            # metadata — it beats the wrapper's netloc for authority/trust,
            # while ``url`` keeps the working discovery link untouched.
            domain = result_domain(r.url, getattr(r, "metadata", None))
            c_url = r.canonical_url or canonicalize_url(r.url)
            fingerprint = r.fingerprint or result_fingerprint(c_url, r.title or "", snippet)
            sources.append(
                Source(
                    source_id=(
                        f"{pname}:"
                        f"{hashlib.sha256(self._canonical(r.url).encode()).hexdigest()[:12]}"
                    ),
                    url=r.url,
                    canonical_url=c_url,
                    title=r.title or "",
                    fingerprint=fingerprint,
                    domain=domain,
                    description=snippet,
                    published_at=published,
                    score=float(r.score or 0.0),
                    search_provider=pname,
                    content_provider="none",
                    content_length=0,
                    retrieval_observations=list(r.retrieval_observations or []),
                    source_lane=str(getattr(r, "source_type", "") or "") or None,
                )
            )
        return sources

    def _dedupe(self, sources: list[Source]) -> list[Source]:
        seen: dict[str, Source] = {}
        for s in sources:
            key = self._canonical(s.url)
            existing = seen.get(key)
            if existing is None or s.score > existing.score:
                seen[key] = s
        return list(seen.values())

    def _rank(
        self,
        sources: list[Source],
        query: str,
        freshness_required: bool = False,
    ) -> list[Source]:
        qterms = set(re.findall(r"\w+", query.lower()))
        for s in sources:
            text = f"{s.title} {s.description} {s.content}".lower()
            overlap = sum(1 for t in qterms if t in text)
            relevance = overlap / max(len(qterms), 1)
            authority = authority_for(s.domain, s.source_lane)
            freshness = self._freshness_bonus(s.published_at, s.source_lane)
            engine = min(1.0, float(s.score or 0.0))
            if freshness_required:
                composite = relevance * 0.35 + authority * 0.30 + freshness * 0.25 + engine * 0.10
            else:
                composite = relevance * 0.35 + authority * 0.35 + freshness * 0.15 + engine * 0.15
            if authority < 0.25:
                composite *= 0.5
            s.score = composite
        sources.sort(key=lambda x: x.score, reverse=True)
        return sources

    def _coverage(self, sources: list[Source]) -> float:
        total = sum(len(s.content or "") for s in sources)
        return min(1.0, total / 3000.0)

    def _confidence(self, sources: list[Source]) -> float:
        if not sources:
            return 0.0
        return min(1.0, sum(s.score for s in sources) / len(sources) / 2.0)

    def _stop(self, pack: EvidencePack) -> bool:
        return pack.coverage >= 0.9 and pack.confidence >= 0.85

    async def _route_query(
        self,
        sq: ProviderSearchQuery,
        budget: SearchBudget,
        mode: str,
        profile,
        progress: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> tuple[list[tuple[str, ProviderSearchResult]], list[str], FanOutResult]:
        """Adaptive parallel fan-out across the registry.

        Returns (provider-tagged results, names attempted, raw fan-out result).
        """
        plan = self.router.plan(
            sq.query,
            profile,
            self.registry,
            mode=mode,
            remaining_queries=max(budget.remaining_queries, 0),
        )
        if progress:
            await progress(
                "searching",
                {
                    "query": sq.query,
                    "providers": plan.provider_names,
                    "skipped": dict(plan.skipped),
                    "freshness": profile.freshness_required,
                },
            )
        executor = FederatedExecutor(self.monitor, budget=budget)
        fanout = await executor.execute(
            plan,
            self.registry,
            sq,
            lambda pick: self.router.context_for(pick, profile, mode),
        )
        # ProviderResult.source always carries the provider name (executor
        # fills it via to_provider_result for legacy/dict payloads too).
        flat = [(r.source or "", r) for r in fanout.results]
        return flat, list(fanout.attempted), fanout

    async def _search_query(
        self,
        query: str,
        budget: SearchBudget,
        profile,
        mode: str,
        progress: Callable[[str, dict], Awaitable[None]] | None = None,
        overrides: dict | None = None,
    ) -> list[Source]:
        if not self.registry or not self.registry.all():
            return []

        categories = (
            [SearchCategory.news, SearchCategory.general]
            if profile.freshness_required
            else [SearchCategory.general]
        )
        if overrides and "categories" in overrides:
            categories = overrides["categories"]
        max_results = (
            overrides["max_results"]
            if overrides and "max_results" in overrides
            else settings.max_results
        )
        sq = ProviderSearchQuery(
            query=query,
            categories=categories,
            max_results=max_results,
            lang=profile.language,
            safe=False,
        )

        flat, _, _fanout = await self._route_query(sq, budget, mode, profile, progress)

        if not flat:
            return []

        sources = self._normalize(flat)
        sources = self._dedupe(sources)
        sources = self._rank(sources, query, profile.freshness_required)
        return sources

    async def _fetch_top(self, top: list[Source], budget: SearchBudget) -> list[Source]:
        if not top:
            return top
        top = top[: budget.remaining_fetches]
        if not top:
            return top
        budget.use_fetch(len(top))

        urls = [s.url for s in top]
        read_results: list[ReadResult] = []
        try:
            read_results = list(
                await reader.read_batch(
                    urls,
                    timeout=settings.scrape_timeout,
                    max_concurrent=3,
                )
            )
        except Exception:
            read_results = []

        read_by_url = {rr.url: rr for rr in read_results}
        for s in top:
            rr = read_by_url.get(s.url)
            if rr is None:
                s.content = ""
                s.error = "fetch failed"
                continue
            if rr.success:
                s.content = rr.text or ""
                s.content_length = len(s.content)
                s.content_provider = rr.tier or "reader"
                # L17: backfill fetched content into the internal indexes
                # (OpenSearch + Qdrant) — fire-and-forget, never blocks.
                try:
                    from workers.indexing_worker import submit_document

                    submit_document(s.url, s.title, s.content, published_at=s.published_at)
                except Exception:  # noqa: BLE001 — indexing is best-effort
                    pass
                metadata = rr.metadata.get("firecrawl_meta") or {}
                s.published_at = (
                    s.published_at
                    or rr.metadata.get("published_at")
                    or next(
                        (
                            str(metadata[key])
                            for key in ("publishedTime", "datePublished", "published_at")
                            if metadata.get(key)
                        ),
                        None,
                    )
                )
            else:
                s.content = ""
                s.error = rr.error
                s.content_provider = "reader"

        return top

    async def search(
        self,
        query: str,
        mode: str = "normal",
        progress: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> EvidencePack:
        """Run the full research loop. Optional ``progress`` async callback
        receives (event, data) — used by /v1/research/stream for real SSE
        progress (review-codex #3: claim-aware stop criteria + progress)."""
        profile = self.query_understanding.analyze(query)
        budget = SearchBudget.for_mode(mode)

        if progress:
            await progress(
                "planning",
                {
                    "query": query,
                    "mode": mode,
                    "intent": profile.intent,
                    "freshness_required": profile.freshness_required,
                    "budget": budget.to_model().model_dump(mode="json"),
                },
            )

        sources = await self._search_query(query, budget, profile, mode, progress)

        if not sources:
            return EvidencePack(answer="", budget_used=budget.to_model())

        top_n = min(budget.max_fetches, len(sources))
        top = sources[:top_n]

        if progress:
            await progress(
                "fetching",
                {
                    "to_fetch": len(top),
                    "total_sources": len(sources),
                },
            )

        await self._fetch_top(top, budget)

        if progress:
            await progress(
                "fetched",
                {
                    "fetched": len([s for s in top if s.content]),
                    "failed": len([s for s in top if s.error]),
                },
            )

        pack = self._build_pack(sources, budget)
        pack = await self._follow_up_loop(query, pack, sources, budget, profile, mode, progress)
        pack.budget_used = budget.to_model()
        return pack

    async def research(
        self,
        query: str,
        mode: str = "normal",
        max_hops: int | None = None,
        progress: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> EvidencePack:
        """Multi-hop research: decompose the query, search each sub-query,
        merge sources, and fetch the top results for the original query."""
        profile = self.query_understanding.analyze(query)
        budget = SearchBudget.for_mode(mode)

        mode_hops = {"fast": 1, "normal": 2, "deep": 3}
        if max_hops is None:
            max_hops = mode_hops.get(mode.lower(), 2)

        sub_queries = self.query_understanding.decompose(query, max_hops)

        if progress:
            await progress(
                "planning",
                {
                    "query": query,
                    "mode": mode,
                    "max_hops": max_hops,
                    "sub_queries": sub_queries,
                    "intent": profile.intent,
                    "freshness_required": profile.freshness_required,
                    "budget": budget.to_model().model_dump(mode="json"),
                },
            )

        all_sources: list[Source] = []
        for sq in sub_queries:
            if budget.remaining_queries <= 0:
                break
            sub_sources = await self._search_query(sq, budget, profile, mode, progress)
            if sub_sources:
                all_sources.extend(sub_sources)

        if not all_sources:
            return EvidencePack(answer="", budget_used=budget.to_model())

        all_sources = self._dedupe(all_sources)
        all_sources = self._rank(all_sources, query, profile.freshness_required)

        top_n = min(budget.max_fetches, len(all_sources))
        top = all_sources[:top_n]

        if progress:
            await progress(
                "fetching",
                {
                    "to_fetch": len(top),
                    "total_sources": len(all_sources),
                },
            )

        await self._fetch_top(top, budget)

        if progress:
            await progress(
                "fetched",
                {
                    "fetched": len([s for s in top if s.content]),
                    "failed": len([s for s in top if s.error]),
                },
            )

        pack = self._build_pack(all_sources, budget)
        pack.budget_used = budget.to_model()
        return pack

    def _build_pack(self, all_sources: list[Source], budget: SearchBudget) -> EvidencePack:
        all_sources.sort(key=lambda x: x.score, reverse=True)
        coverage = self._coverage(all_sources)
        confidence = self._confidence(all_sources)
        return EvidencePack(
            answer="",
            sources=all_sources,
            coverage=coverage,
            confidence=confidence,
            budget_used=budget.to_model(),
        )

    async def _follow_up_loop(
        self,
        query: str,
        pack: EvidencePack,
        existing: list[Source],
        budget: SearchBudget,
        profile,
        mode: str,
        progress: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> EvidencePack:
        followups = 0
        while (
            followups < budget.max_followups
            and budget.remaining_queries > 0
            and budget.remaining_fetches > 0
            and not self._stop(pack)
        ):
            budget.use_followup()
            followups += 1
            fq = query + " latest"
            if progress:
                await progress(
                    "followup",
                    {
                        "round": followups,
                        "query": fq,
                        "reason": "coverage/confidence below stop criteria",
                    },
                )
            sq = ProviderSearchQuery(
                query=fq,
                categories=[SearchCategory.news],
                max_results=5,
                lang=profile.language,
                safe=False,
            )
            flat, _, _fanout = await self._route_query(sq, budget, mode, profile)
            if not flat:
                continue
            new_sources = self._normalize(flat)
            new_sources = self._dedupe(new_sources)
            new_sources = self._rank(new_sources, fq, profile.freshness_required)
            seen = {self._canonical(s.url) for s in existing}
            for s in new_sources:
                if self._canonical(s.url) not in seen:
                    existing.append(s)
                    seen.add(self._canonical(s.url))
            top = new_sources[: budget.remaining_fetches]
            await self._fetch_top(top, budget)
            pack = self._build_pack(existing, budget)
        return pack
