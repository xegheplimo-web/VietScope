"""P16 field-level provenance + confidence resolution.

Each contributing source row votes for a canonical field. Winner weight:

    weight = 0.6 · authority(provider, field)          (source_policies)
           + 0.3 · recency(observed_at)                (e^-days/180)
           + 0.1 · corroboration                       (same-value share)

The winning value is written onto canonical_places; every candidate value
is kept in place_field_provenance with chosen flags — re-resolution is
deterministic and auditable, and a later better source displaces the
winner without losing history.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from resolution.match import NormSource

FIELDS = (
    "name",
    "address",
    "phone",
    "website",
    "category",
    "hours",
    "location",
    "status",
    # P2.0 rich-card fields — promoted verbatim from source raw_payload
    # (runner._rich_fields); they vote through the same weight machinery.
    "rating",
    "review_count",
    "price_level",
    "images",
)

# Fallback when a provider has no source_policies row.
_DEFAULT_AUTHORITY = {
    "name": 0.5,
    "address": 0.5,
    "phone": 0.5,
    "website": 0.5,
    "category": 0.5,
    "hours": 0.5,
    "location": 0.5,
    "status": 0.5,
    "rating": 0.5,
    "review_count": 0.5,
    "price_level": 0.5,
    "images": 0.5,
}

# source_policies.authority uses business-facing keys; map them to fields.
_AUTHORITY_KEY = {
    "name": "name",
    "address": "location",
    "phone": "phone",
    "website": "website",
    "category": "category",
    "hours": "opening_hours",
    "location": "location",
    "status": "legal_status",
    "rating": "rating",
    "review_count": "review_count",
    "price_level": "price_level",
    "images": "images",
}

# Per-field recency half-life in days: volatile fields decay fast,
# near-static ones keep authority for years.
_TAU_BY_FIELD = {
    "hours": 45.0,
    "status": 45.0,
    "phone": 180.0,
    "website": 180.0,
    "category": 365.0,
    "name": 730.0,
    "address": 730.0,
    "location": 1460.0,
}
_TAU_DAYS = 180.0  # fallback for fields not in _TAU_BY_FIELD


def recency(observed_at: Any, now: datetime | None = None, field: str | None = None) -> float:
    if observed_at is None:
        return 0.5
    now = now or datetime.now(UTC)
    try:
        days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    except TypeError:
        return 0.5
    return math.exp(-days / _TAU_BY_FIELD.get(field or "", _TAU_DAYS))


def field_signature(field: str, value: Any) -> Any:
    """Comparison signature for corroboration — same real-world value
    counts as the same regardless of provider formatting."""
    from resolution.normalize import (
        canonical_category,
        norm_address,
        norm_name,
        norm_phone,
        norm_status,
        website_domain,
    )

    if value is None:
        return None
    if field == "name":
        return norm_name(str(value))
    if field == "address":
        return norm_address(str(value))
    if field == "phone":
        return norm_phone(str(value)) or str(value).strip().lower()
    if field == "website":
        return website_domain(str(value)) or str(value).strip().lower()
    if field == "category":
        return canonical_category(str(value)) or str(value).strip().lower()
    if field == "status":
        return norm_status(str(value)) or str(value).strip().lower()
    if field == "location" and isinstance(value, dict):
        lat, lon = value.get("lat"), value.get("lon")
        if lat is None or lon is None:
            return None
        # ~100 m grid — two records agreeing within a cell corroborate
        return (round(float(lat), 3), round(float(lon), 3))
    if isinstance(value, dict):
        import json as _json

        return _json.dumps(value, sort_keys=True)
    return value


def field_weight(
    provider: str,
    fld: str,
    observed_at: Any,
    corroboration: float,
    policies: dict[str, dict],
    now: datetime | None = None,
) -> float:
    auth_map = (policies.get(provider) or {}).get("authority") or {}
    authority = float(auth_map.get(_AUTHORITY_KEY[fld], _DEFAULT_AUTHORITY[fld]))
    return round(
        0.6 * authority + 0.3 * recency(observed_at, now, field=fld) + 0.1 * corroboration,
        4,
    )


def resolve_fields(
    place_id: int,
    contributors: list[NormSource],
    policies: dict[str, dict],
    now: datetime | None = None,
) -> tuple[dict[str, Any], list]:
    """Pick the winning value per field; return (canonical_fields, prov_rows).

    contributors: ``NormSource``-shaped views (provider/record_id/
    observed_at/fields attributes). Canonical output keys map onto
    canonical_places columns.
    """
    from resolution.store import ProvRow

    prov: list[ProvRow] = []
    canonical: dict[str, Any] = {}
    for fld in FIELDS:
        cands = [c for c in contributors if c.fields.get(fld) is not None]
        if not cands:
            continue
        # corroboration = share of candidates with the same *signature* —
        # 'Công Ty ABC', 'CONG TY ABC' and 'cong ty abc' all agree
        sigs = [field_signature(fld, c.fields[fld]) for c in cands]
        rows = []
        for i, c in enumerate(cands):
            same = sum(1 for s in sigs if s == sigs[i])
            w = field_weight(
                c.provider,
                fld,
                c.observed_at,
                same / len(cands),
                policies,
                now,
            )
            rows.append((w, c))
        best_w, best = max(rows, key=lambda x: (x[0], -(x[1].record_id or 0)))
        for w, c in rows:
            prov.append(
                ProvRow(
                    place_id=place_id,
                    field=fld,
                    source_record_id=c.record_id,
                    provider=c.provider,
                    value=c.fields[fld],
                    weight=w,
                    observed_at=c.observed_at,
                    chosen=c is best,
                )
            )
        canonical[fld] = best.fields[fld]
    return canonical, prov


def map_canonical(fields: dict[str, Any]) -> dict[str, Any]:
    """Field names → canonical_places columns."""
    out: dict[str, Any] = {}
    if "name" in fields:
        out["canonical_name"] = fields["name"]
    if "address" in fields:
        out["address"] = fields["address"]
    if "phone" in fields:
        out["phone"] = fields["phone"]
    if "website" in fields:
        out["website"] = fields["website"]
    if "category" in fields:
        out["canonical_category"] = fields["category"]
    if "hours" in fields:
        out["opening_hours"] = fields["hours"]
    if "status" in fields:
        out["status"] = fields["status"]
    if "location" in fields and isinstance(fields["location"], dict):
        out["lat"] = fields["location"]["lat"]
        out["lon"] = fields["location"]["lon"]
    # P2.0 rich-card columns — pass-through of the voted source values.
    if "rating" in fields:
        out["rating"] = fields["rating"]
    if "review_count" in fields:
        out["review_count"] = fields["review_count"]
    if "price_level" in fields:
        out["price_level"] = fields["price_level"]
    if "images" in fields:
        images = fields["images"]
        out["images"] = images
        out["primary_image_url"] = images[0] if images else None
    return out


def confidence_from(prov_rows: list) -> float:
    """Place-level confidence = mean weight of chosen fields (0..1)."""
    chosen = [r.weight for r in prov_rows if r.chosen]
    return round(sum(chosen) / len(chosen), 4) if chosen else 0.0
