"""P17 place caches — Redis-backed, memory fallback, epoch invalidation.

Three logical caches share one versioned namespace
(``places:v<DOCUMENT_VERSION>``):

- **search** — full result lists keyed by every query parameter that can
  change them (folded text, rounded geo, radius, category, admin, status
  set, bbox, limit) plus a global ``epoch`` so a canonical update
  invalidates every derived key in O(1).
- **place** — place-by-id documents (``/v1/places/{id}``), deleted
  precisely on update and epoch-scoped like the derived caches so a
  rebuild/reconcile (``invalidate_all``) orphans stale entries instead
  of serving a deleted place until TTL expiry.
- **suggest** — autocomplete lists, same epoch scheme as search.

The epoch is a Redis counter the indexer bumps whenever it writes or
deletes a place: search/suggest keys embed the epoch observed at write
time, so post-update reads miss once and re-cache. When Redis is
unreachable the cache transparently degrades to a bounded in-memory map —
search still works, just without shared caching.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

from config import settings
from storage.cache import deserialize, serialize
from storage.redis_client import get_redis, mark_redis_unavailable

from serving.places.document import PLACE_DOCUMENT_VERSION
from serving.places.query import LocalQuerySpec

logger = logging.getLogger(__name__)

_EPOCH_KEY = "epoch"
_MEM_MAX = 1000


def _round(v: float | None, ndigits: int = 3) -> float | None:
    """~110 m quantization — keeps geo keys stable without collapsing
    genuinely different locations into one entry."""
    return round(v, ndigits) if v is not None else None


class _MemStore:
    """Bounded in-process TTL store — the no-Redis fallback."""

    def __init__(self, max_size: int = _MEM_MAX) -> None:
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max = max_size
        self.epoch = 0

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        exp, raw = entry
        if time.time() > exp:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return raw

    def set(self, key: str, raw: str, ttl: int) -> None:
        self._data[key] = (time.time() + ttl, raw)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self.epoch = 0


class PlaceCache:
    """Versioned namespace over Redis (or memory) for the places lane.

    ``client`` may be an explicit async-Redis-like object (tests); when
    omitted the shared ``get_redis()`` resolution applies per call.
    ``redis_enabled=False`` pins the cache to memory regardless.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        namespace: str = "places",
        redis_enabled: bool = True,
        ttl_search: int | None = None,
        ttl_place: int | None = None,
        ttl_suggest: int | None = None,
    ) -> None:
        self._ns = f"{namespace}:v{PLACE_DOCUMENT_VERSION}"
        self._client = client
        self._redis_enabled = redis_enabled
        self._mem = _MemStore()
        self.ttl_search = ttl_search or getattr(settings, "places_cache_ttl_search", 300)
        self.ttl_place = ttl_place or getattr(settings, "places_cache_ttl_place", 3600)
        self.ttl_suggest = ttl_suggest or getattr(settings, "places_cache_ttl_suggest", 300)

    # ── plumbing ─────────────────────────────────────────────────────────

    async def _redis(self):
        if not self._redis_enabled:
            return None
        if self._client is not None:
            return self._client
        return await get_redis()

    def _key(self, kind: str, parts: str) -> str:
        return f"{self._ns}:{kind}:{parts}"

    async def _epoch(self) -> int:
        cli = await self._redis()
        if cli is None:
            return self._mem.epoch
        try:
            raw = await cli.get(self._key("meta", _EPOCH_KEY))
            return int(raw) if raw is not None else 0
        except Exception:
            mark_redis_unavailable(cli)
            return self._mem.epoch

    async def _bump_epoch(self) -> int:
        cli = await self._redis()
        if cli is None:
            self._mem.epoch += 1
            return self._mem.epoch
        try:
            return int(await cli.incr(self._key("meta", _EPOCH_KEY)))
        except Exception:
            mark_redis_unavailable(cli)
            self._mem.epoch += 1
            return self._mem.epoch

    async def _get(self, kind: str, parts: str) -> Any | None:
        key = self._key(kind, parts)
        cli = await self._redis()
        raw = None
        if cli is not None:
            try:
                raw = await cli.get(key)
            except Exception:
                mark_redis_unavailable(cli)
                cli = None
        if raw is None and cli is None:
            raw = self._mem.get(key)
        if raw is None:
            return None
        try:
            return deserialize(raw)
        except Exception:
            return None

    async def _set(self, kind: str, parts: str, value: Any, ttl: int) -> None:
        key = self._key(kind, parts)
        raw = serialize(value)
        cli = await self._redis()
        if cli is not None:
            try:
                await cli.set(key, raw, ex=ttl)
                return
            except Exception:
                mark_redis_unavailable(cli)
        self._mem.set(key, raw, ttl)

    async def _delete(self, kind: str, parts: str) -> None:
        key = self._key(kind, parts)
        cli = await self._redis()
        if cli is not None:
            try:
                await cli.delete(key)
            except Exception:
                mark_redis_unavailable(cli)
        self._mem.delete(key)

    # ── key builders ─────────────────────────────────────────────────────

    def _place_parts(self, place_id: str, epoch: int) -> str:
        return f"{epoch}:{place_id}"

    def _search_parts(self, spec: LocalQuerySpec, epoch: int) -> str:
        raw = "|".join(
            [
                spec.text,
                str(_round(spec.lat)),
                str(_round(spec.lon)),
                str(int(spec.radius_m)),
                spec.category or "",
                str(spec.admin_unit_id or ""),
                ",".join(sorted(spec.statuses)),
                ",".join(str(x) for x in spec.bbox) if spec.bbox else "",
                str(spec.limit),
                str(spec.admin_contains),
                # P17.1 — every result-shaping param must key the entry;
                # open_now verdicts additionally age out via ttl_search.
                str(spec.open_now),
                str(spec.min_rating or ""),
                spec.price_level or "",
                spec.sort or "",
                str(epoch),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def get_search(self, spec: LocalQuerySpec) -> Any | None:
        epoch = await self._epoch()
        return await self._get("s", self._search_parts(spec, epoch))

    async def set_search(self, spec: LocalQuerySpec, value: Any) -> None:
        epoch = await self._epoch()
        await self._set("s", self._search_parts(spec, epoch), value, self.ttl_search)

    async def get_place(self, place_id: str) -> Any | None:
        epoch = await self._epoch()
        return await self._get("p", self._place_parts(str(place_id), epoch))

    async def set_place(self, place_id: str, value: Any) -> None:
        epoch = await self._epoch()
        await self._set("p", self._place_parts(str(place_id), epoch), value, self.ttl_place)

    async def get_suggest(
        self, q: str, lat: float | None, lon: float | None, limit: int
    ) -> Any | None:
        epoch = await self._epoch()
        parts = hashlib.sha256(
            f"{q}|{_round(lat, 2)}|{_round(lon, 2)}|{limit}|{epoch}".encode()
        ).hexdigest()[:32]
        return await self._get("a", parts)

    async def set_suggest(
        self, q: str, lat: float | None, lon: float | None, limit: int, value: Any
    ) -> None:
        epoch = await self._epoch()
        parts = hashlib.sha256(
            f"{q}|{_round(lat, 2)}|{_round(lon, 2)}|{limit}|{epoch}".encode()
        ).hexdigest()[:32]
        await self._set("a", parts, value, self.ttl_suggest)

    # ── invalidation ─────────────────────────────────────────────────────

    async def invalidate_place(self, place_id: str) -> None:
        """Canonical update landed: drop the by-id entry and bump the epoch
        so every derived (search/suggest) key misses exactly once."""
        await self._delete("p", self._place_parts(str(place_id), await self._epoch()))
        await self._bump_epoch()

    async def invalidate_all(self) -> None:
        await self._bump_epoch()

    async def stats(self) -> dict[str, Any]:
        cli = await self._redis()
        return {
            "namespace": self._ns,
            "backend": "redis" if cli is not None else "memory",
            "epoch": await self._epoch(),
            "memory_keys": len(self._mem._data),
        }
