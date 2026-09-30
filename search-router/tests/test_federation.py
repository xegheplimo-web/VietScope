"""Source Federation Layer — health scoring, circuit breaker, adaptive fan-out.

Covers the user's Phase-2 acceptance criteria:
- captcha-heavy provider (Qwant scenario) degrades → circuit opens → traffic stops
- after cooldown a single probe is admitted; success → closed, failure → re-open
- intent-driven lane weights (news / ecommerce / legal queries)
- executor: outcome classification, budget consumption, skipped-by-circuit
"""

from __future__ import annotations

import asyncio
import time

from core.federation import FederatedExecutor
from core.provider_health import (
    Admission,
    CircuitState,
    HealthStatus,
    ProviderHealthMonitor,
)
from core.provider_registry import (
    PROVIDER_SPECS,
    ProviderRegistry,
    ProviderSearchQuery,
    _spec_from_mapping,
    load_provider_specs,
)
from core.query_understanding import QueryProfile
from core.source_router import HIGH, MEDIUM, OFF, VERY_HIGH, SourceRouter
from providers.base import (
    CallOutcome,
    ProviderCallReport,
    ProviderResult,
    ProviderSpec,
    SearchContext,
    SourceType,
    classify_error_text,
    classify_exception,
)


def _report(outcome=CallOutcome.success, latency_ms=100.0, results=5, probe=False):
    return ProviderCallReport(
        outcome=outcome,
        latency_ms=latency_ms,
        result_count=results,
        unique_result_count=results,
        usable_result_count=results,
        probe=probe,
    )


# ── health scoring / circuit breaker ─────────────────────────────────────────


class TestHealthScoring:
    def test_healthy_provider_stays_closed(self):
        m = ProviderHealthMonitor()
        for _ in range(6):
            m.record("p", _report())
        h = m.snapshot("p")
        assert h.circuit == CircuitState.closed
        assert h.status == HealthStatus.healthy
        assert h.success_rate == 1.0
        assert m.allow("p") == Admission.allow

    def test_qwant_captcha_scenario_trips_breaker(self):
        """Qwant: CAPTCHA storm → score collapses → circuit opens → no traffic."""
        m = ProviderHealthMonitor()
        # a couple of successes then a captcha burst (≥50% over ≥4 calls)
        m.record("qwant", _report())
        for _ in range(3):
            h = m.record("qwant", _report(outcome=CallOutcome.captcha, results=0))
        assert h.circuit == CircuitState.open
        # captcha storm → DEGRADED/UNHEALTHY, router stops traffic
        assert h.status != HealthStatus.healthy
        assert h.captcha_rate > 0.5
        assert h.consecutive_failures == 3
        assert m.allow("qwant") == Admission.deny

    def test_consecutive_failures_trip(self):
        m = ProviderHealthMonitor()
        for _ in range(3):
            m.record("p", _report(outcome=CallOutcome.error, results=0))
        assert m.snapshot("p").circuit == CircuitState.open

    def test_half_open_probe_recovers(self):
        m = ProviderHealthMonitor(cooldown_s=0.05)
        for _ in range(3):
            m.record("p", _report(outcome=CallOutcome.captcha, results=0))
        assert m.allow("p") == Admission.deny
        time.sleep(0.06)
        # cooldown elapsed → exactly one probe admitted
        assert m.allow("p") == Admission.probe
        # concurrent call denied while probe in flight
        assert m.allow("p") == Admission.deny
        # probe succeeds → circuit closes again
        h = m.record("p", _report(probe=True))
        assert h.circuit == CircuitState.closed
        assert h.opens == 0
        assert m.allow("p") == Admission.allow

    def test_failed_probe_reopens_with_backoff(self):
        m = ProviderHealthMonitor(cooldown_s=0.05, max_cooldown_s=600)
        for _ in range(3):
            m.record("p", _report(outcome=CallOutcome.timeout, results=0))
        time.sleep(0.06)
        assert m.allow("p") == Admission.probe
        h = m.record("p", _report(outcome=CallOutcome.timeout, results=0, probe=True))
        assert h.circuit == CircuitState.open
        assert h.opens >= 1  # re-opened; cooldown backs off
        assert h.cooldown_until and h.cooldown_until > time.time() + 0.05

    def test_empty_calls_degrade_not_killer(self):
        """EMPTY (zero results) is a soft signal — alone it shouldn't open."""
        m = ProviderHealthMonitor()
        for _ in range(6):
            h = m.record("p", _report(results=0, outcome=CallOutcome.empty))
        assert h.circuit == CircuitState.closed
        assert h.status in (HealthStatus.healthy, HealthStatus.degraded)

    def test_metrics_reported(self):
        m = ProviderHealthMonitor()
        m.record("p", _report(latency_ms=200.0))
        m.record("p", _report(outcome=CallOutcome.timeout, results=0))
        h = m.snapshot("p")
        assert h.total_requests == 2
        assert h.timeout_rate == 0.5
        assert h.p50_latency_ms > 0
        assert h.last_success > 0
        assert 0.0 < h.health_score < 1.0


# ── adaptive fan-out ─────────────────────────────────────────────────────────


class _Dummy:
    async def search(self, sq, ctx=None):
        return [ProviderResult(url="http://x", title="x", snippet="", source="d")]


class TestAdaptiveFanout:
    def _reg(self):
        reg = ProviderRegistry()
        for name in ("searxng", "ddgs", "arxiv", "hn"):
            reg.register(name, _Dummy(), _spec_from_mapping(name, PROVIDER_SPECS[name]))
        return reg

    def test_news_query_weights(self):
        r = SourceRouter()
        w = r.lane_weights("OpenAI vừa ra model gì?", QueryProfile())
        assert w[SourceType.general_web] == HIGH
        assert w[SourceType.news] == VERY_HIGH
        assert w[SourceType.ecommerce] == OFF
        assert w[SourceType.places] == OFF

    def test_price_query_weights(self):
        r = SourceRouter()
        w = r.lane_weights("iPhone 17 Pro giá bao nhiêu?", QueryProfile())
        assert w[SourceType.ecommerce] >= HIGH
        assert w[SourceType.general_web] == HIGH
        assert w[SourceType.academic] == OFF

    def test_legal_query_weights(self):
        r = SourceRouter()
        w = r.lane_weights("Nghị định mới nhất về hóa đơn điện tử", QueryProfile())
        assert w[SourceType.government] == VERY_HIGH
        assert w[SourceType.legal] == VERY_HIGH
        assert w[SourceType.social] <= MEDIUM

    def test_plan_picks_and_skips_by_lane(self):
        # academic query → hn/arxiv lanes on; searxng still picked via general_web
        plan = SourceRouter().plan("transformer attention paper", QueryProfile(), self._reg())
        names = plan.provider_names
        assert "arxiv" in names
        skipped = dict(plan.skipped)
        assert "arxiv" not in skipped

    def test_circuit_open_provider_skipped(self):
        mon = ProviderHealthMonitor()
        for _ in range(3):
            mon.record("searxng", _report(outcome=CallOutcome.captcha, results=0))
        plan = SourceRouter(mon).plan("anything", QueryProfile(), self._reg())
        assert "searxng" not in plan.provider_names
        assert ("searxng", "circuit_open") in plan.skipped
        assert "ddgs" in plan.provider_names  # fallback still picked

    def test_locale_mismatch_skipped(self):
        reg = ProviderRegistry()
        spec = ProviderSpec(
            name="vn", source_types=[SourceType.news], countries=["VN"], languages=["vi"]
        )
        reg.register("vn", _Dummy(), spec)
        plan = SourceRouter().plan("tin tức", QueryProfile(language="en"), reg)
        assert ("vn", "locale_mismatch") in plan.skipped

    def test_internal_provider_always_in(self):
        reg = ProviderRegistry()
        reg.register(
            "opensearch",
            _Dummy(),
            ProviderSpec(name="opensearch", source_types=[SourceType.index], internal=True),
        )
        reg.register("broken", _Dummy())
        mon = ProviderHealthMonitor()
        for _ in range(3):
            mon.record("broken", _report(outcome=CallOutcome.error, results=0))
        plan = SourceRouter(mon).plan("q", QueryProfile(), reg)
        assert "opensearch" in plan.provider_names
        assert "broken" not in plan.provider_names


class TestVNTaxonomy:
    """P1 VN taxonomy lanes — the user's routing examples must hold."""

    def _w(self, q: str):
        return SourceRouter().lane_weights(q, QueryProfile())

    def test_gold_price_is_market_not_shopping(self):
        w = self._w("giá vàng hôm nay")
        assert w[SourceType.market] == VERY_HIGH
        assert w[SourceType.news] == VERY_HIGH
        assert w[SourceType.ecommerce] == OFF
        assert w[SourceType.finance] >= MEDIUM

    def test_decree_is_legal_government(self):
        w = self._w("nghị định mới nhất về hóa đơn điện tử")
        assert w[SourceType.legal] == VERY_HIGH
        assert w[SourceType.government] == VERY_HIGH

    def test_pho_nearby_is_place(self):
        w = self._w("quán phở gần đây")
        assert w[SourceType.places] >= HIGH
        assert w[SourceType.ecommerce] <= MEDIUM

    def test_iphone_price_is_product_ecommerce(self):
        w = self._w("giá iPhone 17 Pro")
        assert w[SourceType.product] == VERY_HIGH
        assert w[SourceType.ecommerce] == VERY_HIGH

    def test_company_revenue_is_company_finance(self):
        w = self._w("Vingroup doanh thu năm 2025")
        assert w[SourceType.company] == VERY_HIGH
        assert w[SourceType.finance] >= MEDIUM
        assert w[SourceType.business] >= HIGH

    def test_vf8_reviews_are_forum_social_product(self):
        w = self._w("người dùng đánh giá VF8 thế nào")
        assert w[SourceType.forum] >= HIGH
        assert w[SourceType.social] >= MEDIUM
        assert w[SourceType.product] == VERY_HIGH

    def test_administrative_merger(self):
        w = self._w("danh sách tỉnh thành sau sáp nhập")
        assert w[SourceType.administrative] == VERY_HIGH
        assert w[SourceType.government] >= MEDIUM

    def test_medical_symptoms(self):
        w = self._w("triệu chứng cảm cúm")
        assert w[SourceType.medical] == HIGH

    def test_document_form(self):
        w = self._w("mẫu đơn xin việc")
        assert w[SourceType.document] == HIGH
        assert w[SourceType.government] >= MEDIUM

    def test_bank_interest_is_finance(self):
        w = self._w("lãi suất ngân hàng Vietcombank")
        assert w[SourceType.finance] == HIGH
        assert w[SourceType.ecommerce] == OFF


# ── executor ─────────────────────────────────────────────────────────────────


class _Ok:
    async def search(self, sq, ctx=None):
        return [
            ProviderResult(
                url="http://a/1", title="t", snippet="s", source="x", source_type="general_web"
            )
        ]


class _Empty:
    async def search(self, sq, ctx=None):
        return []


class _Boom:
    async def search(self, sq, ctx=None):
        raise RuntimeError("HTTP 429 too many requests")


class _Slow:
    async def search(self, sq, ctx=None):
        await asyncio.sleep(5)
        return []


def _sq():
    return ProviderSearchQuery(query="test", categories=["general"], max_results=5)


class TestExecutor:
    def test_success_records_and_returns(self):
        mon = ProviderHealthMonitor()
        ex = FederatedExecutor(mon)
        results, rep = asyncio.run(ex.call_provider("x", _Ok(), _sq(), SearchContext()))
        assert rep.outcome == CallOutcome.success
        assert len(results) == 1
        assert results[0].source == "x"
        assert mon.snapshot("x").success_rate == 1.0

    def test_empty_outcome(self):
        ex = FederatedExecutor(ProviderHealthMonitor())
        results, rep = asyncio.run(ex.call_provider("e", _Empty(), _sq(), SearchContext()))
        assert rep.outcome == CallOutcome.empty
        assert results == []

    def test_rate_limit_classified(self):
        mon = ProviderHealthMonitor()
        ex = FederatedExecutor(mon)
        _, rep = asyncio.run(ex.call_provider("r", _Boom(), _sq(), SearchContext()))
        assert rep.outcome == CallOutcome.rate_limited

    def test_timeout_outcome(self):
        mon = ProviderHealthMonitor()
        ex = FederatedExecutor(mon)
        _, rep = asyncio.run(
            ex.call_provider("s", _Slow(), _sq(), SearchContext(), spec_timeout_s=0.05)
        )
        assert rep.outcome == CallOutcome.timeout

    def test_engine_signals_escalate_to_captcha(self):
        """SearXNG case: HTTP ok + 0 results + engines reporting captcha → captcha."""

        class SearXNGish:
            last_engine_signals = None

            async def search(self, sq, ctx=None):
                self.last_engine_signals = {
                    "qwant": "got unexpected response: CAPTCHA",
                    "bing": "got unexpected response: CAPTCHA",
                }
                return []

        mon = ProviderHealthMonitor()
        ex = FederatedExecutor(mon)
        _, rep = asyncio.run(ex.call_provider("searxng", SearXNGish(), _sq(), SearchContext()))
        assert rep.outcome == CallOutcome.captcha
        assert rep.engine_signals["qwant"].lower().find("captcha") >= 0

    def test_budget_exhausted_short_circuits(self):
        from core.budget import SearchBudget

        budget = SearchBudget.for_mode("fast")
        for _ in range(budget.max_queries):
            budget.use_query()  # exhaust
        ex = FederatedExecutor(ProviderHealthMonitor(), budget=budget)
        results, rep = asyncio.run(ex.call_provider("x", _Ok(), _sq(), SearchContext()))
        assert rep.outcome == CallOutcome.disabled
        assert results == []

    def test_execute_merges_picks_and_skips(self):
        mon = ProviderHealthMonitor()
        reg = ProviderRegistry()
        reg.register("a", _Ok())
        reg.register("b", _Empty())
        router = SourceRouter(mon)
        plan = router.plan("q", QueryProfile(), reg)
        ex = FederatedExecutor(mon)
        fanout = asyncio.run(
            ex.execute(plan, reg, _sq(), lambda p: router.context_for(p, QueryProfile(), "normal"))
        )
        assert set(fanout.attempted) == {"a", "b"}
        assert len(fanout.results) == 1
        assert fanout.reports["b"].outcome == CallOutcome.empty


# ── classification helpers ───────────────────────────────────────────────────


class TestClassification:
    def test_classify_exception(self):
        assert classify_exception(TimeoutError()) == CallOutcome.timeout
        assert classify_exception(RuntimeError("429")) == CallOutcome.rate_limited
        assert classify_exception(RuntimeError("captcha detected")) == CallOutcome.captcha
        assert classify_exception(RuntimeError("boom")) == CallOutcome.error

    def test_classify_error_text(self):
        assert classify_error_text("ddg ratelimit") == CallOutcome.rate_limited
        assert classify_error_text("Client response error 429") == CallOutcome.rate_limited
        assert classify_error_text("timed out") == CallOutcome.timeout
        assert classify_error_text("something else") == CallOutcome.error
        assert classify_error_text(None) == CallOutcome.error


# ── spec loading ─────────────────────────────────────────────────────────────


class TestSpecLoading:
    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_SEARXNG_ENABLED", "false")
        specs = load_provider_specs()
        assert specs["searxng"].enabled is False

    def test_json_overlay(self, monkeypatch, tmp_path):
        p = tmp_path / "providers.json"
        p.write_text(
            '{"providers": {"ddgs": {"enabled": false, "priority": 0.1, '
            '"countries": ["VN"], "languages": ["vi"]}, '
            '"new_rss": {"name": "new_rss", "source_types": ["news"], '
            '"enabled": true}}}'
        )
        monkeypatch.setenv("HUB_PROVIDERS_CONFIG", str(p))
        specs = load_provider_specs()
        assert specs["ddgs"].enabled is False
        assert specs["ddgs"].priority == 0.1
        assert specs["ddgs"].countries == ["VN"]
        assert specs["new_rss"].source_types == [SourceType.news]
        assert specs["new_rss"].enabled is True

    def test_unspecd_provider_gets_default_spec(self):
        reg = ProviderRegistry()
        reg.register("custom", _Dummy())  # no spec — must still fan out
        assert reg.spec("custom") is None  # spec optional; router supplies default
        plan = SourceRouter().plan("q", QueryProfile(), reg)
        assert "custom" in plan.provider_names
