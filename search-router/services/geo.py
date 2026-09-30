"""OSM geo services — Nominatim geocoding + Overpass amenity search (P7).

Both calls degrade to ``None``/``[]`` on timeout or HTTP error — geo is a
best-effort enrichment lane, never a hard dependency. Public endpoints
need no key; a descriptive User-Agent is sent per OSM usage policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from models import BusinessEntity

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_UA = {"User-Agent": "SearchHub/3.0 (VietScope; github.com/xegheplimo-web/search-hub)"}
_GEOCODE_TIMEOUT = 8.0
_OVERPASS_TIMEOUT = 15.0


@dataclass(frozen=True)
class GeoPoint:
    """A geocoded anchor: what it resolved to + where it is."""

    name: str
    lat: float
    lon: float
    display_name: str = ""


async def geocode(name: str, *, timeout: float = _GEOCODE_TIMEOUT) -> GeoPoint | None:
    """Resolve a place name to coordinates via Nominatim (VN-biased).

    ``countrycodes=vn`` keeps "Bình Dương" inside Vietnam; falls back to
    worldwide when the VN-scoped lookup misses.
    """
    if not (name or "").strip():
        return None
    for params in (
        {"q": name, "format": "json", "limit": 1, "countrycodes": "vn"},
        {"q": name, "format": "json", "limit": 1},
    ):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=_UA, follow_redirects=True
            ) as client:
                resp = await client.get(NOMINATIM_URL, params=params)
                resp.raise_for_status()
                rows = resp.json()
        except Exception as exc:
            logger.info("geocode %r failed: %s", name, exc)
            return None
        if rows:
            row = rows[0]
            try:
                return GeoPoint(
                    name=name,
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    display_name=str(row.get("display_name") or ""),
                )
            except (KeyError, TypeError, ValueError):
                return None
    return None


async def overpass_amenities(
    lat: float,
    lon: float,
    radius_km: float,
    tag: str | None = None,
    *,
    limit: int = 20,
    timeout: float = _OVERPASS_TIMEOUT,
) -> list[BusinessEntity]:
    """OSM POIs around a point — the live fallback for PostGIS.

    ``tag`` is a raw Overpass tag filter like ``["amenity"="pharmacy"]``
    or ``["shop"="supermarket"]`` (see ``osm_tag_for``); when omitted,
    every named amenity node within the radius is returned.
    """
    if radius_km <= 0:
        return []
    around = int(radius_km * 1000)
    tag_filter = tag if tag else '["amenity"]'
    ql = (
        f"[out:json][timeout:{int(timeout)}];"
        f"nwr{tag_filter}(around:{around},{lat},{lon});"
        f"out center {max(1, limit)};"
    )
    try:
        async with httpx.AsyncClient(
            timeout=timeout + 5.0, headers=_UA, follow_redirects=True
        ) as client:
            resp = await client.post(OVERPASS_URL, content=ql)
            resp.raise_for_status()
            elements = resp.json().get("elements") or []
    except Exception as exc:
        logger.info("overpass %s@%s,%s failed: %s", tag_filter, lat, lon, exc)
        return []

    out: list[BusinessEntity] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        center = el.get("center") or {}
        elat = center.get("lat", el.get("lat"))
        elon = center.get("lon", el.get("lon"))
        addr = " ".join(
            p
            for p in (
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:district") or tags.get("addr:city"),
            )
            if p
        )
        out.append(
            BusinessEntity(
                name=str(name),
                category=str(tags.get("amenity") or ""),
                address=addr,
                phone=tags.get("phone") or tags.get("contact:phone"),
                hours=tags.get("opening_hours"),
                website=tags.get("website") or tags.get("contact:website"),
                lat=float(elat) if elat is not None else None,
                lon=float(elon) if elon is not None else None,
                description=str(tags.get("description") or ""),
                source_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            )
        )
    return out
