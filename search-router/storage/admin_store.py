"""Versioned administrative gazetteer store (P14A).

The canonical dataset lives in ``administrative_units`` /
``administrative_aliases`` / ``administrative_relations`` (migration 006),
seeded from ``db/seeds/vn_admin_units.json`` — the temporal administrative
graph covering the pre-2025 three-level geography (province → district →
commune), the December-2024 dissolution round, and the current post-2025
two-level geography (province → commune, effective 2025-07-01).

Two stores share one contract:

* ``PgAdminStore`` — reads the seeded Postgres tables via ``pg_client``;
  degrades to ``None`` graphs / empty results when the DB is unconfigured.
* ``DictAdminStore`` — the same graph held in memory, built from the
  bundled seed file; the offline path the resolver uses when no database
  is configured, and the fixture unit tests run against.

Unit identity inside a graph is the seed ``key`` (``"new:79"`` /
``"old:221"``); Postgres rows additionally carry their surrogate
``unit_id``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storage import pg_client

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parents[1] / "db" / "seeds" / "vn_admin_units.json"

UNITS_SQL = """
SELECT unit_id, code, name, normalized_name, type, admin_level,
       parent_id, valid_from, valid_to, status, source
FROM administrative_units
"""

ALIASES_SQL = """
SELECT unit_id, alias, normalized_alias, alias_type, valid_from, valid_to
FROM administrative_aliases
"""

RELATIONS_SQL = """
SELECT from_unit_id, to_unit_id, relation_type, effective_date, source
FROM administrative_relations
"""

POINT_SQL = """
SELECT unit_id, code, name, normalized_name, type, admin_level,
       parent_id, valid_from, valid_to, status, source
FROM administrative_units
WHERE geometry IS NOT NULL
  AND status = 'current'
  AND ST_Contains(
        geometry,
        ST_SetSRID(ST_MakePoint($2, $1), 4326)
  )
ORDER BY admin_level DESC
"""


@dataclass(frozen=True)
class AdminUnit:
    key: str  # logical id: "new:79" | "old:221"
    unit_id: int | None  # surrogate PK when backed by Postgres
    code: str  # official administrative code (per era)
    name: str  # display name ("Huyện Yên Dũng")
    normalized_name: str  # folded, type-stripped ("yen dung")
    type: str  # tinh|thanh_pho|quan|huyen|thi_xa|phuong|xa|...
    admin_level: int  # 1 province | 2 district | 3 commune
    parent_key: str | None
    valid_from: str | None
    valid_to: str | None
    status: str  # current | historical | proposed
    source: str | None = None
    geometry: dict | None = None  # GeoJSON MultiPolygon (current era only)


@dataclass(frozen=True)
class AdminAlias:
    unit_key: str
    alias: str
    normalized_alias: str
    alias_type: str  # official|historical|abbreviation|alternate|english
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass(frozen=True)
class AdminRelation:
    from_key: str
    to_key: str
    relation_type: str  # renamed_to|merged_into|split_into|replaced_by|boundary_changed
    effective_date: str | None
    source: str | None = None


@dataclass
class AdminGraph:
    """The whole gazetteer in memory — small enough (~15k units)."""

    units: dict[str, AdminUnit] = field(default_factory=dict)
    aliases: list[AdminAlias] = field(default_factory=list)
    relations: list[AdminRelation] = field(default_factory=list)
    # populated by resolver at index time
    children: dict[str | None, list[str]] = field(default_factory=dict)


def load_seed(path: Path | None = None) -> dict:
    """Read the bundled canonical seed (``db/seeds/vn_admin_units.json``)."""
    return json.loads((path or SEED_PATH).read_text(encoding="utf-8"))


def graph_from_seed(seed: dict) -> AdminGraph:
    """Build an ``AdminGraph`` from the seed JSON document."""
    graph = AdminGraph()
    for u in seed.get("units", []):
        graph.units[u["key"]] = AdminUnit(
            key=u["key"],
            unit_id=None,
            code=u["code"],
            name=u["name"],
            normalized_name=u.get("normalized_name") or "",
            type=u["type"],
            admin_level=u["admin_level"],
            parent_key=u.get("parent_key"),
            valid_from=u.get("valid_from"),
            valid_to=u.get("valid_to"),
            status=u.get("status", "current"),
            source=u.get("source"),
            geometry=u.get("geometry"),
        )
    for a in seed.get("aliases", []):
        graph.aliases.append(
            AdminAlias(
                unit_key=a["unit_key"],
                alias=a["alias"],
                normalized_alias=a.get("normalized_alias") or "",
                alias_type=a.get("alias_type", "alternate"),
                valid_from=a.get("valid_from"),
                valid_to=a.get("valid_to"),
            )
        )
    for r in seed.get("relations", []):
        graph.relations.append(
            AdminRelation(
                from_key=r["from_key"],
                to_key=r["to_key"],
                relation_type=r["relation_type"],
                effective_date=r.get("effective_date"),
                source=r.get("source"),
            )
        )
    for key, u in graph.units.items():
        graph.children.setdefault(u.parent_key, []).append(key)
    return graph


def _row_key(row: dict) -> str:
    """Reconstruct a unit's seed key from its row (era encoded in status)."""
    era = "new" if row["status"] == "current" else "old"
    return f"{era}:{row['code']}"


def graph_from_rows(
    unit_rows: list[dict],
    alias_rows: list[dict],
    relation_rows: list[dict],
) -> AdminGraph:
    graph = AdminGraph()
    id_to_key: dict[int, str] = {}
    for r in unit_rows:
        key = _row_key(r)
        id_to_key[r["unit_id"]] = key
        graph.units[key] = AdminUnit(
            key=key,
            unit_id=r["unit_id"],
            code=r["code"],
            name=r["name"],
            normalized_name=r["normalized_name"] or "",
            type=r["type"],
            admin_level=r["admin_level"],
            parent_key=None,  # resolved below
            valid_from=str(r["valid_from"]) if r["valid_from"] else None,
            valid_to=str(r["valid_to"]) if r["valid_to"] else None,
            status=r["status"],
            source=r["source"],
        )
    # parent ids → keys
    for r in unit_rows:
        if r["parent_id"] is None:
            continue
        key = _row_key(r)
        pk = id_to_key.get(r["parent_id"])
        u = graph.units[key]
        graph.units[key] = AdminUnit(**{**u.__dict__, "parent_key": pk})
    for a in alias_rows:
        key = id_to_key.get(a["unit_id"])
        if key:
            graph.aliases.append(
                AdminAlias(
                    unit_key=key,
                    alias=a["alias"],
                    normalized_alias=a["normalized_alias"] or "",
                    alias_type=a["alias_type"],
                    valid_from=str(a["valid_from"]) if a["valid_from"] else None,
                    valid_to=str(a["valid_to"]) if a["valid_to"] else None,
                )
            )
    for r in relation_rows:
        fk, tk = id_to_key.get(r["from_unit_id"]), id_to_key.get(r["to_unit_id"])
        if fk and tk:
            graph.relations.append(
                AdminRelation(
                    from_key=fk,
                    to_key=tk,
                    relation_type=r["relation_type"],
                    effective_date=str(r["effective_date"]) if r["effective_date"] else None,
                    source=r["source"],
                )
            )
    for key, u in graph.units.items():
        graph.children.setdefault(u.parent_key, []).append(key)
    return graph


def _geojson_bounds(geom: dict) -> tuple[float, float, float, float] | None:
    """(minx, miny, maxx, maxy) over a GeoJSON Polygon/MultiPolygon."""
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    stack = [geom.get("coordinates") or []]
    while stack:
        c = stack.pop()
        if not isinstance(c, (list, tuple)) or not c:
            continue
        if isinstance(c[0], (int, float)):
            if c[0] < minx:
                minx = c[0]
            if c[1] < miny:
                miny = c[1]
            if c[0] > maxx:
                maxx = c[0]
            if c[1] > maxy:
                maxy = c[1]
        else:
            stack.extend(c)
    return None if minx == float("inf") else (minx, miny, maxx, maxy)


class DictAdminStore:
    """In-memory gazetteer — tests and the no-DB fallback."""

    def __init__(self, graph: AdminGraph | None = None):
        self._graph = graph or AdminGraph()
        self._geom_idx: list[tuple[tuple[float, float, float, float], dict, AdminUnit]] | None = (
            None
        )

    @classmethod
    def from_seed(cls, path: Path | None = None) -> DictAdminStore:
        return cls(graph_from_seed(load_seed(path)))

    async def graph(self) -> AdminGraph:
        return self._graph

    def _geom_index(self) -> list[tuple[tuple[float, float, float, float], dict, AdminUnit]]:
        """Per-unit (bbox, geometry, unit) list, built once — the bbox
        pre-filter keeps point lookup O(candidates) instead of ray-casting
        every polygon in the gazetteer."""
        if self._geom_idx is None:
            idx = []
            for u in self._graph.units.values():
                if u.geometry is None or u.status != "current":
                    continue
                b = _geojson_bounds(u.geometry)
                if b is not None:
                    idx.append((b, u.geometry, u))
            self._geom_idx = idx
        return self._geom_idx

    async def units_containing(self, lat: float, lon: float) -> list[AdminUnit]:
        """Point lookup on GeoJSON geometries (no PostGIS dependency)."""
        hits = [
            u
            for (x0, y0, x1, y1), geom, u in self._geom_index()
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _point_in_geojson(lon, lat, geom)
        ]
        hits.sort(key=lambda u: u.admin_level, reverse=True)
        return hits


def _point_in_geojson(lon: float, lat: float, geom: dict) -> bool:
    """Ray-cast point-in-polygon over a GeoJSON Polygon/MultiPolygon."""
    polys = (
        [geom["coordinates"]]
        if geom.get("type") == "Polygon"
        else geom.get("coordinates", [])
        if geom.get("type") == "MultiPolygon"
        else []
    )
    return any(
        _ring_contains(lon, lat, poly[0]) and not any(_ring_contains(lon, lat, h) for h in poly[1:])
        for poly in polys
    )


def _ring_contains(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class PgAdminStore:
    """Postgres gazetteer; degrades when hub-postgres is unconfigured."""

    def __init__(self) -> None:
        self._available = bool(pg_client.database_url())

    async def _pool(self) -> Any | None:
        if not self._available:
            return None
        return await pg_client.get_pool()

    async def graph(self) -> AdminGraph | None:
        try:
            pool = await self._pool()
            if pool is None:
                return None
            units = [dict(r) for r in await pool.fetch(UNITS_SQL)]
            aliases = [dict(r) for r in await pool.fetch(ALIASES_SQL)]
            relations = [dict(r) for r in await pool.fetch(RELATIONS_SQL)]
        except Exception as exc:
            logger.warning("admin graph load failed: %s", exc)
            return None
        if not units:
            return None
        return graph_from_rows(units, aliases, relations)

    async def units_containing(self, lat: float, lon: float) -> list[AdminUnit]:
        """Point → current commune/province via PostGIS ST_Contains."""
        try:
            pool = await self._pool()
            if pool is None:
                return []
            rows = await pool.fetch(POINT_SQL, lat, lon)
        except Exception as exc:
            logger.warning("admin point lookup failed: %s", exc)
            return []
        id_to_key: dict[int, str] = {}
        out = []
        for r in rows:
            d = dict(r)
            key = _row_key(d)
            id_to_key[d["unit_id"]] = key
            out.append(
                AdminUnit(
                    key=key,
                    unit_id=d["unit_id"],
                    code=d["code"],
                    name=d["name"],
                    normalized_name=d["normalized_name"] or "",
                    type=d["type"],
                    admin_level=d["admin_level"],
                    parent_key=None,
                    valid_from=str(d["valid_from"]) if d["valid_from"] else None,
                    valid_to=str(d["valid_to"]) if d["valid_to"] else None,
                    status=d["status"],
                    source=d["source"],
                )
            )
        return out
