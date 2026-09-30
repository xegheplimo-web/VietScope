"""P17 place service — read-path orchestration for ``/v1/places/*``.

One request flows: normalize → cache → OpenSearch candidates → PostGIS
precision → fusion rank → response. Every dependency is optional by
design:

- Redis down → cache misses, everything still answers.
- OpenSearch down/empty → PostGIS lane answers from canonical truth.
- Postgres down → the OpenSearch read model still serves (distance falls
  back to haversine; detail serves the indexed doc without provenance).
- Both down → empty/unavailable, never fabricated rows.

Lane timings and degradation are collected in ``LaneMeta`` and surfaced
as ``X-Places-*`` response headers + per-row ``score_debug``.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings
from storage import pg_client

from serving.places import geo
from serving.places.cache import PlaceCache
from serving.places.document import PlaceDocumentV1
from serving.places.os_index import PlaceIndexUnavailable, PlaceOSIndex
from serving.places.projection import (
    ALIASES_SQL,
    PLACE_BY_ID_SQL,
    PROVENANCE_FOR_SQL,
    SOURCES_FOR_SQL,
    project_row,
)
from serving.places.query import fold_text, parse_local_query
from serving.places.ranking import (
    Candidate,
    RankWeights,
    debug_components,
    haversine_m,
    rank,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LaneMeta:
    """Per-request observability: which lanes served, how long, what broke."""

    lanes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    cache_hit: bool = False
    candidates: int = 0
    cache_ms: float = 0.0
    os_ms: float = 0.0
    pg_ms: float = 0.0
    fusion_ms: float = 0.0
    total_ms: float = 0.0


@dataclass(slots=True)
class DetailResult:
    """Outcome of a by-id lookup; the endpoint maps status → HTTP."""

    status: str  # ok | not_found | unavailable
    payload: dict[str, Any] | None = None
    meta: LaneMeta = field(default_factory=LaneMeta)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 2)


# ── serve-time derivations (P2.0) ─────────────────────────────────────

# Canonical hours are Vietnam-local; there is no per-place timezone column,
# so open_now resolves against a fixed UTC+7 clock. A naive ``now`` argument
# (tests) is treated as already-UTC+7 wall time.
_VN_TZ = timezone(timedelta(hours=7), name="UTC+7")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The live corpus is a Google Maps -lang vi scrape: opening_hours keys days
# in Vietnamese. Map the (normalized, lowercase) VN names onto the canonical
# English names so day lookup below stays a single code path.
_VN_WEEKDAYS = {
    "thứ hai": "monday",
    "thứ ba": "tuesday",
    "thứ tư": "wednesday",
    "thứ năm": "thursday",
    "thứ sáu": "friday",
    "thứ bảy": "saturday",
    "chủ nhật": "sunday",
}
# Recognized "closed" marker — an explicit statement, not an unparseable
# blob. A day listing only markers resolves to closed (like ``[]``).
_CLOSED_RE = re.compile(r"đóng cửa|closed", re.I)


def _norm_text(s: Any) -> str:
    """NFC + whitespace-collapse — scraped VN text varies in accents/spacing."""
    return " ".join(unicodedata.normalize("NFC", str(s)).split())


def _hm_to_min(text: Any) -> int | None:
    """'8:00' / '08:00' / '24:00' → minutes since midnight."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 24 or minute > 59 or (h == 24 and minute):
        return None
    return h * 60 + minute


def _day_ranges(hours: dict[str, Any], day: str) -> tuple[list | None, list[tuple[int, int]]]:
    """(items, ranges) for ``day``: items None = no entry at all, [] = an
    explicit empty list (closed that day), non-empty = entries that may or
    may not parse into (start,end) minute ranges."""
    lowered: dict[str, Any] = {}
    for k, v in hours.items():
        key = _norm_text(k).lower()
        lowered[_VN_WEEKDAYS.get(key, key)] = v
    if day.lower() not in lowered:
        return None, []
    raw = lowered[day.lower()]
    items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    items = [i for i in items if not (isinstance(i, str) and _CLOSED_RE.fullmatch(_norm_text(i)))]
    ranges: list[tuple[int, int]] = []
    for item in items:
        if isinstance(item, str):
            text = _norm_text(item)
            if re.search(r"24\s*(hours?|hrs?|h)\b|24/7|mở cửa cả ngày", text, re.I):
                ranges.append((0, 24 * 60))
                continue
            parts = re.split(r"[–—-]", text)
            if len(parts) != 2:
                continue
            pair = (_hm_to_min(parts[0]), _hm_to_min(parts[1]))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pair = (_hm_to_min(item[0]), _hm_to_min(item[1]))
        else:
            continue
        if pair[0] is not None and pair[1] is not None:
            ranges.append((pair[0], pair[1]))
    return items, ranges


def open_now(opening_hours: dict[str, Any] | None, *, now: datetime | None = None) -> bool | None:
    """Resolve 'is the place open right now' from canonical opening_hours.

    Returns None when hours are missing or nothing parses — we never guess.
    A day with no entry is unknown (not closed): only an explicit ``[]``
    means closed. Overnight ranges ("18:00–02:00") attribute the
    post-midnight tail to the day they open on.
    """
    if not isinstance(opening_hours, dict) or not opening_hours:
        return None
    if now is None:
        now = datetime.now(_VN_TZ)
    elif now.tzinfo is not None:
        now = now.astimezone(_VN_TZ)
    minute = now.hour * 60 + now.minute
    today = _WEEKDAYS[now.weekday()]
    yesterday = _WEEKDAYS[(now.weekday() - 1) % 7]

    items_t, todays = _day_ranges(opening_hours, today)
    for start, end in todays:
        if start <= end and start <= minute <= end:
            return True
        if start > end and minute >= start:  # overnight, pre-midnight side
            return True
    for start, end in _day_ranges(opening_hours, yesterday)[1]:
        if start > end and minute <= end:  # overnight tail, post-midnight
            return True

    if items_t is not None:
        # Listed today: ranges present but unmatched → closed; explicit
        # empty list → closed; unparseable entries → don't guess.
        return False if todays or not items_t else None
    # No entry today and no overnight tail — nothing was recorded for
    # this day, so open/closed is unknown rather than a guessed "closed".
    return None


def _map_url(lat: float | None, lon: float | None) -> str | None:
    if lat is None or lon is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


# Serve-time fields the cache never owns: open_now is a verdict about
# *now*, so a cached entry keeps only its evidence (opening_hours, lat/lon)
# and every response — hit or miss — re-derives it against the live clock.
_VOLATILE_KEYS = ("open_now", "map_url")


def _strip_volatile(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _VOLATILE_KEYS}


class PlaceService:
    """The P17 read path. Dependencies injectable for tests."""

    def __init__(
        self,
        *,
        os_index: Any | None = None,
        cache: PlaceCache | None = None,
        pool_getter: Any | None = None,
        weights: RankWeights | None = None,
        candidate_topk: int | None = None,
        clock: Any | None = None,
    ) -> None:
        self._os = os_index if os_index is not None else PlaceOSIndex()
        self._cache = cache if cache is not None else PlaceCache()
        self._pool_getter = pool_getter  # None → resolve pg_client per call
        self._weights = weights or RankWeights.from_json(
            getattr(settings, "places_rank_weights", "")
        )
        self._topk = candidate_topk or getattr(settings, "places_candidate_topk", 100)
        self._clock = clock  # callable → datetime; None → live UTC+7 clock

    def _now(self) -> datetime | None:
        return self._clock() if self._clock is not None else None

    def _refresh_volatile(self, row: dict[str, Any]) -> dict[str, Any]:
        """Re-derive the fields the cache does not store."""
        if isinstance(row, dict):
            row["open_now"] = open_now(row.get("opening_hours"), now=self._now())
            row["map_url"] = _map_url(row.get("lat"), row.get("lon"))
        return row

    def _apply_open_verdicts(self, candidates: list[Candidate]) -> None:
        """Resolve ``doc.open_now`` once per candidate on the request clock.

        In a dedicated method because ``search`` takes an ``open_now``
        kwarg — inside its body the name binds the bool, not the fn.
        """
        now = self._now()
        for c in candidates:
            c.doc.open_now = open_now(c.doc.opening_hours, now=now)

    async def _pool(self):
        # Resolved per call (not bound at __init__) so tests can monkeypatch
        # pg_client.get_pool and runtime failovers re-probe.
        getter = self._pool_getter or pg_client.get_pool
        try:
            return await getter()
        except Exception as exc:
            logger.warning("postgres pool unavailable: %r", exc)
            return None

    # ── search ───────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        q: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float | None = None,
        category: str | None = None,
        admin_unit_id: int | None = None,
        status: str | None = None,
        bbox: str | None = None,
        limit: int = 20,
        debug: bool = False,
        admin_contains: bool = False,
        open_now: bool | None = None,
        min_rating: float | None = None,
        price_level: str | None = None,
        sort: str | None = None,
    ) -> tuple[list[dict[str, Any]], LaneMeta]:
        meta = LaneMeta()
        t_start = time.perf_counter()
        spec = parse_local_query(
            q=q,
            lat=lat,
            lon=lon,
            radius_m=radius_m,
            category=category,
            admin_unit_id=admin_unit_id,
            status=status,
            bbox=bbox,
            limit=limit,
            debug=debug,
            admin_contains=admin_contains,
            open_now=open_now,
            min_rating=min_rating,
            price_level=price_level,
            sort=sort,
        )

        if not debug and spec.open_now is None:
            t = time.perf_counter()
            cached = await self._cache.get_search(spec)
            meta.cache_ms = _ms(t)
            if cached is not None:
                meta.cache_hit = True
                meta.lanes.append("cache")
                meta.total_ms = _ms(t_start)
                return [self._refresh_volatile(r) for r in cached], meta

        pool = await self._pool()
        candidates: list[Candidate] = []

        # Polygon containment is a PostGIS-only capability — go straight to
        # the canonical lane when requested.
        os_hits: list[tuple[PlaceDocumentV1, float | None]] | None = None
        if not spec.admin_contains:
            t = time.perf_counter()
            try:
                os_hits = await self._os.search(spec, top_k=self._topk)
                meta.lanes.append("opensearch")
            except PlaceIndexUnavailable as exc:
                meta.degraded.append("opensearch")
                logger.warning("places opensearch lane down: %s", exc)
            except Exception as exc:  # noqa: BLE001 — lane must degrade, not crash
                meta.degraded.append("opensearch")
                logger.warning("places opensearch lane error: %r", exc)
            meta.os_ms = _ms(t)

        if os_hits:
            candidates = [
                Candidate(doc=doc, os_score=score, lane="opensearch") for doc, score in os_hits
            ]
            if spec.lat is not None and spec.lon is not None:
                ids = [int(c.doc.place_id) for c in candidates if str(c.doc.place_id).isdigit()]
                dists: dict[int, float] = {}
                if pool is not None:
                    t = time.perf_counter()
                    try:
                        dists = await geo.distances(pool, ids, spec.lat, spec.lon)
                        meta.lanes.append("postgis-dist")
                    except Exception as exc:  # noqa: BLE001
                        meta.degraded.append("postgis")
                        logger.warning("postgis distance lane failed: %r", exc)
                    meta.pg_ms += _ms(t)
                for c in candidates:
                    pid = int(c.doc.place_id) if str(c.doc.place_id).isdigit() else -1
                    c.distance_m = dists.get(pid)
                    if c.distance_m is None and c.doc.lat is not None and c.doc.lon is not None:
                        c.distance_m = haversine_m(spec.lat, spec.lon, c.doc.lat, c.doc.lon)
        else:
            # Index miss or outage — canonical PostGIS/text fallback lane.
            if pool is not None:
                t = time.perf_counter()
                try:
                    # open_now post-filters below — over-fetch so the filter
                    # does not starve the limit (mirrors the OS lane's top_k).
                    fetch_spec = (
                        replace(spec, limit=max(spec.limit, self._topk))
                        if spec.open_now is not None
                        else spec
                    )
                    rows = await geo.search_candidates(pool, fetch_spec)
                    meta.lanes.append("postgis")
                except Exception as exc:  # noqa: BLE001
                    meta.degraded.append("postgis")
                    logger.warning("postgis candidate lane failed: %r", exc)
                    rows = []
                meta.pg_ms += _ms(t)
                candidates = [
                    Candidate(
                        doc=project_row(r),
                        distance_m=r.get("distance_m"),
                        lane="postgis",
                    )
                    for r in rows
                ]
            elif not os_hits:
                meta.degraded.append("postgres")

        # Serve-time open verdicts: resolved once per candidate against the
        # request clock — the ranker's open_now_boost and the response's
        # open_now field both read this value. ``open_now=`` post-filters
        # the over-fetched pool (top_k wide) since no index can store "now".
        self._apply_open_verdicts(candidates)
        if spec.open_now is not None:
            candidates = [c for c in candidates if c.doc.open_now is spec.open_now]

        meta.candidates = len(candidates)
        t = time.perf_counter()
        ranked = rank(
            candidates,
            query_category=spec.category,
            weights=self._weights,
            limit=spec.limit,
            sort=spec.sort,
        )
        rows_out = [self._out_row(c, debug=spec.debug, now=self._now()) for c in ranked]
        meta.fusion_ms = _ms(t)
        meta.total_ms = _ms(t_start)

        # open_now is a time-varying verdict: cached filtered sets go stale
        # across an open/close boundary. Bypass result-caching entirely when
        # open_now is requested so the freshest verdict always drives the filter.
        if not spec.debug and spec.open_now is None:
            await self._cache.set_search(spec, [_strip_volatile(r) for r in rows_out])
        return rows_out, meta

    # ── autocomplete ─────────────────────────────────────────────────────

    async def autocomplete(
        self,
        *,
        q: str,
        lat: float | None = None,
        lon: float | None = None,
        limit: int = 10,
    ) -> tuple[list[dict[str, Any]], LaneMeta]:
        meta = LaneMeta()
        t_start = time.perf_counter()
        qf = fold_text(q)
        limit = max(1, min(int(limit), 50))
        if len(qf) < 2:
            meta.total_ms = _ms(t_start)
            return [], meta

        cached = await self._cache.get_suggest(qf, lat, lon, limit)
        if cached is not None:
            meta.cache_hit = True
            meta.lanes.append("cache")
            meta.total_ms = _ms(t_start)
            return cached, meta

        out: list[dict[str, Any]] = []
        t = time.perf_counter()
        hits = None
        try:
            hits = await self._os.autocomplete(qf, limit=limit, lat=lat, lon=lon)
            meta.lanes.append("opensearch")
        except Exception as exc:  # noqa: BLE001
            meta.degraded.append("opensearch")
            logger.warning("places autocomplete lane failed: %r", exc)
        meta.os_ms = _ms(t)

        if hits:
            for doc, _score in hits[:limit]:
                d = None
                if (
                    lat is not None
                    and lon is not None
                    and doc.lat is not None
                    and doc.lon is not None
                ):
                    d = haversine_m(lat, lon, doc.lat, doc.lon)
                out.append(self._suggest_row(doc, d))
        else:
            pool = await self._pool()
            if pool is not None:
                t = time.perf_counter()
                try:
                    spec = parse_local_query(q=q, lat=lat, lon=lon, radius_m=None, limit=limit)
                    rows = await geo.search_candidates(pool, spec)
                    meta.lanes.append("postgis")
                    for r in rows:
                        d = dict(r)
                        doc = project_row(d)
                        out.append(self._suggest_row(doc, d.get("distance_m")))
                except Exception as exc:  # noqa: BLE001
                    meta.degraded.append("postgis")
                    logger.warning("postgis autocomplete fallback failed: %r", exc)
                meta.pg_ms += _ms(t)
            elif not hits:
                meta.degraded.append("postgres")

        out = out[:limit]
        await self._cache.set_suggest(qf, lat, lon, limit, out)
        meta.total_ms = _ms(t_start)
        return out, meta

    # ── by-id detail ─────────────────────────────────────────────────────

    async def get_place(self, place_id: int | str) -> DetailResult:
        meta = LaneMeta()
        t_start = time.perf_counter()
        pid = str(place_id)

        t = time.perf_counter()
        cached = await self._cache.get_place(pid)
        meta.cache_ms = _ms(t)
        if cached is not None:
            meta.cache_hit = True
            meta.lanes.append("cache")
            meta.total_ms = _ms(t_start)
            return DetailResult(status="ok", payload=self._refresh_volatile(cached), meta=meta)

        pool = await self._pool()
        payload: dict[str, Any] | None = None
        if pool is not None:
            t = time.perf_counter()
            try:
                row = await pool.fetchrow(PLACE_BY_ID_SQL, int(place_id))
            except Exception as exc:  # noqa: BLE001
                meta.degraded.append("postgres")
                logger.warning("place fetch failed: %r", exc)
                row = None
            meta.pg_ms += _ms(t)
            if row is None and not meta.degraded:
                meta.total_ms = _ms(t_start)
                return DetailResult(status="not_found", meta=meta)
            if row is not None:
                meta.lanes.append("postgres")
                try:
                    alias_rows = await pool.fetch(ALIASES_SQL, [int(place_id)])
                    aliases = [dict(r).get("raw_name") for r in alias_rows]
                except Exception:  # noqa: BLE001
                    aliases = []
                doc = project_row(dict(row), alias_names=aliases)
                sources = prov = []
                try:
                    sources = [dict(r) for r in await pool.fetch(SOURCES_FOR_SQL, int(place_id))]
                    prov = [dict(r) for r in await pool.fetch(PROVENANCE_FOR_SQL, int(place_id))]
                except Exception as exc:  # noqa: BLE001
                    meta.degraded.append("provenance")
                    logger.warning("provenance fetch failed: %r", exc)
                payload = self._detail_payload(doc, sources, prov, now=self._now())
        else:
            meta.degraded.append("postgres")

        if payload is None:
            # Canonical store absent or errored — serve the indexed doc.
            t = time.perf_counter()
            try:
                doc = await self._os.get_doc(pid)
            except PlaceIndexUnavailable:
                doc = None
                meta.degraded.append("opensearch")
            except Exception as exc:  # noqa: BLE001
                doc = None
                meta.degraded.append("opensearch")
                logger.warning("place index fetch failed: %r", exc)
            meta.os_ms += _ms(t)
            if doc is not None:
                meta.lanes.append("opensearch")
                payload = self._detail_payload(doc, [], [], now=self._now())
                payload["degraded"] = True
            elif "postgres" in meta.degraded:
                meta.total_ms = _ms(t_start)
                return DetailResult(status="unavailable", meta=meta)
            else:
                meta.total_ms = _ms(t_start)
                return DetailResult(status="not_found", meta=meta)

        meta.total_ms = _ms(t_start)
        await self._cache.set_place(pid, _strip_volatile(payload))
        return DetailResult(status="ok", payload=payload, meta=meta)

    # ── row mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _out_row(c: Candidate, *, debug: bool, now: datetime | None = None) -> dict[str, Any]:
        doc = c.doc
        out: dict[str, Any] = {
            "place_id": int(doc.place_id) if str(doc.place_id).isdigit() else doc.place_id,
            "business_id": int(doc.business_id)
            if doc.business_id and str(doc.business_id).isdigit()
            else None,
            "name": doc.name,
            "canonical_name": doc.name,
            "canonical_category": doc.category_ids[0] if doc.category_ids else None,
            "address": doc.address,
            "phone": doc.phone[0] if doc.phone else None,
            "website": doc.website,
            "opening_hours": doc.opening_hours,
            "lat": doc.lat,
            "lon": doc.lon,
            "admin_unit_id": int(doc.admin_unit_id) if doc.admin_unit_id else None,
            "status": doc.status,
            "confidence": doc.confidence,
            "source_count": doc.source_count,
            "freshness_score": doc.freshness_score,
            "distance_m": round(c.distance_m, 1) if c.distance_m is not None else None,
            "last_verified_at": doc.last_verified_at.isoformat() if doc.last_verified_at else None,
            "aliases": doc.aliases,
            "rating": doc.rating,
            "review_count": doc.review_count,
            "price_level": doc.price_level,
            "open_now": open_now(doc.opening_hours, now=now),
            "map_url": _map_url(doc.lat, doc.lon),
            "primary_image_url": doc.primary_image_url,
            "images": doc.images,
        }
        if debug:
            out["score_debug"] = debug_components(c)
        return out

    @staticmethod
    def _suggest_row(doc: PlaceDocumentV1, distance_m: float | None) -> dict[str, Any]:
        return {
            "place_id": int(doc.place_id) if str(doc.place_id).isdigit() else doc.place_id,
            "name": doc.name,
            "canonical_category": doc.category_ids[0] if doc.category_ids else None,
            "lat": doc.lat,
            "lon": doc.lon,
            "status": doc.status,
            "distance_m": round(distance_m, 1) if distance_m is not None else None,
        }

    @staticmethod
    def _detail_payload(
        doc: PlaceDocumentV1,
        sources: list[dict],
        prov: list[dict],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        # Serialized to final JSON-safe form here so the cached copy holds
        # no datetimes; open_now/map_url are stripped on store and re-derived
        # per response (see _refresh_volatile).
        def _iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else v

        return {
            "place_id": int(doc.place_id) if str(doc.place_id).isdigit() else doc.place_id,
            "business_id": int(doc.business_id)
            if doc.business_id and str(doc.business_id).isdigit()
            else None,
            "name": doc.name,
            "canonical_name": doc.name,
            "canonical_category": doc.category_ids[0] if doc.category_ids else None,
            "address": doc.address,
            "phone": doc.phone[0] if doc.phone else None,
            "website": doc.website,
            "opening_hours": doc.opening_hours,
            "lat": doc.lat,
            "lon": doc.lon,
            "admin_unit_id": int(doc.admin_unit_id) if doc.admin_unit_id else None,
            "status": doc.status,
            "confidence": doc.confidence,
            "source_count": doc.source_count,
            "freshness_score": doc.freshness_score,
            "distance_m": None,
            "last_verified_at": doc.last_verified_at.isoformat() if doc.last_verified_at else None,
            "aliases": doc.aliases,
            "rating": doc.rating,
            "review_count": doc.review_count,
            "price_level": doc.price_level,
            "open_now": open_now(doc.opening_hours, now=now),
            "map_url": _map_url(doc.lat, doc.lon),
            "primary_image_url": doc.primary_image_url,
            "images": doc.images,
            "sources": [
                {
                    "provider": s.get("provider"),
                    "external_id": s.get("external_id"),
                    "source_record_id": s.get("source_record_id"),
                    "linked_at": _iso(s.get("linked_at")),
                }
                for s in sources
            ],
            "provenance": [
                {
                    "field": p.get("field"),
                    "provider": p.get("provider"),
                    "value": p.get("value"),
                    "weight": p.get("weight"),
                    "observed_at": _iso(p.get("observed_at")),
                    "chosen": bool(p.get("chosen")),
                }
                for p in prov
            ],
        }


_default_service: PlaceService | None = None


def get_place_service() -> PlaceService:
    """Process-wide service — dependencies resolve per call so test
    monkeypatching and runtime failovers both work."""
    global _default_service
    if _default_service is None:
        _default_service = PlaceService()
    return _default_service
