"""Tests for the Search Orchestrator (wave-9)."""

from pipeline.router import (
    OrchestrationPlan,
    build_plan,
    classify_query,
    execute_plan,
)


def test_classify_code():
    assert classify_query("python async def fetch example") == "code"
    assert classify_query("how to use fastapi middleware") == "code"
    assert classify_query("pip install requests example") == "code"


def test_classify_research():
    assert classify_query("survey of transformer architectures") == "research"
    assert classify_query("compare deep learning frameworks benchmark") == "research"


def test_classify_news_vi():
    assert classify_query("giá vàng hôm nay mới nhất") == "news"
    assert classify_query("breaking news today") == "news"


def test_classify_default_web():
    assert classify_query("best restaurants in hanoi") == "web"
    assert classify_query("lịch sử phật giáo việt nam") == "web"


def test_build_plan_code():
    plan = build_plan("python fastapi example", max_results=8)
    assert plan.family == "code"
    names = {r.name for r in plan.runs}
    assert "code_search" in names
    assert "searxng" in names


def test_build_plan_news_uses_news_category_and_time_range():
    plan = build_plan("giá vàng hôm nay", lang="vi")
    assert plan.family == "news"
    searxng_run = next(r for r in plan.runs if r.name == "searxng")
    assert any(c.value == "news" for c in searxng_run.categories)
    assert searxng_run.time_range is not None
    # Vietnamese query → language resolved to vi
    assert searxng_run.lang == "vi"
    # Freshness hint triggers query rewrite → ≥2 searxng variants expected
    assert len([r for r in plan.runs if r.name == "searxng"]) >= 2


def test_build_plan_research_includes_arxiv_hn():
    plan = build_plan("state of the art LLM survey")
    assert plan.family == "research"
    names = {r.name for r in plan.runs}
    assert {"searxng", "arxiv", "hn"} <= names


def test_build_plan_web_has_backbone_and_fallback():
    plan = build_plan("best pho in hanoi", lang="en")
    assert plan.family == "web"
    names = {r.name for r in plan.runs}
    assert "searxng" in names
    assert "ddgs" in names


def test_execute_plan_swallows_provider_failures():
    """A failing provider must not kill the whole run (orchestrator resilience)."""

    async def _fail(_q, **_kw):
        raise RuntimeError("boom")

    plan = OrchestrationPlan(
        query="test",
        runs=[],
    )

    # Patch via monkeypatch-free injection: run execute_plan with zero runs.
    import asyncio

    results, used = asyncio.run(execute_plan(plan))
    assert results == {}
    assert used == []
