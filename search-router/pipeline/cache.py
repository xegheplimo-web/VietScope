"""TTL cache for Search Hub — Redis-backed with in-memory fallback.

Public API is unchanged (search_cache/scrape_cache, TTL_*,
_cache_key) so main.py keeps working. When
``settings.redis_url`` points at a reachable Redis the cache stores values
in Redis (shared across instances); otherwise it transparently degrades to
an in-memory TTL cache.
"""

import asyncio
import time
from collections import OrderedDict
from typing import Any

from config import settings
from storage.cache import RedisCache
from storage.redis_client import get_redis, mark_redis_unavailable


class TTLCache:
    """Thread-safe in-memory TTL cache with max size eviction (fallback)."""

    def __init__(self, max_size: int = 500):
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_size = max_size

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if time.time() > expiry:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl_seconds: int):
        async with self._lock:
            self._store[key] = (time.time() + ttl_seconds, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    async def delete(self, key: str) -> int:
        async with self._lock:
            return 1 if self._store.pop(key, None) is not None else 0

    async def clear(self):
        async with self._lock:
            self._store.clear()

    async def stats(self) -> dict:
        async with self._lock:
            return {"size": len(self._store), "max_size": self._max_size}


class Cache:
    """Redis-backed TTL cache that degrades to in-memory when Redis is down.

    Mirrors the old TTLCache async API (get/set/delete/clear/stats) so
    callers do not need to change.
    """

    def __init__(self, name: str, max_size: int = 500):
        self._name = name
        self._memory = TTLCache(max_size=max_size)
        self._redis: RedisCache | None = None
        self._redis_loop: int | None = None
        self._redis_enabled = bool(settings.redis_url)

    async def _backend(self) -> RedisCache | None:
        if not self._redis_enabled:
            return None
        client = await get_redis()
        if client is None:
            self._redis = None
            return None
        loop_id = id(asyncio.get_running_loop())
        if self._redis is None or self._redis_loop != loop_id:
            self._redis = RedisCache(client, prefix="searchhub", namespace=self._name)
            self._redis_loop = loop_id
        return self._redis

    async def get(self, key: str) -> Any | None:
        try:
            backend = await self._backend()
            if backend is not None:
                value = await backend.get(key)
                if value is not None:
                    return value
        except Exception:
            if self._redis is not None:
                mark_redis_unavailable(self._redis._client)
        return await self._memory.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: int):
        try:
            backend = await self._backend()
            if backend is not None:
                await backend.set(key, value, ttl_seconds)
        except Exception:
            if self._redis is not None:
                mark_redis_unavailable(self._redis._client)
        await self._memory.set(key, value, ttl_seconds)

    async def delete(self, key: str) -> int:
        redis_deleted = 0
        try:
            backend = await self._backend()
            if backend is not None:
                redis_deleted = await backend.delete(key)
        except Exception:
            if self._redis is not None:
                mark_redis_unavailable(self._redis._client)
        memory_deleted = await self._memory.delete(key)
        return 1 if redis_deleted or memory_deleted else 0

    async def clear(self):
        try:
            backend = await self._backend()
            if backend is not None:
                await backend.clear()
        except Exception:
            if self._redis is not None:
                mark_redis_unavailable(self._redis._client)
        await self._memory.clear()

    async def stats(self) -> dict:
        try:
            backend = await self._backend()
            if backend is not None:
                return await backend.stats()
        except Exception:
            if self._redis is not None:
                mark_redis_unavailable(self._redis._client)
        return await self._memory.stats()


# Global cache instances with different TTLs (kept for back-compat)
search_cache = Cache("search", max_size=300)  # web/image/news search
scrape_cache = Cache("scrape", max_size=200)  # scraped page content

# TTL presets (seconds)
TTL_SEARCH_WEB = 1800  # 30 min
TTL_SEARCH_NEWS = 600  # 10 min
TTL_SEARCH_IMAGES = 3600  # 1 hour
TTL_SCRAPE = 86400  # 24 hours (pages don't change often)
TTL_SCRAPE_NEWS = 3600  # 1 hour (news pages change)


def _cache_key(*parts) -> str:
    """Build a cache key from parts."""
    return "|".join(str(p) for p in parts)
