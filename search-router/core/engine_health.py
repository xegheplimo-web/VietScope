"""Engine Health Manager — tracks per-engine performance and auto-disables failing engines.

Phase 1: Track success_rate, latency, 429_rate, captcha_rate, last_success,
temporary_disabled_until. Router auto-disables engines that exceed failure
thresholds and re-enables them after a cooldown period.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import RLock

logger = logging.getLogger(__name__)


@dataclass
class EngineHealthStats:
    """Health metrics for a single search engine."""

    engine_name: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limited_count: int = 0
    captcha_count: int = 0
    total_latency_ms: float = 0.0
    last_success: float | None = None
    last_failure: float | None = None
    temporary_disabled_until: float | None = None
    consecutive_failures: int = 0

    @property
    def success_rate(self) -> float:
        """Success ratio in [0, 1]."""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests

    @property
    def failure_rate(self) -> float:
        """Failure ratio in [0, 1]."""
        if self.total_requests == 0:
            return 0.0
        return self.failed_requests / self.total_requests

    @property
    def rate_429(self) -> float:
        """HTTP 429 ratio in [0, 1]."""
        if self.total_requests == 0:
            return 0.0
        return self.rate_limited_count / self.total_requests

    @property
    def captcha_rate(self) -> float:
        """CAPTCHA/SolveMedia ratio in [0, 1]."""
        if self.total_requests == 0:
            return 0.0
        return self.captcha_count / self.total_requests

    @property
    def avg_latency_ms(self) -> float:
        """Average latency in milliseconds."""
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests

    @property
    def is_disabled(self) -> bool:
        """True when the engine is temporarily disabled."""
        if self.temporary_disabled_until is None:
            return False
        return time.time() < self.temporary_disabled_until


class EngineHealthManager:
    """Thread-safe health tracker for search engines.

    Records per-engine success/failure/latency stats and temporarily disables
    engines that exceed configurable failure thresholds.  Disabled engines
    are re-enabled after a cooldown period.
    """

    def __init__(
        self,
        failure_threshold: float = 0.5,
        rate_429_threshold: float = 0.3,
        captcha_threshold: float = 0.2,
        max_consecutive_failures: int = 5,
        cooldown_seconds: float = 300.0,
        min_requests_for_disable: int = 5,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._rate_429_threshold = rate_429_threshold
        self._captcha_threshold = captcha_threshold
        self._max_consecutive_failures = max_consecutive_failures
        self._cooldown_seconds = cooldown_seconds
        self._min_requests_for_disable = min_requests_for_disable
        self._stats: dict[str, EngineHealthStats] = {}
        self._lock = RLock()

    def _get_or_create(self, engine_name: str) -> EngineHealthStats:
        stats = self._stats.get(engine_name)
        if stats is None:
            stats = EngineHealthStats(engine_name=engine_name)
            self._stats[engine_name] = stats
        return stats

    def record_success(self, engine_name: str, latency_ms: float) -> None:
        """Record a successful engine response."""
        with self._lock:
            stats = self._get_or_create(engine_name)
            stats.total_requests += 1
            stats.successful_requests += 1
            stats.total_latency_ms += latency_ms
            stats.last_success = time.time()
            stats.consecutive_failures = 0
            # Clear any temporary disable on success
            if (
                stats.temporary_disabled_until is not None
                and time.time() >= stats.temporary_disabled_until
            ):
                stats.temporary_disabled_until = None

    def record_failure(
        self,
        engine_name: str,
        *,
        is_429: bool = False,
        is_captcha: bool = False,
        latency_ms: float = 0.0,
    ) -> None:
        """Record a failed engine response. May trigger auto-disable."""
        with self._lock:
            stats = self._get_or_create(engine_name)
            stats.total_requests += 1
            stats.failed_requests += 1
            stats.total_latency_ms += latency_ms
            stats.last_failure = time.time()
            stats.consecutive_failures += 1
            if is_429:
                stats.rate_limited_count += 1
            if is_captcha:
                stats.captcha_count += 1

            # Auto-disable when thresholds are exceeded with enough data
            if stats.total_requests >= self._min_requests_for_disable:
                should_disable = (
                    stats.failure_rate >= self._failure_threshold
                    or stats.rate_429 >= self._rate_429_threshold
                    or stats.captcha_rate >= self._captcha_threshold
                    or stats.consecutive_failures >= self._max_consecutive_failures
                )
                if should_disable and stats.temporary_disabled_until is None:
                    stats.temporary_disabled_until = time.time() + self._cooldown_seconds
                    logger.warning(
                        "Auto-disabling engine %r: failure_rate=%.2f, 429_rate=%.2f, "
                        "captcha_rate=%.2f, consecutive_failures=%d (cooldown %.0fs)",
                        engine_name,
                        stats.failure_rate,
                        stats.rate_429,
                        stats.captcha_rate,
                        stats.consecutive_failures,
                        self._cooldown_seconds,
                    )

    def is_engine_enabled(self, engine_name: str) -> bool:
        """Check whether an engine is currently enabled (not disabled)."""
        with self._lock:
            stats = self._stats.get(engine_name)
            if stats is None:
                return True  # No data = enabled
            if stats.temporary_disabled_until is None:
                return True
            # Cooldown expired → re-enable
            if time.time() >= stats.temporary_disabled_until:
                stats.temporary_disabled_until = None
                logger.info("Re-enabling engine %r (cooldown expired)", engine_name)
                return True
            return False

    def get_stats(self, engine_name: str) -> EngineHealthStats | None:
        """Return health stats for a single engine (or None)."""
        with self._lock:
            return self._stats.get(engine_name)

    def get_all_stats(self) -> dict[str, EngineHealthStats]:
        """Return a copy of all engine health stats."""
        with self._lock:
            return dict(self._stats)

    def get_disabled_engines(self) -> list[str]:
        """Return list of currently disabled engine names."""
        with self._lock:
            return [name for name, stats in self._stats.items() if stats.is_disabled]


# Singleton process-wide engine health manager
_DEFAULT_MANAGER: EngineHealthManager | None = None


def get_engine_health_manager() -> EngineHealthManager:
    """Return the process-wide default EngineHealthManager singleton."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = EngineHealthManager()
    return _DEFAULT_MANAGER
