"""Retriever — research-lane retrieval through the Source Federation Layer.

Fan-out is adaptive (Source Router picks providers per intent) and
health-gated (circuit-open providers are skipped). Results normalize to
``ProviderResult`` then map to ``SourceResult`` for the research agent.
"""

from config import settings
from models import SearchCategory
from research_models.research_state import SourceResult


async def _opensearch_lane(query: str, max_results: int) -> list[SourceResult]:
    """Internal index lane (v2.1 §9) — BM25 over web_passages.

    Returns [] when disabled/unreachable; live-web lanes stay authoritative.
    """
    if not settings.opensearch_enabled:
        return []
    try:
        from core.provider_registry import ProviderSearchQuery
        from providers.base import result_domain
        from providers.opensearch_index import OpenSearchIndexProvider

        provider = OpenSearchIndexProvider()
        items = await provider.search(ProviderSearchQuery(query=query, max_results=max_results))
        return [
            SourceResult(
                source_id=f"os_{i:03d}",
                url=r.url,
                title=r.title or "",
                description=r.description or "",
                domain=result_domain(r.url, r.metadata),
                score=r.score or 0.0,
                published_at=r.published_date,
                engine="opensearch",
            )
            for i, r in enumerate(items)
        ]
    except Exception:  # noqa: BLE001 — index lane is optional
        return []


async def retrieve(query: str, max_results: int = 10, lang: str = "vi") -> list[SourceResult]:
    """Retrieve via the federated registry (adaptive fan-out, parallel)."""
    try:
        from core.federation import FederatedExecutor
        from core.provider_health import get_provider_health_monitor
        from core.provider_registry import ProviderSearchQuery, create_default_registry
        from core.query_understanding import QueryUnderstanding
        from core.source_router import SourceRouter
        from providers.base import result_domain

        registry = create_default_registry()
        if not registry.all():
            return []

        profile = QueryUnderstanding().analyze(query)
        monitor = get_provider_health_monitor()
        router = SourceRouter(monitor)
        plan = router.plan(query, profile, registry, mode="normal")
        sq = ProviderSearchQuery(
            query=query,
            categories=[SearchCategory.general],
            max_results=max_results,
            lang=lang,
        )
        executor = FederatedExecutor(monitor)
        fanout = await executor.execute(
            plan, registry, sq, lambda p: router.context_for(p, profile, "normal")
        )

        sources: list[SourceResult] = []
        seen_urls: set[str] = set()
        for i, r in enumerate(fanout.results):
            if not r.url or r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            sources.append(
                SourceResult(
                    source_id=f"src_{i:03d}",
                    url=r.url,
                    title=r.title or "",
                    description=r.snippet or "",
                    domain=result_domain(r.url, r.metadata),
                    score=r.score or 0.0,
                    published_at=r.published_at,
                    engine=r.source or "",
                )
            )
        return sources
    except Exception:  # noqa: BLE001 — one failed query must not kill a round
        return []
