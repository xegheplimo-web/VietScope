"""P16 canonical store — canonical_places / place_sources / provenance.

Pg-backed in production, dict-backed for tests and degraded operation
(same pattern as the P14 admin store). Resolution logic never branches
on the backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from resolution.match import RESOLVER_VERSION, NormSource, PlaceNorm, haversine_m

_FIELDS = ("name", "address", "phone", "website", "category", "hours", "location")


@dataclass
class PlaceRow:
    """A canonical place as the resolver sees it (normalized fields)."""

    place_id: int
    business_id: int | None
    canonical_name: str
    normalized_name: str
    canonical_category: str | None
    address: str | None
    normalized_address: str | None
    phone: str | None
    website: str | None
    opening_hours: dict | None
    lat: float | None
    lon: float | None
    admin_unit_id: int | None
    status: str
    confidence: float
    source_count: int
    tokens: frozenset[str] = field(default_factory=frozenset)
    domain: str | None = None
    resolution_run_id: int | None = None
    suspicious_merge: bool = False
    suspicious_reason: str | None = None
    # P2.0 rich-card fields — promoted from source raw_payload, NULL when
    # no contributing record reports them.
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    primary_image_url: str | None = None
    images: list[str] | None = None

    def as_norm(self) -> PlaceNorm:
        return PlaceNorm(
            place_id=self.place_id,
            norm_name=self.normalized_name,
            tokens=self.tokens,
            phone=self.phone,
            domain=self.domain,
            category=self.canonical_category,
            lat=self.lat,
            lon=self.lon,
            admin_unit_id=self.admin_unit_id,
        )


@dataclass
class ProvRow:
    place_id: int
    field: str
    source_record_id: int
    provider: str
    value: Any
    weight: float
    observed_at: Any
    chosen: bool = False


class CanonicalStore(Protocol):
    async def candidates(self, src: NormSource) -> list[PlaceRow]: ...
    async def find_place_by_source(self, provider: str, external_id: str) -> int | None: ...
    async def find_place_by_record(self, record_id: int) -> int | None: ...
    async def find_business_by_name(
        self,
        normalized_name: str,
        *,
        category: str | None = None,
        phone: str | None = None,
        website_domain: str | None = None,
        admin_unit_id: int | None = None,
    ) -> int | None: ...
    async def create_business(self, name: str, normalized_name: str) -> int: ...
    async def create_place(self, row: dict[str, Any]) -> int: ...
    async def update_place(self, place_id: int, fields: dict[str, Any]) -> None: ...
    async def link_source(
        self,
        place_id: int,
        record_id: int,
        provider: str,
        external_id: str | None,
        run_id: int,
        *,
        score: float | None = None,
        reason: str | None = None,
    ) -> bool: ...
    async def sources_for(self, place_id: int) -> list[dict[str, Any]]: ...
    async def write_provenance(self, rows: list[ProvRow]) -> None: ...
    async def audit_merges(
        self, run_id: int, geo_veto_m: float = 200.0, max_sources: int = 8
    ) -> dict[str, Any]: ...
    async def get_place(self, place_id: int) -> PlaceRow | None: ...
    async def search_places(
        self,
        q: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 2000.0,
        admin_unit_id: int | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> list[PlaceRow]: ...


# ── dict store (tests + degrade) ───────────────────────────────────────────


class DictCanonicalStore:
    def __init__(self) -> None:
        self.places: dict[int, PlaceRow] = {}
        self.businesses: dict[int, dict] = {}
        self.links: dict[int, set[int]] = {}  # place_id → source_record_ids
        self.sources: dict[int, dict[str, Any]] = {}  # record_id → staged row
        self.ext_index: dict[tuple[str, str], int] = {}  # (provider, ext) → place_id
        self.record_place: dict[int, int] = {}  # source_record_id → place_id
        self.link_meta: dict[int, dict[str, Any]] = {}  # record_id → decision metadata
        self.provenance: list[ProvRow] = []
        self._pid = 0
        self._bid = 0

    def seed_source(self, record_id: int, row: dict[str, Any]) -> None:
        self.sources[record_id] = dict(row)

    async def candidates(self, src: NormSource) -> list[PlaceRow]:
        out: list[PlaceRow] = []
        for p in self.places.values():
            same_admin = src.admin_unit_id and p.admin_unit_id == src.admin_unit_id
            near = (
                src.lat is not None
                and src.lon is not None
                and p.lat is not None
                and p.lon is not None
                and haversine_m(src.lat, src.lon, p.lat, p.lon) <= 500.0
            )
            if (
                (src.phone and p.phone == src.phone)
                or (src.domain and p.domain == src.domain)
                or ((same_admin or near) and src.tokens & p.tokens)
                or (same_admin and src.name_sig == " ".join(sorted(p.tokens)))
            ):
                out.append(p)
        return out

    async def find_place_by_source(self, provider: str, external_id: str) -> int | None:
        return self.ext_index.get((provider, external_id))

    async def find_place_by_record(self, record_id: int) -> int | None:
        return self.record_place.get(record_id)

    async def find_business_by_name(
        self,
        normalized_name: str,
        *,
        category: str | None = None,
        phone: str | None = None,
        website_domain: str | None = None,
        admin_unit_id: int | None = None,
    ) -> int | None:
        for bid, b in self.businesses.items():
            if b["normalized_name"] != normalized_name:
                continue
            # brand reuse needs evidence beyond the name: an existing place
            # of this business sharing category, phone, domain, or commune
            for p in self.places.values():
                if p.business_id != bid:
                    continue
                if (
                    (category and p.canonical_category == category)
                    or (phone and p.phone == phone)
                    or (website_domain and p.domain == website_domain)
                    or (admin_unit_id and p.admin_unit_id == admin_unit_id)
                ):
                    return bid
        return None

    async def create_business(self, name: str, normalized_name: str) -> int:
        self._bid += 1
        self.businesses[self._bid] = {
            "display_name": name,
            "normalized_name": normalized_name,
        }
        return self._bid

    async def create_place(self, row: dict[str, Any]) -> int:
        from resolution.normalize import name_tokens

        self._pid += 1
        row.setdefault("tokens", name_tokens(row.get("normalized_name") or ""))
        row.setdefault("domain", urlparse_host(row.get("website")))
        self.places[self._pid] = PlaceRow(place_id=self._pid, **row)
        return self._pid

    async def update_place(self, place_id: int, fields: dict[str, Any]) -> None:
        from resolution.normalize import name_tokens

        p = self.places[place_id]
        if "normalized_name" in fields:
            object.__setattr__(p, "tokens", name_tokens(fields["normalized_name"] or ""))
        if "website" in fields:
            p.domain = urlparse_host(fields["website"])
        for k, v in fields.items():
            if hasattr(p, k) and k != "tokens":
                setattr(p, k, v)

    async def link_source(
        self,
        place_id: int,
        record_id: int,
        provider: str,
        external_id: str | None,
        run_id: int,
        *,
        score: float | None = None,
        reason: str | None = None,
    ) -> bool:
        existing = self.record_place.get(record_id)
        if existing is not None:
            return existing == place_id
        self.links.setdefault(place_id, set()).add(record_id)
        self.record_place[record_id] = place_id
        self.link_meta[record_id] = {
            "matcher_version": RESOLVER_VERSION,
            "resolution_score": score,
            "resolution_reason": reason,
            "resolution_run_id": run_id,
        }
        if external_id:
            self.ext_index[(provider, external_id)] = place_id
        return True

    async def audit_merges(
        self, run_id: int, geo_veto_m: float = 200.0, max_sources: int = 8
    ) -> dict[str, Any]:
        """Same checks as the pg audit: linked-record geo span and
        excessive source count, scoped to places touched by this run."""
        flagged: list[dict[str, Any]] = []
        for p in self.places.values():
            if p.resolution_run_id != run_id:
                continue
            reasons = []
            recs = [self.sources[r] for r in self.links.get(p.place_id, ()) if r in self.sources]
            coords = [
                (r["lat"], r["lon"])
                for r in recs
                if r.get("lat") is not None and r.get("lon") is not None
            ]
            max_d = 0.0
            for i, a in enumerate(coords):
                for b in coords[i + 1 :]:
                    max_d = max(max_d, haversine_m(a[0], a[1], b[0], b[1]))
            if max_d > geo_veto_m:
                reasons.append(f"geo_span:{max_d:.0f}m")
            if len(recs) > max_sources:
                reasons.append(f"source_count:{len(recs)}")
            p.suspicious_merge = bool(reasons)
            p.suspicious_reason = "; ".join(reasons) or None
            if reasons:
                flagged.append({"place_id": p.place_id, "reason": p.suspicious_reason})
        return {
            "suspicious": len(flagged),
            "checks": {"geo_veto_m": geo_veto_m, "max_sources": max_sources},
            "places": flagged[:50],
        }

    async def sources_for(self, place_id: int) -> list[dict[str, Any]]:
        return [self.sources[r] for r in self.links.get(place_id, set()) if r in self.sources]

    async def write_provenance(self, rows: list[ProvRow]) -> None:
        for r in rows:
            for old in self.provenance:
                if (
                    old.place_id == r.place_id
                    and old.field == r.field
                    and old.source_record_id == r.source_record_id
                ):
                    old.weight, old.observed_at, old.value = r.weight, r.observed_at, r.value
                    old.chosen = r.chosen
                    break
            else:
                self.provenance.append(r)
            if r.chosen:
                for old in self.provenance:
                    if old is not r and old.place_id == r.place_id and old.field == r.field:
                        old.chosen = False

    async def get_place(self, place_id: int) -> PlaceRow | None:
        return self.places.get(place_id)

    async def search_places(
        self,
        q=None,
        lat=None,
        lon=None,
        radius_m=2000.0,
        admin_unit_id=None,
        category=None,
        limit=20,
    ) -> list[PlaceRow]:
        from resolution.normalize import norm_name

        qn = norm_name(q) if q else ""
        out = []
        for p in self.places.values():
            if admin_unit_id and p.admin_unit_id != admin_unit_id:
                continue
            if category and p.canonical_category != category:
                continue
            if qn and not (set(qn.split()) & p.tokens):
                continue
            if (
                lat is not None
                and lon is not None
                and p.lat is not None
                and p.lon is not None
                and haversine_m(lat, lon, p.lat, p.lon) > radius_m
            ):
                continue
            out.append(p)
        return out[:limit]


# ── pg store ───────────────────────────────────────────────────────────────

_PLACE_COLS = (
    "place_id",
    "business_id",
    "canonical_name",
    "normalized_name",
    "canonical_category",
    "address",
    "normalized_address",
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
    "suspicious_merge",
    "suspicious_reason",
    "rating",
    "review_count",
    "price_level",
    "primary_image_url",
    "images",
)


class PgCanonicalStore:
    def __init__(self, pool: Any):
        self._pool = pool

    @staticmethod
    def _row(r) -> PlaceRow:
        from resolution.normalize import name_tokens

        website = r["website"]
        domain = r["website_domain"] or (urlparse_host(website) if website else None)
        norm = r["normalized_name"] or ""
        return PlaceRow(
            place_id=r["place_id"],
            business_id=r["business_id"],
            canonical_name=r["canonical_name"],
            normalized_name=norm,
            canonical_category=r["canonical_category"],
            address=r["address"],
            normalized_address=r["normalized_address"],
            phone=r["phone"],
            website=website,
            opening_hours=r["opening_hours"],
            lat=r["lat"],
            lon=r["lon"],
            admin_unit_id=r["admin_unit_id"],
            status=r["status"],
            confidence=r["confidence"],
            source_count=r["source_count"],
            tokens=name_tokens(norm),
            domain=domain,
            suspicious_merge=bool(r["suspicious_merge"]),
            suspicious_reason=r["suspicious_reason"],
            rating=r["rating"],
            review_count=r["review_count"],
            price_level=r["price_level"],
            primary_image_url=r["primary_image_url"],
            images=_jsonb_list(r["images"]),
        )

    async def candidates(self, src: NormSource) -> list[PlaceRow]:
        """Blocking: same admin unit sharing a name token, same phone,
        same domain, or inside a ~500 m envelope. Exact-key matches and
        nearest candidates rank first so the LIMIT can't evict them."""
        clauses: list[str] = []
        args: list[Any] = []
        phone_i = domain_i = geo_i = None
        if src.phone:
            args.append(src.phone)
            phone_i = len(args)
            clauses.append(f"phone = ${phone_i}")
        if src.domain:
            args.append(src.domain)
            domain_i = len(args)
            clauses.append(f"website_domain = ${domain_i}")
        if src.admin_unit_id and src.tokens:
            args.append(src.admin_unit_id)
            unit = f"admin_unit_id = ${len(args)}"
            like = " OR ".join(
                f"normalized_name LIKE '%' || ${len(args) + i + 1} || '%'"
                for i, t in enumerate(src.tokens)
                if len(t) >= 3
            )
            args.extend(t for t in src.tokens if len(t) >= 3)
            if like:
                clauses.append(f"({unit} AND ({like}))")
        if src.lat is not None and src.lon is not None:
            args.append(src.lon)
            args.append(src.lat)
            args.append(500.0)
            geo_i = len(args) - 2  # $geo_i = lon, $geo_i+1 = lat
            clauses.append(
                f"location IS NOT NULL AND ST_DWithin(location::geography,"
                f" ST_SetSRID(ST_MakePoint(${geo_i}, ${geo_i + 1}), 4326)::geography,"
                f" ${len(args)})"
            )
        if not clauses:
            return []
        cases = []
        if phone_i is not None:
            cases.append(f"WHEN phone = ${phone_i} THEN 0")
        if domain_i is not None:
            cases.append(f"WHEN website_domain = ${domain_i} THEN 1")
        order_parts = [f"CASE {' '.join(cases)} ELSE 2 END"] if cases else []
        if geo_i is not None:
            order_parts.append(
                f"location <-> ST_SetSRID(ST_MakePoint(${geo_i}, ${geo_i + 1}), 4326) NULLS LAST"
            )
        order = f" ORDER BY {', '.join(order_parts)}" if order_parts else ""
        sql = (
            f"SELECT {', '.join(_PLACE_COLS)} FROM canonical_places"
            f" WHERE {' OR '.join(clauses)}{order} LIMIT 50"
        )
        rows = await self._pool.fetch(sql, *args)
        return [self._row(r) for r in rows]

    async def find_place_by_source(self, provider: str, external_id: str) -> int | None:
        r = await self._pool.fetchrow(
            "SELECT place_id FROM place_sources WHERE provider = $1 AND external_id = $2",
            provider,
            external_id,
        )
        return r["place_id"] if r else None

    async def find_place_by_record(self, record_id: int) -> int | None:
        r = await self._pool.fetchrow(
            "SELECT place_id FROM place_sources WHERE source_record_id = $1",
            record_id,
        )
        return r["place_id"] if r else None

    async def find_business_by_name(
        self,
        normalized_name: str,
        *,
        category: str | None = None,
        phone: str | None = None,
        website_domain: str | None = None,
        admin_unit_id: int | None = None,
    ) -> int | None:
        if not normalized_name:
            return None
        r = await self._pool.fetchrow(
            """SELECT b.business_id FROM canonical_businesses b
               WHERE b.normalized_name = $1
                 AND EXISTS (
                     SELECT 1 FROM canonical_places p
                     WHERE p.business_id = b.business_id
                       AND (($2::text   IS NOT NULL AND p.canonical_category = $2)
                         OR ($3::text   IS NOT NULL AND p.phone = $3)
                         OR ($4::text   IS NOT NULL AND p.website_domain = $4)
                         OR ($5::bigint IS NOT NULL AND p.admin_unit_id = $5)))
               ORDER BY b.business_id LIMIT 1""",
            normalized_name,
            category,
            phone,
            website_domain,
            admin_unit_id,
        )
        return int(r["business_id"]) if r else None

    async def create_business(self, name: str, normalized_name: str) -> int:
        r = await self._pool.fetchrow(
            "INSERT INTO canonical_businesses (display_name, normalized_name)"
            " VALUES ($1, $2) RETURNING business_id",
            name,
            normalized_name,
        )
        return int(r["business_id"])

    async def create_place(self, row: dict[str, Any]) -> int:
        r = await self._pool.fetchrow(
            """INSERT INTO canonical_places
               (business_id, canonical_name, normalized_name, canonical_category,
                address, normalized_address, phone, website, website_domain,
                opening_hours, lat, lon, location, admin_unit_id, status,
                confidence, source_count, resolution_run_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                       CASE WHEN $11::float8 IS NULL THEN NULL
                            ELSE ST_SetSRID(ST_MakePoint($12::float8,$11::float8),4326) END,
                       $13,$14,$15,$16,$17)
               RETURNING place_id""",
            row["business_id"],
            row["canonical_name"],
            row["normalized_name"],
            row["canonical_category"],
            row["address"],
            row["normalized_address"],
            row["phone"],
            row["website"],
            row.get("website_domain") or urlparse_host(row.get("website")),
            _jsonb_param(row.get("opening_hours")),
            row["lat"],
            row["lon"],
            row["admin_unit_id"],
            row["status"],
            row["confidence"],
            row["source_count"],
            row.get("resolution_run_id"),
        )
        return int(r["place_id"])

    async def update_place(self, place_id: int, fields: dict[str, Any]) -> None:
        sets, args = [], []
        allowed = {
            "canonical_name",
            "normalized_name",
            "canonical_category",
            "address",
            "normalized_address",
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
            "resolution_run_id",
            "suspicious_merge",
            "suspicious_reason",
            "rating",
            "review_count",
            "price_level",
            "primary_image_url",
            "images",
        }
        for k, v in fields.items():
            if k not in allowed:
                continue
            args.append(_jsonb_param(v) if k in ("opening_hours", "images") else v)
            sets.append(f"{k} = ${len(args)}")
        if not sets:
            return
        if fields.get("lat") is not None and fields.get("lon") is not None:
            sets.append(
                f"location = ST_SetSRID(ST_MakePoint(${len(args) + 1}, ${len(args) + 2}), 4326)"
            )
            args.extend([fields["lon"], fields["lat"]])
        sets.append("updated_at = now()")
        args.append(place_id)
        await self._pool.execute(
            f"UPDATE canonical_places SET {', '.join(sets)} WHERE place_id = ${len(args)}",
            *args,
        )

    async def link_source(
        self,
        place_id: int,
        record_id: int,
        provider: str,
        external_id: str | None,
        run_id: int,
        *,
        score: float | None = None,
        reason: str | None = None,
    ) -> bool:
        r = await self._pool.execute(
            """INSERT INTO place_sources
               (place_id, source_record_id, provider, external_id, resolution_run_id,
                matcher_version, resolution_score, resolution_reason, resolved_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now())
               ON CONFLICT (source_record_id) DO UPDATE SET
                   resolution_run_id = EXCLUDED.resolution_run_id,
                   matcher_version = EXCLUDED.matcher_version,
                   resolution_score = EXCLUDED.resolution_score,
                   resolution_reason = EXCLUDED.resolution_reason,
                   resolved_at = EXCLUDED.resolved_at
               WHERE place_sources.place_id = EXCLUDED.place_id""",
            place_id,
            record_id,
            provider,
            external_id,
            run_id,
            RESOLVER_VERSION,
            score,
            reason,
        )
        # asyncpg returns 'INSERT 0 1' on insert/update, 'INSERT 0 0' when the
        # conflicting row belongs to a different place (lost the link race)
        return not str(r).endswith("0")

    async def audit_merges(
        self, run_id: int, geo_veto_m: float = 200.0, max_sources: int = 8
    ) -> dict[str, Any]:
        """Flag run-touched places whose linked records are implausible:
        coordinate span beyond the same-place radius, or an excessive
        record count. Returns the audit summary persisted on the run."""
        await self._pool.execute(
            """UPDATE canonical_places SET suspicious_merge = false,
               suspicious_reason = NULL
               WHERE resolution_run_id = $1 AND suspicious_merge""",
            run_id,
        )
        rows = await self._pool.fetch(
            """WITH touched AS (
                   SELECT place_id FROM canonical_places WHERE resolution_run_id = $1),
               pairs AS (
                   SELECT s1.place_id,
                          ST_DistanceSphere(
                              ST_SetSRID(ST_MakePoint(r1.lon, r1.lat), 4326),
                              ST_SetSRID(ST_MakePoint(r2.lon, r2.lat), 4326)) AS d
                   FROM place_sources s1
                   JOIN place_sources s2 ON s2.place_id = s1.place_id
                                        AND s2.source_record_id > s1.source_record_id
                   JOIN place_source_records r1 ON r1.id = s1.source_record_id
                   JOIN place_source_records r2 ON r2.id = s2.source_record_id
                   WHERE r1.lat IS NOT NULL AND r1.lon IS NOT NULL
                     AND r2.lat IS NOT NULL AND r2.lon IS NOT NULL
                     AND s1.place_id IN (SELECT place_id FROM touched)),
               geo_bad AS (
                   SELECT place_id, max(d) AS d FROM pairs
                   GROUP BY place_id HAVING max(d) > $2),
               fat AS (
                   SELECT place_id, count(*) AS n FROM place_sources
                   WHERE place_id IN (SELECT place_id FROM touched)
                   GROUP BY place_id HAVING count(*) > $3),
               flagged AS (
                   SELECT coalesce(g.place_id, f.place_id) AS place_id,
                          concat_ws('; ',
                              CASE WHEN g.place_id IS NOT NULL
                                   THEN 'geo_span:' || round(g.d)::int || 'm' END,
                              CASE WHEN f.place_id IS NOT NULL
                                   THEN 'source_count:' || f.n END) AS reason
                   FROM geo_bad g FULL JOIN fat f ON f.place_id = g.place_id)
               UPDATE canonical_places p
               SET suspicious_merge = true, suspicious_reason = fl.reason
               FROM flagged fl WHERE p.place_id = fl.place_id
               RETURNING p.place_id, fl.reason""",
            run_id,
            geo_veto_m,
            max_sources,
        )
        flagged = [{"place_id": int(r["place_id"]), "reason": r["reason"]} for r in rows]
        return {
            "suspicious": len(flagged),
            "checks": {"geo_veto_m": geo_veto_m, "max_sources": max_sources},
            "places": flagged[:50],
        }

    async def sources_for(self, place_id: int) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """SELECT s.source_record_id AS id, r.provider, r.external_id,
                      r.raw_name, r.raw_address, r.raw_phone, r.raw_website,
                      r.raw_category, r.raw_hours, r.raw_status, r.lat, r.lon,
                      r.admin_unit_id, r.observed_at, r.raw_payload
               FROM place_sources s
               JOIN place_source_records r ON r.id = s.source_record_id
               WHERE s.place_id = $1""",
            place_id,
        )
        return [dict(r) for r in rows]

    async def write_provenance(self, rows: list[ProvRow]) -> None:
        if not rows:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            place_ids = {r.place_id for r in rows}
            fields = {r.field for r in rows}
            for pid in place_ids:
                for f in fields:
                    await conn.execute(
                        "UPDATE place_field_provenance SET chosen = false"
                        " WHERE place_id = $1 AND field = $2",
                        pid,
                        f,
                    )
            for r in rows:
                await conn.execute(
                    """INSERT INTO place_field_provenance
                       (place_id, field, source_record_id, provider, value,
                        weight, observed_at, chosen)
                       VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8)
                       ON CONFLICT (place_id, field, source_record_id) DO UPDATE
                       SET value = EXCLUDED.value, weight = EXCLUDED.weight,
                           observed_at = EXCLUDED.observed_at,
                           chosen = EXCLUDED.chosen, updated_at = now()""",
                    r.place_id,
                    r.field,
                    r.source_record_id,
                    r.provider,
                    json.dumps(r.value, ensure_ascii=False),
                    r.weight,
                    r.observed_at,
                    r.chosen,
                )

    async def get_place(self, place_id: int) -> PlaceRow | None:
        r = await self._pool.fetchrow(
            f"SELECT {', '.join(_PLACE_COLS)} FROM canonical_places WHERE place_id = $1",
            place_id,
        )
        return self._row(r) if r else None

    async def search_places(
        self,
        q=None,
        lat=None,
        lon=None,
        radius_m=2000.0,
        admin_unit_id=None,
        category=None,
        limit=20,
    ) -> list[PlaceRow]:
        from resolution.normalize import norm_name

        clauses, args = ["status NOT IN ('closed', 'permanently_closed')"], []
        if admin_unit_id:
            args.append(admin_unit_id)
            clauses.append(f"admin_unit_id = ${len(args)}")
        if category:
            args.append(category)
            clauses.append(f"canonical_category = ${len(args)}")
        if q:
            # every normalized query token must appear in the name
            for t in norm_name(q).split():
                if len(t) >= 2:
                    args.append(t)
                    clauses.append(f"normalized_name LIKE '%' || ${len(args)} || '%'")
        if lat is not None and lon is not None:
            args.append(lon)
            args.append(lat)
            args.append(radius_m)
            clauses.append(
                f"location IS NOT NULL AND ST_DWithin(location::geography,"
                f" ST_SetSRID(ST_MakePoint(${len(args) - 2}, ${len(args) - 1}), 4326)::geography,"
                f" ${len(args)})"
            )
        order = ""
        if lat is not None and lon is not None:
            order = f" ORDER BY location <-> ST_SetSRID(ST_MakePoint(${len(args) - 2}, ${len(args) - 1}), 4326)"
        args.append(limit)
        sql = (
            f"SELECT {', '.join(_PLACE_COLS)} FROM canonical_places"
            f" WHERE {' AND '.join(clauses)}{order} LIMIT ${len(args)}"
        )
        rows = await self._pool.fetch(sql, *args)
        return [self._row(r) for r in rows]


def _jsonb_list(v: Any) -> list | None:
    """asyncpg returns jsonb as str unless a codec is registered."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, list) else None


def _jsonb_param(v: Any) -> str | None:
    """Bind param for a jsonb column: dict/list dump once; a str holding JSON
    is unwrapped to its object first — asyncpg hands staged jsonb back as str,
    so a naive dumps here would store a JSON *string* containing the object
    (the P2.0.3 double-encode). Parse failures and non-obj/list values → None."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return None


def urlparse_host(url: str | None) -> str | None:
    from urllib.parse import urlparse

    if not url:
        return None
    h = urlparse(url).netloc
    return h[4:] if h.startswith("www.") else h
