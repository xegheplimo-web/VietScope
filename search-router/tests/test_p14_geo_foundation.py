"""P14 — VN local data foundation: admin gazetteer + osm_pois lane.

Covers the seed dataset (post-2025 34 provinces + 29 former-province
anchors), the folded-name lookup in services/admin.py, the degrade-safe
osm_pois lane in services/geo_postgis.py, the tag-kv refactor of
core/business_entity.py, and the /v1/business/search wiring. No live
DB or network needed — all store access is faked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import config
import pytest
from core.business_entity import osm_tag_for, osm_tag_kv
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import BusinessEntity
from services import admin, geo_postgis
from services.admin import admin_anchor, lookup_admin, match_forms
from services.geo import GeoPoint
from services.geo_postgis import osm_pois_nearby
from storage import pg_client

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "admin_units_vn.json"

# Rough bounding box of Vietnam (+ margin): lat 8–24, lon 100–112.
_VN_BBOX = (8.0, 100.0, 24.0, 112.0)


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """No DSN, no LLM — every store must degrade without touching Postgres."""
    monkeypatch.setattr(config.settings, "hub_database_url", "")
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    admin._reset_cache()
    geo_postgis._reset_cache()


class _FakePool:
    """Minimal asyncpg pool stand-in: canned fetch/fetchval results."""

    def __init__(self, rows=(), fetchval=None):
        self._rows = list(rows)
        self._fetchval = fetchval
        self.fetch_sql: list[str] = []

    async def fetch(self, sql, *args):
        self.fetch_sql.append(sql)
        return list(self._rows)

    async def fetchval(self, sql, *args):
        return self._fetchval


def _pool(monkeypatch, pool):
    monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=pool))


def _seed_row(unit: dict, aliases=()) -> dict:
    """Shape of the _LOAD_SQL result row for a seed unit."""
    return {
        "code": unit["code"],
        "name": unit["name"],
        "type": unit["type"],
        "lat": unit["lat"],
        "lon": unit["lon"],
        "current": unit.get("valid_to") is None,
        "aliases": list(aliases) + list(unit.get("aliases") or []),
    }


def _admin_pool(units) -> _FakePool:
    """Pool whose fetch() replays seed units as _LOAD_SQL rows."""
    return _FakePool(rows=[_seed_row(u) for u in units])


# ─── seed dataset ────────────────────────────────────────────────────────────


class TestSeedDataset:
    @pytest.fixture(scope="class")
    def units(self):
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))["units"]

    def test_34_current_29_former(self, units):
        current = [u for u in units if u.get("valid_to") is None]
        former = [u for u in units if u["type"] == "former_province"]
        assert len(current) == 34
        assert len(former) == 29
        assert len(units) == 63

    def test_codes_unique_and_parents_resolve(self, units):
        codes = [u["code"] for u in units]
        assert len(codes) == len(set(codes))
        current_codes = {u["code"] for u in units if u.get("valid_to") is None}
        for u in units:
            parent = u.get("parent")
            if parent:
                assert parent in current_codes, f"{u['code']} -> missing parent {parent}"
                assert u["type"] == "former_province"

    def test_current_codes_are_official(self, units):
        """Level-1 codes come from provinces.open-api.vn (numeric)."""
        for u in units:
            if u.get("valid_to") is None:
                assert u["code"].isdigit(), u["code"]
            else:
                assert u["code"].startswith("former:"), u["code"]

    def test_coords_inside_vietnam(self, units):
        for u in units:
            assert _VN_BBOX[0] <= u["lat"] <= _VN_BBOX[2], u["code"]
            assert _VN_BBOX[1] <= u["lon"] <= _VN_BBOX[3], u["code"]

    def test_types(self, units):
        for u in units:
            assert u["type"] in ("province", "municipality", "former_province")


# ─── match_forms / lookup_admin ──────────────────────────────────────────────


class TestMatchForms:
    def test_folds_name_and_aliases(self):
        forms = match_forms("Bắc Ninh", ["Bac Ninh", "Tỉnh Bắc Ninh"])
        assert "bac ninh" in forms
        assert "tinh bac ninh" in forms
        assert len(forms) == len(set(forms))

    def test_empty_surfaces_skipped(self):
        assert match_forms("  ", [""]) == []


class TestLookupAdmin:
    def _lookup(self, text, units=None):
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))["units"]
        pool = _admin_pool(units if units is not None else data)
        with patch.object(pg_client, "get_pool", AsyncMock(return_value=pool)):
            admin._reset_cache()
            return asyncio.run(lookup_admin(text))

    def test_exact_folded_match(self):
        div = self._lookup("Bac Ninh")
        assert div is not None and div.code == "24"
        assert div.name == "Thành phố Bắc Ninh"

    def test_admin_prefix_stripped(self):
        assert self._lookup("tỉnh Hưng Yên").code == "33"
        assert self._lookup("thành phố Hồ Chí Minh").code == "79"

    def test_alias_match(self):
        assert self._lookup("Saigon").code == "79"
        assert self._lookup("Đà Lạt").code == "68"

    def test_former_province_has_own_anchor(self):
        div = self._lookup("Vũng Tàu")
        assert div is not None
        assert div.type == "former_province"
        assert not div.current
        # Vũng Tàu city, not the TP.HCM centroid.
        assert abs(div.lat - 10.346) < 0.01
        assert abs(div.lon - 107.0843) < 0.01

    def test_contained_match(self):
        div = self._lookup("quán cà phê ở hà nội")
        assert div is not None and div.code == "01"

    def test_longest_form_wins(self):
        # "bà rịa vũng tàu" beats its own "vũng tàu"/"bà rịa" forms.
        div = self._lookup("tỉnh bà rịa vũng tàu cũ")
        assert div is not None and div.code == "former:77"

    def test_unknown_returns_none(self):
        assert self._lookup("xyzabc") is None
        assert self._lookup("") is None

    def test_no_db_returns_none(self, monkeypatch):
        monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=None))
        admin._reset_cache()
        assert asyncio.run(lookup_admin("Bắc Ninh")) is None
        assert asyncio.run(admin_anchor("Bắc Ninh")) is None

    def test_admin_anchor_geopoint(self, monkeypatch):
        pool = _admin_pool(
            [
                {
                    "code": "24",
                    "name": "Thành phố Bắc Ninh",
                    "type": "municipality",
                    "lat": 21.1861,
                    "lon": 106.0763,
                }
            ]
        )
        _pool(monkeypatch, pool)
        admin._reset_cache()
        point = asyncio.run(admin_anchor("Bắc Ninh"))
        assert isinstance(point, GeoPoint)
        assert point.lat == 21.1861 and "Bắc Ninh" in point.display_name


# ─── osm_tag_kv ──────────────────────────────────────────────────────────────


class TestOsmTagKv:
    def test_category_pairs(self):
        assert osm_tag_kv("nhà thuốc") == ("amenity", "pharmacy")
        assert osm_tag_kv("siêu thị") == ("shop", "supermarket")
        assert osm_tag_kv("khách sạn") == ("tourism", "hotel")

    def test_value_none_means_any_key(self):
        assert osm_tag_kv("cửa hàng") == ("shop", None)
        assert osm_tag_kv("xyzzy") == ("amenity", None)

    def test_overpass_render_unchanged(self):
        """osm_tag_for must still emit Overpass syntax for the live lane."""
        assert osm_tag_for("nhà thuốc") == '["amenity"="pharmacy"]'
        assert osm_tag_for("cửa hàng") == '["shop"]'
        assert osm_tag_for("xyzzy") == '["amenity"]'


# ─── osm_pois lane ───────────────────────────────────────────────────────────


class TestOsmPoisNearby:
    def test_no_table_degrades(self, monkeypatch):
        _pool(monkeypatch, _FakePool(fetchval=False))
        assert asyncio.run(osm_pois_nearby(10.0, 106.0, 2.0)) == []

    def test_no_db_degrades(self, monkeypatch):
        monkeypatch.setattr(pg_client, "get_pool", AsyncMock(return_value=None))
        geo_postgis._reset_cache()
        assert asyncio.run(osm_pois_nearby(10.0, 106.0, 2.0)) == []

    def test_unlisted_tag_key_short_circuits(self, monkeypatch):
        pool = _FakePool(fetchval=True, rows=[])
        _pool(monkeypatch, pool)
        geo_postgis._reset_cache()
        out = asyncio.run(osm_pois_nearby(10.0, 106.0, 2.0, tag_kv=("name; DROP", "x")))
        assert out == []
        assert pool.fetch_sql == []  # never reached the DB

    def test_rows_map_to_entities(self, monkeypatch):
        rows = [
            {
                "osm_id": 42,
                "osm_type": "N",
                "name": "Nhà Thuốc A",
                "category": "pharmacy",
                "addr_housenumber": "12",
                "addr_street": "Lê Lợi",
                "addr_district": None,
                "addr_city": "Bắc Ninh",
                "phone": "0222",
                "website": None,
                "opening_hours": "08:00-22:00",
                "lat": 21.18,
                "lon": 106.07,
            }
        ]
        _pool(monkeypatch, _FakePool(fetchval=True, rows=rows))
        geo_postgis._reset_cache()
        ents = asyncio.run(osm_pois_nearby(21.18, 106.07, 2.0, tag_kv=("amenity", "pharmacy")))
        assert len(ents) == 1
        e = ents[0]
        assert e.name == "Nhà Thuốc A"
        assert e.category == "pharmacy"
        assert e.address == "12 Lê Lợi Bắc Ninh"
        assert e.hours == "08:00-22:00"
        assert e.source_url == "https://www.openstreetmap.org/node/42"

    def test_tag_filter_in_sql(self, monkeypatch):
        pool = _FakePool(fetchval=True, rows=[])
        _pool(monkeypatch, pool)
        geo_postgis._reset_cache()
        asyncio.run(osm_pois_nearby(10.0, 106.0, 2.0, tag_kv=("amenity", "pharmacy")))
        assert "amenity = $4" in pool.fetch_sql[-1]
        asyncio.run(osm_pois_nearby(10.0, 106.0, 2.0, tag_kv=("shop", None)))
        assert "shop IS NOT NULL" in pool.fetch_sql[-1]


# ─── importer helpers ────────────────────────────────────────────────────────


class TestImporter:
    def test_load_units_validates(self, tmp_path):
        from scripts.import_admin_data import load_units

        bad = tmp_path / "bad.json"
        bad.write_text('{"units": [{"code": "x"}]}', encoding="utf-8")
        with pytest.raises(ValueError):
            load_units(bad)

    def test_topo_order_parents_first(self):
        from scripts.import_admin_data import topo_order

        units = [
            {"code": "former:77", "parent": "79"},
            {"code": "79"},
            {"code": "former:74", "parent": "79"},
        ]
        ordered = [u["code"] for u in topo_order(units)]
        assert ordered[0] == "79"

    def test_topo_cycle_detected(self):
        from scripts.import_admin_data import topo_order

        with pytest.raises(ValueError):
            topo_order([{"code": "a", "parent": "b"}, {"code": "b", "parent": "a"}])


# ─── endpoint wiring ─────────────────────────────────────────────────────────


class TestBusinessSearchWiring:
    def _post(self, query, **extra):
        app = FastAPI()
        from api.v1 import router

        app.include_router(router)
        client = TestClient(app)
        payload = {"query": query, "limit": 5}
        payload.update(extra)
        return client.post("/v1/business/search", json=payload)

    def test_local_admin_anchor_beats_nominatim(self):
        point = GeoPoint(name="Thành phố Bắc Ninh", lat=21.1861, lon=106.0763)
        geocode = AsyncMock(return_value=None)
        with (
            patch("api.v1.admin_anchor", new=AsyncMock(return_value=point)),
            patch("api.v1.geocode", new=geocode),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post("quán cà phê ở Bắc Ninh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anchor"]["resolved_from"] == "entity"
        assert data["anchor"]["name"] == "Thành phố Bắc Ninh"
        assert data["lat"] == 21.1861
        geocode.assert_not_awaited()  # local gazetteer won

    def test_osm_local_lane_reports(self):
        poi = BusinessEntity(
            name="Cafe A",
            category="cafe",
            lat=21.18,
            lon=106.07,
            source_url="https://www.openstreetmap.org/node/7",
        )
        with (
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=[poi])),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post("quán cà phê", lat=21.18, lon=106.07)
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "osm_local+web"
        assert data["entities"][0]["name"] == "Cafe A"
