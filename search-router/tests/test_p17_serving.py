"""P17 serving layer — unit coverage for the local-place read path.

All tests run infra-free: FakeOS simulates the OpenSearch lane, FakePool
simulates canonical Postgres, FakeRedis/PlaceCache(memory) covers caching.
Live-stack verification lives in test_p17_live.py (E2E=1) and the
scripts/bench_places.py benchmark.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from serving.places import query as qmod
from serving.places.cache import PlaceCache
from serving.places.document import PlaceDocumentV1
from serving.places.indexer import DictIndexState, PgIndexState, PlaceIndexer
from serving.places.os_index import (
    PlaceIndexUnavailable,
    PlaceOSIndex,
    build_autocomplete_body,
    build_search_body,
)
from serving.places.projection import (
    doc_from_index_source,
    doc_to_index_source,
    freshness_score,
    project_row,
)
from serving.places.query import fold_text, parse_local_query
from serving.places.ranking import Candidate, RankWeights, haversine_m, rank
from serving.places.service import PlaceService

NOW = datetime(2026, 9, 20, tzinfo=UTC)


# ─── fixtures ────────────────────────────────────────────────────────────────


def _doc(i: int, name: str | None = None, **kw) -> PlaceDocumentV1:
    base: dict[str, Any] = {
        "place_id": str(i),
        "business_id": None,
        "name": name or f"Nhà thuốc P17 {i}",
        "normalized_name": fold_text(name or f"Nhà thuốc P17 {i}"),
        "category_ids": ["health"],
        "lat": 10.77 + (i % 50) * 0.001,
        "lon": 106.70 + (i % 50) * 0.001,
        "admin_unit_id": "501",
        "status": "open",
        "confidence": 0.8,
        "freshness_score": 0.9,
        "source_count": 2,
        "last_verified_at": NOW,
    }
    return PlaceDocumentV1(**{**base, **kw})


def _row(i: int, name: str | None = None, **kw) -> dict[str, Any]:
    base = {
        "place_id": i,
        "business_id": None,
        "canonical_name": name or f"Nhà thuốc P17 {i}",
        "normalized_name": fold_text(name or f"Nhà thuốc P17 {i}"),
        "canonical_category": "health",
        "address": f"{i} Bench Ward",
        "phone": None,
        "website": None,
        "website_domain": None,
        "opening_hours": None,
        "lat": 10.77 + (i % 50) * 0.001,
        "lon": 106.70 + (i % 50) * 0.001,
        "admin_unit_id": 501,
        "status": "open",
        "confidence": 0.8,
        "source_count": 2,
        "last_seen": NOW,
        "updated_at": NOW + timedelta(seconds=i),
    }
    return {**base, **kw}


class FakeRedis:
    """Minimal async-Redis stand-in (dict)."""

    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None):
        self.d[k] = v
        return True

    async def delete(self, *ks):
        n = 0
        for k in ks:
            n += self.d.pop(k, None) is not None
        return n

    async def incr(self, k):
        self.d[k] = str(int(self.d.get(k) or 0) + 1)
        return int(self.d[k])


class FakeOS:
    """In-memory OpenSearch lane: stores docs per concrete index, evaluates
    LocalQuerySpec semantics for candidates, records built bodies."""

    def __init__(self, docs: list[PlaceDocumentV1] | None = None):
        self.indices: dict[str, dict[str, dict]] = {"places_v1": {}}
        self.alias = "places"
        self.alias_to = "places_v1"
        self.bodies: list[dict] = []
        self.ac_bodies: list[dict] = []
        self.available = True
        for d in docs or []:
            self.indices["places_v1"][d.place_id] = d

    def _target(self, index: str | None) -> dict[str, dict]:
        name = index or self.alias_to
        return self.indices.setdefault(name, {})

    async def ensure(self):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        return self.alias

    async def create_generation(self, generation: int):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        name = f"{self.alias}_v1_g{generation}"
        self.indices.setdefault(name, {})
        return name

    async def swap_alias(self, new_concrete: str):
        old = [self.alias_to]
        self.alias_to = new_concrete
        return old

    async def reindex(self, source: str, dest: str) -> int:
        src = self.indices.get(source, {})
        target = self.indices.setdefault(dest, {})
        for pid, doc in src.items():
            target[pid] = doc
        return len(src)

    async def drop_index(self, name: str):
        self.indices.pop(name, None)

    async def current_concrete(self):
        return self.alias_to

    async def refresh(self, index=None):
        return None

    async def upsert_docs(self, docs, index=None):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        store = self._target(index)
        for d in docs:
            store[d.place_id] = d
        return {"indexed": len(docs), "failed": 0, "errors": []}

    async def delete(self, place_id, index=None):
        store = self._target(index)
        return store.pop(str(place_id), None) is not None

    async def delete_ids(self, place_ids):
        store = self._target(None)
        n = 0
        for p in place_ids:
            n += store.pop(str(p), None) is not None
        return n

    async def get_doc(self, place_id):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        return self._target(None).get(str(place_id))

    async def all_ids(self, batch_size=2000):
        return set(self._target(None).keys())

    async def stats(self):
        return {
            "index": self.alias_to,
            "exists": True,
            "docs": len(self._target(None)),
            "store_bytes": 1234,
        }

    # ── lane evaluation ──────────────────────────────────────────────────

    def _match(self, doc: PlaceDocumentV1, spec) -> float | None:
        """Spec-level simulation of the real bool query. Returns a score or
        None when the doc does not match."""
        if spec.statuses and doc.status not in spec.statuses:
            return None
        if spec.category and spec.category not in doc.category_ids:
            return None
        if spec.admin_unit_id is not None and doc.admin_unit_id != str(spec.admin_unit_id):
            return None
        if spec.has_geo:
            if doc.lat is None or doc.lon is None:
                return None
            if haversine_m(spec.lat, spec.lon, doc.lat, doc.lon) > spec.radius_m:
                return None
        if spec.bbox:
            min_lon, min_lat, max_lon, max_lat = spec.bbox
            if doc.lon is None or not (min_lon <= doc.lon <= max_lon):
                return None
            if doc.lat is None or not (min_lat <= doc.lat <= max_lat):
                return None
        if spec.tokens:
            hay = doc.normalized_name or fold_text(doc.name)
            hay += " " + " ".join(fold_text(a) for a in doc.aliases)
            hits = sum(1 for t in spec.tokens if t in hay)
            if hits == 0:
                return None
            return float(hits) + (5.0 if fold_text(doc.name) == spec.text else 0.0)
        return 1.0

    async def search(self, spec, top_k):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        self.bodies.append(build_search_body(spec, top_k=top_k))
        out = []
        for d in self._target(None).values():
            s = self._match(d, spec)
            if s is not None:
                out.append((d, s))
        return out[:top_k]

    async def autocomplete(self, q_folded, limit, lat, lon):
        if not self.available:
            raise PlaceIndexUnavailable("down")
        self.ac_bodies.append(build_autocomplete_body(q_folded, limit=limit, lat=lat, lon=lon))
        out = []
        for d in self._target(None).values():
            if d.status == "permanently_closed":
                continue
            names = [d.name, d.normalized_name, *d.aliases]
            if any(
                fold_text(n).startswith(q_folded) or q_folded in fold_text(n) for n in names if n
            ):
                out.append((d, 1.0))
        out.sort(key=lambda t: t[0].name)
        return out[:limit]


class FakePool:
    """Canonical-side fake: keyset/delta scans, alias/provenance lookups and
    semantic evaluation of the PostGIS candidate lane."""

    def __init__(
        self,
        rows: list[dict] | None = None,
        aliases: list[dict] | None = None,
        sources: list[dict] | None = None,
        prov: list[dict] | None = None,
    ):
        self.rows = sorted(rows or [], key=lambda r: r["place_id"])
        self.alias_rows = aliases or []
        self.source_rows = sources or []
        self.prov_rows = prov or []
        self.calls: list[tuple[str, str, tuple]] = []
        self.fail_on: str | None = None  # substring → raise

    async def _route(self, sql: str):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"boom: {self.fail_on}")
        if "place_id = ANY" in sql and "ST_DistanceSphere" in sql:
            return "distances"
        if "JOIN place_source_records" in sql:
            return "aliases"
        if "FROM place_sources" in sql:
            return "sources"
        if "place_field_provenance" in sql:
            return "prov"
        if "SELECT place_id FROM canonical_places" in sql:
            return "ids"
        if "updated_at > $1" in sql:
            return "delta"
        if "FROM canonical_places" in sql and "place_id > $1" in sql:
            return "scan"
        if "FROM canonical_places" in sql:
            return "candidates"
        return "other"

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        kind = await self._route(sql)
        if kind == "distances":
            ids, lon, lat = args[0], args[1], args[2]
            return [
                {"place_id": r["place_id"], "distance_m": haversine_m(lat, lon, r["lat"], r["lon"])}
                for r in self.rows
                if r["place_id"] in ids and r.get("lat") is not None
            ]
        if kind == "aliases":
            return [a for a in self.alias_rows if a["place_id"] in set(args[0])]
        if kind == "sources":
            return [s for s in self.source_rows if s.get("place_id") == args[0]]
        if kind == "prov":
            return [p for p in self.prov_rows if p.get("place_id") == args[0]]
        if kind == "ids":
            return [{"place_id": r["place_id"]} for r in self.rows if r["place_id"] > args[0]][
                : args[1]
            ]
        if kind == "scan":
            return [r for r in self.rows if r["place_id"] > args[0]][: args[1]]
        if kind == "delta":
            ts, pid, lim = args
            out = [
                r
                for r in self.rows
                if r["updated_at"] > ts or (r["updated_at"] == ts and r["place_id"] > pid)
            ]
            out.sort(key=lambda r: (r["updated_at"], r["place_id"]))
            return out[:lim]
        if kind == "candidates":
            return self._eval_candidates(sql, args)
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"boom: {self.fail_on}")
        if "canonical_places" in sql:
            for r in self.rows:
                if r["place_id"] == args[0]:
                    return r
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    def _eval_candidates(self, sql, args):
        i = 0
        res = list(self.rows)
        geo_on = "ST_DWithin" in sql
        if geo_on:
            lon, lat, radius = args[0], args[1], args[2]
            i = 3
            kept = []
            for r in res:
                if r.get("lat") is None:
                    continue
                d = haversine_m(lat, lon, r["lat"], r["lon"])
                if d <= radius:
                    kept.append({**r, "distance_m": d})
            res = kept
            res.sort(key=lambda r: r["distance_m"])
        if "status = ANY" in sql:
            s = args[i]
            i += 1
            res = [r for r in res if r.get("status") in s]
        if "canonical_category = $" in sql:
            c = args[i]
            i += 1
            res = [r for r in res if r.get("canonical_category") == c]
        if "admin_unit_id = $" in sql or "ST_Contains" in sql:
            a = args[i]
            i += 1
            res = [r for r in res if r.get("admin_unit_id") == a]
        if "ST_MakeEnvelope" in sql:
            b = args[i : i + 4]
            i += 4
            res = [
                r
                for r in res
                if r.get("lon") is not None
                and b[0] <= r["lon"] <= b[2]
                and b[1] <= r["lat"] <= b[3]
            ]
        n_tok = sql.count("normalized_name LIKE")
        for _ in range(n_tok):
            tok = args[i]
            i += 1
            res = [r for r in res if tok in (r.get("normalized_name") or "")]
        if not geo_on:
            res.sort(
                key=lambda r: (
                    -(r.get("confidence") or 0),
                    -(r.get("source_count") or 0),
                    r["place_id"],
                )
            )
        return res[: args[-1]]


def _service(docs=None, rows=None, **kw):
    pool = FakePool(
        rows=rows, **{k: v for k, v in kw.items() if k in ("aliases", "sources", "prov")}
    )
    os_index = FakeOS(docs=docs)
    cache = PlaceCache(client=FakeRedis())

    async def _pool():
        return pool

    svc = PlaceService(os_index=os_index, cache=cache, pool_getter=_pool)
    return svc, os_index, pool, cache


# ─── query parsing ──────────────────────────────────────────────────────────


class TestQueryParsing:
    def test_fold_and_proximity_strip(self):
        spec = parse_local_query(q="nhà thuốc gần tôi", lat=10.7, lon=106.7)
        assert spec.text == "nha thuoc"
        assert spec.category == "health" and spec.category_hinted
        assert spec.has_geo

    def test_explicit_category_wins(self):
        spec = parse_local_query(q="nhà thuốc", category="retail")
        assert spec.category == "retail" and not spec.category_hinted

    def test_status_default_excludes_permanently_closed(self):
        spec = parse_local_query()
        assert "permanently_closed" not in spec.statuses
        assert parse_local_query(status="permanently_closed").statuses == frozenset(
            {"permanently_closed"}
        )
        assert len(parse_local_query(status="all").statuses) == 4
        assert "open" in parse_local_query(status="bogus,open").statuses

    def test_bbox_and_limits(self):
        spec = parse_local_query(bbox="106.6,10.7,106.8,10.8", limit=500, radius_m=-1)
        assert spec.bbox == (106.6, 10.7, 106.8, 10.8)
        assert spec.limit == 100  # capped
        assert spec.radius_m == qmod.DEFAULT_RADIUS_M
        assert parse_local_query(bbox="bad").bbox is None
        assert parse_local_query(bbox="2,1,1,2").bbox is None  # inverted

    def test_category_hints(self):
        assert qmod.hint_category("nha thuoc tay") == "health"
        assert qmod.hint_category("quan ca phe dep") == "food"
        assert qmod.hint_category("xyz nothing") is None


# ─── projection ──────────────────────────────────────────────────────────────


class TestProjection:
    def test_row_to_document(self):
        doc = project_row(
            _row(7, canonical_category="food", status="closed_garbage"),
            alias_names=["Phở Bảy", "Nhà thuốc P17 7", "PHỞ BẢY"],
            now=NOW,
        )
        assert doc.place_id == "7"
        assert doc.name == "Nhà thuốc P17 7"
        # canonical name + case-variant folded out, one alias remains
        assert doc.aliases == ["Phở Bảy"]
        assert doc.status == "unknown"  # unrecognized status sanitized
        assert doc.category_ids == ["food"]
        assert doc.last_verified_at == NOW
        assert 0.99 < doc.freshness_score <= 1.0

    def test_jsonb_str_opening_hours(self):
        # asyncpg returns jsonb as str unless a codec is registered — a
        # canonical row carrying hours must still project to dict, not 500.
        doc = project_row(_row(8, opening_hours='{"raw": "07:00-21:00"}'))
        assert doc.opening_hours == {"raw": "07:00-21:00"}
        assert project_row(_row(9, opening_hours="not json")).opening_hours is None
        assert project_row(_row(10, opening_hours='["a"]')).opening_hours is None

    def test_freshness_decay(self):
        assert freshness_score(None) == 0.5
        assert freshness_score(NOW, NOW) == 1.0
        stale = freshness_score(NOW - timedelta(days=180), NOW)
        assert 0.35 < stale < 0.40  # e^-1

    def test_index_source_roundtrip(self):
        doc = _doc(3, lat=None, lon=None)
        src = doc_to_index_source(doc)
        assert src["location"] is None
        back = doc_from_index_source(src)
        assert back.place_id == "3" and back.name == doc.name
        doc2 = _doc(4)
        src2 = doc_to_index_source(doc2)
        assert src2["location"]["lat"] == doc2.lat
        assert doc_from_index_source(src2).place_id == "4"


# ─── OpenSearch query building ───────────────────────────────────────────────


class TestOsQueryBuild:
    def test_search_body_filters(self):
        spec = parse_local_query(
            q="nhà thuốc",
            lat=10.7,
            lon=106.7,
            radius_m=3000,
            category="health",
            admin_unit_id=501,
            bbox="106.6,10.6,106.8,10.8",
            limit=10,
        )
        body = build_search_body(spec, top_k=50)
        b = body["query"]["bool"]
        filters = b["filter"]
        assert {"terms": {"status": sorted(spec.statuses)}} in filters
        assert {"term": {"category_ids": "health"}} in filters
        assert {"term": {"admin_unit_id": "501"}} in filters
        assert any("geo_distance" in f for f in filters)
        assert any("geo_bounding_box" in f for f in filters)
        # exact-folded boost + fuzzy fallback present
        should = b["must"][0]["bool"]["should"]
        assert any("name.exact" in s.get("term", {}) for s in should)
        assert any(
            mm.get("multi_match", {}).get("fuzziness") == "AUTO" for s in should for mm in [s]
        )
        assert body["size"] == 50

    def test_geo_only_sorts_by_distance(self):
        spec = parse_local_query(lat=10.7, lon=106.7)
        body = build_search_body(spec, top_k=10)
        assert body["query"]["bool"]["must"] == [{"match_all": {}}]
        assert "_geo_distance" in body["sort"][0]

    def test_autocomplete_body(self):
        body = build_autocomplete_body("nha thu", limit=8, lat=10.7, lon=106.7)
        assert "function_score" in body["query"]
        inner = body["query"]["function_score"]["query"]["bool"]
        should = inner["must"][0]["bool"]["should"]
        assert any("name.ac" in s.get("match_bool_prefix", {}) for s in should)
        assert "permanently_closed" not in str(inner["filter"])
        no_geo = build_autocomplete_body("nha", limit=5, lat=None, lon=None)
        assert "function_score" not in no_geo["query"]


# ─── ranking ────────────────────────────────────────────────────────────────


class TestRanking:
    def test_components_and_determinism(self):
        cands = [
            Candidate(doc=_doc(1, confidence=0.9), os_score=10.0, distance_m=100),
            Candidate(doc=_doc(2, confidence=0.3), os_score=8.0, distance_m=900),
            Candidate(doc=_doc(3, confidence=0.5), os_score=1.0, distance_m=50),
        ]
        r1 = rank(list(cands), query_category="health", limit=3)
        r2 = rank(list(cands), query_category="health", limit=3)
        assert [c.doc.place_id for c in r1] == [c.doc.place_id for c in r2]
        assert r1[0].doc.place_id == "1"  # best text + close + confident
        assert set(r1[0].components) == {
            "text",
            "distance",
            "category",
            "confidence",
            "freshness",
            "sources",
            "status",
            "rating_quality",
            "popularity",
            "open_now_boost",
        }

    def test_weight_flip_changes_order(self):
        near_weak = Candidate(doc=_doc(1, confidence=0.1), os_score=0.5, distance_m=30)
        far_strong = Candidate(doc=_doc(2, confidence=0.95), os_score=20.0, distance_m=1500)
        default = rank([near_weak, far_strong], query_category=None)
        assert default[0].doc.place_id == "2"  # text dominates
        heavy_geo = RankWeights(distance=0.9, text=0.02, confidence=0.02)
        flipped = rank(
            [
                Candidate(doc=_doc(1, confidence=0.1), os_score=0.5, distance_m=30),
                Candidate(doc=_doc(2, confidence=0.95), os_score=20.0, distance_m=1500),
            ],
            query_category=None,
            weights=heavy_geo,
        )
        assert flipped[0].doc.place_id == "1"  # distance dominates

    def test_weights_from_json(self):
        w = RankWeights.from_json('{"distance": 0.9, "bogus": 5, "text": 0.1}')
        assert w.distance == 0.9 and w.text == 0.1
        assert RankWeights.from_json("not json").text == RankWeights().text


# ─── service: search ─────────────────────────────────────────────────────────


class TestServiceSearch:
    def test_text_only(self):
        docs = [_doc(1, name="Nhà thuốc An Tâm"), _doc(2, name="Quán phở Bình")]
        svc, os_i, pool, _ = _service(docs=docs)
        rows, meta = asyncio.run(svc.search(q="nhà thuốc an tâm"))
        assert meta.lanes == ["opensearch"] and not meta.cache_hit
        assert rows[0]["canonical_name"] == "Nhà thuốc An Tâm"
        assert rows[0]["canonical_category"] == "health"

    def test_geo_only_and_distance(self):
        docs = [
            _doc(1, lat=10.770, lon=106.700),
            _doc(2, lat=11.5, lon=106.7),  # ~80 km away → filtered
        ]
        svc, os_i, pool, _ = _service(docs=docs)
        rows, meta = asyncio.run(svc.search(lat=10.7705, lon=106.7005, radius_m=2000))
        assert [r["place_id"] for r in rows] == [1]
        assert rows[0]["distance_m"] is not None and rows[0]["distance_m"] < 2000
        assert "postgis-dist" in meta.lanes  # PostGIS distance oracle ran

    def test_text_geo_combo(self):
        docs = [
            _doc(1, name="Nhà thuốc X", lat=10.771, lon=106.701),
            _doc(2, name="Nhà thuốc Y", lat=10.9, lon=107.5),  # out of radius
            _doc(3, name="Quán ăn Z", lat=10.771, lon=106.701, category_ids=["food"]),
        ]
        svc, _, _, _ = _service(docs=docs)
        rows, _ = asyncio.run(svc.search(q="nhà thuốc", lat=10.77, lon=106.70, radius_m=3000))
        ids = [r["place_id"] for r in rows]
        assert ids == [1]  # health hint + geo + name token all applied

    def test_category_and_admin_filters(self):
        docs = [
            _doc(1, category_ids=["food"], admin_unit_id="501"),
            _doc(2, category_ids=["health"], admin_unit_id="502"),
            _doc(3, category_ids=["health"], admin_unit_id="501"),
        ]
        svc, os_i, _, _ = _service(docs=docs)
        rows, _ = asyncio.run(svc.search(category="health", admin_unit_id=501))
        assert [r["place_id"] for r in rows] == [3]
        body = os_i.bodies[-1]
        assert {"term": {"category_ids": "health"}} in body["query"]["bool"]["filter"]

    def test_admin_contains_uses_postgis(self):
        svc, os_i, pool, _ = _service(rows=[_row(1), _row(2, admin_unit_id=999)])
        rows, meta = asyncio.run(svc.search(admin_unit_id=501, admin_contains=True))
        assert meta.lanes == ["postgis"]  # containment is PostGIS-only
        sql = pool.calls[0][1]
        assert "ST_Contains" in sql and "administrative_units" in sql
        assert [r["place_id"] for r in rows] == [1]

    def test_status_filtering(self):
        docs = [
            _doc(1, status="open"),
            _doc(2, status="permanently_closed"),
            _doc(3, status="temporarily_closed"),
        ]
        svc, _, _, _ = _service(docs=docs)
        rows, _ = asyncio.run(svc.search(q="p17"))
        assert sorted(r["place_id"] for r in rows) == [1, 3]  # closed excluded
        rows, _ = asyncio.run(svc.search(q="p17", status="permanently_closed"))
        assert [r["place_id"] for r in rows] == [2]

    def test_debug_components(self):
        svc, _, _, _ = _service(docs=[_doc(1)])
        rows, meta = asyncio.run(svc.search(q="nhà thuốc p17 1", debug=True))
        dbg = rows[0]["score_debug"]
        assert "components" in dbg and "score" in dbg
        assert meta.lanes == ["opensearch"]

    def test_cache_hit_and_miss(self):
        svc, os_i, _, _ = _service(docs=[_doc(1)])
        kw = {"q": "nhà thuốc", "lat": 10.77, "lon": 106.70}
        _, m1 = asyncio.run(svc.search(**kw))
        assert not m1.cache_hit
        _, m2 = asyncio.run(svc.search(**kw))
        assert m2.cache_hit and m2.lanes == ["cache"]
        assert len(os_i.bodies) == 1  # second call never hit the lane

    def test_stale_cache_invalidated_on_update(self):
        svc, os_i, _, cache = _service(docs=[_doc(1)])
        kw = {"q": "nhà thuốc"}
        asyncio.run(svc.search(**kw))
        asyncio.run(cache.invalidate_place("1"))
        _, meta = asyncio.run(svc.search(**kw))
        assert not meta.cache_hit  # epoch bump → fresh fetch
        assert len(os_i.bodies) == 2

    def test_redis_down_still_serves(self):
        docs = [_doc(1)]
        pool = FakePool()
        cache = PlaceCache(redis_enabled=False)  # memory-only

        async def _pool():
            return pool

        svc = PlaceService(os_index=FakeOS(docs=docs), cache=cache, pool_getter=_pool)
        rows, meta = asyncio.run(svc.search(q="nhà thuốc"))
        assert rows and not meta.cache_hit
        # memory fallback caches too
        _, meta2 = asyncio.run(svc.search(q="nhà thuốc"))
        assert meta2.cache_hit

    def test_opensearch_down_postgis_fallback(self):
        svc, os_i, pool, _ = _service(rows=[_row(1), _row(2, status="permanently_closed")])
        os_i.available = False
        rows, meta = asyncio.run(svc.search(q="nhà thuốc", lat=10.77, lon=106.70))
        assert "opensearch" in meta.degraded and "postgis" in meta.lanes
        assert [r["place_id"] for r in rows] == [1]
        assert rows[0]["distance_m"] is not None

    def test_all_backends_down_empty_not_fabricated(self):
        cache = PlaceCache(redis_enabled=False)
        os_i = FakeOS()
        os_i.available = False

        async def _none():
            return None

        svc = PlaceService(os_index=os_i, cache=cache, pool_getter=_none)
        rows, meta = asyncio.run(svc.search(q="x", lat=1.0, lon=1.0))
        assert rows == []
        assert set(meta.degraded) == {"opensearch", "postgres"}

    def test_deterministic_ranking_e2e(self):
        docs = [_doc(i) for i in range(1, 15)]
        svc, _, _, _ = _service(docs=docs)
        kw = {"q": "p17", "lat": 10.77, "lon": 106.70, "radius_m": 8000}
        r1, _ = asyncio.run(svc.search(**kw))
        r2, _ = asyncio.run(svc.search(**{**kw, "debug": True}))
        assert [r["place_id"] for r in r1] == [r["place_id"] for r in r2]


# ─── service: autocomplete + detail ──────────────────────────────────────────


class TestSuggestAndDetail:
    def test_autocomplete(self):
        docs = [
            _doc(1, name="Nhà thuốc An Tâm"),
            _doc(2, name="Nhà thuốc Bảo Châu"),
            _doc(3, name="Quán phở"),
        ]
        svc, os_i, _, _ = _service(docs=docs)
        rows, meta = asyncio.run(svc.autocomplete(q="nhà thu", limit=5))
        names = [r["name"] for r in rows]
        assert "Nhà thuốc An Tâm" in names and "Nhà thuốc Bảo Châu" in names
        assert "Quán phở" not in names
        assert meta.lanes == ["opensearch"]
        # cache hit on repeat
        _, meta2 = asyncio.run(svc.autocomplete(q="nhà thu", limit=5))
        assert meta2.cache_hit

    def test_autocomplete_min_length(self):
        svc, os_i, _, _ = _service(docs=[_doc(1)])
        rows, _ = asyncio.run(svc.autocomplete(q="x"))
        assert rows == [] and not os_i.ac_bodies

    def test_detail_pg_with_provenance(self):
        svc, _, pool, _ = _service(
            rows=[_row(9)],
            sources=[
                {
                    "place_id": 9,
                    "provider": "osm",
                    "external_id": "n1",
                    "source_record_id": 5,
                    "linked_at": NOW,
                }
            ],
            prov=[
                {
                    "place_id": 9,
                    "field": "phone",
                    "provider": "osm",
                    "value": "+8490",
                    "weight": 0.9,
                    "observed_at": NOW,
                    "chosen": True,
                }
            ],
        )
        res = asyncio.run(svc.get_place(9))
        assert res.status == "ok"
        p = res.payload
        assert p["canonical_name"] == "Nhà thuốc P17 9"
        assert p["sources"][0]["provider"] == "osm"
        assert p["provenance"][0]["field"] == "phone"
        # cached second read
        res2 = asyncio.run(svc.get_place(9))
        assert res2.meta.cache_hit

    def test_detail_pg_down_served_from_index(self):
        os_i = FakeOS(docs=[_doc(9, name="Nhà thuốc Index")])

        async def _none():
            return None

        svc = PlaceService(os_index=os_i, cache=PlaceCache(redis_enabled=False), pool_getter=_none)
        res = asyncio.run(svc.get_place(9))
        assert res.status == "ok" and res.payload["degraded"] is True
        assert res.payload["canonical_name"] == "Nhà thuốc Index"
        assert res.payload["sources"] == []

    def test_detail_unavailable_when_all_down(self):
        os_i = FakeOS()
        os_i.available = False

        async def _none():
            return None

        svc = PlaceService(os_index=os_i, cache=PlaceCache(redis_enabled=False), pool_getter=_none)
        res = asyncio.run(svc.get_place(9))
        assert res.status == "unavailable"

    def test_detail_not_found(self):
        svc, _, _, _ = _service(rows=[])
        res = asyncio.run(svc.get_place(12345))
        assert res.status == "not_found"

    def test_stale_by_id_cache_cleared_on_invalidate_all(self):
        """by-id keys embed the epoch so a rebuild/reconcile (invalidate_all)
        stops serving a canonical-deleted place instead of returning it until
        TTL expiry."""
        svc, _, pool, cache = _service(rows=[_row(9)])
        res = asyncio.run(svc.get_place(9))
        assert res.status == "ok"
        res2 = asyncio.run(svc.get_place(9))
        assert res2.meta.cache_hit
        pool.rows = []  # canonical DELETE, then rebuild/reconcile bumps epoch
        asyncio.run(cache.invalidate_all())
        res3 = asyncio.run(svc.get_place(9))
        assert res3.status == "not_found" and not res3.meta.cache_hit


# ─── cache freshness of derived fields ───────────────────────────────────────


class TestOpenNowCacheFreshness:
    """open_now is derived at response assembly, never stored — a cache hit
    across an open/close boundary must re-derive, not serve the verdict that
    was true when the entry was written."""

    @staticmethod
    def _clocked(docs=None, rows=None):
        now = [datetime(2026, 9, 29, 16, 0)]  # Tuesday 16:00 — naive = UTC+7
        pool = FakePool(rows=rows)
        redis = FakeRedis()
        cache = PlaceCache(client=redis)

        async def _pool():
            return pool

        svc = PlaceService(
            os_index=FakeOS(docs=docs),
            cache=cache,
            pool_getter=_pool,
            clock=lambda: now[0],
        )
        return svc, now, redis

    def test_search_hit_recomputes_open_now(self):
        hours = {"Tuesday": ["08:00–17:00"]}
        svc, now, redis = self._clocked(docs=[_doc(1, opening_hours=hours)])
        kw = {"q": "nhà thuốc"}
        rows1, m1 = asyncio.run(svc.search(**kw))
        assert not m1.cache_hit and rows1[0]["open_now"] is True
        # cache stores opening_hours, not the derived verdict
        from storage.cache import deserialize

        cached = deserialize(next(v for k, v in redis.d.items() if ":s:" in k))
        assert "opening_hours" in cached[0] and "open_now" not in cached[0]
        now[0] = datetime(2026, 9, 29, 18, 0)  # past the 17:00 close
        rows2, m2 = asyncio.run(svc.search(**kw))
        assert m2.cache_hit and m2.lanes == ["cache"]
        assert rows2[0]["open_now"] is False  # re-derived, not the cached True

    def test_detail_hit_recomputes_open_now(self):
        hours = {"Tuesday": ["08:00–17:00"]}
        svc, now, redis = self._clocked(rows=[_row(9, opening_hours=hours)])
        res1 = asyncio.run(svc.get_place(9))
        assert res1.status == "ok" and res1.payload["open_now"] is True
        from storage.cache import deserialize

        cached = deserialize(next(v for k, v in redis.d.items() if ":p:" in k))
        assert "opening_hours" in cached and "open_now" not in cached
        now[0] = datetime(2026, 9, 29, 18, 0)
        res2 = asyncio.run(svc.get_place(9))
        assert res2.meta.cache_hit
        assert res2.payload["open_now"] is False

    def test_open_now_bypasses_result_cache(self):
        """open_now is time-varying: cached filtered sets go stale across an
        open/close boundary. Bypass result-caching entirely when open_now
        is requested so the freshest verdict always drives the filter."""
        hours = {"Tuesday": ["08:00–17:00"]}
        svc, now, redis = self._clocked(docs=[_doc(1, opening_hours=hours)])
        kw = {"q": "nhà thuốc", "open_now": True}
        _, m1 = asyncio.run(svc.search(**kw))
        assert not m1.cache_hit
        # Second identical open_now=True call must also bypass — no cache entry
        # is written, so there is nothing to hit.
        _, m2 = asyncio.run(svc.search(**kw))
        assert not m2.cache_hit
        assert not any(":s:" in k for k in redis.d)


# ─── indexer ─────────────────────────────────────────────────────────────────


class TestIndexer:
    def _idx(self, rows, docs=None):
        pool = FakePool(rows=rows)
        os_i = FakeOS(docs=docs)
        cache = PlaceCache(client=FakeRedis())
        state = DictIndexState()
        return PlaceIndexer(pool, os_index=os_i, cache=cache, state=state), pool, os_i, cache, state

    def test_full_rebuild(self):
        rows = [_row(i) for i in range(1, 8)]
        idx, pool, os_i, _, state = self._idx(rows)
        res = asyncio.run(idx.rebuild(batch_size=3))
        assert res["status"] == "done" and res["scanned"] == 7
        assert len(os_i._target(None)) == 7
        assert os_i.alias_to.endswith("_g1")
        st = asyncio.run(state.load())
        assert st["generation"] == 1 and st["docs_indexed"] == 7

    def test_incremental_cursor_and_idempotency(self):
        rows = [_row(i) for i in range(1, 6)]
        idx, pool, os_i, cache, state = self._idx(rows)
        r1 = asyncio.run(idx.sync(batch_size=2))
        assert r1["scanned"] == 5 and len(os_i._target(None)) == 5
        st = asyncio.run(state.load())
        assert st["cursor_place_id"] == 5
        # rerun: nothing new → idempotent
        r2 = asyncio.run(idx.sync(batch_size=2))
        assert r2["scanned"] == 0
        # update row 3 → only it re-indexes
        pool.rows[2]["updated_at"] = NOW + timedelta(days=1)
        pool.rows[2]["canonical_name"] = "Đổi tên P17 3"
        pool.rows[2]["normalized_name"] = "doi ten p17 3"
        r3 = asyncio.run(idx.sync(batch_size=2))
        assert r3["scanned"] == 1
        doc = os_i._target(None)["3"]
        assert doc.name == "Đổi tên P17 3"

    def test_delete_tombstone(self):
        idx, _, os_i, cache, _ = self._idx([], docs=[_doc(1), _doc(2)])
        res = asyncio.run(idx.delete(1))
        assert res["deleted"] is True
        assert "1" not in os_i._target(None)

    def test_reconcile_drops_orphans(self):
        idx, _, os_i, _, _ = self._idx([_row(1), _row(2)], docs=[_doc(1), _doc(2), _doc(9)])
        res = asyncio.run(idx.reconcile())
        assert res["extra_ids"] == 1 and res["removed"] == 1
        assert set(os_i._target(None)) == {"1", "2"}

    def test_sync_failure_keeps_cursor(self):
        rows = [_row(i) for i in range(1, 5)]
        idx, _, os_i, _, state = self._idx(rows)

        class FlakyOS(FakeOS):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def upsert_docs(self, docs, index=None):
                self.calls += 1
                if self.calls == 1:
                    raise PlaceIndexUnavailable("flaky")
                return await super().upsert_docs(docs, index=index)

        flaky = FlakyOS()
        idx._os = flaky
        res = asyncio.run(idx.sync(batch_size=4))
        assert res["status"] == "done"  # retried once then succeeded
        assert len(flaky._target(None)) == 4

    def test_indexer_unavailable_without_pool(self):
        idx = PlaceIndexer(None, os_index=FakeOS(), state=DictIndexState())
        res = asyncio.run(idx.sync())
        assert res["status"] == "unavailable"

    def test_migrate_price_level_mapping(self):
        """Mapping migration: builds fresh gen, reindexes docs from the old
        concrete, atomically swaps the alias, and preserves the old index."""
        idx, pool, os_i, cache, state = self._idx([_row(i) for i in range(1, 4)])
        # Seed: rebuild puts docs into places_v1_g1 at generation 1
        asyncio.run(idx.rebuild(batch_size=3))
        old_index = os_i.alias_to
        assert old_index == "places_v1_g1"

        # Simulate having advanced to generation 26 (pre-rollover state)
        state.state["generation"] = 26

        # Migrate — creates g27 from current_gen + 1, reindexes, swaps
        res = asyncio.run(idx.migrate_price_level_mapping())
        assert res["status"] == "done"
        assert res["generation"] == 27
        assert res["index"] == "places_v1_g27"
        assert os_i.alias_to == "places_v1_g27"
        # Old index preserved (not dropped)
        assert old_index in os_i.indices
        assert old_index in res["preserved"]
        # Docs copied across
        assert len(os_i._target(None)) == 3

    def test_migrate_is_guarded_and_idempotent(self):
        """Re-running at or above the current generation is a no-op."""
        idx, pool, os_i, cache, state = self._idx([_row(1, name="keep")])
        asyncio.run(idx.rebuild(batch_size=3))
        state.state["generation"] = 27  # already at target

        res = asyncio.run(idx.migrate_price_level_mapping(generation=27))
        assert res["status"] == "skipped"
        assert res["generation"] == 27
        assert os_i.alias_to == "places_v1_g1"  # unchanged


# ─── endpoint wiring ─────────────────────────────────────────────────────────


class TestEndpoints:
    def test_places_search_shape(self, monkeypatch):
        from api.v1 import places_search
        from serving.places import service as svc_mod

        svc, _, _, _ = _service(docs=[_doc(1)])
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        rows = asyncio.run(places_search(q="nhà thuốc"))
        assert len(rows) == 1 and rows[0].canonical_name == "Nhà thuốc P17 1"
        assert rows[0].name == "Nhà thuốc P17 1"

    def test_places_search_no_backends(self, monkeypatch):
        from api.v1 import places_search
        from serving.places import service as svc_mod

        os_i = FakeOS()
        os_i.available = False

        async def _none():
            return None

        svc = PlaceService(os_index=os_i, cache=PlaceCache(redis_enabled=False), pool_getter=_none)
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        assert asyncio.run(places_search()) == []

    def test_place_detail_404_503(self, monkeypatch):
        from api.v1 import place_detail
        from serving.places import service as svc_mod

        svc, _, _, _ = _service(rows=[])
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        with pytest.raises(Exception) as exc:
            asyncio.run(place_detail(999))
        assert "404" in str(exc.value)

        os_i = FakeOS()
        os_i.available = False

        async def _none():
            return None

        svc2 = PlaceService(os_index=os_i, cache=PlaceCache(redis_enabled=False), pool_getter=_none)
        monkeypatch.setattr(svc_mod, "_default_service", svc2)
        with pytest.raises(Exception) as exc2:
            asyncio.run(place_detail(999))
        assert "503" in str(exc2.value)

    def test_autocomplete_endpoint(self, monkeypatch):
        from api.v1 import places_autocomplete
        from serving.places import service as svc_mod

        svc, _, _, _ = _service(docs=[_doc(1, name="Nhà thuốc An Tâm")])
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        rows = asyncio.run(places_autocomplete(q="nhà thu"))
        assert rows[0].name == "Nhà thuốc An Tâm"
        assert rows[0].place_id == 1

    def test_reindex_endpoint(self, monkeypatch):
        import serving.places.indexer as idx_mod
        from api.v1 import ReindexRequest, places_reindex
        from storage import pg_client

        class _StubIndexer:
            def __init__(self, pool):
                self.pool = pool

            async def sync(self, **kw):
                return {
                    "status": "done",
                    "mode": "incremental",
                    "scanned": 2,
                    "indexed": 2,
                    "failed": 0,
                    "took_ms": 1,
                }

            async def rebuild(self, **kw):
                return {"status": "done", "mode": "rebuild", "indexed": 0}

            async def reconcile(self, **kw):
                return {"status": "done", "removed": 0}

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        out = asyncio.run(places_reindex(ReindexRequest()))
        assert out.available is False and out.status == "unavailable"

        # stub the indexer so the endpoint never reaches a real OpenSearch
        monkeypatch.setattr(idx_mod, "PlaceIndexer", _StubIndexer)
        pool = FakePool(rows=[_row(1), _row(2)])

        async def _pool():
            return pool

        monkeypatch.setattr(pg_client, "get_pool", _pool)
        out = asyncio.run(places_reindex(ReindexRequest(mode="incremental")))
        assert out.status == "done" and out.detail["scanned"] == 2


# ─── opensearch-py client-param paths ─────────────────────────────────────────
#
# PlaceOSIndex delegates to a sync opensearch-py client (wrapped in
# asyncio.to_thread); these fakes record the kwargs each call receives.


class _ClientShim:
    """Stands in for ``OpenSearchClient`` — only ``_get_client`` is used."""

    def __init__(self, inner):
        self._inner = inner

    def _get_client(self):
        return self._inner


class TestOSIndexParams:
    def _idx(self):
        inner = MagicMock()
        return PlaceOSIndex(client=_ClientShim(inner)), inner

    def test_upsert_docs_bulk_refresh_param(self):
        idx, inner = self._idx()
        inner.bulk.return_value = {"items": [{"index": {"status": 200}}]}
        res = asyncio.run(idx.upsert_docs([_doc(1)]))
        assert res["indexed"] == 1 and res["failed"] == 0
        assert inner.bulk.call_args.kwargs["params"] == {"refresh": "false"}

    def test_delete_refresh_param(self):
        idx, inner = self._idx()
        assert asyncio.run(idx.delete("7")) is True
        assert inner.delete.call_args.kwargs["params"] == {"refresh": "true"}

    def test_delete_ids_params(self):
        idx, inner = self._idx()
        inner.delete_by_query.return_value = {"deleted": 2}
        assert asyncio.run(idx.delete_ids(["1", "2"])) == 2
        assert inner.delete_by_query.call_args.kwargs["params"] == {
            "conflicts": "proceed",
            "refresh": "true",
        }

    def test_all_ids_scrolls_with_param(self):
        idx, inner = self._idx()
        inner.search.return_value = {
            "_scroll_id": "s1",
            "hits": {"hits": [{"_source": {"place_id": "7"}}]},
        }
        inner.scroll.return_value = {"hits": {"hits": []}}
        assert asyncio.run(idx.all_ids()) == {"7"}
        assert inner.scroll.call_args.kwargs["params"] == {"scroll": "2m"}
        inner.clear_scroll.assert_called_once_with(scroll_id="s1")


# ─── canonical address passthrough (P17 fix) ─────────────────────────────────
#
# canonical_places.address held data but every serving lane returned null:
# the document lacked the field, the projection never mapped the column, and
# the response assemblers never emitted it. These pin the field end to end.


class TestAddressServing:
    _ALL_DAY = {
        d: ["00:00–23:59"]
        for d in (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )
    }

    def test_project_row_maps_address(self):
        doc = project_row(_row(11, address="123 Đường Láng, Hà Nội"))
        assert doc.address == "123 Đường Láng, Hà Nội"
        assert project_row(_row(12, address=None)).address is None

    def test_search_rows_carry_address_os_lane(self):
        svc, _, _, _ = _service(docs=[_doc(1, address="1 Bench Ward")])
        rows, meta = asyncio.run(svc.search(q="nhà thuốc"))
        assert meta.lanes == ["opensearch"]
        assert rows[0]["address"] == "1 Bench Ward"

    def test_search_rows_carry_address_postgis_lane(self):
        svc, os_i, _, _ = _service(rows=[_row(5, address="5 Canonical Alley")])
        os_i.available = False
        rows, meta = asyncio.run(svc.search(q="nhà thuốc"))
        assert "postgis" in meta.lanes
        assert rows[0]["address"] == "5 Canonical Alley"

    def test_search_cache_hit_keeps_address(self):
        svc, _, _, _ = _service(docs=[_doc(1, address="1 Bench Ward")])
        kw = {"q": "nhà thuốc"}
        asyncio.run(svc.search(**kw))
        rows, meta = asyncio.run(svc.search(**kw))
        assert meta.cache_hit and meta.lanes == ["cache"]
        assert rows[0]["address"] == "1 Bench Ward"

    def test_detail_payload_carries_address(self):
        svc, _, _, _ = _service(rows=[_row(9, address="9 Canonical Alley")])
        res = asyncio.run(svc.get_place(9))
        assert res.status == "ok" and res.payload["address"] == "9 Canonical Alley"
        # cache hit is a pass-through of the same payload
        res2 = asyncio.run(svc.get_place(9))
        assert res2.meta.cache_hit and res2.payload["address"] == "9 Canonical Alley"

    def test_detail_degraded_index_doc_carries_address(self):
        async def _none():
            return None

        svc = PlaceService(
            os_index=FakeOS(docs=[_doc(9, address="9 Index Rd")]),
            cache=PlaceCache(redis_enabled=False),
            pool_getter=_none,
        )
        res = asyncio.run(svc.get_place(9))
        assert res.status == "ok" and res.payload["degraded"] is True
        assert res.payload["address"] == "9 Index Rd"

    def test_index_source_roundtrips_address(self):
        src = doc_to_index_source(_doc(3, address="3 Index St"))
        assert src["address"] == "3 Index St"
        assert doc_from_index_source(src).address == "3 Index St"
        # pre-fix index docs lack the key — field defaults to None
        src.pop("address")
        assert doc_from_index_source(src).address is None

    def test_open_now_still_bool_when_hours_parse(self):
        # Guard: the address fix must not disturb the P2.0 serve-time
        # hours→open_now derivation on either response shape.
        svc, _, _, _ = _service(docs=[_doc(1, opening_hours=self._ALL_DAY)])
        rows, _ = asyncio.run(svc.search(q="nhà thuốc"))
        assert isinstance(rows[0]["open_now"], bool) and rows[0]["open_now"] is True

        svc2, _, _, _ = _service(rows=[_row(9, opening_hours=self._ALL_DAY)])
        res = asyncio.run(svc2.get_place(9))
        assert isinstance(res.payload["open_now"], bool)
        assert res.payload["open_now"] is True

    def test_places_search_endpoint_returns_address(self, monkeypatch):
        from api.v1 import places_search
        from serving.places import service as svc_mod

        svc, _, _, _ = _service(docs=[_doc(1, address="1 Wire St")])
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        rows = asyncio.run(places_search(q="nhà thuốc"))
        assert rows[0].address == "1 Wire St"

    def test_place_detail_endpoint_returns_address(self, monkeypatch):
        from api.v1 import place_detail
        from serving.places import service as svc_mod

        svc, _, _, _ = _service(rows=[_row(9, address="9 Wire St")])
        monkeypatch.setattr(svc_mod, "_default_service", svc)
        out = asyncio.run(place_detail(9))
        assert out.address == "9 Wire St"


class _StatePool:
    """Async-pool shim recording (sql, args) execute calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


class TestPgIndexState:
    def test_set_docs_indexed_updates_row(self):
        pool = _StatePool()
        asyncio.run(PgIndexState(pool, "places").set_docs_indexed(7))
        assert len(pool.calls) == 2
        assert "INSERT INTO serving_index_state" in pool.calls[0][0]
        assert "docs_indexed = $2" in pool.calls[1][0]
        assert pool.calls[1][1] == ("places", 7)
