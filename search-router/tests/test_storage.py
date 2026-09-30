"""Tests for the storage package: RedisCache, rate limiter, distributed lock.

Real Redis is used when reachable (TEST_REDIS_URL or settings.redis_url);
otherwise an in-process FakeRedis exercises the same code paths so the suite
never requires a running container.

redis-py asyncio clients bind their connection pool to the event loop they
were created on (as FastAPI does in production), so all coroutines in this
module run on a single shared loop via ``run_coro``.
"""

import asyncio
import fnmatch
import math
import os
import time
from typing import Any

import config
import storage.cache as cache_mod
import storage.lock as lock_mod
import storage.rate_limiter as rate_limiter_mod
from pydantic import BaseModel
from storage.cache import RedisCache
from storage.lock import MemoryLock, RedisLock
from storage.rate_limiter import MemoryRateLimiter, RedisRateLimiter

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def run_coro(coro):
    return _loop.run_until_complete(coro)


# ─── Fake Redis (minimal in-memory impl of the commands we use) ──────────────


class FakeRedis:
    def __init__(self):
        self._data: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}

    async def ping(self):
        return True

    def _prune(self, name):
        exp = self._expiry.get(name)
        if exp is not None and time.time() > exp:
            self._data.pop(name, None)
            self._expiry.pop(name, None)

    async def get(self, name):
        self._prune(name)
        return self._data.get(name)

    async def set(self, name, value, ex=None, nx=False, **kwargs):
        if nx and name in self._data:
            return None
        self._data[name] = value
        if ex is not None:
            self._expiry[name] = time.time() + ex
        else:
            self._expiry.pop(name, None)
        return True

    async def delete(self, *names):
        removed = 0
        for name in names:
            if name in self._data:
                self._data.pop(name, None)
                self._expiry.pop(name, None)
                removed += 1
        return removed

    async def scan_iter(self, match=None):
        for name in list(self._data.keys()):
            if match and not fnmatch.fnmatch(name, match):
                continue
            yield name

    async def zadd(self, name, mapping):
        zset = self._data.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = score
        return added

    async def zcard(self, name):
        zset = self._data.get(name)
        return len(zset) if zset else 0

    async def zremrangebyscore(self, name, min_, max_):
        zset = self._data.get(name)
        if not zset:
            return 0

        def num(v):
            return -math.inf if v == "-inf" else (math.inf if v == "+inf" else float(v))

        lo, hi = num(min_), num(max_)
        to_remove = [m for m, s in zset.items() if lo <= s <= hi]
        for m in to_remove:
            del zset[m]
        return len(to_remove)

    async def expire(self, name, seconds):
        if name in self._data:
            self._expiry[name] = time.time() + seconds
            return True
        return False

    async def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        rest = args[numkeys:]
        if "== ARGV[1]" in script:
            key, token = keys[0], rest[0]
            if self._data.get(key) == token:
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                return 1
            return 0
        raise NotImplementedError(f"eval not implemented for: {script}")

    async def aclose(self):
        pass


# ─── Backend selection: real Redis when reachable, else FakeRedis ────────────


def _try_real():
    import redis.asyncio as aioredis

    url = os.getenv("TEST_REDIS_URL") or config.settings.redis_url
    try:
        client = aioredis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        run_coro(asyncio.wait_for(client.ping(), 2.0))
        return client
    except Exception:
        return None


_client = None

_TEST_PREFIXES = (
    "test:",
    "sh:ratelimit:",
    "sh:lock:",
    "sh:search:",
    "sh:scrape:",
    "sh:research:",
)


async def _flush_test_keys(client):
    """Remove keys left by previous runs so tests are deterministic."""
    for prefix in _TEST_PREFIXES:
        async for key in client.scan_iter(match=f"{prefix}*"):
            await client.delete(key)


def get_client():
    global _client
    if _client is None:
        _client = _try_real() or FakeRedis()
        run_coro(_flush_test_keys(_client))
    return _client


# ─── RedisCache tests ─────────────────────────────────────────────────────────


def test_cache_set_get():
    cache = RedisCache(get_client(), prefix="test", namespace="cache1")

    async def _run():
        await cache.set("a", {"k": 1}, 60)
        return await cache.get("a")

    assert run_coro(_run()) == {"k": 1}


class DummyModel(BaseModel):
    query: str
    count: int = 0
    tags: list[str] = []


def test_cache_pydantic_roundtrip():
    cache = RedisCache(get_client(), prefix="test", namespace="cache2")
    value = DummyModel(query="hello", count=3, tags=["a", "b"])

    async def _run():
        await cache.set("m", value, 60)
        return await cache.get("m")

    out = run_coro(_run())
    assert isinstance(out, DummyModel)
    assert out.query == "hello"
    assert out.count == 3
    assert out.tags == ["a", "b"]


def test_cache_get_missing_is_none():
    cache = RedisCache(get_client(), prefix="test", namespace="cache3")

    async def _run():
        return await cache.get("nope")

    assert run_coro(_run()) is None


def test_cache_delete():
    cache = RedisCache(get_client(), prefix="test", namespace="cache4")

    async def _run():
        await cache.set("d", "v", 60)
        await cache.delete("d")
        return await cache.get("d")

    assert run_coro(_run()) is None


def test_cache_ttl_expiry():
    cache = RedisCache(get_client(), prefix="test", namespace="cache5")

    async def _run():
        await cache.set("t", "x", 1)
        before = await cache.get("t")
        time.sleep(1.2)
        after = await cache.get("t")
        return before, after

    before, after = run_coro(_run())
    assert before == "x"
    assert after is None


def test_cache_clear_and_stats():
    cache = RedisCache(get_client(), prefix="test", namespace="cache6")

    async def _run():
        await cache.clear()
        await cache.set("1", "a", 60)
        await cache.set("2", "b", 60)
        stats_before = await cache.stats()
        await cache.clear()
        stats_after = await cache.stats()
        return stats_before, stats_after

    before, after = run_coro(_run())
    assert before["size"] == 2
    assert after["size"] == 0


def test_serialize_deserialize_roundtrip():
    for value in ["str", 42, 3.14, True, None, {"a": [1, 2]}, ["x", "y"]]:
        raw = cache_mod.serialize(value)
        assert cache_mod.deserialize(raw) == value


# ─── Rate limiter tests ───────────────────────────────────────────────────────


def test_rate_limiter_sliding_window():
    async def _run(limiter):
        return [
            await limiter.allow("k", 3, 60),
            await limiter.allow("k", 3, 60),
            await limiter.allow("k", 3, 60),
            await limiter.allow("k", 3, 60),
        ]

    for limiter in (RedisRateLimiter(get_client()), MemoryRateLimiter()):
        results = run_coro(_run(limiter))
        assert results == [True, True, True, False]


def test_rate_limiter_window_resets():
    async def _run(limiter):
        first = await limiter.allow("w", 1, 1)
        time.sleep(1.2)
        second = await limiter.allow("w", 1, 1)
        return first, second

    for limiter in (RedisRateLimiter(get_client()), MemoryRateLimiter()):
        first, second = run_coro(_run(limiter))
        assert first is True
        assert second is True


def test_module_rate_limit_allow():
    async def _run():
        results = [
            await rate_limiter_mod.allow("m", 3, 60),
            await rate_limiter_mod.allow("m", 3, 60),
            await rate_limiter_mod.allow("m", 3, 60),
            await rate_limiter_mod.allow("m", 3, 60),
        ]
        return results

    assert run_coro(_run()) == [True, True, True, False]


# ─── Distributed lock tests ───────────────────────────────────────────────────


def test_lock_acquire_release():
    async def _run(lock):
        token1 = await lock.acquire("job", 30)
        token2 = await lock.acquire("job", 30)  # already held -> fail
        rel = await lock.release("job", token1)
        token3 = await lock.acquire("job", 30)  # after release -> ok
        return token1, token2, rel, token3

    for lock in (RedisLock(get_client()), MemoryLock()):
        token1, token2, rel, token3 = run_coro(_run(lock))
        assert token1
        assert token2 is None
        assert rel is True
        assert token3


def test_lock_release_with_token():
    async def _run(lock):
        token = "secret-token"
        await lock._client.set(lock._key("j"), token, ex=30)
        wrong = await lock.release("j", "bad-token")
        still_held = await lock.acquire("j", 30)
        right = await lock.release("j", token)
        return wrong, still_held, right

    lock = RedisLock(get_client())
    wrong, still_held, right = run_coro(_run(lock))
    assert wrong is False
    assert still_held is None  # still locked by the original token
    assert right is True


def test_module_lock():
    async def _run():
        token = await lock_mod.acquire("mod", 30)
        rel = await lock_mod.release("mod", token)
        return token, rel

    token, rel = run_coro(_run())
    assert token
    assert rel is True


# ─── pipeline/cache fallback (Redis down -> in-memory, no crash) ─────────────


def test_pipeline_cache_fallback_no_redis(monkeypatch):
    from pipeline import cache as pipeline_cache

    async def _no_redis():
        return None

    monkeypatch.setattr(pipeline_cache, "get_redis", _no_redis)

    async def _run():
        # not set yet -> None, no crash
        missing = await pipeline_cache.search_cache.get("test")
        await pipeline_cache.search_cache.set("test", "hello", 60)
        found = await pipeline_cache.search_cache.get("test")
        return missing, found

    missing, found = run_coro(_run())
    assert missing is None
    assert found == "hello"


def test_pipeline_cache_public_api_surface():
    from pipeline import cache as pipeline_cache

    for attr in ("search_cache", "scrape_cache"):
        obj = getattr(pipeline_cache, attr)
        assert all(hasattr(obj, m) for m in ("get", "set", "delete", "clear", "stats"))
    for attr in (
        "TTL_SEARCH_WEB",
        "TTL_SEARCH_NEWS",
        "TTL_SEARCH_IMAGES",
        "TTL_SCRAPE",
        "TTL_SCRAPE_NEWS",
    ):
        assert hasattr(pipeline_cache, attr)
    assert callable(pipeline_cache._cache_key)


def test_import_surface():
    from storage.cache import RedisCache
    from storage.redis_client import get_redis

    assert callable(get_redis)
    assert RedisCache is not None
