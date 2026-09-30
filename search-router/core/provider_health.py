"""Provider Health Monitor — rolling metrics + circuit breaker (Phase 2).

Every provider call lands here as a ``ProviderCallReport``. The monitor keeps
a bounded rolling window per provider, computes a ``health_score`` in
``[0, 1]`` from availability + yield + latency, and runs the circuit breaker:

    CLOSED  → normal traffic
    OPEN    → tripped (consecutive failures, low score, or CAPTCHA storm) —
              the router stops sending traffic for ``cooldown`` seconds
    HALF-OPEN → cooldown expired: one probe call is admitted; success closes
              the circuit again, failure re-opens it with backoff

That is what lets the router drop Qwant-style CAPTCHA'd or timing-out
providers out of the fan-out instead of depending on them — and bring them
back once they recover.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock

from providers.base import CallOutcome, ProviderCallReport

logger = logging.getLogger(__name__)

# Outcome → per-call quality in [0, 1]. ``empty`` counts as "answered but no
# yield" — it does not trip the breaker, but it does pull the score down via
# the yield term. ``disabled`` never reaches the window.
_OUTCOME_QUALITY = {
    CallOutcome.success: 1.0,
    CallOutcome.empty: 0.5,
    CallOutcome.timeout: 0.1,
    CallOutcome.rate_limited: 0.15,
    CallOutcome.captcha: 0.0,
    CallOutcome.error: 0.2,
}

# Outcomes that count as hard failures for consecutive_failures / probes.
_FAILURE_OUTCOMES = frozenset(
    {
        CallOutcome.timeout,
        CallOutcome.captcha,
        CallOutcome.rate_limited,
        CallOutcome.error,
    }
)


class CircuitState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class HealthStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class Admission(StrEnum):
    """What the router may do with a provider right now."""

    allow = "allow"  # normal traffic
    probe = "probe"  # half-open: this call is the recovery probe
    deny = "deny"  # circuit open: do not send traffic


@dataclass
class CallRecord:
    outcome: CallOutcome
    latency_ms: float
    result_count: int
    unique_result_count: int
    usable_result_count: int
    ts: float
    probe: bool = False


@dataclass
class ProviderHealth:
    """Point-in-time health view for one provider."""

    name: str
    status: HealthStatus = HealthStatus.healthy
    circuit: CircuitState = CircuitState.closed
    health_score: float = 1.0

    total_requests: int = 0
    window_requests: int = 0
    success_rate: float = 1.0  # fraction returning >= 1 result
    ok_rate: float = 1.0  # fraction that answered at all (success + empty)
    timeout_rate: float = 0.0
    captcha_rate: float = 0.0
    rate_limited_rate: float = 0.0
    empty_rate: float = 0.0

    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    avg_result_count: float = 0.0
    avg_unique_result_count: float = 0.0
    relevant_result_rate: float = 0.0  # fraction of calls returning usable hits

    last_success: float | None = None
    last_failure: float | None = None
    consecutive_failures: int = 0
    opens: int = 0  # lifetime circuit trips (backoff exponent)
    cooldown_until: float | None = None
    probe_in_flight: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "circuit": self.circuit.value,
            "health_score": round(self.health_score, 3),
            "total_requests": self.total_requests,
            "window_requests": self.window_requests,
            "success_rate": round(self.success_rate, 3),
            "ok_rate": round(self.ok_rate, 3),
            "timeout_rate": round(self.timeout_rate, 3),
            "captcha_rate": round(self.captcha_rate, 3),
            "rate_limited_rate": round(self.rate_limited_rate, 3),
            "empty_rate": round(self.empty_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "avg_result_count": round(self.avg_result_count, 2),
            "avg_unique_result_count": round(self.avg_unique_result_count, 2),
            "relevant_result_rate": round(self.relevant_result_rate, 3),
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "consecutive_failures": self.consecutive_failures,
            "opens": self.opens,
            "cooldown_until": self.cooldown_until,
        }


@dataclass
class _ProviderState:
    window: deque = field(default_factory=deque)
    total_requests: int = 0
    last_success: float | None = None
    last_failure: float | None = None
    consecutive_failures: int = 0
    opens: int = 0
    circuit: CircuitState = CircuitState.closed
    cooldown_until: float | None = None
    probe_in_flight: bool = False


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


class ProviderHealthMonitor:
    """Thread-safe per-provider health tracker + circuit breaker."""

    def __init__(
        self,
        window_size: int = 64,
        max_window_age_s: float = 1800.0,
        open_score_threshold: float = 0.30,
        degraded_score_threshold: float = 0.70,
        max_consecutive_failures: int = 3,
        captcha_trip_rate: float = 0.5,
        captcha_min_calls: int = 4,
        min_calls_for_score_trip: int = 5,
        cooldown_s: float = 300.0,
        max_cooldown_s: float = 1800.0,
        yield_target: float = 5.0,
    ) -> None:
        self._window_size = window_size
        self._max_window_age_s = max_window_age_s
        self._open_score = open_score_threshold
        self._degraded_score = degraded_score_threshold
        self._max_consecutive = max_consecutive_failures
        self._captcha_trip_rate = captcha_trip_rate
        self._captcha_min_calls = captcha_min_calls
        self._min_calls_score_trip = min_calls_for_score_trip
        self._cooldown_s = cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._yield_target = yield_target
        self._states: dict[str, _ProviderState] = {}
        self._lock = RLock()

    # ── internals ────────────────────────────────────────────────────────

    def _state(self, name: str) -> _ProviderState:
        st = self._states.get(name)
        if st is None:
            st = _ProviderState()
            self._states[name] = st
        return st

    def _prune(self, st: _ProviderState, now: float) -> None:
        cutoff = now - self._max_window_age_s
        while st.window and st.window[0].ts < cutoff:
            st.window.popleft()
        while len(st.window) > self._window_size:
            st.window.popleft()

    @staticmethod
    def _cooldown(st: _ProviderState, base: float, cap: float) -> float:
        # Exponential backoff per trip: 300s → 600 → 1200 → cap.
        return min(cap, base * (2 ** max(0, st.opens - 1)))

    def _metrics(self, st: _ProviderState, target_latency_ms: float) -> ProviderHealth:
        recs = list(st.window)
        n = len(recs)
        h = ProviderHealth(name="", total_requests=st.total_requests, window_requests=n)
        if n == 0:
            h.circuit = st.circuit
            h.last_success = st.last_success
            h.last_failure = st.last_failure
            h.consecutive_failures = st.consecutive_failures
            h.opens = st.opens
            h.cooldown_until = st.cooldown_until
            h.probe_in_flight = st.probe_in_flight
            return h

        counts = dict.fromkeys(CallOutcome, 0)
        lat: list[float] = []
        total_results = total_unique = usable_calls = 0
        quality = 0.0
        for r in recs:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
            lat.append(r.latency_ms)
            total_results += r.result_count
            total_unique += r.unique_result_count
            if r.usable_result_count > 0:
                usable_calls += 1
            quality += _OUTCOME_QUALITY.get(r.outcome, 0.2)

        ok = counts.get(CallOutcome.success, 0) + counts.get(CallOutcome.empty, 0)
        h.success_rate = counts.get(CallOutcome.success, 0) / n
        h.ok_rate = ok / n
        h.timeout_rate = counts.get(CallOutcome.timeout, 0) / n
        h.captcha_rate = counts.get(CallOutcome.captcha, 0) / n
        h.rate_limited_rate = counts.get(CallOutcome.rate_limited, 0) / n
        h.empty_rate = counts.get(CallOutcome.empty, 0) / n
        lat.sort()
        h.avg_latency_ms = sum(lat) / n
        h.p50_latency_ms = _percentile(lat, 50)
        h.p95_latency_ms = _percentile(lat, 95)
        h.avg_result_count = total_results / n
        h.avg_unique_result_count = total_unique / n
        h.relevant_result_rate = usable_calls / n  # calls that yielded usable hits

        base_quality = quality / n
        yield_score = min(1.0, h.avg_result_count / self._yield_target)
        latency_score = (
            1.0
            if h.p50_latency_ms <= target_latency_ms or h.p50_latency_ms <= 0
            else target_latency_ms / h.p50_latency_ms
        )
        h.health_score = max(
            0.0,
            min(
                1.0,
                0.55 * base_quality
                + 0.20 * yield_score
                + 0.15 * latency_score
                + 0.10 * (1.0 - h.captcha_rate),
            ),
        )
        h.status = (
            HealthStatus.healthy
            if h.health_score >= self._degraded_score
            else HealthStatus.degraded
            if h.health_score >= self._open_score
            else HealthStatus.unhealthy
        )
        h.circuit = st.circuit
        h.last_success = st.last_success
        h.last_failure = st.last_failure
        h.consecutive_failures = st.consecutive_failures
        h.opens = st.opens
        h.cooldown_until = st.cooldown_until
        h.probe_in_flight = st.probe_in_flight
        return h

    # ── admission ────────────────────────────────────────────────────────

    def allow(self, name: str) -> Admission:
        """Decide whether traffic may reach ``name`` — and claim the probe slot."""
        with self._lock:
            st = self._state(name)
            now = time.time()
            if st.circuit == CircuitState.closed:
                return Admission.allow
            if st.circuit == CircuitState.half_open:
                return Admission.deny if st.probe_in_flight else self._claim_probe(st)
            # OPEN
            if st.cooldown_until is not None and now < st.cooldown_until:
                return Admission.deny
            # Cooldown expired → half-open, admit exactly one probe.
            st.circuit = CircuitState.half_open
            st.probe_in_flight = False
            logger.info("provider %r circuit half-open — admitting probe", name)
            return self._claim_probe(st)

    def _claim_probe(self, st: _ProviderState) -> Admission:
        st.probe_in_flight = True
        return Admission.probe

    # ── recording ────────────────────────────────────────────────────────

    def record(
        self,
        name: str,
        report: ProviderCallReport,
        *,
        target_latency_ms: float = 3000.0,
    ) -> ProviderHealth:
        """Record one call outcome; may open/close the circuit."""
        if report.outcome == CallOutcome.disabled:
            with self._lock:
                st = self._state(name)
                st.total_requests += 1
                # A budget/config skip carries no health verdict — release any
                # probe claim so the next real call can actually probe.
                if st.circuit == CircuitState.half_open:
                    st.probe_in_flight = False
            return self.snapshot(name)

        with self._lock:
            st = self._state(name)
            now = time.time()
            st.total_requests += 1
            st.window.append(
                CallRecord(
                    outcome=report.outcome,
                    latency_ms=report.latency_ms,
                    result_count=report.result_count,
                    unique_result_count=report.unique_result_count,
                    usable_result_count=report.usable_result_count,
                    ts=now,
                    probe=report.probe,
                )
            )
            self._prune(st, now)

            failed = report.outcome in _FAILURE_OUTCOMES
            if failed:
                st.last_failure = now
                st.consecutive_failures += 1
            else:
                st.last_success = now
                st.consecutive_failures = 0

            if report.probe or st.circuit == CircuitState.half_open:
                self._resolve_probe(name, st, failed, now)
            elif st.circuit == CircuitState.closed:
                self._maybe_trip(name, st, target_latency_ms, now)

            return self._metrics(st, target_latency_ms)

    def _resolve_probe(self, name: str, st: _ProviderState, failed: bool, now: float) -> None:
        st.probe_in_flight = False
        if failed:
            st.circuit = CircuitState.open
            st.opens += 1
            st.cooldown_until = now + self._cooldown(st, self._cooldown_s, self._max_cooldown_s)
            logger.warning(
                "provider %r probe failed — circuit re-opened (cooldown %.0fs)",
                name,
                st.cooldown_until - now,
            )
        else:
            st.circuit = CircuitState.closed
            st.opens = 0
            st.cooldown_until = None
            logger.info("provider %r circuit closed after successful probe", name)

    def _maybe_trip(
        self, name: str, st: _ProviderState, target_latency_ms: float, now: float
    ) -> None:
        metrics = self._metrics(st, target_latency_ms)
        n = metrics.window_requests
        trip = (
            st.consecutive_failures >= self._max_consecutive
            or (n >= self._min_calls_score_trip and metrics.health_score < self._open_score)
            or (n >= self._captcha_min_calls and metrics.captcha_rate >= self._captcha_trip_rate)
        )
        if trip:
            st.circuit = CircuitState.open
            st.opens += 1
            st.cooldown_until = now + self._cooldown(st, self._cooldown_s, self._max_cooldown_s)
            logger.warning(
                "provider %r circuit OPEN: score=%.2f captcha_rate=%.2f "
                "consecutive_failures=%d cooldown=%.0fs",
                name,
                metrics.health_score,
                metrics.captcha_rate,
                st.consecutive_failures,
                st.cooldown_until - now,
            )

    # ── introspection ────────────────────────────────────────────────────

    def snapshot(self, name: str, *, target_latency_ms: float = 3000.0) -> ProviderHealth:
        with self._lock:
            st = self._state(name)
            h = self._metrics(st, target_latency_ms)
            h.name = name
            return h

    def snapshot_all(self, *, target_latency_ms: float = 3000.0) -> dict[str, ProviderHealth]:
        with self._lock:
            now = time.time()
            out: dict[str, ProviderHealth] = {}
            for name, st in self._states.items():
                self._prune(st, now)
                h = self._metrics(st, target_latency_ms)
                h.name = name
                out[name] = h
            return out

    def reset(self, name: str | None = None) -> None:
        """Clear recorded state (tests / ops)."""
        with self._lock:
            if name is None:
                self._states.clear()
            else:
                self._states.pop(name, None)


_DEFAULT_MONITOR: ProviderHealthMonitor | None = None


def get_provider_health_monitor() -> ProviderHealthMonitor:
    """Process-wide monitor; thresholds come from ``config.settings``."""
    global _DEFAULT_MONITOR
    if _DEFAULT_MONITOR is None:
        from config import settings

        _DEFAULT_MONITOR = ProviderHealthMonitor(
            window_size=settings.provider_health_window,
            max_window_age_s=settings.provider_health_max_window_age_s,
            cooldown_s=settings.provider_health_cooldown_s,
            max_cooldown_s=settings.provider_health_max_cooldown_s,
            max_consecutive_failures=settings.provider_health_max_consecutive_failures,
            open_score_threshold=settings.provider_health_open_score,
            captcha_trip_rate=settings.provider_health_captcha_trip_rate,
        )
    return _DEFAULT_MONITOR
