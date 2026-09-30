"""Tests for db/migrate.py — versioned, idempotent schema migrations.

Runs against a fake asyncpg connection: no live Postgres needed (the real
round-trip is covered by `docker compose exec search-router python -m
db.migrate` against hub-postgres).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from db import migrate


class _FakeTx:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # asyncpg rolls the tx back on error — model that by dropping any
        # version recorded inside the failed transaction. A clean exit
        # commits, so only the pending (uncommitted) marker is cleared.
        if exc_type is not None and self._conn._pending_version is not None:
            self._conn.applied.remove(self._conn._pending_version)
        self._conn._pending_version = None
        return False


class FakeConn:
    """Minimal asyncpg-connection stand-in for the migration runner.

    ``shared`` lets two FakeConns model concurrent runners on one database:
    the applied ledger, executed scripts and advisory-lock registry are
    shared, and ``pg_advisory_lock`` blocks on a real asyncio.Lock.
    """

    def __init__(self, fail_on: str | None = None, shared: dict | None = None):
        shared = shared if shared is not None else {}
        self.applied: list[str] = shared.setdefault("applied", [])
        self.scripts: list[str] = shared.setdefault("scripts", [])
        self._locks: dict[int, asyncio.Lock] = shared.setdefault("locks", {})
        self.fail_on = fail_on
        self._pending_version: str | None = None
        self.lock_log: list[str] = []

    def transaction(self):
        return _FakeTx(self)

    async def execute(self, sql, *args):
        # Real asyncpg yields on network I/O every call — sleep(0) models that
        # so concurrent-runner races actually interleave in tests.
        await asyncio.sleep(0)
        if "pg_advisory_lock(" in sql:
            lock = self._locks.get(args[0])
            if lock is None:
                lock = self._locks[args[0]] = asyncio.Lock()
            await lock.acquire()
            self.lock_log.append("lock")
            return "SELECT 1"
        if "pg_advisory_unlock(" in sql:
            self._locks[args[0]].release()
            self.lock_log.append("unlock")
            return "SELECT 1"
        if "CREATE TABLE" in sql and "schema_migrations" in sql:
            return "CREATE TABLE"
        if sql.strip().startswith("INSERT INTO schema_migrations"):
            if args[0] in self.applied:
                raise RuntimeError("duplicate key value violates unique constraint")
            self._pending_version = args[0]
            self.applied.append(args[0])
            return "INSERT 0 1"
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("simulated migration failure")
        self.scripts.append(sql)
        return "OK"

    async def fetch(self, sql, *args):
        await asyncio.sleep(0)
        return [{"version": v} for v in self.applied]


def _run(coro):
    return asyncio.run(coro)


def _expected_versions() -> list[str]:
    return [v for v, _ in migrate.migration_files()]


def test_migration_files_sorted():
    files = migrate.migration_files()
    versions = [v for v, _ in files]
    assert versions == sorted(versions)
    assert versions[0] == "001_base"
    assert "002_phase1_foundation" in versions
    for version, path in files:
        assert path.suffix == ".sql"
        assert path.read_text(encoding="utf-8").strip()


def test_businesses_table_lives_in_migration_003():
    """H3: the businesses DDL must be a real migration, not app-side DDL."""
    files = dict(migrate.migration_files())
    sql = files["003_businesses"].read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS businesses" in sql
    assert "DOUBLE PRECISION" in sql
    assert "GIST" in sql
    assert "GEOMETRY(Point, 4326)" in sql


def test_frontier_lease_columns_in_migration():
    """H2: claimed_at + claim_token exist so interrupted claims reclaim."""
    files = dict(migrate.migration_files())
    sql = files["004_frontier_lease"].read_text(encoding="utf-8")
    assert "claimed_at" in sql
    assert "claim_token" in sql


def test_apply_all_then_noop():
    conn = FakeConn()
    applied = _run(migrate.apply_migrations(conn))
    assert applied == _expected_versions()
    assert len(conn.scripts) == len(applied)
    assert "postgis" in conn.scripts[0]
    assert "administrative_units" in conn.scripts[1]

    # Second run: everything recorded → no-op.
    assert _run(migrate.apply_migrations(conn)) == []
    assert len(conn.scripts) == len(applied)


def test_advisory_lock_held_for_the_whole_run():
    conn = FakeConn()
    _run(migrate.apply_migrations(conn))
    assert conn.lock_log[0] == "lock"
    assert conn.lock_log[-1] == "unlock"
    assert conn.lock_log.count("lock") == conn.lock_log.count("unlock") == 1


def test_concurrent_runners_serialize_on_advisory_lock():
    """M2: two runners must not both read an empty ledger and double-apply."""
    shared: dict = {}
    conn_a = FakeConn(shared=shared)
    conn_b = FakeConn(shared=shared)

    async def _both():
        return await asyncio.gather(
            migrate.apply_migrations(conn_a),
            migrate.apply_migrations(conn_b),
        )

    res_a, res_b = _run(_both())
    expected = _expected_versions()
    # One runner applied everything; the other saw a fully-applied ledger.
    assert sorted(res_a + res_b) == expected
    assert [] in (res_a, res_b)
    # Each migration file executed exactly once across both runners.
    assert len(shared["scripts"]) == len(expected)


def test_partial_applied_skips_done():
    conn = FakeConn()
    conn.applied.append("001_base")
    expected = _expected_versions()
    assert _run(migrate.apply_migrations(conn)) == expected[1:]
    assert len(conn.scripts) == len(expected) - 1


def test_failed_migration_retries_next_run():
    conn = FakeConn(fail_on="administrative_units")
    with pytest.raises(RuntimeError):
        _run(migrate.apply_migrations(conn))
    # 001 committed; 002 rolled back — its version was never recorded.
    assert conn.applied == ["001_base"]

    conn.fail_on = None
    assert _run(migrate.apply_migrations(conn)) == _expected_versions()[1:]


def test_apply_custom_dir(tmp_path: Path):
    (tmp_path / "001_x.sql").write_text("CREATE TABLE IF NOT EXISTS t (i int);")
    (tmp_path / "010_late.sql").write_text("CREATE TABLE IF NOT EXISTS t2 (i int);")
    conn = FakeConn()
    assert _run(migrate.apply_migrations(conn, tmp_path)) == ["001_x", "010_late"]
