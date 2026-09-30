"""Search mode definitions — fast / balanced / deep budgets.

Each mode caps the deterministic pipeline knobs: how many sub-queries the
planner may emit, how many raw results to collect, how many pages to scrape,
and how many follow-up rounds the gap loop may run.  The values follow
spec_searchhub_001 (fast ≈ 3 queries / ~25 results / 0 follow-up, balanced ≈
6 / ~50 / 1, deep ≈ 10 / ~100 / 3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchModeConfig:
    """Budget knobs for one search mode.

    Attributes:
        name: Canonical mode name (``fast`` | ``balanced`` | ``deep``).
        num_queries: Upper bound on planner-generated sub-queries.
        max_results: Target pool of raw results collected across queries.
        per_query_results: ``max_results`` passed to each SearXNG call.
        scrape_top_n: URLs forwarded to Firecrawl after reranking.
        max_followups: Gap-driven follow-up search rounds allowed.
        passage_top_n: Passages kept by the second-stage passage reranker.
    """

    name: str
    num_queries: int
    max_results: int
    per_query_results: int
    scrape_top_n: int
    max_followups: int
    passage_top_n: int


SEARCH_MODES: dict[str, SearchModeConfig] = {
    "fast": SearchModeConfig(
        name="fast",
        num_queries=3,
        max_results=25,
        per_query_results=10,
        scrape_top_n=5,
        max_followups=0,
        passage_top_n=12,
    ),
    "balanced": SearchModeConfig(
        name="balanced",
        num_queries=6,
        max_results=50,
        per_query_results=10,
        scrape_top_n=10,
        max_followups=1,
        passage_top_n=16,
    ),
    "deep": SearchModeConfig(
        name="deep",
        num_queries=10,
        max_results=100,
        per_query_results=12,
        scrape_top_n=20,
        max_followups=3,
        passage_top_n=20,
    ),
}

# Aliases accepted from callers — legacy depth names and loose spellings all
# resolve onto the canonical three modes.
_MODE_ALIASES: dict[str, str] = {
    "fast": "fast",
    "quick": "fast",
    "balanced": "balanced",
    "normal": "balanced",
    "auto": "balanced",
    "deep": "deep",
    # Vane "quality" maps here via the Search-Hub adapter (v2.1 §15).
    "research": "deep",
}


def get_mode(name: str | None, default: str = "balanced") -> SearchModeConfig:
    """Resolve a mode name (or alias) to its ``SearchModeConfig``.

    Unknown/empty names fall back to ``default`` (itself alias-resolved).
    """
    resolved = _MODE_ALIASES.get((name or "").strip().lower())
    if resolved is None:
        resolved = _MODE_ALIASES.get(default.strip().lower(), "balanced")
    return SEARCH_MODES[resolved]


def mode_for_depth(depth: str | None) -> SearchModeConfig:
    """Map an intent ``depth`` (none/quick/normal/deep) onto a search mode."""
    mapping = {
        "none": "fast",
        "quick": "fast",
        "normal": "balanced",
        "deep": "deep",
    }
    return get_mode(mapping.get((depth or "").strip().lower(), "balanced"))
