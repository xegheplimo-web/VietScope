"""P16 pg-path coverage: PgCanonicalStore, pool-backed runner, /v1 endpoints."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from resolution.match import NormSource, name_score, phone_score, website_score
from resolution.normalize import canonical_category, norm_website
from resolution.provenance import confidence_from, map_canonical, recency
from resolution.runner import run_resolution
from resolution.store import PgCanonicalStore

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _src(**kw) -> NormSource:
    base = {
        "record_id": 1,
        "provider": "osm",
        "external_id": "n1",
        "norm_name": "pho thin",
        "name_sig": "pho thin",
        "tokens": frozenset({"pho", "thin"}),
        "norm_address": "",
        "phone": None,
        "domain": None,
        "category": None,
        "lat": 21.03,
        "lon": 105.85,
        "admin_unit_id": 101,
        "observed_at": NOW,
        "fields": {},
    }
    return NormSource(**{**base, **kw})


def _place_row(**kw) -> dict[str, Any]:
    base = {
        "place_id": 9,
        "business_id": 3,
        "canonical_name": "Phở Thìn",
        "normalized_name": "pho thin",
        "canonical_category": "food",
        "address": "1 Đường X",
        "normalized_address": "1 duong x",
        "phone": "+84901234567",
        "website": "https://pho-thin.vn",
        "website_domain": "pho-thin.vn",
        "opening_hours": {"raw": "8-20"},
        "lat": 21.0301,
        "lon": 105.8501,
        "admin_unit_id": 101,
        "status": "open",
        "confidence": 0.7,
        "source_count": 2,
        "suspicious_merge": False,
        "suspicious_reason": None,
        "rating": None,
        "review_count": None,
        "price_level": None,
        "primary_image_url": None,
        "images": None,
    }
    return {**base, **kw}


class _FakeConn:
    def __init__(self, lock_ok=True, fetch_rows=None):
        self.queries: list[tuple[str, tuple]] = []
        self.lock_ok = lock_ok
        self.fetch_rows = fetch_rows or []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self.fetch_rows)

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return {"ok": self.lock_ok}
        return None

    def transaction(self):
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _AcquireCtx:
    """asyncpg Pool.acquire() — usable both as `async with` and `await`."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False

    def __await__(self):
        async def _c():
            return self._conn

        return _c().__await__()


class _FakePool:
    """Routes SQL by table/keywords to canned rows; records every call."""

    def __init__(
        self,
        *,
        place_rows=None,
        staged_pages=None,
        link_row=None,
        linked_sources=None,
        prov_rows=None,
        prior_run=None,
        policies=None,
        runs=None,
        lock_ok=True,
        link_result="INSERT 0 1",
        stale_links=None,
        cat_mappings=None,
    ):
        self.place_rows = place_rows or []
        self.staged_pages = list(staged_pages or [])
        self.link_row = link_row
        self.linked_sources = linked_sources or []
        self.prov_rows = prov_rows or []
        self.prior_run = prior_run
        self.policies = policies or []
        self.runs = runs or []
        self.link_result = link_result
        self.cat_mappings = cat_mappings or []
        self.conn = _FakeConn(lock_ok, fetch_rows=stale_links)
        self.calls: list[tuple[str, str, tuple]] = []

    def acquire(self):
        return _AcquireCtx(self.conn)

    async def release(self, conn):
        return None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "place_sources" in sql and "JOIN" in sql.upper():
            return self.linked_sources
        if "place_source_records" in sql:
            return self.staged_pages.pop(0) if self.staged_pages else []
        if "source_policies" in sql:
            return self.policies
        if "source_category_mappings" in sql:
            return self.cat_mappings
        if "place_field_provenance" in sql:
            return self.prov_rows
        if "place_sources" in sql:
            return self.linked_sources
        if "resolution_runs" in sql:
            return self.runs
        if "canonical_places" in sql:
            return self.place_rows
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if "RETURNING run_id" in sql:
            return {"run_id": 42}
        if "resolution_runs" in sql:
            return self.prior_run
        if "canonical_businesses" in sql:
            return {"business_id": 3}
        if "canonical_places" in sql:
            if "RETURNING place_id" in sql:
                return {"place_id": 9}
            return self.place_rows[0] if self.place_rows else None
        if "place_sources" in sql:
            if isinstance(self.link_row, list):
                return self.link_row.pop(0) if self.link_row else None
            return self.link_row
        if "pg_try_advisory_lock" in sql:
            return {"ok": True}
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if "place_sources" in sql:
            return self.link_result
        return "INSERT 0 1"


class TestPgStore:
    def test_candidates_builds_blocking_clauses(self):
        pool = _FakePool(place_rows=[_place_row()])
        store = PgCanonicalStore(pool)
        src = _src(
            phone="+84987654321",
            domain="pho-thin.vn",
            tokens=frozenset({"pho", "thin"}),
        )
        rows = asyncio.run(store.candidates(src))
        assert len(rows) == 1 and rows[0].place_id == 9
        sql = pool.calls[0][1]
        assert "phone = $1" in sql
        assert "website_domain = $2" in sql  # indexed equality, not LIKE
        assert "website LIKE" not in sql
        assert "admin_unit_id" in sql
        assert "ST_DWithin" in sql
        # LIMIT can't evict exact-key / nearest candidates
        assert "ORDER BY CASE WHEN phone = $1 THEN 0" in sql
        assert "<->" in sql and "NULLS LAST" in sql
        assert rows[0].domain == "pho-thin.vn"

    def test_candidates_empty_when_no_keys(self):
        pool = _FakePool()
        store = PgCanonicalStore(pool)
        rows = asyncio.run(
            store.candidates(_src(phone=None, domain=None, lat=None, lon=None, admin_unit_id=None))
        )
        assert rows == [] and not pool.calls

    def test_find_place_by_source(self):
        pool = _FakePool(link_row={"place_id": 7})
        store = PgCanonicalStore(pool)
        assert asyncio.run(store.find_place_by_source("osm", "n1")) == 7
        pool2 = _FakePool(link_row=None)
        store2 = PgCanonicalStore(pool2)
        assert asyncio.run(store2.find_place_by_source("osm", "n1")) is None

    def test_find_business_by_name_requires_evidence(self):
        """Brand reuse needs a matching place field, not just the name."""
        pool = _FakePool()
        store = PgCanonicalStore(pool)
        biz = asyncio.run(
            store.find_business_by_name(
                "the gioi di dong",
                category="retail",
                phone="+84901234567",
                website_domain="tgdđ.vn",
                admin_unit_id=101,
            )
        )
        assert biz == 3
        sql, args = pool.calls[0][1], pool.calls[0][2]
        assert "EXISTS" in sql and "canonical_places" in sql
        assert "canonical_category = $2" in sql
        assert "phone = $3" in sql and "website_domain = $4" in sql
        assert "admin_unit_id = $5" in sql
        assert args == ("the gioi di dong", "retail", "+84901234567", "tgdđ.vn", 101)
        # empty name → no query at all
        pool2 = _FakePool()
        assert asyncio.run(PgCanonicalStore(pool2).find_business_by_name("")) is None
        assert not pool2.calls

    def test_create_business_and_place(self):
        pool = _FakePool()
        store = PgCanonicalStore(pool)
        biz = asyncio.run(store.create_business("Phở Thìn", "pho thin"))
        assert biz == 3
        pid = asyncio.run(
            store.create_place(
                {
                    "business_id": 3,
                    "canonical_name": "Phở Thìn",
                    "normalized_name": "pho thin",
                    "canonical_category": "food",
                    "address": "1 Đường X",
                    "normalized_address": "1 duong x",
                    "phone": "+84901234567",
                    "website": "https://pho-thin.vn",
                    "opening_hours": {"raw": "8-20"},
                    "lat": 21.03,
                    "lon": 105.85,
                    "admin_unit_id": 101,
                    "status": "open",
                    "confidence": 0.0,
                    "source_count": 1,
                    "resolution_run_id": 42,
                }
            )
        )
        assert pid == 9
        sql = [c for c in pool.calls if "canonical_places" in c[1]][0][1]
        assert "ST_MakePoint" in sql

    def test_update_place_with_location(self):
        pool = _FakePool()
        store = PgCanonicalStore(pool)
        asyncio.run(
            store.update_place(
                9,
                {
                    "canonical_name": "New",
                    "lat": 21.1,
                    "lon": 105.9,
                    "confidence": 0.8,
                    "evil_column": "x",
                },
            )
        )
        sql = pool.calls[0][1]
        assert "canonical_name = $" in sql and "ST_MakePoint" in sql
        assert "evil_column" not in sql
        assert "updated_at = now()" in sql
        # no allowed fields → no execute
        pool2 = _FakePool()
        store2 = PgCanonicalStore(pool2)
        asyncio.run(store2.update_place(9, {"unknown_col": 1}))
        assert not pool2.calls

    def test_link_source_and_sources_for(self):
        pool = _FakePool()
        store = PgCanonicalStore(pool)
        assert asyncio.run(store.link_source(9, 1, "osm", "n1", 42)) is True
        linked = [
            {
                "id": 1,
                "provider": "osm",
                "external_id": "n1",
                "raw_name": "Phở Thìn",
                "raw_address": "1 Đường X",
                "raw_phone": None,
                "raw_website": None,
                "raw_category": None,
                "raw_hours": None,
                "raw_status": "CLOSED_PERMANENTLY",
                "lat": 21.03,
                "lon": 105.85,
                "admin_unit_id": 101,
                "observed_at": NOW,
            }
        ]
        pool2 = _FakePool(linked_sources=linked)
        rows = asyncio.run(PgCanonicalStore(pool2).sources_for(9))
        assert rows[0]["raw_name"] == "Phở Thìn"
        assert rows[0]["raw_status"] == "CLOSED_PERMANENTLY"
        sql = pool2.calls[0][1]
        assert "raw_status" in sql  # contributors must carry the status field

    def test_write_provenance_clears_then_upserts(self):
        from resolution.store import ProvRow

        pool = _FakePool()
        store = PgCanonicalStore(pool)
        asyncio.run(
            store.write_provenance(
                [
                    ProvRow(
                        place_id=9,
                        field="phone",
                        source_record_id=1,
                        provider="osm",
                        value="+84901234567",
                        weight=0.9,
                        observed_at=NOW,
                        chosen=True,
                    )
                ]
            )
        )
        sqls = [q for q, _ in pool.conn.queries]
        assert any("chosen = false" in s for s in sqls)
        assert any("ON CONFLICT (place_id, field, source_record_id)" in s for s in sqls)
        # empty list → no-op, no acquire
        pool2 = _FakePool()
        asyncio.run(PgCanonicalStore(pool2).write_provenance([]))
        assert not pool2.conn.queries

    def test_get_place_and_search(self):
        pool = _FakePool(place_rows=[_place_row()])
        store = PgCanonicalStore(pool)
        p = asyncio.run(store.get_place(9))
        assert p is not None and p.tokens == frozenset({"pho", "thin"})
        pool2 = _FakePool(place_rows=[])
        assert asyncio.run(PgCanonicalStore(pool2).get_place(9)) is None

        pool3 = _FakePool(place_rows=[_place_row()])
        rows = asyncio.run(
            PgCanonicalStore(pool3).search_places(
                q="quán phở",
                lat=21.0,
                lon=105.8,
                radius_m=5000,
                admin_unit_id=101,
                category="food",
                limit=5,
            )
        )
        assert len(rows) == 1
        sql, args = pool3.calls[0][1], pool3.calls[0][2]
        assert "normalized_name LIKE" in sql
        assert "ST_DWithin" in sql
        assert "ORDER BY location <->" in sql
        assert "permanently_closed" in sql  # closures must not be served
        assert args[-1] == 5


class TestPgRunner:
    def test_full_run_creates_and_merges(self):
        pool = _FakePool(
            staged_pages=[
                [
                    {
                        "id": 1,
                        "provider": "osm",
                        "external_id": "n1",
                        "raw_name": "Phở Thìn",
                        "raw_address": "1 Đường X",
                        "raw_phone": "0901 234 567",
                        "raw_website": "pho-thin.vn",
                        "raw_category": "restaurant",
                        "raw_hours": {"raw": "8-20"},
                        "lat": 21.03,
                        "lon": 105.85,
                        "admin_unit_id": 101,
                        "observed_at": NOW,
                    }
                ],
                [],
            ],
            policies=[{"provider": "osm", "authority": {"name": 0.8}}],
        )
        out = asyncio.run(run_resolution(pool, batch_size=10))
        assert out["status"] == "done" and out["run_id"] == 42
        assert out["created"] == 1 and out["cursor"] == 1
        # run row created, checkpointed, finalized
        execs = [c for c in pool.calls if c[0] == "execute"]
        assert any("UPDATE resolution_runs SET cursor" in c[1] for c in execs)
        assert any("status = $2, completed_at" in c[1] for c in execs)
        # place_sources link inserted
        assert any("INSERT INTO place_sources" in c[1] for c in execs)

    def test_provider_filter_rewrites_scan(self):
        pool = _FakePool(staged_pages=[[]])
        asyncio.run(run_resolution(pool, provider="google_maps"))
        scan = [c for c in pool.calls if "place_source_records" in c[1]][0]
        assert "provider = $3" in scan[1]
        assert scan[2][-1] == "google_maps"

    def test_resume_of_loads_prior_cursor(self):
        prior = {
            "parameters": json.dumps({"provider": "osm", "since_id": 0}),
            "cursor": 77,
            "checkpoint": None,
        }
        pool = _FakePool(prior_run=prior, staged_pages=[[]])
        out = asyncio.run(run_resolution(pool, resume_of=5))
        assert out["resume_of"] == 5
        # resumed scan starts at prior cursor
        scan = [c for c in pool.calls if "place_source_records" in c[1]][0]
        assert scan[2][0] == 77

    def test_resume_missing_prior_raises(self):
        pool = _FakePool(prior_run=None)
        with pytest.raises(ValueError):
            asyncio.run(run_resolution(pool, resume_of=999))

    def test_resume_keeps_stored_provider(self):
        """Resume with no explicit provider must not clobber the stored one."""
        prior = {
            "parameters": {"provider": "osm", "since_id": 0},
            "cursor": 5,
            "checkpoint": None,
        }
        pool = _FakePool(prior_run=prior, staged_pages=[[]])
        asyncio.run(run_resolution(pool, resume_of=5))
        scan = [c for c in pool.calls if "place_source_records" in c[1]][0]
        assert scan[2][-1] == "osm"  # provider arg still applied

    def test_advisory_lock_busy_raises(self):
        pool = _FakePool(lock_ok=False, staged_pages=[[]])
        with pytest.raises(RuntimeError):
            asyncio.run(run_resolution(pool))

    def test_link_conflict_folds_into_winner(self):
        """Lost link race: provenance lands on the winner's place, not the orphan."""
        pool = _FakePool(
            staged_pages=[
                [
                    {
                        "id": 1,
                        "provider": "osm",
                        "external_id": None,
                        "raw_name": "Phở Thìn",
                        "raw_address": None,
                        "raw_phone": None,
                        "raw_website": None,
                        "raw_category": None,
                        "raw_hours": None,
                        "lat": None,
                        "lon": None,
                        "admin_unit_id": None,
                        "observed_at": NOW,
                    }
                ],
                [],
            ],
            # first lookup: no link; after failed INSERT: winner found
            link_row=[None, {"place_id": 7}],
            link_result="INSERT 0 0",
        )
        asyncio.run(run_resolution(pool))
        updates = [
            c
            for c in pool.calls
            if c[0] == "execute" and "UPDATE canonical_places" in c[1] and "WHERE place_id" in c[1]
        ]
        assert updates and all(c[2][-1] == 7 for c in updates)

    def test_category_mappings_overlay_and_unknown_log(self):
        """DB mappings resolve labels built-ins can't; unmapped raw labels
        upsert into unknown_source_categories for ops review."""

        def staged(i, cat):
            return {
                "id": i,
                "provider": "google_maps",
                "external_id": f"g{i}",
                "raw_name": f"Place {i}",
                "raw_address": "1 Đường X",
                "raw_phone": None,
                "raw_website": None,
                "raw_category": cat,
                "raw_hours": None,
                "lat": 21.03 + i * 0.2,
                "lon": 105.85,
                "admin_unit_id": 101,
                "observed_at": NOW,
            }

        pool = _FakePool(
            cat_mappings=[
                {
                    "provider": "google_maps",
                    "category_key": "quay thuoc",
                    "canonical_category": "health",
                }
            ],
            staged_pages=[
                [staged(1, "Quầy thuốc"), staged(2, "Tiệm vàng")],
                [],
            ],
        )
        out = asyncio.run(run_resolution(pool))
        assert out["status"] == "done" and out["unknown_categories"] == 1
        # the mapping table was consulted
        assert any(c[0] == "fetch" and "source_category_mappings" in c[1] for c in pool.calls)
        # 'quầy thuốc' resolved to health via the DB row on its place insert
        inserts = [c for c in pool.calls if "INSERT INTO canonical_places" in c[1]]
        assert "health" in inserts[0][2]
        # 'tiệm vàng' mapped nowhere → one idempotent upsert
        execs = [c for c in pool.calls if c[0] == "execute"]
        upserts = [c for c in execs if "unknown_source_categories" in c[1]]
        assert len(upserts) == 1
        assert upserts[0][2][0] == "google_maps"
        assert upserts[0][2][1] == "tiem vang"
        assert upserts[0][2][2] == "Tiệm vàng"
        assert upserts[0][2][3] == 1

    def test_relink_stale_drops_tainted_places(self):
        """--relink-stale rebuild: links from a different matcher_version get
        their whole place dropped (provenance, links, place — FK order), so
        the records re-resolve under the current matcher."""
        pool = _FakePool(staged_pages=[[]], stale_links=[{"place_id": 5}])
        out = asyncio.run(run_resolution(pool, relink_stale=True))
        assert out["status"] == "done" and out["rebuilt_places"] == 1
        conn_sql = [q for q, _ in pool.conn.queries]
        prov_i = next(i for i, q in enumerate(conn_sql) if "place_field_provenance" in q)
        link_i = next(i for i, q in enumerate(conn_sql) if "DELETE FROM place_sources" in q)
        place_i = next(i for i, q in enumerate(conn_sql) if "DELETE FROM canonical_places" in q)
        assert prov_i < link_i < place_i  # FK-safe order

    def test_relink_stale_off_by_default(self):
        pool = _FakePool(staged_pages=[[]])
        out = asyncio.run(run_resolution(pool))
        assert out["rebuilt_places"] == 0
        conn_sql = [q for q, _ in pool.conn.queries]
        assert not any("DELETE FROM" in q for q in conn_sql)

    def test_failed_batch_marks_run_failed(self):
        class _BoomPool(_FakePool):
            async def fetch(self, sql, *args):
                if "place_source_records" in sql:
                    raise RuntimeError("scan exploded")
                return await super().fetch(sql, *args)

        out = asyncio.run(run_resolution(_BoomPool()))
        assert out["status"] == "failed"
        assert "scan exploded" in out["errors"]["fatal"]


class TestSmallBranches:
    def test_name_score_empty_inputs(self):
        assert name_score("", frozenset(), "x", frozenset({"x"})) == 0.3
        assert name_score("a", frozenset(), "b", frozenset()) == 0.3
        assert name_score("abc", frozenset(), "abd", frozenset()) == 0.3

    def test_phone_website_scores(self):
        assert phone_score(None, "+1") == 0.3
        assert phone_score("+1", "+2") == 0.0
        assert website_score(None, "a.vn") == 0.3
        assert website_score("a.vn", "b.vn") == 0.0

    def test_recency_edges(self):
        assert recency(None) == 0.5
        assert recency("not a datetime", NOW) == 0.5
        assert recency(NOW - timedelta(days=3650), NOW) < 0.001
        assert recency(NOW, NOW) == 1.0

    def test_map_canonical_location(self):
        out = map_canonical({"location": {"lat": 1.0, "lon": 2.0}, "name": "A"})
        assert out["lat"] == 1.0 and out["lon"] == 2.0
        # location not dict → skipped
        assert map_canonical({"location": "x"}) == {}

    def test_norm_website_adds_scheme(self):
        assert norm_website("pho-thin.vn/x").startswith("https://")
        assert norm_website("") is None

    def test_canonical_category_types_list(self):
        assert canonical_category(None, {"types": ["convenience_store"]}) == "retail"

    def test_confidence_from_empty(self):
        assert confidence_from([]) == 0.0


class TestApiEndpoints:
    def test_resolve_unavailable_without_pool(self, monkeypatch):
        from api.v1 import ResolveRequest, resolve_sources
        from storage import pg_client

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        out = asyncio.run(resolve_sources(ResolveRequest()))
        assert out.available is False and out.status == "unavailable"

    def test_resolve_runs_with_pool(self, monkeypatch):
        from api.v1 import ResolveRequest, resolve_sources
        from storage import pg_client

        async def _pool():
            return object()

        async def _fake_run(pool, **kw):
            return {
                "run_id": 5,
                "status": "done",
                "resume_of": None,
                "resolver_version": "p16-v1",
                "cursor": 3,
                "scanned": 3,
                "scored": 2,
                "created": 2,
                "merged": 1,
                "fields_written": 9,
                "errors": {},
            }

        import resolution.runner as rr

        monkeypatch.setattr(pg_client, "get_pool", _pool)
        monkeypatch.setattr(rr, "run_resolution", _fake_run)
        out = asyncio.run(resolve_sources(ResolveRequest(provider="osm")))
        assert out.run_id == 5 and out.merged == 1 and out.available is True

    def test_places_search_and_detail(self, monkeypatch):
        import api.v1 as api_v1
        from api.v1 import place_detail, places_search
        from serving.places.cache import PlaceCache
        from serving.places.os_index import PlaceIndexUnavailable
        from serving.places.service import PlaceService
        from storage import pg_client

        class _DeadOS:
            """Pin the OpenSearch lane down so the test stays off whatever
            live index the host happens to have populated."""

            available = False

            async def search(self, *a, **k):
                raise PlaceIndexUnavailable("down")

            async def autocomplete(self, *a, **k):
                raise PlaceIndexUnavailable("down")

            async def get_doc(self, *a, **k):
                return None

        # Pool resolves through pg_client.get_pool per call, so patching it
        # here covers both service paths below.
        svc = PlaceService(os_index=_DeadOS(), cache=PlaceCache(redis_enabled=False))
        monkeypatch.setattr(api_v1, "_get_places_service", lambda: svc)

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        assert asyncio.run(places_search()) == []
        with pytest.raises(Exception) as exc:
            asyncio.run(place_detail(1))
        assert "503" in str(exc.value)

        pool = _FakePool(
            place_rows=[_place_row()],
            prov_rows=[
                {
                    "field": "phone",
                    "provider": "google_maps",
                    "value": "+84901234567",
                    "weight": 0.9,
                    "observed_at": NOW,
                    "chosen": True,
                }
            ],
            linked_sources=[
                {
                    "provider": "osm",
                    "external_id": "n1",
                    "source_record_id": 1,
                    "linked_at": NOW,
                }
            ],
        )

        async def _pool():
            return pool

        monkeypatch.setattr(pg_client, "get_pool", _pool)
        rows = asyncio.run(places_search(q="phở", lat=21.0, lon=105.8))
        assert len(rows) == 1 and rows[0].canonical_name == "Phở Thìn"

        detail = asyncio.run(place_detail(9))
        assert detail.sources[0]["provider"] == "osm"
        assert detail.provenance[0].field == "phone"

        empty = _FakePool(place_rows=[])

        async def _empty_pool():
            return empty

        monkeypatch.setattr(pg_client, "get_pool", _empty_pool)
        with pytest.raises(Exception) as exc:
            asyncio.run(place_detail(404))
        assert "404" in str(exc.value)

    def test_resolution_runs_endpoint(self, monkeypatch):
        from api.v1 import resolution_runs
        from storage import pg_client

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        assert asyncio.run(resolution_runs()) == []

        pool = _FakePool(
            runs=[
                {
                    "run_id": 42,
                    "status": "done",
                    "parameters": {"provider": "osm"},
                    "resolver_version": "p16-v1",
                    "resume_of": None,
                    "started_at": NOW,
                    "completed_at": NOW,
                    "records_scanned": 10,
                    "pairs_scored": 5,
                    "places_created": 3,
                    "places_merged": 2,
                    "fields_written": 20,
                    "cursor": 10,
                    "error_summary": {},
                }
            ]
        )

        async def _pool():
            return pool

        monkeypatch.setattr(pg_client, "get_pool", _pool)
        out = asyncio.run(resolution_runs(limit=5))
        assert out[0].run_id == 42 and out[0].records_scanned == 10
        limit_arg = [c for c in pool.calls if "resolution_runs" in c[1]][0][2][0]
        assert limit_arg == 5
