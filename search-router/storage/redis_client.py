"""Redis client wrapper with graceful in-memory fallback (SPEC-v3 §13).

Provides a process-wide async Redis client. If Redis is unreachable the
``get_redis()`` helper returns ``None`` instead of raising, so callers can
fall back to in-memory storage without crashing.

The client is created lazily **per event loop** because redis-py asyncio
binds its connection pool to the loop it was created on. FastAPI uses a
single loop, but scripts/tests may run coroutines across several loops.
"""

import asyncio
import contextlib
import logging
import os
import time
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_redis_clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_probe_failures: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_probe_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

_PROBE_TIMEOUT_SECONDS = 2.0
_CONNECT_TIMEOUT_SECONDS = 1.0
_OP_TIMEOUT_SECONDS = 2.0
_REPROBE_INTERVAL_SECONDS = 5.0


def redis_url() -> str:
    """Resolve the configured Redis URL (REDIS_URL env wins over default)."""
    from config import settings

    url = getattr(settings, "redis_url", "") or os.getenv("REDIS_URL", "")
    return url or "redis://localhost:6379"


async def _probe(client) -> bool:
    """Return True if the client answers PONG within the probe timeout."""
    try:
        await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT_SECONDS)
        return True
    except Exception:
        return False


async def _create_client():
    import redis.asyncio as aioredis

    return aioredis.Redis.from_url(
        redis_url(),
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_OP_TIMEOUT_SECONDS,
    )


async def get_redis() -> Any | None:
    """Return an async Redis client bound to the current event loop.

    Returns ``None`` when Redis is unreachable so callers transparently
    degrade to in-memory storage. Failed connections are re-probed after a
    short cooldown so a transient outage does not disable Redis forever.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    client = _redis_clients.get(loop)
    if client is not None:
        return client
    failed_at = _probe_failures.get(loop)
    if failed_at is not None and time.monotonic() - failed_at < _REPROBE_INTERVAL_SECONDS:
        return None

    lock = _probe_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _probe_locks[loop] = lock
    async with lock:
        client = _redis_clients.get(loop)
        if client is not None:
            return client
        failed_at = _probe_failures.get(loop)
        if failed_at is not None and time.monotonic() - failed_at < _REPROBE_INTERVAL_SECONDS:
            return None

        try:
            client = await _create_client()
            if await _probe(client):
                _redis_clients[loop] = client
                _probe_failures.pop(loop, None)
                return client
            with contextlib.suppress(Exception):
                await client.aclose()
            logger.warning("Redis unreachable — using in-memory fallback")
        except Exception as exc:
            logger.warning("Redis unavailable (%s) — using in-memory fallback", exc)
        _probe_failures[loop] = time.monotonic()
        return None


def reset_redis_state() -> None:
    """Forget cached clients so connections are re-probed next call.

    Intended for tests that need a fresh backend decision.
    """
    _redis_clients.clear()
    _probe_failures.clear()
    _probe_locks.clear()


def mark_redis_unavailable(client: Any | None = None) -> None:
    """Drop a failed client so subsequent calls use fallback, then re-probe."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    current = _redis_clients.get(loop)
    if client is None or current is client:
        _redis_clients.pop(loop, None)
        _probe_failures[loop] = time.monotonic()
