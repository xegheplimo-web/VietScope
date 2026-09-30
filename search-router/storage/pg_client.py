"""Async Postgres pool for hub-postgres (P10).

Follows the same contract as ``storage/redis_client.py``: ``get_pool()``
returns ``None`` when the DB is unreachable so callers degrade gracefully —
auth disabled mode, no usage logging, in-memory quota. The pool is created
lazily per event loop (asyncpg binds pools to a loop).
"""

import asyncio
import logging
import time
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_pools: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_failures: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

_CONNECT_TIMEOUT_SECONDS = 2.0
_REPROBE_INTERVAL_SECONDS = 10.0


def database_url() -> str:
    """Resolve the hub-postgres DSN (HUB_DATABASE_URL env wins)."""
    from config import settings

    return getattr(settings, "hub_database_url", "") or __import__("os").getenv(
        "HUB_DATABASE_URL", ""
    )


async def _create_pool():
    import asyncpg

    return await asyncio.wait_for(
        asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=8, command_timeout=10),
        timeout=_CONNECT_TIMEOUT_SECONDS + 2.0,
    )


async def get_pool() -> Any | None:
    """Return the shared asyncpg pool, or None if DB is unavailable."""
    if not database_url():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    pool = _pools.get(loop)
    if pool is not None:
        return pool

    last_fail = _failures.get(loop, 0.0)
    if time.monotonic() - last_fail < _REPROBE_INTERVAL_SECONDS:
        return None

    lock = _locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop] = lock

    async with lock:
        pool = _pools.get(loop)
        if pool is not None:
            return pool
        try:
            pool = await _create_pool()
            _pools[loop] = pool
            _failures.pop(loop, None)
            logger.info("hub-postgres pool created")
            return pool
        except Exception as exc:
            _failures[loop] = time.monotonic()
            logger.warning("hub-postgres unavailable: %r", exc)
            return None
