# ruff: noqa: S608 — SQL constants interpolate only the module-local
# ``_PLACE_COLS_SQL`` column list; all values are parameterized at call sites.
"""P17 read projection — canonical place rows → ``PlaceDocumentV1``.

The ONLY component allowed to read the canonical schema
(``canonical_places`` / ``place_sources`` / ``place_source_records`` /
``place_field_provenance``). It produces the frozen
``PlaceDocumentV1`` contract; everything downstream (OpenSearch indexer,
PostGIS lane, Redis caches, ``/v1/places/*``) consumes documents, never
raw rows.

Boundary rules honored here:

- No ``resolution/`` imports — the projection reads canonical tables
  directly; the resolver's normalization/matching is its own concern.
- ``freshness_score`` is derived at projection time (recency decay over
  ``last_seen``), matching the contract's documented semantics.
- ``aliases`` are the distinct ``raw_name`` variants across a place's
  linked source records, minus the canonical name itself.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from serving.places.document import PLACE_STATUSES, PlaceDocumentV1

# Recency half-life for the serving freshness score (days). Mirrors the
# resolver's neutral field tau — a place observed today scores ~1.0, one
# stale by ~180 days ~0.37, ancient data → ~0.
_FRESHNESS_TAU_DAYS = 180.0
_FRESHNESS_MISSING = 0.5

# Column list for the canonical scan — kept SQL-text-identical wherever the
# row feeds the document, so fixtures can route on the table name.
PLACE_COLS = (
    "place_id",
    "business_id",
    "canonical_name",
    "normalized_name",
    "canonical_category",
    "address",
    "phone",
    "website",
    "website_domain",
    "opening_hours",
    "lat",
    "lon",
    "admin_unit_id",
    "status",
    "confidence",
    "source_count",
    "last_seen",
    "updated_at",
    # P2.0 rich-card columns (migration 015)
    "rating",
    "review_count",
    "price_level",
    "primary_image_url",
    "images",
)

_PLACE_COLS_SQL = ", ".join(PLACE_COLS)

# Full-scan page for indexing / rebuilds — keyset-ordered by place_id.
SCAN_PAGE_SQL = f"""
SELECT {_PLACE_COLS_SQL}
FROM canonical_places
WHERE place_id > $1
ORDER BY place_id
LIMIT $2
"""

# Incremental delta scan — (updated_at, place_id) cursor from the ledger.
SCAN_DELTA_SQL = f"""
SELECT {_PLACE_COLS_SQL}
FROM canonical_places
WHERE updated_at > $1
   OR (updated_at = $1 AND place_id > $2)
ORDER BY updated_at, place_id
LIMIT $3
"""

# Alias lineage: distinct raw names across a place's source records.
ALIASES_SQL = """
SELECT s.place_id, r.raw_name
FROM place_sources s
JOIN place_source_records r ON r.id = s.source_record_id
WHERE s.place_id = ANY($1::bigint[])
"""

PLACE_BY_ID_SQL = f"""
SELECT {_PLACE_COLS_SQL}
FROM canonical_places
WHERE place_id = $1
"""

PLACE_IDS_PAGE_SQL = """
SELECT place_id FROM canonical_places
WHERE place_id > $1
ORDER BY place_id
LIMIT $2
"""

# Detail-endpoint lineage — verbatim contract of the P16 endpoint.
SOURCES_FOR_SQL = """
SELECT provider, external_id, source_record_id, linked_at
FROM place_sources WHERE place_id = $1
"""

PROVENANCE_FOR_SQL = """
SELECT field, provider, value, weight, observed_at, chosen
FROM place_field_provenance
WHERE place_id = $1 ORDER BY field, weight DESC
"""


def freshness_score(last_seen: datetime | None, now: datetime | None = None) -> float:
    """Projection-derived recency: exp decay on days since last_seen."""
    if last_seen is None:
        return _FRESHNESS_MISSING
    now = now or datetime.now(UTC)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    try:
        days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    except TypeError:
        return _FRESHNESS_MISSING
    return math.exp(-days / _FRESHNESS_TAU_DAYS)


def _jsonb_obj(v: Any) -> dict | None:
    """asyncpg returns jsonb as str unless a codec is registered. Rows
    written before P2.0.3 are double-encoded — a JSON string containing the
    object — so a str result earns one more parse (bounded at two layers)."""
    if not isinstance(v, str):
        return v
    for _ in range(2):
        try:
            v = json.loads(v)
        except ValueError:
            return None
        if not isinstance(v, str):
            break
    return v if isinstance(v, dict) else None


def _jsonb_list(v: Any) -> list:
    """jsonb (str or real list) → list[str]; NULL/malformed → []."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    if not isinstance(v, (list, tuple)):
        return []
    return [str(u) for u in v if isinstance(u, str)]


def _norm_aliases(
    raw_names: Iterable[str | None], canonical_name: str, normalized: str
) -> list[str]:
    """Distinct source-name variants ≠ the canonical name (folded compare)."""
    seen: dict[str, str] = {}
    canonical_keys = {canonical_name.strip().lower(), normalized.strip().lower()}
    for raw in raw_names:
        name = (raw or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in canonical_keys or key in seen:
            continue
        seen[key] = name
    return sorted(seen.values())


def project_row(
    row: dict[str, Any],
    *,
    alias_names: Iterable[str | None] = (),
    now: datetime | None = None,
) -> PlaceDocumentV1:
    """Map one ``canonical_places`` row (+ source name variants) → document."""
    status = row.get("status") or "unknown"
    if status not in PLACE_STATUSES:
        status = "unknown"
    phone = row.get("phone")
    images = _jsonb_list(row.get("images"))
    return PlaceDocumentV1(
        place_id=str(row["place_id"]),
        business_id=str(row["business_id"]) if row.get("business_id") is not None else None,
        name=row.get("canonical_name") or "",
        aliases=_norm_aliases(
            alias_names,
            row.get("canonical_name") or "",
            row.get("normalized_name") or "",
        ),
        normalized_name=row.get("normalized_name") or "",
        category_ids=[row["canonical_category"]] if row.get("canonical_category") else [],
        lat=row.get("lat"),
        lon=row.get("lon"),
        admin_unit_id=(str(row["admin_unit_id"]) if row.get("admin_unit_id") is not None else None),
        status=status,
        confidence=float(row.get("confidence") or 0.0),
        freshness_score=freshness_score(row.get("last_seen"), now),
        address=row.get("address"),
        phone=[phone] if phone else [],
        website=row.get("website"),
        website_domain=row.get("website_domain"),
        opening_hours=_jsonb_obj(row.get("opening_hours")),
        rating=float(row["rating"]) if row.get("rating") is not None else None,
        review_count=int(row["review_count"]) if row.get("review_count") is not None else None,
        price_level=row.get("price_level"),
        primary_image_url=row.get("primary_image_url") or (images[0] if images else None),
        images=images,
        source_count=int(row.get("source_count") or 0),
        last_verified_at=row.get("last_seen"),
    )


def project_rows(
    rows: list[dict[str, Any]],
    *,
    aliases_by_place: dict[int, list[str]] | None = None,
    now: datetime | None = None,
) -> list[PlaceDocumentV1]:
    return [
        project_row(r, alias_names=(aliases_by_place or {}).get(r["place_id"], ()), now=now)
        for r in rows
    ]


def doc_to_index_source(doc: PlaceDocumentV1) -> dict[str, Any]:
    """Document → OpenSearch ``_source`` (adds the geo_point field)."""
    data = doc.to_dict()
    if doc.lat is not None and doc.lon is not None:
        data["location"] = {"lat": doc.lat, "lon": doc.lon}
    else:
        data["location"] = None
    return data


def doc_from_index_source(src: dict[str, Any]) -> PlaceDocumentV1:
    """OpenSearch ``_source`` → document (tolerates the extra geo field)."""
    src = dict(src)
    src.pop("location", None)
    src.pop("document_version", None)
    lv = src.get("last_verified_at")
    if isinstance(lv, str):
        try:
            src["last_verified_at"] = datetime.fromisoformat(lv)
        except ValueError:
            src["last_verified_at"] = None
    return PlaceDocumentV1(**{k: v for k, v in src.items() if k in _DOC_FIELDS})


_DOC_FIELDS = frozenset(PlaceDocumentV1.__dataclass_fields__)
