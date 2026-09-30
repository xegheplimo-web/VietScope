"""Local OSM POI lane (P14) — PostGIS mirror filled by osm2pgsql.

``osm_pois`` is created by the flex import (``deploy/osm/pois.lua`` via
``scripts/import-osm-vietnam.sh``), not by a migration — an absent table
is a normal state. Presence is checked cheaply via ``to_regclass`` and
cached, and every failure degrades to ``[]``; live Overpass remains the
fallback lane in ``api.v1.business_search``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from models import BusinessEntity
from storage import pg_client

logger = logging.getLogger(__name__)

# Tag columns the flex style writes — the SQL whitelist for tag filters.
_TAG_COLUMNS = frozenset({"amenity", "shop", "tourism", "leisure", "office", "craft"})

_TABLE_CHECK_TTL_S = 60.0
_table_present = False
_table_checked_at = 0.0

_OSM_TYPE_PATH = {"N": "node", "W": "way", "R": "relation"}

_SQL = """
SELECT osm_id, osm_type, name,
       COALESCE(amenity, shop, tourism, leisure, office, craft) AS category,
       addr_housenumber, addr_street, addr_district, addr_city,
       phone, website, opening_hours,
       ST_Y(geom) AS lat, ST_X(geom) AS lon,
       ST_Distance(
           geom::geography,
           ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
       ) AS distance_m
FROM osm_pois
WHERE ST_DWithin(
    geom::geography,
    ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
    $3
)
"""


async def _has_table() -> bool:
    """Presence probe for the import-owned table (cached, degrade-safe)."""
    global _table_present, _table_checked_at
    if _table_present and time.monotonic() - _table_checked_at < _TABLE_CHECK_TTL_S:
        return True
    pool = await pg_client.get_pool()
    if pool is None:
        return False
    try:
        present = await pool.fetchval("SELECT to_regclass('public.osm_pois') IS NOT NULL")
    except Exception as exc:  # noqa: BLE001 — probe must not raise
        logger.info("osm_pois presence check failed: %s", exc)
        return False
    _table_present = bool(present)
    _table_checked_at = time.monotonic()
    return _table_present


async def osm_pois_nearby(
    lat: float,
    lon: float,
    radius_km: float,
    tag_kv: tuple[str, str | None] | None = None,
    limit: int = 20,
) -> list[BusinessEntity]:
    """POIs within ``radius_km`` of (lat, lon) from the local OSM mirror.

    ``tag_kv`` is a ``(key, value)`` pair from ``osm_tag_kv`` — ``value``
    of ``None`` filters on ``key IS NOT NULL``. Keys outside the imported
    tag columns return ``[]`` without touching the DB.
    """
    if radius_km <= 0:
        return []
    key, value = tag_kv or (None, None)
    if key is not None and key not in _TAG_COLUMNS:
        return []
    if not await _has_table():
        return []
    pool = await pg_client.get_pool()
    if pool is None:
        return []

    sql = _SQL
    params: list[Any] = [lat, lon, radius_km * 1000.0]
    if key is not None:
        if value is not None:
            sql += f" AND {key} = ${len(params) + 1}"
            params.append(value)
        else:
            sql += f" AND {key} IS NOT NULL"
    sql += f" ORDER BY distance_m LIMIT ${len(params) + 1}"
    params.append(max(1, limit))

    try:
        rows = await pool.fetch(sql, *params)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        logger.info("osm_pois_nearby failed: %s", exc)
        return []
    return [_row_to_entity(row) for row in rows]


def _row_to_entity(row: Any) -> BusinessEntity:
    addr = " ".join(
        p
        for p in (
            row["addr_housenumber"],
            row["addr_street"],
            row["addr_district"] or row["addr_city"],
        )
        if p
    )
    osm_type = _OSM_TYPE_PATH.get(str(row["osm_type"] or "").upper(), "node")
    return BusinessEntity(
        name=row["name"] or "",
        category=row["category"] or "",
        address=addr,
        phone=row["phone"],
        hours=row["opening_hours"],
        website=row["website"],
        lat=float(row["lat"]) if row["lat"] is not None else None,
        lon=float(row["lon"]) if row["lon"] is not None else None,
        description="",
        source_url=f"https://www.openstreetmap.org/{osm_type}/{row['osm_id']}",
    )


def _reset_cache() -> None:
    """Test hook — drop the cached presence flag."""
    global _table_present, _table_checked_at
    _table_present, _table_checked_at = False, 0.0
