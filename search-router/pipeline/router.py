"""Query router — decides which providers to use based on query type.

DEPRECATED(phase0): this is the *legacy-path* orchestrator — query-family
classification + provider fan-out feeding ``pipeline.evidence_aggregator``
for the deprecated tool endpoints in ``main.py`` (``/search``, ``/answer``).
It is NOT one of the two canonical orchestrators: ``core.orchestrator``
(provider-registry + budget + evidence pack for ``/v1/*``) and
``agent.orchestrator`` (research-agent state machine for ``/v1/search?mode``
and ``/v1/answer``).  See ``docs/phase0-dedup-map.md``.
"""

import asyncio
import re
from dataclasses import dataclass, field

from models import SearchCategory, SearchResultItem

from observability.prometheus import observe_degraded, observe_search


def detect_query_type(query: str) -> str:
    """Classify query type: web | research | code | factual.

    Heuristic routing:
    - Code-related keywords → code
    - Deep research indicators → research
    - Simple factual → web
    - Default → web
    """
    query_lower = query.lower()

    # Code search indicators
    code_patterns = [
        r"\b(function|class|method|api|endpoint|bug|error|stack trace|exception)\b",
        r"\b(python|javascript|typescript|rust|golang|java|c\+\+|ruby|php)\b",
        r"\b(github|gitlab|npm|pip|cargo|crates?\.io)\b",
        r"[a-zA-Z_]\w*\.[a-zA-Z_]\w*\(",  # function call pattern
        r"```",
        r"\b(import|export|require|module|package)\b",
    ]
    for pattern in code_patterns:
        if re.search(pattern, query_lower):
            return "code"

    # Deep research indicators
    research_patterns = [
        r"\b(compare|comparison|analysis|deep dive|comprehensive|thorough)\b",
        r"\b(pros and cons|advantages|disadvantages|trade-?offs?)\b",
        r"\b(architecture|design pattern|best practices?|guide|tutorial)\b",
        r"\b(history of|evolution of|state of)\b",
        r"\b(research|study|investigate|analyze)\b",
    ]
    for pattern in research_patterns:
        if re.search(pattern, query_lower):
            return "research"

    return "web"


def get_categories_for_query(query: str) -> list[SearchCategory]:
    """Suggest search categories based on query content."""
    query_lower = query.lower()
    cats: list[SearchCategory] = [SearchCategory.general]

    if any(w in query_lower for w in ["image", "photo", "picture", "logo"]):
        cats.append(SearchCategory.images)

    if any(w in query_lower for w in ["news", "latest", "recent", "today", "2026", "2025"]):
        cats.append(SearchCategory.news)

    if any(w in query_lower for w in ["video", "youtube", "tutorial video"]):
        cats.append(SearchCategory.videos)

    if any(w in query_lower for w in ["science", "paper", "research", "academic", "arxiv"]):
        cats.append(SearchCategory.science)

    if any(w in query_lower for w in ["it", "server", "linux", "docker", "kubernetes", "devops"]):
        cats.append(SearchCategory.it)

    return cats


# ─── Search Orchestrator (wave-9) — extends the legacy router ────────────────
# The legacy router classified queries (web/research/code). The orchestrator
# goes one step further: it builds a provider plan per query and fans out the
# searches. Output feeds the Evidence Aggregator — never the answer directly.

try:
    from core.query_understanding import QueryUnderstanding as _QU
except Exception:  # pragma: no cover
    _QU = None

_CODE_RE = re.compile(
    r"\b(function|class|method|api|endpoint|import|export|require|"
    r"pip install|npm install|github|gitlab|stackoverflow|docs\.|"
    r"exception|traceback|snippet|implementation of)\b",
    re.IGNORECASE,
)
_CODE_LANG_RE = re.compile(
    r"\b(python|javascript|typescript|rust|golang|go\b|java|kotlin|swift|c\+\+|"
    r"ruby|php|sql|bash|shell|powershell|react|vue|django|fastapi|flask)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(research|paper|study|survey|analysis|arxiv|benchmark|empirical|"
    r"academic|thesis|literature|sota|state of the art|comparison of)\b",
    re.IGNORECASE,
)
_NEWS_RE = re.compile(
    r"\b(news|today|latest|breaking|hôm nay|mới nhất|tin nóng|vừa|"
    r"release|announce|launch|update|ra mắt)\b",
    re.IGNORECASE,
)
_TIME_RANGE_RE = re.compile(
    r"\b(2025|2026|this week|this month|last week|last month|tuần này|tháng này)\b",
    re.IGNORECASE,
)
_RESEARCH_KEYWORDS = frozenset(
    [
        "research",
        "paper",
        "study",
        "analysis",
        "algorithm",
        "implementation",
        "framework",
        "library",
        "benchmark",
        "performance",
        "comparison",
        "architecture",
        "design",
        "pattern",
        "paper",
        "arxiv",
        "survey",
        "review",
        "academic",
        "scientific",
        "model",
        "neural",
        "network",
        "machine",
        "learning",
        "deep",
        "learning",
        "transformer",
        "llm",
        "gpt",
        "bert",
        "embedding",
        "vector",
        "database",
        "distributed",
        "system",
        "consensus",
        "protocol",
        "raft",
        "paxos",
        "blockchain",
        "cryptography",
        "paper",
        "survey",
        "evaluation",
        "empirical",
        "theoretical",
    ]
)


@dataclass
class ProviderRun:
    """One provider invocation with its own parameters."""

    name: str
    query: str
    categories: list[SearchCategory] = field(default_factory=lambda: [SearchCategory.general])
    max_results: int = 10
    lang: str = "en"
    time_range: str | None = None
    repo: str | None = None  # code_search only
    variant: int = 0  # query-variant index (0 = original) for diagnostics


@dataclass
class OrchestrationPlan:
    """What the orchestrator decided for a query."""

    query: str
    family: str = "web"  # web | news | research | code
    runs: list[ProviderRun] = field(default_factory=list)
    reason: list[str] = field(default_factory=list)


def classify_query(query: str) -> str:
    """Return query family: code | research | news | web (default)."""
    q = query or ""
    if _CODE_RE.search(q) or _CODE_LANG_RE.search(q):
        return "code"
    if _RESEARCH_RE.search(q) or (set(re.findall(r"\w+", q.lower())) & _RESEARCH_KEYWORDS):
        return "research"
    if _NEWS_RE.search(q) or _TIME_RANGE_RE.search(q):
        return "news"
    return "web"


def _resolve_lang_auto(query: str, explicit: str) -> str:
    if explicit and explicit not in ("en", "auto"):
        return explicit
    try:
        if _QU is not None:
            return _QU().analyze(query).language
    except Exception:
        pass
    return "en"


def build_plan(
    query: str,
    *,
    type_hint: str = "web",
    lang: str = "en",
    max_results: int = 10,
    time_range: str | None = None,
) -> OrchestrationPlan:
    """Decide the provider mix for a query."""
    q = query or ""
    family = classify_query(q)
    if family == "web" and type_hint in ("news", "image"):
        family = type_hint

    resolved_lang = _resolve_lang_auto(q, lang)
    reason: list[str] = [f"family={family}", f"lang={resolved_lang}"]
    runs: list[ProviderRun] = []

    # Query rewriting: 1-3 deterministic variants (original always first).
    # searxng (the backbone) fans out over variants for wider recall; other
    # providers keep the original query only (arxiv/HN/code search match exact
    # identifiers better than rewritten text).
    try:
        from core.query_rewriter import rewrite_variants

        variants = rewrite_variants(q, family=family, lang=resolved_lang)
    except Exception:
        variants = [q]
    if not variants:
        variants = [q]
    reason.append(f"variants={len(variants)}")

    def _searxng_runs(
        cats: list[SearchCategory], limit: int, tr: str | None = None
    ) -> list[ProviderRun]:
        out = []
        for i, vq in enumerate(variants[:2]):  # cap at 2 variants for cost
            out.append(
                ProviderRun(
                    "searxng",
                    vq,
                    categories=cats,
                    max_results=limit,
                    lang=resolved_lang,
                    time_range=tr,
                    variant=i,
                )
            )
        return out

    if family == "code":
        runs.append(ProviderRun("code_search", q, max_results=max_results, lang=resolved_lang))
        runs.extend(_searxng_runs([SearchCategory.general], min(max_results, 8)))
        reason.append("code → code_search + searxng docs")
    elif family == "research":
        runs.extend(_searxng_runs([SearchCategory.general], max_results))
        runs.append(ProviderRun("arxiv", q, max_results=min(max_results, 5), lang=resolved_lang))
        runs.append(ProviderRun("hn", q, max_results=min(max_results, 5), lang=resolved_lang))
        reason.append("research → searxng + arxiv + hn")
    elif family == "news":
        cats = (
            [SearchCategory.news]
            if type_hint == "news"
            else [SearchCategory.general, SearchCategory.news]
        )
        runs.extend(_searxng_runs(cats, max_results, tr=time_range or "month"))
        runs.append(
            ProviderRun(
                "ddgs", q, categories=cats, max_results=min(max_results, 8), lang=resolved_lang
            )
        )
        reason.append(f"news → searxng(news, time_range={time_range or 'month'}) + ddgs")
    else:  # web
        runs.extend(_searxng_runs([SearchCategory.general], max_results, tr=time_range))
        runs.append(
            ProviderRun(
                "ddgs",
                q,
                categories=[SearchCategory.general],
                max_results=min(max_results, 8),
                lang=resolved_lang,
            )
        )
        reason.append("web → searxng + ddgs")

    return OrchestrationPlan(query=q, family=family, runs=runs, reason=reason)


async def execute_plan(
    plan: OrchestrationPlan,
    *,
    max_results: int = 10,
) -> tuple[dict[str, list[SearchResultItem]], list[str]]:
    """Run a plan concurrently, returning {provider_name: results} + used list."""
    results_by_provider: dict[str, list[SearchResultItem]] = {}

    async def _safe_run(run: ProviderRun) -> tuple[str, list[SearchResultItem]]:
        try:
            if run.name == "code_search":
                from providers.code_search import github_search, grep_app_search

                gh, gp = await asyncio.gather(
                    github_search(run.query, run.max_results),
                    grep_app_search(run.query, run.max_results),
                    return_exceptions=True,
                )
                merged: list[SearchResultItem] = []
                for r in (gh, gp):
                    if isinstance(r, list):
                        merged.extend(r)
                return "code_search", merged
            if run.name == "arxiv":
                from providers.arxiv import arxiv_search

                return "arxiv", await arxiv_search(run.query, max_results=run.max_results)
            if run.name == "hn":
                from providers.hn import hn_search

                return "hn", await hn_search(run.query, max_results=run.max_results)
            if run.name == "ddgs":
                from providers.ddgs import _DDGS_AVAILABLE, ddgs_search

                if not _DDGS_AVAILABLE:
                    return "ddgs", []
                return "ddgs", await ddgs_search(
                    query=run.query,
                    categories=run.categories,
                    max_results=run.max_results,
                    lang=run.lang,
                )
            from providers.searxng import searxng_search

            return "searxng", await searxng_search(
                query=run.query,
                categories=run.categories,
                max_results=run.max_results,
                lang=run.lang,
                time_range=run.time_range,
            )
        except Exception:
            return run.name, []

    gathered = await asyncio.gather(*[_safe_run(r) for r in plan.runs])
    used: list[str] = []
    for name, items in gathered:
        if items:
            # Merge multiple runs of the same provider (query variants).
            results_by_provider.setdefault(name, []).extend(items)
            if name not in used:
                used.append(name)
    return results_by_provider, used


async def orchestrate_search(
    query: str,
    *,
    type_hint: str = "web",
    lang: str = "auto",
    max_results: int = 10,
    time_range: str | None = None,
) -> tuple[dict[str, list[SearchResultItem]], str, list[str]]:
    """One-shot orchestration: build plan + execute.

    Returns (results_by_provider, family, used_providers) ready for the
    Evidence Aggregator.
    """
    plan = build_plan(
        query,
        type_hint=type_hint,
        lang=lang,
        max_results=max_results,
        time_range=time_range,
    )
    results, used = await execute_plan(plan, max_results=max_results)
    observe_search("orchestrator", plan.family, sum(len(v) for v in results.values()))
    if not used:
        observe_degraded("all_providers_empty")
    return results, plan.family, used
