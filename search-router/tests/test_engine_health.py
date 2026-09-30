"""Tests for Engine Health Manager (Phase 1)."""

import time

from core.engine_health import EngineHealthManager, EngineHealthStats, get_engine_health_manager


class TestEngineHealthStats:
    """Unit tests for the EngineHealthStats dataclass."""

    def test_initial_state(self):
        stats = EngineHealthStats(engine_name="test_engine")
        assert stats.engine_name == "test_engine"
        assert stats.total_requests == 0
        assert stats.successful_requests == 0
        assert stats.failed_requests == 0
        assert stats.rate_limited_count == 0
        assert stats.captcha_count == 0
        assert stats.total_latency_ms == 0.0
        assert stats.last_success is None
        assert stats.last_failure is None
        assert stats.temporary_disabled_until is None
        assert stats.consecutive_failures == 0
        assert stats.success_rate == 1.0  # No data = 100% success
        assert stats.failure_rate == 0.0
        assert stats.rate_429 == 0.0
        assert stats.captcha_rate == 0.0
        assert stats.avg_latency_ms == 0.0
        assert not stats.is_disabled

    def test_success_rate(self):
        stats = EngineHealthStats(engine_name="test")
        stats.total_requests = 10
        stats.successful_requests = 8
        assert stats.success_rate == 0.8

    def test_failure_rate(self):
        stats = EngineHealthStats(engine_name="test")
        stats.total_requests = 10
        stats.failed_requests = 3
        assert stats.failure_rate == 0.3

    def test_rate_429(self):
        stats = EngineHealthStats(engine_name="test")
        stats.total_requests = 10
        stats.rate_limited_count = 2
        assert stats.rate_429 == 0.2

    def test_captcha_rate(self):
        stats = EngineHealthStats(engine_name="test")
        stats.total_requests = 10
        stats.captcha_count = 1
        assert stats.captcha_rate == 0.1

    def test_avg_latency(self):
        stats = EngineHealthStats(engine_name="test")
        stats.total_requests = 3
        stats.total_latency_ms = 300.0
        assert stats.avg_latency_ms == 100.0

    def test_is_disabled_when_in_cooldown(self):
        stats = EngineHealthStats(engine_name="test")
        stats.temporary_disabled_until = time.time() + 300
        assert stats.is_disabled

    def test_is_disabled_when_cooldown_expired(self):
        stats = EngineHealthStats(engine_name="test")
        stats.temporary_disabled_until = time.time() - 1
        assert not stats.is_disabled

    def test_is_disabled_when_not_set(self):
        stats = EngineHealthStats(engine_name="test")
        assert not stats.is_disabled


class TestEngineHealthManager:
    """Tests for the EngineHealthManager."""

    def test_record_success(self):
        mgr = EngineHealthManager()
        mgr.record_success("engine_a", latency_ms=50.0)
        stats = mgr.get_stats("engine_a")
        assert stats is not None
        assert stats.total_requests == 1
        assert stats.successful_requests == 1
        assert stats.failed_requests == 0
        assert stats.total_latency_ms == 50.0
        assert stats.last_success is not None
        assert stats.consecutive_failures == 0

    def test_record_failure(self):
        mgr = EngineHealthManager()
        mgr.record_failure("engine_b", is_429=True, latency_ms=100.0)
        stats = mgr.get_stats("engine_b")
        assert stats is not None
        assert stats.total_requests == 1
        assert stats.failed_requests == 1
        assert stats.rate_limited_count == 1
        assert stats.last_failure is not None
        assert stats.consecutive_failures == 1

    def test_record_captcha_failure(self):
        mgr = EngineHealthManager()
        mgr.record_failure("engine_c", is_captcha=True)
        stats = mgr.get_stats("engine_c")
        assert stats is not None
        assert stats.captcha_count == 1

    def test_is_enabled_for_unknown_engine(self):
        mgr = EngineHealthManager()
        assert mgr.is_engine_enabled("unknown_engine") is True

    def test_auto_disable_on_high_failure_rate(self):
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=3,
        )
        # 3 failures out of 3 requests = 100% failure rate
        for _ in range(3):
            mgr.record_failure("failing_engine")
        assert not mgr.is_engine_enabled("failing_engine")

    def test_auto_disable_on_consecutive_failures(self):
        mgr = EngineHealthManager(max_consecutive_failures=3, min_requests_for_disable=3)
        mgr.record_failure("engine_d")
        mgr.record_failure("engine_d")
        mgr.record_failure("engine_d")
        assert not mgr.is_engine_enabled("engine_d")

    def test_auto_disable_on_429_rate(self):
        mgr = EngineHealthManager(
            rate_429_threshold=0.3,
            min_requests_for_disable=3,
        )
        for _ in range(3):
            mgr.record_failure("rate_limited", is_429=True)
        assert not mgr.is_engine_enabled("rate_limited")

    def test_auto_disable_on_captcha_rate(self):
        mgr = EngineHealthManager(
            captcha_threshold=0.2,
            min_requests_for_disable=3,
        )
        for _ in range(3):
            mgr.record_failure("captcha_engine", is_captcha=True)
        assert not mgr.is_engine_enabled("captcha_engine")

    def test_reenable_after_cooldown(self):
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=2,
            cooldown_seconds=0.01,  # Very short cooldown
        )
        for _ in range(3):
            mgr.record_failure("temp_disabled")
        assert not mgr.is_engine_enabled("temp_disabled")
        # After cooldown expires, should be re-enabled
        time.sleep(0.02)
        assert mgr.is_engine_enabled("temp_disabled")

    def test_no_disable_with_min_requests_not_met(self):
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=10,
        )
        for _ in range(5):
            mgr.record_failure("engine_e")
        # Only 5 requests, threshold is 10
        assert mgr.is_engine_enabled("engine_e")

    def test_clears_disable_on_success(self):
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=2,
            cooldown_seconds=300.0,
        )
        for _ in range(3):
            mgr.record_failure("flaky_engine")
        assert not mgr.is_engine_enabled("flaky_engine")
        # Force the disable to expire so success can clear it
        stats = mgr.get_stats("flaky_engine")
        stats.temporary_disabled_until = time.time() - 1  # expired
        # A success should clear the disable
        mgr.record_success("flaky_engine", latency_ms=50.0)
        stats = mgr.get_stats("flaky_engine")
        assert stats.temporary_disabled_until is None
        assert not stats.is_disabled

    def test_success_resets_consecutive_failures(self):
        mgr = EngineHealthManager(max_consecutive_failures=2)
        mgr.record_failure("engine_f")
        mgr.record_failure("engine_f")
        assert mgr.get_stats("engine_f").consecutive_failures == 2
        mgr.record_success("engine_f", latency_ms=10.0)
        assert mgr.get_stats("engine_f").consecutive_failures == 0

    def test_get_all_stats(self):
        mgr = EngineHealthManager()
        mgr.record_success("e1", latency_ms=10.0)
        mgr.record_failure("e2")
        stats = mgr.get_all_stats()
        assert "e1" in stats
        assert "e2" in stats

    def test_get_disabled_engines(self):
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=2,
        )
        for _ in range(3):
            mgr.record_failure("disabled_engine")
        mgr.record_success("healthy_engine", latency_ms=10.0)
        disabled = mgr.get_disabled_engines()
        assert "disabled_engine" in disabled
        assert "healthy_engine" not in disabled

    def test_default_manager_singleton(self):
        mgr1 = get_engine_health_manager()
        mgr2 = get_engine_health_manager()
        assert mgr1 is mgr2

    def test_disabled_engine_stats(self):
        """Verify that disabled engines report correct rates."""
        mgr = EngineHealthManager(
            failure_threshold=0.5,
            min_requests_for_disable=3,
        )
        for _ in range(6):
            mgr.record_failure("test_engine", is_429=True, is_captcha=True, latency_ms=100.0)
        stats = mgr.get_stats("test_engine")
        assert stats.total_requests == 6
        assert stats.success_rate == 0.0
        assert stats.failure_rate == 1.0
        assert stats.rate_429 == 1.0
        assert stats.captcha_rate == 1.0
        assert stats.avg_latency_ms == 100.0
        # Engine is disabled with a 300s cooldown
        assert stats.is_disabled
