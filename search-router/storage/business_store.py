"""PostGIS-backed local business store.

Canonical DB contract (Phase 1/T2): hub-postgres access goes through
``storage.pg_client`` — the shared asyncpg pool fed by ``HUB_DATABASE_URL``.
When the DSN is unset or the DB is unreachable the store degrades:
``search_nearby`` returns ``[]`` and ``upsert`` returns ``False``.

The ``businesses`` table is owned by ``db/migrations/003_businesses.sql``
(applied by the router at startup via ``db.migrate``) — there is no
app-side schema bootstrap here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from models import BusinessEntity

from storage import pg_client

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO businesses (
    id, name, category, address, phone, hours, rating, price_level,
    website, lat, lon, admin_unit_id, location, description, source_url
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $11, $14,
    ST_SetSRID(ST_MakePoint($11::float8, $10::float8), 4326),
    $12, $13
)
ON CONFLICT (lat, lon, name) DO UPDATE SET
    category = EXCLUDED.category,
    address = EXCLUDED.address,
    phone = EXCLUDED.phone,
    hours = EXCLUDED.hours,
    rating = EXCLUDED.rating,
    price_level = EXCLUDED.price_level,
    website = EXCLUDED.website,
    admin_unit_id = EXCLUDED.admin_unit_id,
    location = EXCLUDED.location,
    description = EXCLUDED.description,
    source_url = EXCLUDED.source_url,
    updated_at = NOW()
"""

_SEARCH_NEARBY_SQL = """
SELECT
    id, name, category, address, phone, hours, rating,
    price_level, website, lat, lon, admin_unit_id, description, source_url,
    ST_Distance(
        location::geography,
        ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
    ) AS distance_m
FROM businesses
WHERE ST_DWithin(
    location::geography,
    ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
    $3
)
"""


class BusinessStore:
    """Postgres + PostGIS store for local business entities."""

    def __init__(self) -> None:
        self.dsn = pg_client.database_url()
        self._available = bool(self.dsn)

    async def upsert(self, entity: BusinessEntity) -> bool:
        """Insert or update a business entity."""
        pool = await self._pool()
        if pool is None:
            return False
        try:
            await pool.execute(_UPSERT_SQL, *self._entity_to_params(entity))
            return True
        except Exception as exc:
            logger.warning("Business upsert failed: %s", exc)
            return False

    async def search_nearby(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        category: str | None = None,
        limit: int = 20,
    ) -> list[BusinessEntity]:
        """Search businesses within ``radius_km`` of (lat, lon).

        Returns an empty list when the store is unconfigured or unavailable.
        """
        if radius_km <= 0:
            return []
        pool = await self._pool()
        if pool is None:
            return []

        sql = _SEARCH_NEARBY_SQL
        params: list[Any] = [lat, lon, radius_km * 1000.0]
        if category:
            sql += " AND category ILIKE $4"
            params.append(f"%{category}%")
        sql += f" ORDER BY distance_m ASC LIMIT ${len(params) + 1}"
        params.append(max(1, limit))

        try:
            rows = await pool.fetch(sql, *params)
            return [self._row_to_entity(row) for row in rows]
        except Exception as exc:
            logger.warning("search_nearby failed: %s", exc)
            return []

    async def _pool(self) -> Any | None:
        if not self._available:
            return None
        return await pg_client.get_pool()

    @staticmethod
    def _entity_to_params(entity: BusinessEntity) -> tuple[Any, ...]:
        return (
            uuid.uuid4(),
            entity.name,
            entity.category or None,
            entity.address or None,
            entity.phone,
            entity.hours,
            entity.rating,
            entity.price_level,
            entity.website,
            entity.lat,
            entity.lon,
            entity.description or None,
            entity.source_url or None,
            entity.admin_unit_id,
        )

    @staticmethod
    def _row_to_entity(row) -> BusinessEntity:
        return BusinessEntity(
            name=row["name"] or "",
            category=row["category"] or "",
            address=row["address"] or "",
            phone=row["phone"],
            hours=row["hours"],
            rating=float(row["rating"]) if row["rating"] is not None else None,
            price_level=row["price_level"],
            website=row["website"],
            lat=float(row["lat"]) if row["lat"] is not None else None,
            lon=float(row["lon"]) if row["lon"] is not None else None,
            admin_unit_id=(int(row["admin_unit_id"]) if row["admin_unit_id"] is not None else None),
            description=row["description"] or "",
            source_url=row["source_url"] or "",
        )
