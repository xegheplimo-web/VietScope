# ruff: noqa: S608 — this module is a parameterized-SQL builder; every value
# travels via ``args`` ($n placeholders), only static clause text is joined.
"""P17 PostGIS lane — geo-precision retrieval straight from canonical truth.

Used three ways by the serving layer:

1. **Candidate lane** when OpenSearch is down/empty or the query needs a
   polygon containment it cannot express (``admin_contains``).
2. **Distance oracle** — ``ST_DistanceSphere`` over candidate ids so the
   ranker works with precise meters, not index-side approximations.
3. **Fallback text lane** — folded ``normalized_name`` LIKE matching keeps
   ordinary local search answering while the index is rebuilt.

All queries are plain parameterized SQL against ``canonical_places`` (+ the
``administrative_units`` geometry for containment). The lane reads the same
columns the projection owns; it never writes and never mutates canonical
state. GIST support comes from ``idx_cplaces_location`` (migration 010).
"""

from __future__ import annotations

from typing import Any

from serving.places.projection import _PLACE_COLS_SQL
from serving.places.query import LocalQuerySpec

# Distance over candidates already retrieved by the OpenSearch lane.
DISTANCE_SQL = """
SELECT place_id,
       ST_DistanceSphere(location, ST_SetSRID(ST_MakePoint($2, $3), 4326)) AS distance_m
FROM canonical_places
WHERE place_id = ANY($1::bigint[]) AND location IS NOT NULL
"""


def _filters(spec: LocalQuerySpec, args: list[Any], *, geo: bool) -> list[str]:
    """Shared WHERE clauses for the PostGIS candidate lane.

    ``$1/$2`` are reserved for lon/lat when ``geo`` is true; every other
    clause appends to ``args`` in order.
    """
    clauses: list[str] = []
    if spec.statuses:
        args.append(sorted(spec.statuses))
        clauses.append(f"status = ANY(${len(args)})")
    if spec.category:
        args.append(spec.category)
        clauses.append(f"canonical_category = ${len(args)}")
    # P17.1 rich filters — expressible as plain predicates on canonical
    # columns; open_now stays request-time (computed from opening_hours).
    if spec.min_rating is not None:
        args.append(spec.min_rating)
        clauses.append(f"rating >= ${len(args)}")
    if spec.price_level:
        args.append(spec.price_level)
        clauses.append(f"price_level = ${len(args)}")
    if spec.admin_unit_id is not None and spec.admin_contains:
        # Polygon containment over the unit's geometry — catches places whose
        # admin_unit_id was never assigned at resolution time.
        args.append(spec.admin_unit_id)
        clauses.append(
            "ST_Contains("
            f"(SELECT geometry FROM administrative_units"
            f"  WHERE unit_id = ${len(args)} AND geometry IS NOT NULL"
            f"  LIMIT 1),"
            " location)"
        )
    elif spec.admin_unit_id is not None:
        args.append(spec.admin_unit_id)
        clauses.append(f"admin_unit_id = ${len(args)}")
    if spec.bbox:
        min_lon, min_lat, max_lon, max_lat = spec.bbox
        args.extend([min_lon, min_lat, max_lon, max_lat])
        i = len(args) - 3
        clauses.append(f"location && ST_MakeEnvelope(${i}, ${i + 1}, ${i + 2}, ${i + 3}, 4326)")
    if spec.tokens:
        # AND of folded token LIKEs — order-independent match on the
        # normalized (accent-folded) name column.
        for tok in spec.tokens:
            args.append(tok)
            clauses.append(f"normalized_name LIKE '%' || ${len(args)} || '%'")
    return clauses


def _candidate_sql(spec: LocalQuerySpec, args: list[Any]) -> str:
    if spec.has_geo:
        args.extend([spec.lon, spec.lat, spec.radius_m])
        geo_pred = (
            "location IS NOT NULL AND ST_DWithin(location::geography,"
            " ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, $3)"
        )
        where = [geo_pred, *_filters(spec, args, geo=True)]
        # P17.1 — over-fetch when open_now is set: the serve-time post-filter
        # drops closed-now candidates, so request extra candidates to avoid
        # starving the downstream limit. The service also adjusts spec.limit,
        # but we guard here so direct callers are correct too.
        db_limit = max(spec.limit, 100) if spec.open_now is not None else spec.limit
        args.append(db_limit)
        # P17.1 — sort-aware ordering in the geo branch: rating/popularity
        # override the default distance ordering so the right candidates
        # make the cut before LIMIT, not just after.
        order = "location <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)"
        if spec.sort == "rating":
            order = "rating DESC NULLS LAST, " + order
        elif spec.sort == "popularity":
            order = "review_count DESC NULLS LAST, " + order
        return f"""
SELECT {_PLACE_COLS_SQL},
       ST_DistanceSphere(location, ST_SetSRID(ST_MakePoint($1, $2), 4326)) AS distance_m
FROM canonical_places
WHERE {" AND ".join(where)}
ORDER BY {order}
LIMIT ${len(args)}
"""
    where = _filters(spec, args, geo=False)
    args.append(spec.limit)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    # P17.1 sort — the LIMIT happens in SQL, so the ordering must pick
    # the right candidates, not just re-rank them downstream.
    order = "confidence DESC, source_count DESC, place_id"
    if spec.sort == "rating":
        order = "rating DESC NULLS LAST, " + order
    elif spec.sort == "popularity":
        order = "review_count DESC NULLS LAST, " + order
    return f"""
SELECT {_PLACE_COLS_SQL}, NULL::float8 AS distance_m
FROM canonical_places
{where_sql}
ORDER BY {order}
LIMIT ${len(args)}
"""


async def search_candidates(pool: Any, spec: LocalQuerySpec) -> list[dict[str, Any]]:
    """Canonical-side candidate retrieval (fallback/primary geo lane)."""
    args: list[Any] = []
    sql = _candidate_sql(spec, args)
    rows = await pool.fetch(sql, *args)
    return [dict(r) for r in rows]


async def distances(pool: Any, place_ids: list[int], lat: float, lon: float) -> dict[int, float]:
    """Precise meters from (lat, lon) for each candidate id."""
    if not place_ids:
        return {}
    rows = await pool.fetch(DISTANCE_SQL, [int(p) for p in place_ids], lon, lat)
    out: dict[int, float] = {}
    for r in rows:
        d = dict(r)
        if d.get("distance_m") is not None and d.get("place_id") is not None:
            out[int(d["place_id"])] = float(d["distance_m"])
    return out
