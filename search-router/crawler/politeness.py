"""Per-domain politeness: minimum interval between fetches to one host.

``DomainRateLimiter.wait(domain)`` serializes requests per domain via an
``asyncio.Lock`` and sleeps until ``min_interval`` seconds have passed since
the previous fetch *to that domain*. Other domains are unaffected.

A robots.txt ``Crawl-delay`` larger than ``min_interval`` wins, capped at
``max_crawl_delay`` (default 30 s) so a hostile robots file cannot freeze a
worker for hours.

The per-domain map is bounded (``max_domains``): entries may be evicted on
acquire only when they are *fully idle* — no holder, no queued waiter — AND
their cooldown has already expired. A ``Lock.locked()`` check alone is not
enough: a scheduled-but-not-yet-holding waiter reads as unheld, and evicting
it would mint a second lock for the same domain (split serialization, two
fetches racing the same host). Likewise evicting a live cooldown would erase
the rate-limit timestamp and let the next fetch skip its wait entirely.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

DEFAULT_MIN_INTERVAL_S = 2.0
DEFAULT_MAX_CRAWL_DELAY_S = 30.0
DEFAULT_MAX_DOMAINS = 1024


@dataclass
class _DomainEntry:
    """Per-domain state: serialization lock + cooldown + liveness counts."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0  # currently inside the critical section
    waiters: int = 0  # queued on the lock (scheduled but not holding)
    next_allowed: float = 0.0  # monotonic deadline for the next fetch


class DomainRateLimiter:
    """Token-bucket-lite: one outstanding wait per domain, min spacing."""

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL_S,
        max_crawl_delay: float = DEFAULT_MAX_CRAWL_DELAY_S,
        max_domains: int = DEFAULT_MAX_DOMAINS,
    ) -> None:
        self._min_interval = min_interval
        self._max_crawl_delay = max_crawl_delay
        self._max_domains = max_domains
        self._domains: dict[str, _DomainEntry] = {}
        self._guard = asyncio.Lock()

    def _evict_idle(self, keep: str) -> None:
        """Drop entries that are idle AND off cooldown until under the bound.

        Entries with a holder or a queued waiter are never evicted — that
        would split the domain's serialization onto a second lock. Entries
        whose ``next_allowed`` is still in the future are kept too: evicting
        them would discard the cooldown and let the next fetch skip its wait.
        """
        now = time.monotonic()
        for domain, entry in list(self._domains.items()):
            if len(self._domains) <= self._max_domains:
                break
            if domain == keep:
                continue
            if entry.holders or entry.waiters:
                continue
            if entry.next_allowed > now:
                continue  # cooldown still in effect — keep the timestamp
            del self._domains[domain]

    async def wait(self, domain: str, crawl_delay: float | None = None) -> float:
        """Sleep until ``domain`` may be fetched again. Returns seconds slept."""
        interval = self._min_interval
        if crawl_delay:
            interval = max(interval, min(crawl_delay, self._max_crawl_delay))

        async with self._guard:
            entry = self._domains.setdefault(domain, _DomainEntry())
            entry.waiters += 1
            if len(self._domains) > self._max_domains:
                self._evict_idle(domain)

        try:
            await entry.lock.acquire()
        finally:
            entry.waiters -= 1
        entry.holders += 1
        try:
            now = time.monotonic()
            slept = 0.0
            # asyncio.sleep may wake a timer-tick early on some
            # platforms — loop until the interval genuinely elapsed.
            while True:
                remaining = entry.next_allowed - now
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
                slept += remaining
                now = time.monotonic()
            entry.next_allowed = time.monotonic() + interval
            return slept
        finally:
            entry.holders -= 1
            entry.lock.release()
