"""Crawl worker — long-running loop draining the frontier.

``CrawlWorker`` repeatedly claims a batch via ``frontier.pop_batch`` and
processes it through the pipeline with a bounded concurrency semaphore.
When the frontier is empty it idles for ``interval`` seconds.

Shutdown is graceful in both modes: ``stop()`` lets the in-flight batch
finish before the loop exits, and task cancellation awaits the same
in-flight work before re-raising — no claimed URL is abandoned mid-write.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.pipeline import CrawlOutcome, CrawlPipeline

logger = logging.getLogger(__name__)


class CrawlWorker:
    """Batch loop: pop → process (≤ ``concurrency`` in flight) → idle."""

    def __init__(
        self,
        pipeline: CrawlPipeline,
        *,
        interval: float = 5.0,
        batch_size: int = 10,
        concurrency: int = 5,
    ) -> None:
        self._pipeline = pipeline
        self._interval = interval
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._stop = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()

    def stop(self) -> None:
        """Ask the loop to exit after the current batch completes."""
        self._stop.set()

    async def run_once(self) -> list[CrawlOutcome]:
        """Pop one batch and process it; returns outcomes ([] when idle)."""
        batch = await self._pipeline.frontier.pop_batch(self._batch_size)
        if not batch:
            return []
        sem = asyncio.Semaphore(self._concurrency)

        async def _one(task) -> CrawlOutcome:
            async with sem:
                return await self._pipeline.process_one(task)

        inflight = {asyncio.ensure_future(_one(t)) for t in batch}
        self._inflight = inflight
        gather = asyncio.gather(*inflight)
        try:
            try:
                return list(await asyncio.shield(gather))
            except asyncio.CancelledError:
                # The shield keeps the batch running while cancellation
                # reaches us — wait for in-flight URLs to land, then let
                # the CancelledError propagate.
                await gather
                raise
        finally:
            self._inflight = set()

    async def run_forever(self) -> None:
        """Main loop; exits on stop() or cancellation (gracefully)."""
        try:
            while not self._stop.is_set():
                outcomes = await self.run_once()
                if not outcomes:
                    # Idle poll — stop() wakes immediately.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        except asyncio.CancelledError:
            # Lifespan shutdown: let in-flight URLs land before propagating.
            if self._inflight:
                await asyncio.gather(*self._inflight, return_exceptions=True)
            raise
        logger.info("crawl worker stopped")
