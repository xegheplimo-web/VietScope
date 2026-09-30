"""Sliding-window rate limiter for provider calls (SPEC-v3 §13).

Redis-backed via a sorted set of request timestamps per key, with an
in-memory fallback so the Search Hub never crashes when Redis is down.
In-memory state is protected by a process-local lock so ``allow()`` remains
safe when coroutines run across several asyncio loops or threads.
"""

import time
import uuid
from threading import RLock
from typing import Any

from storage.redis_client import get_redis, mark_redis_unavailable

_ALLOW_SCRIPT = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
local count = redis.call('zcard', KEYS[1])
if count >= tonumber(ARGV[2]) then
    return 0
end
redis.call('zadd', KEYS[1], ARGV[3], ARGV[4])
redis.call('expire', KEYS[1], ARGV[5])
return 1
"""


def _validate(limit: int, window_seconds: int) -> None:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")


class MemoryRateLimiter:
    """In-memory sliding-window rate limiter (fallback when Redis is down)."""

    def __init__(self) -> None:
        self._entries: dict[str, list[float]] = {}
        self._lock = RLock()

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        _validate(limit, window_seconds)
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            entries = [ts for ts in self._entries.get(key, []) if ts > cutoff]
            if len(entries) >= limit:
                self._entries[key] = entries
                return False
            entries.append(now)
            self._entries[key] = entries
        return True


class RedisRateLimiter:
    """Sliding-window rate limiter backed by a Redis sorted set."""

    def __init__(self, client: Any, prefix: str = "sh:ratelimit"):
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        _validate(limit, window_seconds)
        redis_key = self._key(key)
        now = time.time()
        cutoff = now - window_seconds
        member = uuid.uuid4().hex
        try:
            result = await self._client.eval(
                _ALLOW_SCRIPT,
                1,
                redis_key,
                cutoff,
                limit,
                now,
                member,
                window_seconds,
            )
            return bool(result)
        except NotImplementedError:
            # Minimal test doubles may not implement Lua. Production Redis
            # always takes the atomic script path above.
            await self._client.zremrangebyscore(redis_key, "-inf", cutoff)
            count = await self._client.zcard(redis_key)
            if count >= limit:
                return False
            await self._client.zadd(redis_key, {member: now})
            await self._client.expire(redis_key, window_seconds)
            return True


_memory_limiter: MemoryRateLimiter | None = None


async def allow(key: str, limit: int, window_seconds: int) -> bool:
    """Auto-selecting rate limit check: Redis when available, else memory.

    Redis state lives in Redis so it is shared across loops/instances; the
    in-memory fallback uses a global limiter (it only touches a plain dict
    and the clock, so it is safe across event loops).
    """
    global _memory_limiter
    client = await get_redis()
    if client is not None:
        try:
            return await RedisRateLimiter(client).allow(key, limit, window_seconds)
        except Exception:
            mark_redis_unavailable(client)

    if _memory_limiter is None:
        _memory_limiter = MemoryRateLimiter()
    return await _memory_limiter.allow(key, limit, window_seconds)
