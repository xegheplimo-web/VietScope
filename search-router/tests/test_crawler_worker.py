"""Tests for crawler/worker.py — batch loop, concurrency cap, shutdown."""

from __future__ import annotations

import asyncio
import time

from crawler.pipeline import CrawlOutcome
from crawler.worker import CrawlWorker
from workers.freshness_worker import RecrawlTask


def _run(coro):
    return asyncio.run(coro)


class WFrontier:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.pops = 0

    async def pop_batch(self, n):
        self.pops += 1
        out, self.tasks = self.tasks[:n], self.tasks[n:]
        return out


class WPipeline:
    def __init__(self, frontier, process_fn=None):
        self.frontier = frontier
        self._fn = process_fn
        self.concurrent = 0
        self.max_concurrent = 0
        self.processed: list[str] = []

    async def process_one(self, task) -> CrawlOutcome:
        if self._fn is not None:
            return await self._fn(task)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0.02)
            self.processed.append(task.url)
            return CrawlOutcome(url=task.url, outcome="changed")
        finally:
            self.concurrent -= 1


def _tasks(n: int) -> list[RecrawlTask]:
    return [RecrawlTask(url=f"https://a.vn/{i}", priority=0.5, scheduled_at=0.0) for i in range(n)]


def test_run_once_processes_batch():
    frontier = WFrontier(_tasks(7))
    pipe = WPipeline(frontier)
    worker = CrawlWorker(pipe, interval=0.01, batch_size=10, concurrency=5)
    outcomes = _run(worker.run_once())
    assert len(outcomes) == 7
    assert len(pipe.processed) == 7
    assert all(o.outcome == "changed" for o in outcomes)


def test_run_once_empty_returns_empty():
    worker = CrawlWorker(WPipeline(WFrontier([])), interval=0.01)
    assert _run(worker.run_once()) == []


def test_concurrency_capped():
    frontier = WFrontier(_tasks(12))
    pipe = WPipeline(frontier)
    worker = CrawlWorker(pipe, interval=0.01, batch_size=12, concurrency=3)
    _run(worker.run_once())
    assert 1 < pipe.max_concurrent <= 3


def test_run_forever_drains_then_stops():
    frontier = WFrontier(_tasks(4))
    pipe = WPipeline(frontier)
    worker = CrawlWorker(pipe, interval=0.02, batch_size=10, concurrency=2)

    async def main():
        run = asyncio.ensure_future(worker.run_forever())
        await asyncio.sleep(0.3)  # let it drain + idle a few loops
        worker.stop()
        await asyncio.wait_for(run, timeout=5)

    _run(main())
    assert len(pipe.processed) == 4
    assert frontier.pops >= 2  # kept polling while idle


def test_graceful_stop_finishes_inflight_batch():
    release = asyncio.Event()
    done: list[str] = []

    async def slow(task) -> CrawlOutcome:
        await release.wait()
        done.append(task.url)
        return CrawlOutcome(url=task.url, outcome="changed")

    frontier = WFrontier(_tasks(3))
    pipe = WPipeline(frontier, process_fn=slow)
    worker = CrawlWorker(pipe, interval=0.01, batch_size=10, concurrency=3)

    async def main():
        run = asyncio.ensure_future(worker.run_forever())
        await asyncio.sleep(0.05)  # all 3 tasks in-flight, blocked on release
        worker.stop()
        await asyncio.sleep(0.02)
        release.set()  # let them finish
        await asyncio.wait_for(run, timeout=5)
        return done

    assert sorted(_run(main())) == sorted(done)


def test_stop_before_start_exits_quickly():
    worker = CrawlWorker(WPipeline(WFrontier(_tasks(2))), interval=0.01)
    worker.stop()

    async def main():
        t0 = time.monotonic()
        await worker.run_forever()
        return time.monotonic() - t0

    assert _run(main()) < 2.0


def test_cancellation_finishes_inflight():
    # task.cancel() (lifespan shutdown) must still let in-flight work land.
    release = asyncio.Event()
    done: list[str] = []

    async def slow(task) -> CrawlOutcome:
        await release.wait()
        done.append(task.url)
        return CrawlOutcome(url=task.url, outcome="changed")

    frontier = WFrontier(_tasks(2))
    pipe = WPipeline(frontier, process_fn=slow)
    worker = CrawlWorker(pipe, interval=0.01, batch_size=10, concurrency=2)

    async def main():
        run = asyncio.ensure_future(worker.run_forever())
        await asyncio.sleep(0.05)
        run.cancel()
        await asyncio.sleep(0.02)
        release.set()
        try:
            await asyncio.wait_for(run, timeout=5)
        except asyncio.CancelledError:
            pass

    _run(main())
    assert len(done) == 2
