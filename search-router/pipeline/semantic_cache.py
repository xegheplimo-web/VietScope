"""4-layer result cache (P5) — Redis-backed with in-memory fallback.

Layer 1: exact answer cache    (sha256 of normalized query + mode + lang)
Layer 2: semantic answer cache (BGE query embedding, cosine >= threshold)
Layer 3: evidence bundle cache (evidence_id → bundle)
Layer 4: page cache            (canonical URL → content payload)

All methods are async and never raise. When Redis is reachable entries are
shared across instances; otherwise the cache transparently degrades to
process-local memory — same contract as ``pipeline.cache.Cache``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from config import settings
from storage.cache import RedisCache
from storage.redis_client import get_redis, mark_redis_unavailable

logger = logging.getLogger(__name__)

# TTL by freshness class (seconds)
TTL_BY_FRESHNESS = {
    "realtime": 60,  # 1 minute
    "high": 900,  # 15 minutes
    "medium": 21600,  # 6 hours
    "slow": 259200,  # 3 days
    "static": 2592000,  # 30 days
}

# Semantic lookup is only worthwhile for content that does not move.
_SEMANTIC_FRESHNESS = frozenset({"slow", "static"})

Embedder = Callable[[str], Awaitable[list[float] | None]]


async def _default_embedder(text: str) -> list[float] | None:
    """Embed via the BGE service (``POST /embed``); None when unavailable."""
    if not settings.embedding_service_enabled or not settings.embedding_service_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.embedding_service_url}/embed",
                json={"texts": [text]},
            )
            resp.raise_for_status()
            vectors = resp.json().get("vectors") or []
            return vectors[0] if vectors and isinstance(vectors[0], list) else None
    except Exception:  # noqa: BLE001 — embedding lane is optional
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


@dataclass
class CacheEntry:
    """A single cache entry (memory fallback store)."""

    key: str
    value: Any
    created_at: float
    ttl: int
    layer: str  # exact | semantic | evidence | page

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


@dataclass
class CacheHit:
    value: Any
    layer: str  # exact | semantic | evidence | page


class SemanticCache:
    """4-layer cache for search answers, evidence bundles, and pages."""

    _SEM_INDEX_NAME = "semidx"  # Redis list of {"k": exact_key, "e": embedding}

    def __init__(
        self,
        embedder: Embedder | None = None,
        sim_threshold: float | None = None,
        max_index: int = 2048,
        max_memory: int = 1000,
    ):
        self._embedder = embedder or _default_embedder
        self._sim = sim_threshold
        if self._sim is None:
            self._sim = getattr(settings, "semantic_cache_sim_threshold", 0.94)
        self._max_index = max_index
        self._max_memory = max_memory
        self._local: OrderedDict[str, CacheEntry] = OrderedDict()
        # (embedding, exact key, key_suffix)
        self._local_sem: list[tuple[list[float], str, str]] = []
        self._lock = asyncio.Lock()
        self._redis: RedisCache | None = None
        self._redis_loop: int | None = None
        self._redis_enabled = bool(settings.redis_url)

    # ── backend resolution ────────────────────────────────────────────────

    async def _backend(self) -> RedisCache | None:
        if not self._redis_enabled:
            return None
        client = await get_redis()
        if client is None:
            self._redis = None
            return None
        loop_id = id(asyncio.get_running_loop())
        if self._redis is None or self._redis_loop != loop_id:
            self._redis = RedisCache(client, prefix="searchhub", namespace="semcache")
            self._redis_loop = loop_id
        return self._redis

    def _redis_down(self) -> None:
        if self._redis is not None:
            mark_redis_unavailable(self._redis._client)

    # ── key helpers ───────────────────────────────────────────────────────

    def _normalize_query(self, query: str, mode: str, lang: str, key_suffix: str = "") -> str:
        normalized = " ".join(query.lower().strip().split())
        return f"{normalized}:{mode}:{lang}:{key_suffix}"

    def _make_key(self, query: str, mode: str, lang: str, key_suffix: str = "") -> str:
        normalized = self._normalize_query(query, mode, lang, key_suffix)
        return hashlib.sha256(normalized.encode()).hexdigest()

    # ── L1/L2: answer cache ───────────────────────────────────────────────

    async def get(
        self,
        query: str,
        mode: str = "balanced",
        lang: str = "en",
        freshness_class: str = "medium",
        key_suffix: str = "",
    ) -> CacheHit | None:
        """Cached answer for a query; semantic fallback for slow/static classes."""
        key = self._make_key(query, mode, lang, key_suffix)
        value = await self._get_entry(f"q:{key}")
        if value is not None:
            return CacheHit(value=value, layer="exact")
        if freshness_class in _SEMANTIC_FRESHNESS:
            return await self._semantic_lookup(query, key_suffix)
        return None

    async def set(
        self,
        query: str,
        result: dict[str, Any],
        mode: str = "balanced",
        lang: str = "en",
        freshness_class: str = "medium",
        key_suffix: str = "",
    ) -> None:
        """Cache an answer; indexes the query embedding for slow/static classes."""
        key = self._make_key(query, mode, lang, key_suffix)
        ttl = TTL_BY_FRESHNESS.get(freshness_class, 3600)
        await self._set_entry(f"q:{key}", result, ttl)
        if freshness_class in _SEMANTIC_FRESHNESS:
            embedding = await self._embed(query)
            if embedding:
                await self._index_embedding(embedding, f"q:{key}", key_suffix)

    async def _get_entry(self, key: str) -> Any | None:
        try:
            backend = await self._backend()
            if backend is not None:
                value = await backend.get(key)
                if value is not None:
                    return value
        except Exception:
            self._redis_down()
        async with self._lock:
            entry = self._local.get(key)
            if entry is None or entry.is_expired:
                return None
            self._local.move_to_end(key)
            return entry.value

    async def _set_entry(self, key: str, value: Any, ttl: int) -> None:
        try:
            backend = await self._backend()
            if backend is not None:
                await backend.set(key, value, ttl)
        except Exception:
            self._redis_down()
        async with self._lock:
            self._local[key] = CacheEntry(
                key=key, value=value, created_at=time.time(), ttl=ttl, layer="exact"
            )
            self._local.move_to_end(key)
            while len(self._local) > self._max_memory:
                self._local.popitem(last=False)

    async def _embed(self, query: str) -> list[float] | None:
        try:
            return await self._embedder(query)
        except Exception:  # noqa: BLE001 — embedder must never break a request
            return None

    async def _index_embedding(
        self, embedding: list[float], key: str, key_suffix: str = ""
    ) -> None:
        row = json.dumps({"k": key, "e": embedding, "s": key_suffix})
        try:
            backend = await self._backend()
            if backend is not None:
                client = backend._client
                idx_key = f"{backend._prefix}:{self._SEM_INDEX_NAME}"
                await client.rpush(idx_key, row)
                await client.ltrim(idx_key, -self._max_index, -1)
        except Exception:
            self._redis_down()
        async with self._lock:
            self._local_sem.append((embedding, key, key_suffix))
            while len(self._local_sem) > self._max_index:
                self._local_sem.pop(0)

    async def _semantic_lookup(self, query: str, key_suffix: str) -> CacheHit | None:
        embedding = await self._embed(query)
        if not embedding:
            return None
        for emb, key, suffix in await self._semantic_index():
            if suffix == key_suffix and _cosine(embedding, emb) >= self._sim:
                value = await self._get_entry(key)
                if value is not None:
                    return CacheHit(value=value, layer="semantic")
        return None

    async def _semantic_index(self) -> list[tuple[list[float], str, str]]:
        try:
            backend = await self._backend()
            if backend is not None:
                client = backend._client
                idx_key = f"{backend._prefix}:{self._SEM_INDEX_NAME}"
                rows = await client.lrange(idx_key, 0, -1)
                out = []
                for raw in rows:
                    try:
                        item = json.loads(raw)
                        out.append((item["e"], item["k"], item.get("s", "")))
                    except (KeyError, TypeError, ValueError):
                        continue
                if out:
                    return out
        except Exception:
            self._redis_down()
        async with self._lock:
            return list(self._local_sem)

    # ── L3: evidence bundles ──────────────────────────────────────────────

    async def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return await self._get_entry(f"ev:{evidence_id}")

    async def set_evidence(
        self,
        evidence_id: str,
        bundle: dict[str, Any],
        ttl: int = 3600,
    ) -> None:
        await self._set_entry(f"ev:{evidence_id}", bundle, ttl)

    # ── L4: pages ─────────────────────────────────────────────────────────

    async def get_page(self, canonical_url: str) -> dict[str, Any] | None:
        key = hashlib.sha256(canonical_url.encode()).hexdigest()
        return await self._get_entry(f"pg:{key}")

    async def set_page(
        self,
        canonical_url: str,
        content: dict[str, Any],
        ttl: int = 3600,
    ) -> None:
        key = hashlib.sha256(canonical_url.encode()).hexdigest()
        await self._set_entry(f"pg:{key}", content, ttl)

    # ── maintenance ───────────────────────────────────────────────────────

    async def invalidate(self, pattern: str = "*") -> int:
        """Drop entries; ``pattern`` is a key-prefix match (``*`` = all)."""
        removed = 0
        try:
            backend = await self._backend()
            if backend is not None:
                client = backend._client
                glob = f"{backend._prefix}:*" if pattern == "*" else f"{backend._prefix}:{pattern}*"
                async for name in client.scan_iter(match=glob):
                    removed += await client.delete(name)
                if pattern == "*":
                    await client.delete(f"{backend._prefix}:{self._SEM_INDEX_NAME}")
        except Exception:
            self._redis_down()
        async with self._lock:
            if pattern == "*":
                removed += len(self._local)
                self._local.clear()
                self._local_sem.clear()
            else:
                doomed = [k for k in self._local if k.startswith(pattern)]
                for k in doomed:
                    del self._local[k]
                removed += len(doomed)
        return removed


# Shared instance — one per process.
semantic_cache = SemanticCache()
