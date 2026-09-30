"""Federated executor — parallel provider calls with health + budget wiring.

Runs a SourceRouter FanOutPlan: each pick is called concurrently, wrapped
with a timeout, outcome classification, per-engine health signals
(SearXNG ``unresponsive_engines`` → EngineHealthManager) and provider-level
circuit-breaker bookkeeping (ProviderHealthMonitor).

The executor is tolerant: providers implementing the Phase-2 contract
``search(sq, ctx) -> list[ProviderResult]`` are called with context; legacy
providers with a one-arg ``search(sq)`` still work. Results of any shape
(``ProviderResult``, ``SearchResultItem``, raw dict) normalize into
``ProviderResult``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from providers.base import (
    CallOutcome,
    ProviderCallReport,
    ProviderResult,
    SearchContext,
    classify_engine_signal,
    classify_error_text,
    classify_exception,
    to_provider_result,
)

from core.budget import SearchBudget
from core.engine_health import get_engine_health_manager
from observability.prometheus import PROVIDER_LATENCY, PROVIDER_REQUESTS, PROVIDER_RESULTS, PROVIDER_UNIQUE_RESULTS
from core.provider_health import ProviderHealthMonitor
from core.provider_registry import ProviderSearchQuery
from core.source_router import FanOutPlan, ProviderPick

logger = logging.getLogger(__name__)


@dataclass
class FanOutResult:
    """Outcome of executing one fan-out plan."""

    results: list[ProviderResult] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    reports: dict[str, ProviderCallReport] = field(default_factory=dict)

    @property
    def provider_names(self) -> list[str]:
        return self.attempted


def _accepts_ctx(search_fn: Callable[..., Any]) -> bool:
    """True if provider.search takes (sq, ctx) — the Phase-2 signature."""
    try:
        params = [
            p
            for p in inspect.signature(search_fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return False
    return len(params) >= 2


class FederatedExecutor:
    """Runs provider calls in parallel and records health signals."""

    def __init__(
        self,
        monitor: ProviderHealthMonitor | None = None,
        *,
        budget: SearchBudget | None = None,
    ) -> None:
        self.monitor = monitor
        self.budget = budget

    # ── single provider call ─────────────────────────────────────────────

    async def call_provider(
        self,
        name: str,
        provider: Any,
        sq: ProviderSearchQuery,
        ctx: SearchContext,
        *,
        spec_timeout_s: float = 20.0,
        target_latency_ms: float = 3000.0,
        internal: bool = False,
    ) -> tuple[list[ProviderResult], ProviderCallReport]:
        """Call one provider, normalize results, report outcome.

        Returns ``(results, report)`` — never raises for provider failures.
        """
        t0 = time.perf_counter()
        report = ProviderCallReport(outcome=CallOutcome.error, latency_ms=0.0, probe=ctx.probe)
        try:
            if internal is False and self.budget is not None:
                if self.budget.remaining_queries <= 0:
                    # Budget skips are not provider failures — don't record.
                    report.outcome = CallOutcome.disabled
                    report.error = "budget_exhausted"
                    report.latency_ms = (time.perf_counter() - t0) * 1000.0
                    return [], report
                self.budget.use_query()
            search_fn = provider.search
            coro = search_fn(sq, ctx) if _accepts_ctx(search_fn) else search_fn(sq)
            raw = await asyncio.wait_for(coro, timeout=spec_timeout_s)
            results = [
                to_provider_result(
                    item,
                    source=name,
                    source_type=ctx.source_types[0] if ctx.source_types else None,
                    language=ctx.language,
                )
                for item in (raw or [])
            ]
            report.result_count = len(results)
            report.unique_result_count = len(
                {(r.canonical_url or r.url or "").lower() for r in results}
            )
            report.usable_result_count = sum(1 for r in results if r.usable)
            report.outcome = CallOutcome.success if results else CallOutcome.empty
        except Exception as exc:  # provider failures must not break the fan-out
            report.outcome = classify_exception(exc)
            report.error = f"{type(exc).__name__}: {exc}"[:300]
            results = []
        report.latency_ms = (time.perf_counter() - t0) * 1000.0
        PROVIDER_LATENCY.labels(name).observe(report.latency_ms / 1000.0)
        PROVIDER_RESULTS.labels(name).inc(report.result_count)
        PROVIDER_UNIQUE_RESULTS.labels(name).inc(report.unique_result_count)
        PROVIDER_REQUESTS.labels(name, report.outcome.value).inc()

        signals = getattr(provider, "last_engine_signals", None)
        if isinstance(signals, dict):
            report.engine_signals = dict(signals)
            self._record_engine_signals(signals, report.latency_ms)
            # A provider that came back empty while most of its engines are
            # captcha-blocked is itself captcha-bound — like Qwant today.
            if report.outcome == CallOutcome.empty and signals:
                kinds = [classify_engine_signal(r) for r in signals.values()]
                if sum(1 for k in kinds if k == "captcha") > len(kinds) / 2:
                    report.outcome = CallOutcome.captcha

        # Providers that swallow errors surface the reason via last_call_error
        # — an empty result that was actually a failure shouldn't score as
        # clean-but-thin.
        last_err = getattr(provider, "last_call_error", None)
        if report.outcome == CallOutcome.empty and last_err:
            report.outcome = classify_error_text(str(last_err))
            report.error = str(last_err)[:300]

        if self.monitor is not None:
            self.monitor.record(name, report, target_latency_ms=target_latency_ms)
        return results, report

    # ── plan execution ───────────────────────────────────────────────────

    async def execute(
        self,
        plan: FanOutPlan,
        registry: Any,
        sq: ProviderSearchQuery,
        ctx_for: Callable[[ProviderPick], SearchContext],
    ) -> FanOutResult:
        """Fan out to every pick in parallel; merge results."""
        out = FanOutResult(skipped=list(plan.skipped))

        async def run(pick: ProviderPick) -> tuple[ProviderPick, Any]:
            provider = registry.get(pick.name)
            if provider is None:
                return pick, None
            ctx = ctx_for(pick)
            timeout_s = pick.spec.timeout_s
            if not isinstance(timeout_s, (int, float)):
                timeout_s = 20.0
            results, report = await self.call_provider(
                pick.name,
                provider,
                sq,
                ctx,
                spec_timeout_s=float(timeout_s),
                target_latency_ms=float(pick.spec.target_latency_ms or 3000.0),
                internal=pick.spec.internal,
            )
            return pick, (results, report)

        for pick, payload in await asyncio.gather(*[run(p) for p in plan.picks]):
            out.attempted.append(pick.name)
            if payload is None:
                out.skipped.append((pick.name, "not_registered"))
                continue
            results, report = payload
            out.reports[pick.name] = report
            out.results.extend(results)
        return out

    # ── engine-level signals ─────────────────────────────────────────────

    @staticmethod
    def _record_engine_signals(signals: dict[str, str], latency_ms: float) -> None:
        mgr = get_engine_health_manager()
        for engine, reason in signals.items():
            kind = classify_engine_signal(reason)
            mgr.record_failure(
                engine,
                is_429=kind == "rate_limited",
                is_captcha=kind == "captcha",
                latency_ms=latency_ms,
            )
