"""P14A — Vietnam administrative graph regression tests.

Covers the versioned gazetteer (db/seeds/vn_admin_units.json), the
resolver (core/geo_resolver.py) and the schema (db/migrations/006).

The dataset models the post-2025 two-level system (34 province-level
units -> commune-level xã/phường/đặc khu, effective 2025-07-01) while
preserving the pre-2025 three-level geography as historical units linked
by transition edges (merged_into / split_into / renamed_to / replaced_by).
"""

from __future__ import annotations

from collections import Counter

import pytest
from core.geo_resolver import GeoResolver, norm_text
from db import migrate
from storage.admin_store import (
    AdminGraph,
    AdminUnit,
    DictAdminStore,
    graph_from_seed,
    load_seed,
)


@pytest.fixture(scope="module")
def seed() -> dict:
    return load_seed()


@pytest.fixture(scope="module")
def resolver(seed) -> GeoResolver:
    return GeoResolver(graph_from_seed(seed))


def _new(seed):
    return [u for u in seed["units"] if u["key"].startswith("new:")]


def _old(seed):
    return [u for u in seed["units"] if u["key"].startswith("old:")]


# ── seed integrity ────────────────────────────────────────────────────────────


class TestSeedIntegrity:
    def test_province_count_is_derived_not_hardcoded(self, seed):
        """The 2025 count is data, not a constant in code."""
        provs = [u for u in _new(seed) if u["admin_level"] == 1]
        assert len(provs) == 34

    def test_commune_level_set(self, seed):
        comms = [u for u in _new(seed) if u["admin_level"] == 3]
        assert len(comms) == 3321
        types = Counter(u["type"] for u in comms)
        # Official per the 2025 catalogue (Công văn 1027/CTK-CSCL).
        assert types["xa"] == 2621
        assert types["phuong"] == 687
        assert types["dac_khu"] == 13

    def test_no_district_level_in_current_era(self, seed):
        assert all(u["admin_level"] != 2 for u in _new(seed))

    def test_historical_era_preserved(self, seed):
        old = _old(seed)
        assert len([u for u in old if u["admin_level"] == 1]) == 63
        assert len([u for u in old if u["admin_level"] == 2]) >= 690
        assert len([u for u in old if u["admin_level"] == 3]) >= 10000
        # Nothing historical is deleted — statuses differ by era only.
        assert all(u["status"] == "historical" for u in old)
        assert all(u["status"] == "current" for u in _new(seed))

    def test_code_reuse_across_eras(self, seed):
        """2025 reassigned codes: uniqueness is (code, era), never code."""
        by_code: dict[str, list[str]] = {}
        for u in seed["units"]:
            by_code.setdefault(u["code"], []).append(u["key"])
        reused = [c for c, ks in by_code.items() if len(ks) > 1]
        assert "24" in reused  # new Bắc Ninh took old Bắc Giang's code
        for code in reused:
            assert {k.split(":")[0] for k in by_code[code]} == {"new", "old"}

    def test_validity_windows(self, seed):
        for u in _new(seed):
            assert u["valid_from"] == "2025-07-01"
            assert u["valid_to"] is None
        for u in _old(seed):
            assert u["valid_to"] in ("2024-12-31", "2025-06-30")

    def test_every_commune_reaches_a_province(self, seed):
        parents = {u["key"]: u.get("parent_key") for u in seed["units"]}
        levels = {u["key"]: u["admin_level"] for u in seed["units"]}
        for u in _new(seed):
            key, seen = u["key"], set()
            while levels[key] != 1:
                key = parents[key]
                assert key is not None and key not in seen
                seen.add(key)

    def test_relation_endpoints_and_provenance(self, seed):
        keys = {u["key"] for u in seed["units"]}
        assert seed["relations"]
        for r in seed["relations"]:
            assert r["from_key"] in keys and r["to_key"] in keys
            assert r["relation_type"] in {
                "merged_into",
                "split_into",
                "renamed_to",
                "replaced_by",
                "boundary_changed",
            }
            assert r["effective_date"]
            assert r["source"]  # legal-source provenance on every edge

    def test_relations_only_run_old_to_new(self, seed):
        for r in seed["relations"]:
            assert r["from_key"].startswith("old:")
            assert r["to_key"].startswith(("new:", "old:"))
            if r["to_key"].startswith("old:"):
                # wave-1 (Jan-2025) intermediate hops only
                assert r["effective_date"] <= "2025-06-30"

    def test_province_merges_present(self, seed):
        merged_from = {
            r["from_key"]
            for r in seed["relations"]
            if r["relation_type"] == "merged_into" and seed_key_level(seed, r["from_key"]) == 1
        }
        # 52 old provinces folded into the 34 (63 - 34 + renamed).
        assert len(merged_from) >= 28

    def test_aliases_cover_name_variants(self, seed):
        norm = {a["normalized_alias"] for a in seed["aliases"]}
        for probe in ("sai gon", "tphcm", "hanoi", "thua thien hue"):
            assert probe in norm
        types = {a["alias_type"] for a in seed["aliases"]}
        assert {"historical", "abbreviation", "english"} <= types

    def test_city_hints_folded_and_current(self, seed):
        hints = seed["city_hints"]
        assert hints["bac giang"] == "Bắc Ninh"
        assert hints["sai gon"] == "Thành phố Hồ Chí Minh"
        assert hints["hue"] == "Thành phố Huế"


def seed_key_level(seed, key):
    return next(u["admin_level"] for u in seed["units"] if u["key"] == key)


# ── resolver: current addresses ──────────────────────────────────────────────


class TestCurrentResolution:
    def test_current_commune_and_province(self, resolver):
        res = resolver.resolve("phường Tân Tiến, thành phố Bắc Giang")
        assert res.status == "resolved"
        provs = {u.key for u in res.provinces}
        assert provs == {"new:24"}  # Bắc Ninh
        assert "new:07699" in {u.key for u in res.communes}  # Phường Tân Tiến

    def test_hcmc_current_commune(self, resolver):
        res = resolver.resolve("Phường Bến Nghé, TP. Hồ Chí Minh")
        assert res.status == "resolved"
        assert "new:79" in {u.key for u in res.provinces}

    def test_accent_insensitive(self, resolver):
        res = resolver.resolve("quan hoan kiem, ha noi")
        assert res.status == "resolved"
        assert "new:01" in {u.key for u in res.provinces}

    def test_alias_resolution(self, resolver):
        for probe in ("Sài Gòn", "TPHCM", "TP. HCM"):
            res = resolver.resolve(probe)
            assert "new:79" in {u.key for u in res.provinces}, probe

    def test_bare_name_lookup(self, resolver):
        res = resolver.resolve_name("Bắc Ninh")
        assert res.status == "resolved"
        assert "new:24" in {u.key for u in res.current}


# ── resolver: historical addresses ───────────────────────────────────────────


class TestHistoricalResolution:
    def test_merged_province(self, resolver):
        """tỉnh Bình Phước (dissolved) → Đồng Nai."""
        res = resolver.resolve("tỉnh Bình Phước")
        assert res.status == "resolved"
        assert "old:70" in {u.key for u in res.matched}
        assert res.current[0].key == "new:75"
        assert any(e.relation_type == "merged_into" for e in res.path)

    def test_renamed_province(self, resolver):
        """Thừa Thiên Huế → Thành phố Huế via renamed_to."""
        res = resolver.resolve("Thừa Thiên Huế")
        assert res.status == "resolved"
        assert "old:46" in {u.key for u in res.matched}
        assert "new:46" in {u.key for u in res.current}
        assert any(e.relation_type == "renamed_to" for e in res.path)

    def test_former_district_address(self, resolver):
        """'huyện Yên Dũng, tỉnh Bắc Giang' → new Bắc Ninh communes.

        The wave-1 dissolved district keeps both hops: merged_into the old
        host city (TP Bắc Giang, eff. 2025-01-01) and split_into the new
        communes (eff. 2025-07-01).
        """
        res = resolver.resolve("huyện Yên Dũng, tỉnh Bắc Giang")
        assert res.status == "resolved"
        keys = {u.key for u in res.matched}
        assert {"old:221", "old:24"} <= keys  # huyện + tỉnh, historical
        assert "new:24" in {u.key for u in res.provinces}
        assert len(res.communes) >= 6  # Yên Dũng's six successor communes
        edge_types = {e.relation_type for e in res.path}
        assert {"merged_into", "split_into"} <= edge_types
        assert any(e.from_key == "old:221" and e.effective_date == "2025-01-01" for e in res.path)

    def test_dissolved_commune_parent_fallback(self, resolver):
        """Commune dissolved in the Dec-2024 wave rides its district edges."""
        res = resolver.resolve("xã Lão Hộ, huyện Yên Dũng")
        assert res.status in ("resolved", "ambiguous")
        assert "new:24" in {u.key for u in res.provinces}
        assert res.communes  # reached the current era

    def test_old_city_split(self, resolver):
        """quận Hoàn Kiếm (dissolved) → its successor phường."""
        res = resolver.resolve("Quận Hoàn Kiếm, Hà Nội")
        assert res.status == "resolved"
        assert "new:01" in {u.key for u in res.provinces}
        assert "new:00070" in {u.key for u in res.communes}  # phường Hoàn Kiếm

    def test_code_lookup_current_and_historical(self, resolver):
        """The same code means different units in different eras."""
        cur = resolver.resolve_name("Bắc Ninh")
        assert "new:24" in {u.key for u in cur.current}
        hist = resolver.resolve("tỉnh Bắc Giang")
        assert "old:24" in {u.key for u in hist.matched}
        assert "new:24" in {u.key for u in hist.provinces}


# ── resolver: ambiguity ────────────────────────────────────────────────────────


class TestAmbiguity:
    def test_same_name_many_places_is_ambiguous(self, resolver):
        """'Châu Thành' exists in several provinces — no context → ambiguous."""
        res = resolver.resolve("Châu Thành")
        assert res.status == "ambiguous"
        assert len(res.ambiguity) > 1

    def test_province_context_disambiguates(self, resolver):
        """'Châu Thành' exists in ~10 old districts; the province narrows it."""
        res = resolver.resolve("huyện Châu Thành, Bến Tre")
        assert res.status == "resolved"
        assert "old:831" in {u.key for u in res.matched}  # Bến Tre's district
        assert {u.key for u in res.provinces} == {"new:86"}  # Vĩnh Long

    def test_unresolvable_returns_not_found(self, resolver):
        res = resolver.resolve("Zzyzx nowhere land")
        assert res.status == "not_found"
        assert not res.current


# ── DictAdminStore ────────────────────────────────────────────────────────────


class TestDictAdminStore:
    def test_geometry_point_lookup(self):
        """Point → containing units, deepest admin_level first."""
        geom = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]]],
        }
        prov = AdminUnit(
            key="new:test-prov",
            unit_id=None,
            code="TP",
            name="Tỉnh Test",
            normalized_name="tinh test",
            type="tinh",
            admin_level=1,
            parent_key=None,
            valid_from="2025-07-01",
            valid_to=None,
            status="current",
            source="test",
            geometry=geom,
        )
        comm = AdminUnit(
            key="new:test-comm",
            unit_id=None,
            code="TC",
            name="Xã Test",
            normalized_name="xa test",
            type="xa",
            admin_level=3,
            parent_key="new:test-prov",
            valid_from="2025-07-01",
            valid_to=None,
            status="current",
            source="test",
            geometry=geom,
        )
        store = DictAdminStore(AdminGraph(units={u.key: u for u in (prov, comm)}))
        import asyncio

        hits = asyncio.run(store.units_containing(1.0, 1.0))
        assert [u.key for u in hits] == ["new:test-comm", "new:test-prov"]
        assert asyncio.run(store.units_containing(5.0, 5.0)) == []

    def test_seed_store_geometry_is_current_era_only(self, seed):
        """P14B: current units carry boundaries; historical stay NULL."""
        store = DictAdminStore.from_seed()
        units = store._graph.units.values()
        assert all((u.geometry is not None) == (u.status == "current") for u in units)


# ── migration ─────────────────────────────────────────────────────────────────


class TestMigration006:
    def test_admin_graph_migration_shape(self):
        files = dict(migrate.migration_files())
        sql = files["006_admin_graph"].read_text(encoding="utf-8")
        # era-scoped code uniqueness replaces the naive UNIQUE(code)
        assert "administrative_units_code_key" in sql  # dropped
        assert "COALESCE(valid_from, DATE '0001-01-01')" in sql
        # transition graph
        assert "administrative_relations" in sql
        for t in ("merged_into", "split_into", "renamed_to"):
            assert t in sql or t in files["006_admin_graph"].read_text(encoding="utf-8")
        # stable FK for businesses
        assert "admin_unit_id" in sql
        # historical units are never deleted — no DROP/DELETE of units
        assert "DROP TABLE administrative_units" not in sql

    def test_additive_only(self):
        files = dict(migrate.migration_files())
        sql = files["006_admin_graph"].read_text(encoding="utf-8")
        assert "IF NOT EXISTS" in sql  # reruns are safe


# ── norm_text ─────────────────────────────────────────────────────────────────


def test_norm_text_strips_types_and_accents():
    assert norm_text("Huyện Yên Dũng") == "yen dung"
    assert norm_text("Thành phố Hồ Chí Minh") == "ho chi minh"
    assert norm_text("xã  Cảnh   Thụy") == "canh thuy"


# ── stores / loader coverage ─────────────────────────────────────────────────


class TestGraphFromRows:
    def test_rows_rebuild_graph_with_keys_and_parents(self):
        from storage.admin_store import graph_from_rows

        unit_rows = [
            {
                "unit_id": 1,
                "code": "1",
                "name": "TP A",
                "normalized_name": "tp a",
                "type": "thanh_pho",
                "admin_level": 1,
                "parent_id": None,
                "valid_from": "2025-07-01",
                "valid_to": None,
                "status": "current",
                "source": "s",
            },
            {
                "unit_id": 2,
                "code": "3",
                "name": "Xã C",
                "normalized_name": "xa c",
                "type": "xa",
                "admin_level": 3,
                "parent_id": 1,
                "valid_from": "2025-07-01",
                "valid_to": None,
                "status": "current",
                "source": "s",
            },
            {
                "unit_id": 3,
                "code": "2",
                "name": "Tỉnh B",
                "normalized_name": "tinh b",
                "type": "tinh",
                "admin_level": 1,
                "parent_id": None,
                "valid_from": None,
                "valid_to": "2025-06-30",
                "status": "historical",
                "source": "s",
            },
        ]
        g = graph_from_rows(
            unit_rows,
            [
                {
                    "unit_id": 2,
                    "alias": "Xa C",
                    "normalized_alias": "xa c",
                    "alias_type": "alternate",
                    "valid_from": None,
                    "valid_to": None,
                },
                {"unit_id": 99, "alias": "orphan", "normalized_alias": "", "alias_type": "x"},
            ],
            [
                {
                    "from_unit_id": 3,
                    "to_unit_id": 1,
                    "relation_type": "merged_into",
                    "effective_date": "2025-07-01",
                    "source": "s",
                },
                {
                    "from_unit_id": 3,
                    "to_unit_id": 99,
                    "relation_type": "merged_into",
                    "effective_date": None,
                    "source": None,
                },
            ],
        )
        assert set(g.units) == {"new:1", "new:3", "old:2"}
        assert g.units["new:3"].parent_key == "new:1"
        assert g.children["new:1"] == ["new:3"]
        assert [a.unit_key for a in g.aliases] == ["new:3"]  # orphan alias skipped
        assert len(g.relations) == 1  # relation with missing endpoint skipped
        assert g.relations[0].from_key == "old:2"


class TestPgAdminStoreDegrade:
    def test_no_dsn_returns_empty(self, monkeypatch):
        import asyncio

        from storage import pg_client
        from storage.admin_store import PgAdminStore

        monkeypatch.setattr(pg_client, "database_url", lambda: "")
        store = PgAdminStore()
        assert asyncio.run(store.graph()) is None
        assert asyncio.run(store.units_containing(10.0, 106.0)) == []


class TestDictAdminStoreGeometry:
    def test_multipolygon_and_status_filter(self):
        import asyncio

        geom = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]]],
                [[[10, 10], [10, 12], [12, 12], [12, 10], [10, 10]]],
            ],
        }
        inside = AdminUnit(
            key="new:m",
            unit_id=None,
            code="M",
            name="Multi",
            normalized_name="multi",
            type="xa",
            admin_level=3,
            parent_key=None,
            valid_from=None,
            valid_to=None,
            status="current",
            source="t",
            geometry=geom,
        )
        stale = AdminUnit(
            key="old:s",
            unit_id=None,
            code="S",
            name="Stale",
            normalized_name="stale",
            type="xa",
            admin_level=3,
            parent_key=None,
            valid_from=None,
            valid_to=None,
            status="historical",
            source="t",
            geometry=geom,
        )
        store = DictAdminStore(AdminGraph(units={u.key: u for u in (inside, stale)}))
        assert [u.key for u in asyncio.run(store.units_containing(11.0, 11.0))] == ["new:m"]
        assert asyncio.run(store.graph()).units["old:s"].name == "Stale"


# ── db.seed_admin (fake asyncpg conn) ────────────────────────────────────────


def _mini_seed() -> dict:
    return {
        "generated_at": "2025-07-01",
        "units": [
            {
                "key": "new:1",
                "code": "1",
                "name": "TP A",
                "normalized_name": "tp a",
                "type": "thanh_pho",
                "admin_level": 1,
                "parent_key": None,
                "valid_from": "2025-07-01",
                "status": "current",
                "source": "s",
            },
            {
                "key": "old:2",
                "code": "2",
                "name": "Tỉnh B",
                "normalized_name": "tinh b",
                "type": "tinh",
                "admin_level": 1,
                "parent_key": None,
                "valid_from": None,
                "valid_to": "2025-06-30",
                "status": "historical",
                "source": "s",
            },
            {
                "key": "new:3",
                "code": "3",
                "name": "Xã C",
                "normalized_name": "xa c",
                "type": "xa",
                "admin_level": 3,
                "parent_key": "new:1",
                "valid_from": "2025-07-01",
                "status": "current",
                "source": "s",
            },
        ],
        "aliases": [
            {
                "unit_key": "new:1",
                "alias": "TP A alias",
                "normalized_alias": "tp a alias",
                "alias_type": "alternate",
            },
            {"unit_key": "ghost", "alias": "orphan"},
        ],
        "relations": [
            {
                "from_key": "old:2",
                "to_key": "new:1",
                "relation_type": "merged_into",
                "effective_date": "2025-07-01",
                "source": "s",
            },
            {"from_key": "ghost", "to_key": "new:1", "relation_type": "merged_into"},
        ],
    }


class _FakeConn:
    """Minimal asyncpg stand-in: recorded fetches/executemany/execute."""

    def __init__(self, fetch_results):
        self._fetch_results = list(fetch_results)
        self.batches: list[tuple[str, list]] = []
        self.stmts: list[tuple[str, tuple]] = []

    async def fetch(self, sql):
        return self._fetch_results.pop(0)

    async def executemany(self, sql, params):
        self.batches.append((sql, list(params)))

    async def execute(self, sql, *args):
        self.stmts.append((sql, args))
        return "INSERT 0 1"


def _id_rows():
    return [
        {"unit_id": 1, "code": "1", "vf": "2025-07-01"},
        {"unit_id": 2, "code": "2", "vf": "0001-01-01"},
        {"unit_id": 3, "code": "3", "vf": "2025-07-01"},
    ]


class TestSeedAdmin:
    def test_fresh_seed_inserts_everything(self):
        import asyncio

        from db.seed_admin import seed_admin

        conn = _FakeConn([[], _id_rows()])
        stats = asyncio.run(seed_admin(conn, _mini_seed()))
        assert stats["units_total"] == 3 and stats["units_inserted"] == 3
        assert stats["relations_inserted"] == 1  # ghost endpoint skipped
        insert_sql, params = conn.batches[0]
        assert "INSERT INTO administrative_units" in insert_sql
        assert len(params) == 3
        # parent wiring: Xã C (unit_id 3) → TP A (unit_id 1)
        parent_sql, parent_params = conn.batches[-1]
        assert "parent_id" in parent_sql
        assert (3, 1) in parent_params
        # alias for new:1 ran; orphan alias skipped; relation ran once
        assert sum("administrative_aliases" in s for s, _ in conn.stmts) == 1
        assert sum("administrative_relations" in s for s, _ in conn.stmts) == 1

    def test_rerun_inserts_nothing(self):
        import asyncio

        from db.seed_admin import seed_admin

        existing = [{"code": r["code"], "vf": r["vf"]} for r in _id_rows()]
        conn = _FakeConn([existing, _id_rows()])
        stats = asyncio.run(seed_admin(conn, _mini_seed()))
        assert stats["units_inserted"] == 0
        assert not any("INSERT INTO administrative_units" in s for s, _ in conn.batches)
        # but the mutable-field UPDATE still runs for every unit
        assert any("UPDATE administrative_units SET" in s for s, _ in conn.batches)


# ── resolver helpers / API coverage ──────────────────────────────────────────


class TestResolverHelpers:
    def test_from_seed_and_from_store(self, seed, tmp_path):
        import asyncio

        p = tmp_path / "seed.json"
        p.write_text(__import__("json").dumps(seed), encoding="utf-8")
        r = GeoResolver.from_seed(p)
        assert r.resolve("Hà Nội").status == "resolved"

        class _NoneStore:
            async def graph(self):
                return None

        class _Store:
            def __init__(self, g):
                self._g = g

            async def graph(self):
                return self._g

        g = graph_from_seed(seed)
        assert asyncio.run(GeoResolver.from_store(_NoneStore())) is None
        assert asyncio.run(GeoResolver.from_store(_Store(g))) is not None

    def test_edge_cases(self, seed):
        r = GeoResolver(graph_from_seed(seed))
        assert r.resolve("").status == "not_found"
        assert r.resolve("Hà Nội,,").status == "resolved"  # empty segments skipped
        assert r.resolve_name("Cần Thơ").status == "resolved"
        # forward of a nonexistent key and of a level-1 orphan
        assert r._forward("ghost:99") == ((), ())
        orphan = AdminUnit(
            key="new:orph",
            unit_id=None,
            code="ZZ",
            name="Orphan",
            normalized_name="orphan",
            type="xa",
            admin_level=3,
            parent_key="ghost:99",
            valid_from=None,
            valid_to=None,
            status="current",
            source="t",
        )
        g2 = AdminGraph(units={"new:orph": orphan})
        r2 = GeoResolver(g2)
        assert r2._province_key("new:orph") is None
        assert r2.ancestors("new:orph") == ()  # dangling parent breaks the walk

    def test_module_singletons(self):
        import asyncio

        from core.geo_resolver import dict_store, get_resolver

        assert get_resolver() is get_resolver()
        store = dict_store()
        assert asyncio.run(store.units_containing(23.0, 104.0)) == []

    def test_resolve_point(self, seed):
        import asyncio

        r = GeoResolver(graph_from_seed(seed))
        store = DictAdminStore(graph_from_seed(seed))
        assert asyncio.run(r.resolve_point(store, 23.0, 104.0)) == []


class TestAdminEndpoints:
    def _client(self):
        from api.v1 import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_resolve_endpoint(self):
        c = self._client()
        resp = c.get("/v1/admin/resolve", params={"q": "huyện Yên Dũng, tỉnh Bắc Giang"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert any(u["key"] == "new:24" and u["admin_level"] == 1 for u in body["current"])
        assert any(u["key"] == "old:221" for u in body["matched"])
        assert {e["relation_type"] for e in body["path"]} >= {"merged_into", "split_into"}

    def test_resolve_endpoint_ambiguous_and_not_found(self):
        c = self._client()
        assert (
            c.get("/v1/admin/resolve", params={"q": "Châu Thành"}).json()["status"] == "ambiguous"
        )
        assert c.get("/v1/admin/resolve", params={"q": "zzzz"}).json()["status"] == "not_found"

    def test_lookup_endpoint(self):
        c = self._client()
        # Ba Đình square → phường Ba Đình + TP Hà Nội (commune first).
        resp = c.get("/v1/admin/lookup", params={"lat": 21.0367, "lon": 105.8346})
        assert resp.status_code == 200
        keys = [u["key"] for u in resp.json()]
        assert keys[0] == "new:00004" and "new:01" in keys
        # outside Vietnam → empty
        resp = c.get("/v1/admin/lookup", params={"lat": 23.0, "lon": 104.0})
        assert resp.status_code == 200 and resp.json() == []

    def test_resolver_degrades_when_seed_missing(self, monkeypatch):
        import api.v1 as v1

        monkeypatch.setattr(v1, "_resolver", None)
        import core.geo_resolver as gr

        def _boom():
            raise FileNotFoundError("no seed")

        monkeypatch.setattr(gr, "get_resolver", _boom)
        # _get_admin_resolver imports get_resolver lazily inside the function
        monkeypatch.setattr(v1, "_resolver", None)
        resolver = v1._get_admin_resolver()
        assert resolver.resolve("Hà Nội").status == "not_found"


class TestVnAddressFallback:
    def test_fallback_aliases_when_seed_unreadable(self, monkeypatch, tmp_path):
        import core.vn_address as va

        monkeypatch.setattr(va, "_SEED_PATH", tmp_path / "missing.json")
        aliases = va._load_city_aliases()
        assert aliases["hcm"] == "Thành phố Hồ Chí Minh"
        assert aliases["ha noi"] == "Thành phố Hà Nội"


# ── P14B: boundary geometry ─────────────────────────────────────────────────


class TestGeometry:
    """Post-2025 boundary polygons (thanglequoc MIT geojson, simplified)."""

    def test_every_current_unit_has_geometry(self, seed):
        missing = [u["key"] for u in _new(seed) if not u.get("geometry")]
        assert missing == []

    def test_historical_units_have_no_geometry(self, seed):
        # Old-era commune boundaries were never republished openly; point
        # lookup serves the current era anyway (stores filter on status).
        assert not any(u.get("geometry") for u in _old(seed))

    def test_geometry_shape_closed_rings_vn_bbox(self, seed):
        for u in _new(seed):
            g = u["geometry"]
            assert g["type"] == "MultiPolygon"
            assert g["coordinates"], u["key"]
            for poly in g["coordinates"]:
                ring = poly[0]
                assert len(ring) >= 4, u["key"]
                assert ring[0] == ring[-1], u["key"]
                for lon, lat in ring[:3]:
                    # 6–24°N × 102–119°E covers mainland + island đặc khu
                    assert 102.0 <= lon <= 119.5
                    assert 6.0 <= lat <= 24.0

    def test_province_encloses_its_communes(self, seed):
        by_key = {u["key"]: u for u in seed["units"]}
        # spot-check: phường Ba Đình sits inside TP Hà Nội's polygon
        from storage.admin_store import _geojson_bounds

        ward = by_key["new:00004"]
        prov = by_key[ward["parent_key"]]
        wb = _geojson_bounds(ward["geometry"])
        pb = _geojson_bounds(prov["geometry"])
        assert pb[0] <= wb[0] and pb[2] >= wb[2]
        assert pb[1] <= wb[1] and pb[3] >= wb[3]

    def test_point_lookup_known_sites(self, seed):
        import asyncio

        store = DictAdminStore(graph_from_seed(seed))
        cases = [
            # Ba Đình square → phường Ba Đình + TP Hà Nội
            (21.0367, 105.8346, "new:00004", "new:01"),
            # Hoàn Kiếm lake → phường Hoàn Kiếm
            (21.028, 105.852, "new:00070", "new:01"),
            # former huyện Yên Dũng town → Bắc Ninh (merged province)
            (21.211, 106.036, None, "new:24"),
            # District 1 HCMC → phường Sài Gòn + TP HCM
            (10.7756, 106.7019, "new:26740", "new:79"),
            # Đà Nẵng center
            (16.0544, 108.2022, None, "new:48"),
        ]
        for lat, lon, commune_key, prov_key in cases:
            units = asyncio.run(store.units_containing(lat, lon))
            keys = [u.key for u in units]
            assert prov_key in keys, (lat, lon, keys)
            assert keys[0].startswith("new:")
            # deepest hit is the commune (admin_level 3), before the province
            assert units[0].admin_level == 3
            if commune_key:
                assert keys[0] == commune_key

    def test_point_outside_vietnam_empty(self, seed):
        import asyncio

        store = DictAdminStore(graph_from_seed(seed))
        assert asyncio.run(store.units_containing(23.0, 104.0)) == []

    def test_bbox_index_matches_bruteforce(self, seed):
        import asyncio

        from storage.admin_store import _point_in_geojson

        store = DictAdminStore(graph_from_seed(seed))
        lat, lon = 21.028, 105.852
        indexed = asyncio.run(store.units_containing(lat, lon))
        brute = [
            u
            for u in store._graph.units.values()
            if u.geometry is not None
            and u.status == "current"
            and _point_in_geojson(lon, lat, u.geometry)
        ]
        assert {u.key for u in indexed} == {u.key for u in brute}

    def test_geojson_bounds_helper(self):
        from storage.admin_store import _geojson_bounds

        g = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [2, 0], [2, 2], [0, 0]]]]}
        assert _geojson_bounds(g) == (0, 0, 2, 2)
        assert _geojson_bounds({"type": "MultiPolygon", "coordinates": []}) is None


class TestRdpSimplify:
    def test_closed_ring_keeps_shape(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import build_vn_admin_seed as b

        square = [[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]]
        out = b._rdp_ring(square, 0.01)
        # first point is duplicated at close; degenerate chord fix must keep
        # the farthest vertex so a closed ring doesn't collapse to 2 pts
        assert len(out) >= 4
        assert out[0] == out[-1]

    def test_adaptive_floor_retries_finer(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import build_vn_admin_seed as b

        # A small zigzag poly whose coarse-tolerance simplification would
        # drop below the ring floor must retry at finer tolerances.
        ring = [[0.01 * i, 0.02 * ((i % 2) * 2 - 1) * ((i % 5) + 1)] for i in range(20)]
        ring.append(ring[0])
        out = b._simplify_multipoly([[ring]], tolerance=0.5, min_ring=8)
        assert out and len(out[0][0]) >= 4

    def test_seed_admin_pushes_geometry(self):
        import asyncio

        from db.seed_admin import seed_admin

        seed = _mini_seed()
        seed["units"][0]["geometry"] = {
            "type": "MultiPolygon",
            "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]],
        }
        conn = _FakeConn([[], _id_rows()])
        stats = asyncio.run(seed_admin(conn, seed))
        assert stats["units_with_geometry"] == 1
        geom_batches = [p for s, p in conn.batches if "ST_GeomFromGeoJSON" in s]
        assert geom_batches and geom_batches[0][0][0] == "1"
