"""Redis distributed lock using SET NX EX (SPEC-v3 §13, async job state).

Acquire is atomic via ``SET key token NX EX ttl``. Release uses a Lua
compare-and-delete so only the lock owner can free it. When Redis is
unavailable an in-memory lock is used instead (no crash). Both backends return
an owner token which is required for release.
"""

import time
import uuid
from threading import RLock
from typing import Any

from storage.redis_client import get_redis, mark_redis_unavailable

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class MemoryLock:
    """In-memory distributed lock (fallback when Redis is down)."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = RLock()

    async def acquire(self, key: str, ttl: int = 30) -> str | None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        now = time.time()
        with self._lock:
            entry = self._held.get(key)
            if entry and entry[1] > now:
                return None
            token = uuid.uuid4().hex
            self._held[key] = (token, now + ttl)
            return token

    async def release(self, key: str, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            entry = self._held.get(key)
            if entry is None or entry[0] != token:
                return False
            return self._held.pop(key, None) is not None


class RedisLock:
    """Distributed lock backed by Redis SET NX EX with token ownership."""

    def __init__(self, client: Any, prefix: str = "sh:lock"):
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def acquire(self, key: str, ttl: int = 30) -> str | None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        token = uuid.uuid4().hex
        acquired = await self._client.set(self._key(key), token, nx=True, ex=ttl)
        return token if acquired else None

    async def release(self, key: str, token: str) -> bool:
        if not token:
            return False
        result = await self._client.eval(_RELEASE_SCRIPT, 1, self._key(key), token)
        return bool(result)


_memory_lock: MemoryLock | None = None


async def _backend() -> Any:
    """Return a lock impl (Redis or global in-memory) with no state kept."""
    global _memory_lock
    client = await get_redis()
    if client is not None:
        return RedisLock(client)
    if _memory_lock is None:
        _memory_lock = MemoryLock()
    return _memory_lock


async def acquire(key: str, ttl: int = 30) -> str | None:
    """Auto-selecting lock acquire: Redis when available, else memory."""
    backend = await _backend()
    try:
        return await backend.acquire(key, ttl)
    except Exception:
        if isinstance(backend, MemoryLock):
            raise
        mark_redis_unavailable(backend._client)
        global _memory_lock
        if _memory_lock is None:
            _memory_lock = MemoryLock()
        return await _memory_lock.acquire(key, ttl)


async def release(key: str, token: str) -> bool:
    """Release only when ``token`` proves ownership of ``key``."""
    backend = await _backend()
    try:
        released = await backend.release(key, token)
    except Exception:
        if isinstance(backend, RedisLock):
            mark_redis_unavailable(backend._client)
        released = False
    if not released and _memory_lock is not None and not isinstance(backend, MemoryLock):
        return await _memory_lock.release(key, token)
    return released
