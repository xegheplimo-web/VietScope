"""LOCAL-1: LocalDiscoveryService — bounded parallel local discovery.

Single orchestration point for `/v1/business/search`. All fallback,
expansion, dedup and ranking logic lives here so the API handler stays
thin and both the HTTP endpoint and (future) MCP `local_search()` tool
delegate to the same service.

Pipeline stages (each timed):
  understand → local_retrieval → quality_gate → external_widen
              → normalize_dedup → rank

Hard deadline on the external widen stage ensures latency stays bounded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from core.business_entity import category_for, osm_tag_kv
from models import BusinessEntity, SearchCategory
from storage.business_store import BusinessStore

from services.admin import admin_anchor
from services.geo import GeoPoint, geocode
from services.geo_postgis import osm_pois_nearby

logger = logging.getLogger(__name__)

# ─── Bounded expansion limits ────────────────────────────────────────────────

MAX_EXPANSIONS = 6
MAX_EXTERNAL_QUERIES = 3  # q0 + top-2 expansions into web
EXTERNAL_DEADLINE_S = 2.5  # hard cancel on external widen

# ─── Vietnamese specialty lexicon ────────────────────────────────────────────

_SPECIALTY_LEXICON: dict[str, list[str]] = {
    "giò chả": ["giò chả", "giò lụa", "chả lụa", "chả giò", "bánh chả"],
    "phở": ["phở", "bún phở", "phở bò", "phở gà"],
    "bún chả": ["bún chả", "chả cá", "bún chả cá"],
    "bánh mì": ["bánh mì", "bánh mì trứng", "bánh mì ốp la"],
    "cà phê": ["cà phê", "coffee", "cafe"],
    "bánh sinh nhật": ["bánh sinh nhật", "bánh kem", "bánh ngọt"],
    "sắt thép": ["sắt thép", "vật liệu xây", "thép"],
}


# ─── Query understanding ─────────────────────────────────────────────────────


@dataclass
class QueryIntent:
    """Deterministic query understanding — NO LLM calls."""

    original: str
    broad_category: str
    specialty: str | None = None
    specialty_variants: list[str] = field(default_factory=list)
    location: str | None = None
    geo_point: GeoPoint | None = None
    specificity: str = "category"


@dataclass
class LocalDiscoveryResult:
    """Internal result shape returned by LocalDiscoveryService.search()."""

    matches: list[BusinessEntity] = field(default_factory=list)
    related: list[BusinessEntity] = field(default_factory=list)
    anchor: GeoPoint | None = None
    lanes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)


def _extract_specialty(query: str) -> tuple[str | None, list[str]]:
    """Deterministic specialty detection (no LLM)."""
    fold_q = query.lower().strip()
    for specialty, variants in _SPECIALTY_LEXICON.items():
        forms = [specialty] + variants
        for form in forms:
            if form in fold_q:
                return specialty, variants
    return None, []


async def _resolve_location(
    query: str, lat: float | None, lon: float | None
) -> tuple[str | None, tuple[float, float] | None]:
    """Resolve location from explicit coords or admin gazer."""
    if lat is not None and lon is not None:
        return None, (lat, lon)
    anchor = await admin_anchor(query)
    if anchor:
        return anchor.display_name or anchor.name, (anchor.lat, anchor.lon)
    point = await geocode(query)
    if point:
        return point.display_name, (point.lat, point.lon)
    return None, None


async def _understand(query: str, lat: float | None, lon: float | None) -> QueryIntent:
    """Stage 1: understand — deterministic query understanding."""
    t0 = time.perf_counter()
    broad_category = category_for(query) or "store"
    specialty, variants = _extract_specialty(query)
    location_text, coords = await _resolve_location(query, lat, lon)
    geo_point = None
    if coords:
        geo_point = GeoPoint(
            name=location_text or query,
            lat=coords[0],
            lon=coords[1],
            display_name=location_text or "",
        )
    intent = QueryIntent(
        original=query,
        broad_category=broad_category,
        specialty=specialty,
        specialty_variants=variants,
        location=location_text,
        geo_point=geo_point,
        specificity="specialty" if specialty else "category",
    )
    intent.understanding_ms = (time.perf_counter() - t0) * 1000.0  # type: ignore[attr-defined]
    return intent


# ─── Query expansion ─────────────────────────────────────────────────────────


def _expand_queries(intent: QueryIntent) -> list[str]:
    """Deterministic bounded query expansion — no LLM."""
    variants: list[str] = [intent.original]
    seen: set[str] = {intent.original}
    if intent.specialty:
        for v in intent.specialty_variants:
            if v == intent.specialty:
                continue
            q = v
            if intent.location:
                q = f"{v} {intent.location}"
            if q not in seen:
                variants.append(q)
                seen.add(q)
            if len(variants) >= MAX_EXPANSIONS:
                break
    if intent.location and len(variants) < MAX_EXPANSIONS:
        cat_q = intent.location
        if cat_q not in seen:
            variants.append(cat_q)
            seen.add(cat_q)
    return variants[:MAX_EXPANSIONS]


# ─── Dedup helpers ───────────────────────────────────────────────────────────


def _normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("84") and len(digits) >= 10:
        digits = "0" + digits[2:]
    return digits or None


def _normalize_website(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().removeprefix("www.")
    return netloc or None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def _name_similarity(a: str, b: str) -> float:
    from core.entity_resolver import fold

    a_tokens = set(fold(a).split())
    b_tokens = set(fold(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    union = a_tokens | b_tokens
    return len(intersection) / len(union) if union else 0.0


def _geo_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _dedup_candidates(candidates: list[BusinessEntity]) -> list[BusinessEntity]:
    """Dedup using strong signals in order."""
    unique: list[BusinessEntity] = []
    groups: list[list[BusinessEntity]] = []
    for cand in candidates:
        merged = False
        for group in groups:
            rep = group[0]
            if (
                rep.phone
                and cand.phone
                and _normalize_phone(rep.phone) == _normalize_phone(cand.phone)
            ):
                group.append(cand)
                merged = True
                break
            if (
                rep.website
                and cand.website
                and _normalize_website(rep.website) == _normalize_website(cand.website)
            ):
                group.append(cand)
                merged = True
                break
            if (
                rep.lat
                and rep.lon
                and cand.lat
                and cand.lon
                and _name_similarity(rep.name, cand.name) >= 0.8
                and _geo_distance_m(rep.lat, rep.lon, cand.lat, cand.lon) < 100
            ):
                group.append(cand)
                merged = True
                break
            if (
                rep.address
                and cand.address
                and _name_similarity(rep.name, cand.name) >= 0.9
                and _normalize_text(rep.address) == _normalize_text(cand.address)
            ):
                group.append(cand)
                merged = True
                break
        if not merged:
            groups.append([cand])
            unique.append(cand)
    return unique


# ─── Quality gate ───────────────────────────────────────────────────────────


def _category_compatible(cat: str, target: str) -> bool:
    if not cat:
        return True
    if cat == target:
        return True
    food_cats = {"restaurant", "cafe", "food", "bar", "store", "convenience"}
    return target in food_cats and cat in food_cats


def _entity_supports_specialty(entity: BusinessEntity, specialty: str, variants: list[str]) -> bool:
    fold_name = _normalize_text(entity.name)
    fold_desc = _normalize_text(entity.description)
    fold_addr = _normalize_text(entity.address)
    all_text = f"{fold_name} {fold_desc} {fold_addr}"
    forms = [specialty] + variants
    return any(form in all_text for form in forms)


def _assess_quality(
    candidates: list[BusinessEntity],
    intent: QueryIntent,
    lat: float | None,
    lon: float | None,
    radius_km: float,
) -> dict[str, Any]:
    useful = 0
    exact_category = 0
    specialty_matches = 0
    geo_relevant = 0
    complete = 0
    max_dist_m = radius_km * 1000.0

    for e in candidates:
        is_category_match = _category_compatible(e.category, intent.broad_category)
        if is_category_match:
            exact_category += 1
        has_specialty = bool(intent.specialty) and _entity_supports_specialty(
            e, intent.specialty, intent.specialty_variants
        )
        if has_specialty:
            specialty_matches += 1
        is_geo_relevant = True
        if lat is not None and lon is not None and e.lat and e.lon:
            dist = _geo_distance_m(lat, lon, e.lat, e.lon)
            is_geo_relevant = dist <= max_dist_m
        elif lat is not None and lon is not None:
            is_geo_relevant = False
        if is_geo_relevant:
            geo_relevant += 1
        if e.name and e.address and (e.phone or e.lat):
            complete += 1
        is_useful = is_category_match and (is_geo_relevant or has_specialty)
        if intent.specialty and not has_specialty:
            is_useful = False
        if is_useful:
            useful += 1

    min_results = min(20, 5)
    sufficient = useful >= min_results and geo_relevant >= 3
    if lat is None or lon is None:
        sufficient = useful >= min_results

    return {
        "useful_count": useful,
        "exact_category_count": exact_category,
        "specialty_match_count": specialty_matches,
        "geo_relevant_count": geo_relevant,
        "complete_count": complete,
        "unique_count": len(candidates),
        "sufficient": sufficient,
        "reason": f"useful={useful}, geo={geo_relevant}, specialty={specialty_matches}"
        if not sufficient
        else "ok",
    }


# ─── Ranking ─────────────────────────────────────────────────────────────────


def _rank_candidates(
    candidates: list[BusinessEntity],
    intent: QueryIntent,
    lat: float | None,
    lon: float | None,
    radius_km: float,
) -> list[BusinessEntity]:
    max_dist_m = radius_km * 1000.0
    for e in candidates:
        score = 0.0
        terms = set(intent.original.lower().split())
        name_lower = e.name.lower() if e.name else ""
        overlap = sum(1 for t in terms if t in name_lower)
        score += (overlap / max(len(terms), 1)) * 0.25
        if _category_compatible(e.category, intent.broad_category):
            score += 0.20
        if intent.specialty and _entity_supports_specialty(
            e, intent.specialty, intent.specialty_variants
        ):
            score += 0.30
        if lat and lon and e.lat and e.lon:
            dist = _geo_distance_m(lat, lon, e.lat, e.lon)
            if dist <= max_dist_m:
                score += 0.15 * (1.0 - dist / max_dist_m)
            else:
                score -= 0.20
        completeness = sum(1 for f in [e.address, e.phone, e.website, e.hours] if f)
        score += (completeness / 4) * 0.10
        e._score = score  # type: ignore[attr-defined]
    candidates.sort(key=lambda x: getattr(x, "_score", 0.0), reverse=True)
    return candidates


# ─── LocalDiscoveryService ──────────────────────────────────────────────────


class LocalDiscoveryService:
    """Shared local discovery service for `/v1/business/search`."""

    async def search(
        self,
        query: str,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float = 2.0,
        category: str | None = None,
        limit: int = 20,
    ) -> LocalDiscoveryResult:
        """Run bounded local discovery with progress stages."""
        all_timings: dict[str, float] = {}
        lanes: list[str] = []

        # ── Stage 1: understand ──
        t = time.perf_counter()
        intent = await _understand(query, lat, lon)
        all_timings["understanding_ms"] = (time.perf_counter() - t) * 1000.0

        effective_lat = lat
        effective_lon = lon
        if intent.geo_point and effective_lat is None:
            effective_lat = intent.geo_point.lat
            effective_lon = intent.geo_point.lon

        # ── Stage 2: local_retrieval (parallel) ──
        t = time.perf_counter()
        local_candidates, local_lanes = await self._local_retrieval(
            intent, effective_lat, effective_lon, radius_km, category, limit
        )
        all_timings["local_ms"] = (time.perf_counter() - t) * 1000.0
        lanes.extend(local_lanes)

        # Dedup local
        local_candidates = _dedup_candidates(local_candidates)

        # ── Stage 3: quality_gate ──
        quality = _assess_quality(local_candidates, intent, effective_lat, effective_lon, radius_km)

        # ── Stage 4: external_widen ──
        external_candidates: list[BusinessEntity] = []
        need_widen = not quality["sufficient"]
        if need_widen:
            t = time.perf_counter()
            external_candidates, ext_lanes = await self._external_widen(
                intent, limit - len(local_candidates)
            )
            all_timings["external_ms"] = (time.perf_counter() - t) * 1000.0
            lanes.extend(ext_lanes)

        # ── Stage 5: normalize_dedup ──
        t = time.perf_counter()
        all_candidates = local_candidates + external_candidates
        all_candidates = _dedup_candidates(all_candidates)
        all_timings["dedup_ms"] = (time.perf_counter() - t) * 1000.0

        # ── Stage 6: rank ──
        t = time.perf_counter()
        ranked = _rank_candidates(all_candidates, intent, effective_lat, effective_lon, radius_km)
        all_timings["rank_ms"] = (time.perf_counter() - t) * 1000.0

        # Split matches vs related
        matches: list[BusinessEntity] = []
        related: list[BusinessEntity] = []
        for e in ranked[:limit]:
            if intent.specialty:
                if _entity_supports_specialty(e, intent.specialty, intent.specialty_variants):
                    matches.append(e)
                else:
                    related.append(e)
            else:
                if _category_compatible(e.category, intent.broad_category):
                    matches.append(e)
                else:
                    related.append(e)

        all_timings["total_ms"] = sum(
            v for k, v in all_timings.items() if k.endswith("_ms") and k != "total_ms"
        )

        quality_final = _assess_quality(
            all_candidates, intent, effective_lat, effective_lon, radius_km
        )

        return LocalDiscoveryResult(
            matches=matches,
            related=related,
            anchor=intent.geo_point,
            lanes=lanes,
            timings=all_timings,
            quality=quality_final,
        )

    async def _local_retrieval(
        self,
        intent: QueryIntent,
        lat: float | None,
        lon: float | None,
        radius_km: float,
        category: str | None,
        limit: int,
    ) -> tuple[list[BusinessEntity], list[str]]:
        """Run canonical Postgres and local OSM lanes in parallel."""
        if lat is None or lon is None:
            return [], []
        store = BusinessStore()
        tag_kv = osm_tag_kv(category or intent.original)
        canonical_task = store.search_nearby(lat, lon, radius_km, category, limit)
        osm_task = osm_pois_nearby(lat, lon, radius_km, tag_kv=tag_kv, limit=limit)
        results = await asyncio.gather(canonical_task, osm_task, return_exceptions=True)
        entities: list[BusinessEntity] = []
        lanes: list[str] = []
        lane_names = ["business_store", "osm_local"]
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.info("Local lane %d failed: %s", i, result)
                continue
            if result:
                entities.extend(result)  # type: ignore[arg-type]
                lanes.append(lane_names[i])
        return entities, lanes

    async def _external_widen(
        self,
        intent: QueryIntent,
        remaining: int,
    ) -> tuple[list[BusinessEntity], list[str]]:
        """Run external web discovery with a hard deadline."""
        from core.business_entity import extract_business_batch
        from core.inference_gateway import get_inference_gateway
        from providers.ddgs import ddgs_search
        from providers.searxng import searxng_search

        queries = _expand_queries(intent)[:MAX_EXTERNAL_QUERIES]
        lanes: list[str] = []
        all_entities: list[BusinessEntity] = []

        async def _search_provider(provider_name: str, q: str) -> list[BusinessEntity]:
            try:
                if provider_name == "searxng":
                    results = await searxng_search(
                        q, categories=[SearchCategory.general], max_results=remaining + 5, lang="vi"
                    )
                elif provider_name == "ddgs":
                    results = await ddgs_search(
                        q, categories=[SearchCategory.general], max_results=remaining + 5, lang="vi"
                    )
                else:
                    return []
                from models import Source

                sources = [
                    Source(
                        source_id=f"{provider_name}:{i}",
                        url=r.url,
                        title=r.title or "",
                        description=r.description or "",
                        content=r.description or "",
                        search_provider=provider_name,
                    )
                    for i, r in enumerate(results)
                ]
                if not sources:
                    return []
                contents = [s.content for s in sources]
                urls = [s.url for s in sources]
                inference = get_inference_gateway()
                llm = inference if inference.api_key else None
                entities = await extract_business_batch(
                    contents, intent.original, llm=llm, source_urls=urls
                )
                return entities
            except Exception as exc:
                logger.info("External provider %s failed: %s", provider_name, exc)
                return []

        # Run SearXNG + DDGS in parallel with deadline
        tasks = []
        for q in queries:
            tasks.append(_search_provider("searxng", q))
            tasks.append(_search_provider("ddgs", q))

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=EXTERNAL_DEADLINE_S,
            )
            for result in results:
                if isinstance(result, list):
                    all_entities.extend(result)
            if all_entities:
                lanes.append("web")
        except TimeoutError:
            logger.info("External widen timed out after %.1fs", EXTERNAL_DEADLINE_S)
            lanes.append("web_timeout")

        return all_entities, lanes
