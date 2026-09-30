"""Query Generator — Sinh truy vấn đa tầng.

Thin adapter over :mod:`agent.query_planner`: the planner owns the LLM
prompt, JSON schema validation and heuristic fallback; this module keeps the
original ``list[str]`` contract used by the orchestrator and tests.  Code —
not the LLM — decides execution: schema validation and the per-depth query
cap are applied inside the planner.
"""

from research_models.research_state import SearchIntent

from agent.query_planner import plan_queries

# Depth → max queries (deterministic policy, không cho LLM tự quyết số lượng).
_MAX_QUERIES = {"quick": 3, "normal": 6, "deep": 10}


async def generate_queries(query: str, intent: SearchIntent) -> list[str]:
    """Generate multiple search queries from a single user query.

    LLM path: asks the model to decompose the query into focused sub-queries
    (entity pairs, site-scoped, topic-specific, bilingual VI+EN). Heuristic
    fallback lives in the planner.
    """
    plan = await plan_queries(query, intent)
    return plan.texts


async def generate_queries_from_gaps(missing: list[str]) -> list[str]:
    """Generate new queries to fill information gaps (gap-driven round 2+)."""
    queries = []
    for gap in missing:
        queries.append(gap)
        # Add quoted version for exact match
        if " " in gap and not gap.startswith('"'):
            queries.append(f'"{gap}"')
    return queries
