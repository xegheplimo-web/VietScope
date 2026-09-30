#!/usr/bin/env python3
"""Schema migration runner for hub-postgres.

Applies ``db/migrations/NNN_*.sql`` files in filename order and records each
applied version in ``schema_migrations(version, applied_at)``. Idempotent:
versions already recorded are skipped, so the runner is safe at every
startup and on volumes where ``db/init.sql`` already ran (all migration
files use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).

DSN resolution matches manage_keys.py: ``--dsn`` > ``HUB_DATABASE_URL`` >
``HUB_PG_*`` (defaults 127.0.0.1:5433, searchhub/searchhub). The repo-root
.env is loaded for HUB_* vars only.

Usage:
    python db/migrate.py [--dsn postgresql://...] [--dir PATH] [--status]
    python -m db.migrate            # same, from the search-router directory
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Session-level advisory lock key — arbitrary stable constant identifying
# the migration runner. Concurrent router replicas all run apply_migrations
# at startup; the lock serializes read-ledger → execute → record so two
# runners can't both see a version as pending and run the same DDL (the
# schema_migrations PK would turn the loser's transaction into a startup
# error even when the DDL itself is IF NOT EXISTS).
_ADVISORY_LOCK_KEY = 0x534D_4947  # "SMIG"

_LOCK_SQL = "SELECT pg_advisory_lock($1)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock($1)"

_SCHEMA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def _applied_versions(conn: Any) -> set[str]:
    await conn.execute(_SCHEMA_TABLE_SQL)
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


def migration_files(migrations_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return (version, path) pairs sorted by filename."""
    directory = migrations_dir or MIGRATIONS_DIR
    return [(f.stem, f) for f in sorted(directory.glob("*.sql"))]


async def apply_migrations(conn: Any, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending migrations on an asyncpg connection.

    Each file runs in its own transaction; the version row is recorded in
    the same transaction so a failed migration never half-applies. The whole
    run holds a session-level advisory lock so concurrent runners serialize
    instead of racing the ledger. Returns the versions applied by this call.
    """
    await conn.execute(_LOCK_SQL, _ADVISORY_LOCK_KEY)
    try:
        applied = await _applied_versions(conn)
        newly_applied: list[str] = []
        for version, path in migration_files(migrations_dir):
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                # asyncpg execute() runs multi-statement scripts via the simple
                # query protocol when no $-parameters are passed.
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
            newly_applied.append(version)
            logger.info("migration applied: %s", version)
        return newly_applied
    finally:
        await conn.execute(_UNLOCK_SQL, _ADVISORY_LOCK_KEY)


async def run(dsn: str | None = None, migrations_dir: Path | None = None) -> list[str]:
    """Connect to hub-postgres and apply pending migrations."""
    import asyncpg

    _load_dotenv()
    conn = await asyncpg.connect(dsn=_resolve_dsn(dsn), timeout=10)
    try:
        return await apply_migrations(conn, migrations_dir)
    finally:
        await conn.close()


async def _print_status(conn: Any, migrations_dir: Path | None) -> None:
    applied = await _applied_versions(conn)
    for version, _ in migration_files(migrations_dir):
        state = "applied" if version in applied else "pending"
        print(f"  {state:8} {version}")
    unknown = sorted(applied - {v for v, _ in migration_files(migrations_dir)})
    for version in unknown:
        print(f"  missing  {version}  (recorded, no file)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hub-postgres schema migrations")
    parser.add_argument("--dsn", help="Postgres DSN (overrides env/default)")
    parser.add_argument("--dir", type=Path, default=None, help="migrations directory override")
    parser.add_argument(
        "--status",
        action="store_true",
        help="list applied/pending migrations without applying",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)

    async def _run() -> int:
        import asyncpg

        conn = await asyncpg.connect(dsn=dsn, timeout=10)
        try:
            if args.status:
                await _print_status(conn, args.dir)
                return 0
            applied = await apply_migrations(conn, args.dir)
            if applied:
                print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
            else:
                print("schema up to date — nothing to apply")
            return 0
        finally:
            await conn.close()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
