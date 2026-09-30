"""Tests for crawler/politeness.py — DomainRateLimiter."""

from __future__ import annotations

import asyncio
import time

from crawler.politeness import DomainRateLimiter


def _run(coro):
    return asyncio.run(coro)


def test_same_domain_enforces_min_interval():
    limiter = DomainRateLimiter(min_interval=0.08)
    t0 = time.monotonic()
    _run(limiter.wait("example.com"))
    _run(limiter.wait("example.com"))
    assert time.monotonic() - t0 >= 0.08


def test_first_fetch_does_not_sleep():
    limiter = DomainRateLimiter(min_interval=5.0)
    t0 = time.monotonic()
    _run(limiter.wait("example.com"))
    assert time.monotonic() - t0 < 1.0


def test_different_domains_do_not_block_each_other():
    limiter = DomainRateLimiter(min_interval=5.0)
    t0 = time.monotonic()
    _run(limiter.wait("a.example"))
    _run(limiter.wait("b.example"))
    _run(limiter.wait("c.example"))
    assert time.monotonic() - t0 < 1.0


def test_crawl_delay_overrides_min_interval():
    limiter = DomainRateLimiter(min_interval=0.02)
    t0 = time.monotonic()
    _run(limiter.wait("example.com", crawl_delay=0.1))
    _run(limiter.wait("example.com", crawl_delay=0.1))
    assert time.monotonic() - t0 >= 0.1


def test_crawl_delay_below_min_interval_ignored():
    limiter = DomainRateLimiter(min_interval=0.08)
    t0 = time.monotonic()
    _run(limiter.wait("example.com", crawl_delay=0.01))
    _run(limiter.wait("example.com", crawl_delay=0.01))
    assert time.monotonic() - t0 >= 0.08


def test_malicious_crawl_delay_capped():
    # robots declaring a huge crawl-delay must not freeze the worker.
    limiter = DomainRateLimiter(min_interval=0.02, max_crawl_delay=0.05)
    t0 = time.monotonic()
    _run(limiter.wait("example.com", crawl_delay=3600.0))
    _run(limiter.wait("example.com", crawl_delay=3600.0))
    elapsed = time.monotonic() - t0
    assert 0.05 <= elapsed < 1.0


def test_concurrent_same_domain_serialized():
    # Two concurrent waits on one domain must both see the interval —
    # the lock serializes timestamp updates.
    limiter = DomainRateLimiter(min_interval=0.08)

    async def main():
        t0 = time.monotonic()
        await asyncio.gather(
            limiter.wait("example.com"),
            limiter.wait("example.com"),
            limiter.wait("example.com"),
        )
        return time.monotonic() - t0

    elapsed = _run(main())
    assert elapsed >= 0.16


def test_locks_map_bounded():
    # L12: per-domain lock/timestamp state must not grow without bound.
    limiter = DomainRateLimiter(min_interval=0.001, max_domains=64)
    for i in range(1000):
        _run(limiter.wait(f"h{i}.vn"))
    assert len(limiter._domains) <= 64


# ─── F5: eviction must respect cooldowns and queued waiters ──────────────


def test_eviction_preserves_active_cooldown():
    # max_domains=1: wait(A) → wait(B) → wait(A) must still honor A's
    # cooldown — evicting A's timestamp would let the refetch run early.
    limiter = DomainRateLimiter(min_interval=0.05, max_domains=1)
    _run(limiter.wait("a.vn"))
    _run(limiter.wait("b.vn"))  # eviction pass: A's cooldown is live → kept
    slept = _run(limiter.wait("a.vn"))
    assert slept > 0.01


def test_evicted_domain_after_cooldown_is_fine():
    # Once the cooldown has expired the entry may be dropped — the next
    # wait owes nothing.
    limiter = DomainRateLimiter(min_interval=0.01, max_domains=1)
    _run(limiter.wait("a.vn"))
    time.sleep(0.02)  # let A's cooldown lapse
    _run(limiter.wait("b.vn"))
    slept = _run(limiter.wait("a.vn"))
    assert slept < 0.01


def test_queued_waiter_not_evicted():
    # A domain whose lock has a scheduled waiter must survive eviction —
    # otherwise a fresh lock splits serialization for that host.
    limiter = DomainRateLimiter(min_interval=60.0, max_domains=1)

    async def main():
        await limiter.wait("a.vn")  # first call only stamps the interval
        holder = asyncio.create_task(limiter.wait("a.vn"))
        await asyncio.sleep(0.02)  # holder inside the critical section
        waiter = asyncio.create_task(limiter.wait("a.vn"))
        await asyncio.sleep(0.02)  # waiter queued on a.vn's lock
        other = asyncio.create_task(limiter.wait("b.vn"))  # forces eviction
        await asyncio.sleep(0.02)
        assert "a.vn" in limiter._domains
        assert limiter._domains["a.vn"].holders == 1
        assert limiter._domains["a.vn"].waiters == 1
        for t in (holder, waiter, other):
            t.cancel()
        for t in (holder, waiter, other):
            try:
                await t
            except asyncio.CancelledError:
                pass

    _run(main())
