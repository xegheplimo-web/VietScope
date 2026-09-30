"""Tests for the FreshnessWorker persistent frontier (crawl_frontier).

Two fakes, two levels of coverage:

- ``FakePool`` records statements — asserts the SQL shape and parameters.
- ``SimPool`` emulates the crawl_frontier table semantics in memory
  (claim lease, ownership tokens, due-time gating) so recrawl/reclaim/flush
  behavior is genuinely exercised end-to-end without Postgres.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from storage import pg_client
from workers import freshness_worker
from workers.freshness_worker import FreshnessWorker, RecrawlTask


class FakePool:
    """Minimal asyncpg-pool stand-in: records execute/fetch calls."""

    def __init__(self, rows: list[dict] | None = None, raises: bool = False):
        self.rows = rows or []
        self.raises = raises
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise RuntimeError("db down")
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise RuntimeError("db down")
        return self.rows


class SimPool:
    """Stateful crawl_frontier emulation.

    Matches the worker's module-level SQL constants and applies their
    semantics to in-memory rows — including the claim lease, ownership
    tokens, and the done-vs-queued completion split — so a pop query that
    never selects the right rows fails the test for real.
    """

    def __init__(self, rows: list[dict] | None = None, raises: bool = False):
        self.rows: dict[str, dict] = {}
        self.raises = raises
        self.calls: list[tuple[str, tuple]] = []
        self._offset = timedelta(0)
        self._token_seq = 0
        for row in rows or []:
            self.rows[row["url"]] = self._fresh_row(**row)

    @property
    def now(self) -> datetime:
        """Simulated DB clock: real time + test-controlled offset."""
        return datetime.now(UTC) + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += timedelta(seconds=seconds)

    @staticmethod
    def _fresh_row(**kw) -> dict:
        row = {
            "domain": None,
            "status": "queued",
            "priority": 0.0,
            "scheduled_at": None,
            "next_crawl_at": None,
            "discovered_from": None,
            "change_rate": None,
            "claimed_at": None,
            "claim_token": None,
            "failure_count": 0,
            "last_crawled_at": None,
            "fetched_at": None,
            "etag": None,
            "last_modified": None,
        }
        row.update(kw)
        return row

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise RuntimeError("db down")
        if sql == freshness_worker._ENQUEUE_SQL:
            return self._enqueue(*args)
        if sql == freshness_worker._COMPLETE_SQL:
            return self._complete(*args)
        if sql == freshness_worker._FAIL_SQL:
            return self._fail(*args)
        if sql == freshness_worker._CHANGE_RATE_SQL:
            row = self.rows.get(args[0])
            if row is None:
                return "UPDATE 0"
            row["change_rate"] = args[1]
            return "UPDATE 1"
        return "OK"

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.raises:
            raise RuntimeError("db down")
        assert sql == freshness_worker._POP_SQL
        limit, lease_seconds = args
        stale_before = self.now - timedelta(seconds=lease_seconds)
        eligible = [
            row
            for row in self.rows.values()
            if (
                row["status"] == "queued"
                and (row["next_crawl_at"] is None or row["next_crawl_at"] <= self.now)
            )
            or (
                row["status"] == "fetching"
                and (row["claimed_at"] is None or row["claimed_at"] < stale_before)
            )
        ]
        eligible.sort(key=lambda r: (-r["priority"], r["scheduled_at"] or self.now))
        claimed = []
        for row in eligible[:limit]:
            self._token_seq += 1
            row["status"] = "fetching"
            row["claimed_at"] = self.now
            row["claim_token"] = f"tok-{self._token_seq}"
            row["last_crawled_at"] = self.now
            claimed.append(dict(row))
        return claimed

    # ── Statement semantics ──────────────────────────────────────────────

    def _enqueue(self, url, domain, priority, next_crawl_at, discovered_from, change_rate):
        row = self.rows.get(url)
        if row is None:
            self.rows[url] = self._fresh_row(
                url=url,
                domain=domain,
                status="queued",
                priority=priority,
                scheduled_at=self.now,
                next_crawl_at=next_crawl_at,
                discovered_from=discovered_from,
                change_rate=change_rate,
            )
            return "INSERT 0 1"
        row["priority"] = max(row["priority"], priority)
        candidates = [t for t in (row["next_crawl_at"], next_crawl_at) if t is not None]
        row["next_crawl_at"] = min(candidates) if candidates else None
        row["discovered_from"] = row["discovered_from"] or discovered_from
        row["change_rate"] = change_rate if change_rate is not None else row["change_rate"]
        if row["status"] in ("done", "failed"):
            row["status"] = "queued"
            row["claimed_at"] = None
            row["claim_token"] = None
        return "INSERT 0 1"

    def _complete(self, url, next_crawl_at, etag, last_modified, change_rate, claim_token):
        row = self.rows.get(url)
        # claim_token IS NOT DISTINCT FROM $6 — a stale claim can't clobber.
        if row is None or row["claim_token"] != claim_token:
            return "UPDATE 0"
        row["status"] = "queued" if next_crawl_at is not None else "done"
        row["fetched_at"] = self.now
        row["last_crawled_at"] = self.now
        row["failure_count"] = 0
        row["next_crawl_at"] = next_crawl_at
        if etag is not None:
            row["etag"] = etag
        if last_modified is not None:
            row["last_modified"] = last_modified
        if change_rate is not None:
            row["change_rate"] = change_rate
        row["claimed_at"] = None
        row["claim_token"] = None
        return "UPDATE 1"

    def _fail(self, url, claim_token):
        row = self.rows.get(url)
        if row is None or row["claim_token"] != claim_token:
            return "UPDATE 0"
        row["status"] = "queued"
        row["fetched_at"] = self.now
        row["failure_count"] += 1
        row["next_crawl_at"] = self.now + timedelta(minutes=min(10 * row["failure_count"], 720))
        row["claimed_at"] = None
        row["claim_token"] = None
        return "UPDATE 1"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def worker():
    return FreshnessWorker()


@pytest.fixture
def pool(monkeypatch):
    """Patch pg_client.get_pool → FakePool; tests override .rows/.raises."""
    fake = FakePool()
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=fake))
    return fake


@pytest.fixture
def sim(monkeypatch):
    """Patch pg_client.get_pool → SimPool (stateful frontier emulation)."""
    fake = SimPool()
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=fake))
    return fake


@pytest.fixture
def no_pool(monkeypatch):
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=None))


def _task(url: str, priority: float = 0.5, scheduled_at: float = 0.0) -> RecrawlTask:
    return RecrawlTask(url=url, priority=priority, scheduled_at=scheduled_at)


# ─── DB path ──────────────────────────────────────────────────────────────


def test_enqueue_writes_frontier(worker, pool):
    task = RecrawlTask(
        url="https://example.com/a",
        priority=0.9,
        scheduled_at=0,
        discovered_from="https://hub.example",
        change_rate_estimate=0.3,
    )
    assert _run(worker.enqueue(task)) == "db"
    sql, args = pool.calls[-1]
    assert "INSERT INTO crawl_frontier" in sql
    assert "ON CONFLICT (url) DO UPDATE" in sql
    assert args[0] == "https://example.com/a"
    assert args[1] == "example.com"
    assert args[2] == 0.9


def test_pop_batch_claims_rows(worker, pool):
    pool.rows = [
        {
            "url": "https://a.example",
            "priority": 0.8,
            "next_crawl_at": datetime(2026, 9, 23, tzinfo=UTC),
            "last_crawled_at": None,
            "failure_count": 2,
            "discovered_from": "https://src.example",
            "change_rate": 0.4,
            "claim_token": "tok-7",
        }
    ]
    batch = _run(worker.pop_batch(5))
    sql, args = pool.calls[-1]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "claim_token" in sql
    assert args == (5, worker._claim_lease_seconds)
    assert len(batch) == 1
    task = batch[0]
    assert task.url == "https://a.example"
    assert task.priority == 0.8
    assert task.fail_count == 2
    assert task.scheduled_at > 0
    assert task.discovered_from == "https://src.example"
    assert task.change_rate_estimate == 0.4
    assert task.claim_token == "tok-7"


def test_pop_batch_reclaims_expired_claims_in_sql(worker, pool):
    _run(worker.pop_batch(5))
    sql, _ = pool.calls[-1]
    assert "fetching" in sql
    assert "claimed_at" in sql
    assert "make_interval" in sql


def test_complete_marks_done(worker, pool):
    assert _run(worker.complete("https://a.example", etag='"abc"')) is True
    sql, args = pool.calls[-1]
    # H1: status is conditional — 'done' only when no next_crawl_at.
    assert "'done'" in sql and "'queued'" in sql
    assert args[0] == "https://a.example"
    assert args[1] is None  # next_crawl_at stays NULL, not now()
    assert args[2] == '"abc"'
    assert args[5] is None  # no claim token supplied


def test_complete_with_next_crawl_requeues(worker, pool):
    assert _run(worker.complete("https://a.example", next_crawl_at=1234.5)) is True
    _, args = pool.calls[-1]
    assert args[1] == datetime.fromtimestamp(1234.5, UTC)


def test_complete_epoch_zero_passes_datetime_not_null(worker, pool):
    # r5r2: epoch 0.0 is a real timestamp, not "no recrawl" — it must reach
    # the DB as a datetime so the row goes 'queued', not 'done'.
    assert _run(worker.complete("https://a.example", next_crawl_at=0.0)) is True
    _, args = pool.calls[-1]
    assert args[1] == datetime.fromtimestamp(0.0, UTC)


def test_complete_reports_stale_claim(worker, pool):
    # UPDATE 0 = ownership token rejected — surface False to the caller.
    pool.execute = AsyncMock(return_value="UPDATE 0")
    assert _run(worker.complete("https://a.example", claim_token="tok-old")) is False


def test_fail_requeues_with_backoff(worker, pool):
    assert _run(worker.fail("https://a.example", claim_token="tok-1")) is True
    sql, args = pool.calls[-1]
    assert "failure_count + 1" in sql
    assert "make_interval" in sql
    assert args == ("https://a.example", "tok-1")


def test_fail_reports_stale_claim(worker, pool):
    pool.execute = AsyncMock(return_value="UPDATE 0")
    assert _run(worker.fail("https://a.example", claim_token="tok-old")) is False


def test_set_change_rate_db(worker, pool):
    assert _run(worker.set_change_rate("https://a.example", 0.7)) is True
    sql, args = pool.calls[-1]
    assert "change_rate = $2" in sql
    assert args == ("https://a.example", 0.7)


# ─── H1: scheduled recrawls become eligible (SimPool semantics) ───────────


def test_complete_with_next_crawl_makes_recrawl_eligible(worker, sim):
    url = "https://a.example/page"
    assert _run(worker.enqueue(_task(url))) == "db"
    batch = _run(worker.pop_batch(1))
    assert [t.url for t in batch] == [url]
    token = batch[0].claim_token
    assert token

    due = sim.now.timestamp() + 30
    assert _run(worker.complete(url, next_crawl_at=due, claim_token=token)) is True
    assert sim.rows[url]["status"] == "queued"
    assert sim.rows[url]["next_crawl_at"] == datetime.fromtimestamp(due, UTC)

    # Not due yet — pop must not return it.
    assert _run(worker.pop_batch(1)) == []
    # Once due, the scheduled recrawl pops again.
    sim.advance(31)
    batch = _run(worker.pop_batch(1))
    assert [t.url for t in batch] == [url]


def test_complete_without_next_crawl_stays_done(worker, sim):
    url = "https://a.example/done"
    _run(worker.enqueue(_task(url)))
    token = _run(worker.pop_batch(1))[0].claim_token
    assert _run(worker.complete(url, claim_token=token)) is True
    assert sim.rows[url]["status"] == "done"
    assert sim.rows[url]["next_crawl_at"] is None  # never coerced to now()
    sim.advance(10_000)
    assert _run(worker.pop_batch(1)) == []


def test_complete_epoch_zero_requeues_immediately_due(worker, sim):
    # r5r2: complete(next_crawl_at=0.0) schedules a recrawl due in the past —
    # 'queued' and pop-eligible right away, not 'done'.
    url = "https://a.example/epoch0"
    _run(worker.enqueue(_task(url)))
    token = _run(worker.pop_batch(1))[0].claim_token
    assert _run(worker.complete(url, next_crawl_at=0.0, claim_token=token)) is True
    assert sim.rows[url]["status"] == "queued"
    assert sim.rows[url]["next_crawl_at"] == datetime.fromtimestamp(0.0, UTC)
    assert [t.url for t in _run(worker.pop_batch(1))] == [url]


# ─── H2: claim lease + ownership tokens (SimPool semantics) ───────────────


def test_expired_fetching_claim_is_reclaimed(sim):
    worker = FreshnessWorker(claim_lease_seconds=60)
    url = "https://a.example/stuck"
    _run(worker.enqueue(_task(url)))
    first = _run(worker.pop_batch(1))[0]
    assert sim.rows[url]["status"] == "fetching"
    first_token = first.claim_token

    # Worker "crashed" — no complete/fail. Within the lease: not reclaimable.
    sim.advance(30)
    assert _run(worker.pop_batch(1)) == []

    # Past the lease: the row is reclaimed with a fresh ownership token.
    sim.advance(31)
    second = _run(worker.pop_batch(1))
    assert [t.url for t in second] == [url]
    assert second[0].claim_token != first_token


def test_late_complete_with_stale_token_does_not_clobber(sim):
    worker = FreshnessWorker(claim_lease_seconds=60)
    url = "https://a.example/race"
    _run(worker.enqueue(_task(url)))
    stale_token = _run(worker.pop_batch(1))[0].claim_token

    sim.advance(61)  # lease expires; another worker reclaims
    fresh_token = _run(worker.pop_batch(1))[0].claim_token
    assert fresh_token != stale_token

    # The crashed worker wakes and completes late — must not clobber.
    assert _run(worker.complete(url, claim_token=stale_token)) is False
    assert sim.rows[url]["status"] == "fetching"
    assert sim.rows[url]["claim_token"] == fresh_token

    # The current owner still completes normally.
    assert _run(worker.complete(url, claim_token=fresh_token)) is True
    assert sim.rows[url]["status"] == "done"


def test_late_fail_with_stale_token_does_not_clobber(sim):
    worker = FreshnessWorker(claim_lease_seconds=60)
    url = "https://a.example/race-fail"
    _run(worker.enqueue(_task(url)))
    stale_token = _run(worker.pop_batch(1))[0].claim_token
    sim.advance(61)
    fresh_token = _run(worker.pop_batch(1))[0].claim_token
    assert _run(worker.fail(url, claim_token=stale_token)) is False
    assert sim.rows[url]["status"] == "fetching"
    assert sim.rows[url]["claim_token"] == fresh_token


def test_enqueue_does_not_break_live_claim(sim):
    worker = FreshnessWorker(claim_lease_seconds=60)
    url = "https://a.example/inflight"
    _run(worker.enqueue(_task(url, priority=0.1)))
    token = _run(worker.pop_batch(1))[0].claim_token

    _run(worker.enqueue(_task(url, priority=0.9)))
    assert sim.rows[url]["status"] == "fetching"
    assert sim.rows[url]["claim_token"] == token


def test_claimless_complete_still_works_on_never_claimed_row(sim):
    worker = FreshnessWorker(claim_lease_seconds=60)
    url = "https://a.example/manual"
    _run(worker.enqueue(_task(url)))
    # No pop → row has no claim token; token-less complete matches.
    assert _run(worker.complete(url)) is True
    assert sim.rows[url]["status"] == "done"


# ─── M1: RAM fallback queue reconciles into Postgres on recovery ──────────


def test_memory_queue_flushes_to_db_on_recovery(worker, no_pool, monkeypatch):
    url = "https://a.example/recovered"
    assert _run(worker.enqueue(_task(url))) == "memory"
    assert [t.url for t in worker._queue] == [url]

    sim = SimPool()
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=sim))
    batch = _run(worker.pop_batch(1))
    assert worker._queue == []
    assert url in sim.rows
    assert [t.url for t in batch] == [url]


def test_flush_respects_due_time(worker, no_pool, monkeypatch):
    url = "https://a.example/future"
    future = datetime.now(UTC).timestamp() + 3600
    assert _run(worker.enqueue(_task(url, scheduled_at=future))) == "memory"

    sim = SimPool()
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=sim))
    assert _run(worker.pop_batch(1)) == []  # flushed, but not due yet
    assert worker._queue == []
    sim.advance(3601)
    assert [t.url for t in _run(worker.pop_batch(1))] == [url]


def test_flush_dedupes_against_db_row(worker, no_pool, monkeypatch):
    url = "https://a.example/dup"
    _run(worker.enqueue(_task(url, priority=0.9)))

    sim = SimPool(rows=[{"url": url, "priority": 0.1, "status": "done"}])
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=sim))
    _run(worker.pop_batch(1))
    # ON CONFLICT merge: no second row, higher priority wins, done → resurrected.
    assert len(sim.rows) == 1
    assert sim.rows[url]["priority"] == 0.9
    assert sim.rows[url]["status"] == "fetching"  # resurrected + claimed


def test_flush_dedup_keeps_earliest_due_time(worker, no_pool, monkeypatch):
    # M1: flushing a URL that already exists 'queued' must merge into that
    # row (no second row) and keep LEAST(next_crawl_at) — the earlier due
    # time wins over the RAM task's later schedule.
    url = "https://a.example/dup-due"
    later = datetime.now(UTC).timestamp() + 7200
    _run(worker.enqueue(_task(url, priority=0.9, scheduled_at=later)))

    earlier = datetime.now(UTC) + timedelta(seconds=1800)
    sim = SimPool(
        rows=[{"url": url, "priority": 0.1, "status": "queued", "next_crawl_at": earlier}]
    )
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=sim))
    assert _run(worker.pop_batch(1)) == []  # merged row not due yet

    assert len(sim.rows) == 1
    assert sim.rows[url]["priority"] == 0.9
    assert sim.rows[url]["next_crawl_at"] == earlier
    sim.advance(1801)
    assert [t.url for t in _run(worker.pop_batch(1))] == [url]


def test_flush_cancel_keeps_pending_tasks(worker, monkeypatch):
    # r5r2 M1: cancelling a flush mid-insert must not drop the in-flight
    # task or tasks not yet attempted — everything unconfirmed stays queued.
    t1 = _task("https://a.example/c1", priority=0.9)
    t2 = _task("https://a.example/c2", priority=0.8)
    worker.add_task(t1)
    worker.add_task(t2)

    async def run():
        started = asyncio.Event()

        class BlockingPool(FakePool):
            async def execute(self, sql, *args):
                started.set()
                await asyncio.sleep(60)
                return "INSERT 0 1"

        monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=BlockingPool()))
        flush = asyncio.ensure_future(worker.pop_batch(1))
        await started.wait()  # first insert is in flight
        flush.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush

    _run(run())
    assert [t.url for t in worker._queue] == [t1.url, t2.url]


def test_flush_cancel_after_partial_confirm_keeps_remainder(worker, monkeypatch):
    # r5r2 M1: cancel while the SECOND insert is in flight — the confirmed
    # insert stays persisted, only the unconfirmed remainder is re-queued.
    t1 = _task("https://a.example/p1", priority=0.9)
    t2 = _task("https://a.example/p2", priority=0.8)
    worker.add_task(t1)
    worker.add_task(t2)

    async def run():
        second_started = asyncio.Event()

        class PartialPool(SimPool):
            async def execute(self, sql, *args):
                if len(self.calls) == 1:  # second statement → hang
                    second_started.set()
                    await asyncio.sleep(60)
                return await super().execute(sql, *args)

        pool = PartialPool()
        monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=pool))
        flush = asyncio.ensure_future(worker.pop_batch(1))
        await second_started.wait()
        flush.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush
        return pool

    pool = _run(run())
    assert t1.url in pool.rows  # confirmed insert survived
    assert [t.url for t in worker._queue] == [t2.url]


def test_flush_keeps_tasks_when_insert_fails(worker, no_pool, monkeypatch):
    url = "https://a.example/flaky"
    _run(worker.enqueue(_task(url)))

    class FlakyPool(SimPool):
        async def execute(self, sql, *args):
            if sql == freshness_worker._ENQUEUE_SQL:
                raise RuntimeError("transient insert failure")
            return await super().execute(sql, *args)

    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=FlakyPool()))
    _run(worker.pop_batch(1))
    assert [t.url for t in worker._queue] == [url]  # not lost — retried later


# ─── RAM fallback when DB is unavailable ──────────────────────────────────


def test_enqueue_falls_back_to_memory(worker, no_pool):
    task = RecrawlTask(url="https://a.example", priority=0.5, scheduled_at=0)
    assert _run(worker.enqueue(task)) == "memory"
    assert [t.url for t in worker._queue] == ["https://a.example"]


def test_pop_batch_falls_back_to_memory(worker, no_pool):
    worker.add_task(RecrawlTask(url="https://b.example", priority=0.9, scheduled_at=0))
    worker.add_task(RecrawlTask(url="https://a.example", priority=0.1, scheduled_at=0))
    batch = _run(worker.pop_batch(1))
    assert [t.url for t in batch] == ["https://b.example"]


def test_complete_fail_no_db(worker, no_pool):
    assert _run(worker.complete("https://a.example")) is False
    assert _run(worker.fail("https://a.example")) is False


def test_db_error_falls_back_to_memory(worker, monkeypatch):
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=FakePool(raises=True)))
    task = RecrawlTask(url="https://a.example", priority=0.5, scheduled_at=0)
    assert _run(worker.enqueue(task)) == "memory"
    assert [t.url for t in worker._queue] == ["https://a.example"]
    # pop on a broken pool still drains the RAM queue rather than raising
    assert [t.url for t in _run(worker.pop_batch(1))] == ["https://a.example"]
