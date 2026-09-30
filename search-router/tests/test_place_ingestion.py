"""P15 ingestion tests: adapters, validation, runner, PBF wire decoding.

The staging boundary is the contract under test: raw provider data into
``place_source_records``/``place_source_errors`` via COPY+merge — never
``businesses`` — with idempotent (provider, external_id) identity.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import zlib
from datetime import UTC, datetime
from typing import Any

import pytest
from ingestion import pbf
from ingestion.adapters.gmaps import GoogleMapsAdapter, entry_to_record
from ingestion.adapters.osm_pbf import OsmPbfAdapter, node_to_record
from ingestion.adapters.web_corpus import WebCorpusAdapter
from ingestion.base import IngestionContext, RawPlaceRecord
from ingestion.runner import _STAGE_COLS, run_ingestion
from ingestion.validate import canonical_website, normalize_phone, validate

NOW = datetime(2026, 9, 24, tzinfo=UTC)

GMAPS_ENTRY = {
    "input_id": "cafe hoan kiem",
    "link": "https://maps.google.com/?cid=12345",
    "cid": "12345",
    "title": "Cà Phê Giảng",
    "categories": ["Coffee shop", "Cafe"],
    "category": "Coffee shop",
    "address": "39 P. Nguyễn Hữu Huân, Hà Nội",
    "open_hours": {"Monday": ["8:00–22:00"]},
    "web_site": "https://caphegiang.vn/",
    "phone": "0904 123 456",
    "place_id": "ChIJtestPlaceId",
    "data_id": "0xabc:0xdef",
    "latitude": 21.0321,
    "longtitude": 105.8523,
    "status": "OPERATIONAL",
    "review_count": 120,
    "review_rating": 4.5,
    "price_range": "₫₫",
    "complete_address": {"street": "39 P. Nguyễn Hữu Huân", "city": "Hà Nội", "country": "Vietnam"},
}


def _ctx(run_id: int = 1) -> IngestionContext:
    return IngestionContext(run_id=run_id, provider="test")


async def _collect(adapter, ctx) -> list[RawPlaceRecord]:
    return [r async for r in adapter.ingest(ctx)]


# ── gmaps adapter ─────────────────────────────────────────────────────


class TestGmapsAdapter:
    def test_entry_to_record_full(self):
        rec = entry_to_record(GMAPS_ENTRY, fetched_at=NOW)
        assert rec is not None
        assert rec.provider == "google_maps"
        assert rec.external_id == "ChIJtestPlaceId"
        assert rec.external_id_type == "google_place_id"
        assert rec.raw_name == "Cà Phê Giảng"
        assert rec.lat == pytest.approx(21.0321)
        assert rec.lon == pytest.approx(105.8523)
        assert rec.raw_phone == "0904 123 456"
        assert rec.raw_hours == {"Monday": ["8:00–22:00"]}
        assert rec.raw_payload["cid"] == "12345"  # verbatim preserved

    def test_longitude_correct_spelling_accepted(self):
        obj = {**GMAPS_ENTRY, "longitude": 106.5}
        obj.pop("longtitude")
        rec = entry_to_record(obj, fetched_at=NOW)
        assert rec is not None and rec.lon == pytest.approx(106.5)

    def test_identity_fallback_cid_then_link(self):
        no_pid = {k: v for k, v in GMAPS_ENTRY.items() if k != "place_id"}
        rec = entry_to_record(no_pid, fetched_at=NOW)
        assert rec and rec.external_id == "12345" and rec.external_id_type == "google_cid"
        bare = {k: v for k, v in no_pid.items() if k not in ("cid", "data_id")}
        rec2 = entry_to_record(bare, fetched_at=NOW)
        assert rec2 and rec2.external_id_type == "google_link"

    def test_empty_record_returns_none(self):
        assert entry_to_record({}, fetched_at=NOW) is None
        assert entry_to_record("x", fetched_at=NOW) is None

    def test_stream_skips_bad_lines_and_marks_them(self):
        adapter = GoogleMapsAdapter(
            lines=[json.dumps(GMAPS_ENTRY), "{not json", "", json.dumps({"x": 1})]
        )
        recs = asyncio.run(_collect(adapter, _ctx()))
        assert len(recs) == 3
        assert recs[0].raw_name == "Cà Phê Giảng"
        assert recs[1].raw_payload.get("__parse_error__")
        assert recs[2].external_id is None and not recs[2].raw_name

    def test_content_hash_stable(self):
        a = entry_to_record(GMAPS_ENTRY, fetched_at=NOW)
        b = entry_to_record(dict(GMAPS_ENTRY), fetched_at=NOW)
        assert a is not None and b is not None
        assert a.content_hash() == b.content_hash()


# ── validation gate ────────────────────────────────────────────────────


class TestValidate:
    def _ok(self, **kw) -> RawPlaceRecord:
        base: dict[str, Any] = {
            "provider": "osm",
            "external_id": "node:1",
            "raw_name": "Phở Bát Đàn",
            "lat": 21.03,
            "lon": 105.85,
            "observed_at": NOW,
        }
        base.update(kw)
        return RawPlaceRecord(**base)

    def test_valid_record_passes(self):
        assert validate(self._ok()) == []

    def test_missing_name_and_identity(self):
        errs = validate(self._ok(raw_name=""))
        assert "missing_name" in errs
        errs2 = validate(self._ok(raw_name="", external_id=None))
        assert "missing_identity" in errs2

    def test_missing_observed_at(self):
        assert "missing_observed_at" in validate(self._ok(observed_at=None))

    def test_outside_vietnam_rejected(self):
        assert "coords_outside_vietnam" in validate(self._ok(lat=48.85, lon=2.35))

    def test_island_extent_allowed(self):
        assert validate(self._ok(lat=16.5, lon=112.4)) == []  # Trường Sa side

    def test_partial_coords(self):
        assert "partial_coordinates" in validate(self._ok(lon=None))

    def test_parse_error_record_rejected(self):
        rec = RawPlaceRecord(
            provider="google_maps", raw_payload={"__parse_error__": "x"}, observed_at=NOW
        )
        assert validate(rec) == ["parse_error"]

    def test_phone_normalize(self):
        assert normalize_phone("0904 123 456") == "+84904123456"
        assert normalize_phone("+84904123456") == "+84904123456"
        assert normalize_phone("84 904123456") == "+84904123456"
        assert normalize_phone("123") is None

    def test_website_canonical(self):
        assert canonical_website("HTTP://Example.COM/Path/") == "http://example.com/Path"
        assert canonical_website("caphegiang.vn") == "https://caphegiang.vn"
        assert canonical_website("") is None


# ── PBF wire decoder ───────────────────────────────────────────────────


def _v(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zz(n: int) -> int:
    """Standard zigzag encode for small (|n| < 2^62) values."""
    return (n << 1) if n >= 0 else ((-n << 1) - 1)


def _fld(no: int, wire: int, val: bytes | int) -> bytes:
    tag = _v((no << 3) | wire)
    if wire == 0:
        return tag + _v(val)  # type: ignore[arg-type]
    return tag + _v(len(val)) + val  # type: ignore[arg-type]


def _pack(vals: list[int], signed: bool = False) -> bytes:
    return b"".join(_v(_zz(v)) if signed else _v(v) for v in vals)


def _build_pbf(nodes: list[tuple[int, float, float, dict[str, str]]]) -> bytes:
    """Encode a minimal valid PBF: header + one OSMData dense-node block."""
    strs: list[bytes] = [b""]
    sidx: dict[bytes, int] = {b"": 0}

    def sid(s: bytes) -> int:
        if s not in sidx:
            sidx[s] = len(strs)
            strs.append(s)
        return sidx[s]

    gran = 100
    ids: list[int] = []
    lats: list[int] = []
    lons: list[int] = []
    kv: list[int] = []
    prev_id = prev_lat = prev_lon = 0
    for nid, lat, lon, tags in nodes:
        raw_lat, raw_lon = round(lat * 1e9 / gran), round(lon * 1e9 / gran)
        ids.append(nid - prev_id)
        lats.append(raw_lat - prev_lat)
        lons.append(raw_lon - prev_lon)
        prev_id, prev_lat, prev_lon = nid, raw_lat, raw_lon
        for k, v in tags.items():
            kv += [sid(k.encode()), sid(v.encode())]
        kv.append(0)

    dense = (
        _fld(1, 2, _pack(ids, signed=True))
        + _fld(8, 2, _pack(lats, signed=True))
        + _fld(9, 2, _pack(lons, signed=True))
        + _fld(10, 2, _pack(kv))
    )
    group = _fld(2, 2, dense)
    st_msg = b"".join(_fld(1, 2, s) for s in strs)
    block = _fld(1, 2, st_msg) + _fld(2, 2, group) + _fld(17, 0, gran)

    def frame(btype: str, payload: bytes) -> bytes:
        blob = _fld(2, 0, len(payload)) + _fld(3, 2, zlib.compress(payload))
        header = _fld(1, 2, btype.encode()) + _fld(3, 0, len(blob))
        return struct.pack(">I", len(header)) + header + blob

    header_block = _fld(4, 2, b"Search-Hub test")  # required_features field
    return frame("OSMHeader", header_block) + frame("OSMData", block)


class TestPbf:
    def test_iter_nodes_decodes_dense(self):
        nodes = [
            (1, 21.0321, 105.8523, {"name": "Cà Phê Giảng", "amenity": "cafe"}),
            (7, 10.7756, 106.7019, {"name": "Chợ Bến Thành", "amenity": "marketplace"}),
            (9, 16.0544, 108.2022, {"amenity": "restaurant"}),  # nameless → still decoded
        ]
        out = list(pbf.iter_nodes(io.BytesIO(_build_pbf(nodes))))
        assert len(out) == 3
        assert out[0].node_id == 1
        assert out[0].lat == pytest.approx(21.0321, abs=1e-5)
        assert out[0].lon == pytest.approx(105.8523, abs=1e-5)
        assert out[0].tags["name"] == "Cà Phê Giảng"
        assert out[1].node_id == 7  # delta decode
        assert out[2].tags == {"amenity": "restaurant"}

    def test_adapter_emits_only_named_pois(self, tmp_path):
        nodes = [
            (1, 21.03, 105.85, {"name": "Cà Phê", "amenity": "cafe", "phone": "+84901112222"}),
            (2, 21.04, 105.86, {"name": "Street Corner"}),  # no POI key → skip
            (3, 21.05, 105.87, {"amenity": "parking"}),  # no name → skip
        ]
        path = tmp_path / "t.osm.pbf"
        path.write_bytes(_build_pbf(nodes))
        adapter = OsmPbfAdapter(path)
        recs = asyncio.run(_collect(adapter, _ctx()))
        assert len(recs) == 1
        rec = recs[0]
        assert rec.external_id == "node:1" and rec.external_id_type == "osm_node"
        assert rec.provider == "osm"
        assert rec.raw_category == "amenity:cafe"
        assert rec.raw_phone == "+84901112222"
        assert "openstreetmap.org/node/1" in (rec.source_url or "")
        assert rec.raw_payload["tags"]["name"] == "Cà Phê"

    def test_node_to_record_address_parts(self):
        node = pbf.OsmNode(
            node_id=5,
            lat=21.0,
            lon=105.9,
            tags={
                "name": "X",
                "amenity": "pharmacy",
                "addr:street": "Lê Lợi",
                "addr:housenumber": "12",
                "addr:city": "Hà Nội",
                "opening_hours": "24/7",
            },
        )
        rec = node_to_record(node, fetched_at=NOW)
        assert rec.raw_address == "12 Lê Lợi, Hà Nội"
        assert rec.raw_hours == {"raw": "24/7"}


# ── web corpus bridge ─────────────────────────────────────────────────


class TestWebCorpus:
    def test_doc_with_place_metadata(self):
        doc = {
            "doc_id": "doc_abc",
            "canonical_url": "https://caphegiang.vn/",
            "title": "Cà Phê Giảng",
            "metadata": {
                "telephone": "0904123456",
                "address": {"streetAddress": "39 Nguyễn Hữu Huân", "addressLocality": "Hà Nội"},
            },
        }
        recs = asyncio.run(_collect(WebCorpusAdapter(docs=[doc]), _ctx()))
        assert len(recs) == 1
        assert recs[0].external_id == "doc_abc"
        assert recs[0].external_id_type == "doc_id"
        assert "39 Nguyễn Hữu Huân" in (recs[0].raw_address or "")

    def test_doc_without_place_signals_skipped(self):
        recs = asyncio.run(
            _collect(WebCorpusAdapter(docs=[{"doc_id": "d1", "title": "News"}]), _ctx())
        )
        assert recs == []


# ── runner (fake pool) ─────────────────────────────────────────────────


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, existing: list[dict] | None = None):
        self.sql: list[str] = []
        self.copies: list[tuple[str, list[str], int]] = []
        self.existing = existing or []

    async def execute(self, sql: str, *args):
        self.sql.append(sql)

    async def fetch(self, sql: str, *args):
        self.sql.append(sql)
        if "identity_hash" in sql:
            return [
                {"identity_hash": e["identity_hash"], "observation_hash": e["observation_hash"]}
                for e in self.existing
                if e.get("external_id") is None
            ]
        return [
            {"external_id": e["external_id"], "observation_hash": e["observation_hash"]}
            for e in self.existing
            if e.get("external_id") is not None
        ]

    async def copy_records_to_table(self, table, *, columns, records):
        self.copies.append((table, list(columns), len(records)))

    def transaction(self):
        return _FakeTx()


class _FakePool:
    def __init__(self, existing: list[dict] | None = None):
        self.conn = _FakeConn(existing)
        self.executed: list[tuple[str, tuple]] = []
        self.run_row: dict[str, Any] = {}

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _A()

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        if "RETURNING run_id" in sql:
            return {"run_id": 42}
        if "FROM ingestion_runs" in sql:
            return None
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _FakeAdmin:
    async def units_containing(self, lat, lon):
        from storage.admin_store import AdminUnit

        if abs(lat - 21.03) < 0.5 and abs(lon - 105.85) < 0.5:
            return [
                AdminUnit(
                    key="new:4",
                    unit_id=777,
                    code="00004",
                    name="Phường Hàng Trống",
                    normalized_name="phuong hang trong",
                    type="phuong",
                    admin_level=3,
                    parent_key="new:1",
                    valid_from="2025-07-01",
                    valid_to=None,
                    status="current",
                )
            ]
        return []


class TestRunner:
    def test_end_to_end_staging(self):
        lines = [
            json.dumps(GMAPS_ENTRY),
            json.dumps({**GMAPS_ENTRY, "place_id": "ChIJ2", "cid": "99"}),
            "{broken",
        ]
        adapter = GoogleMapsAdapter(lines=lines)
        pool = _FakePool()
        out = asyncio.run(
            run_ingestion(adapter, pool=pool, admin_store=_FakeAdmin(), batch_size=10)
        )
        assert out["run_id"] == 42
        assert out["seen"] == 3
        assert out["new"] == 2
        assert out["invalid"] == 1  # broken JSON → DLQ
        # COPY→merge used once; stage table round-tripped
        assert len(pool.conn.copies) == 1
        table, cols, n = pool.conn.copies[0]
        assert table == "_p15_stage" and len(cols) == len(_STAGE_COLS) and n == 2
        merges = [s for s in pool.conn.sql if "INSERT INTO place_source_records" in s]
        assert len(merges) == 2  # with-id + no-id merges
        # DLQ write happened for the broken line
        dlq = [s for s, a in pool.executed if "place_source_errors" in s]
        assert len(dlq) == 1

    def test_counts_changed_unchanged(self):
        existing = [{"external_id": "ChIJtestPlaceId", "observation_hash": "old-hash-differs"}]
        adapter = GoogleMapsAdapter(lines=[json.dumps(GMAPS_ENTRY)])
        pool = _FakePool(existing)
        out = asyncio.run(run_ingestion(adapter, pool=pool, batch_size=1))
        assert out["changed"] == 1 and out["new"] == 0

    def test_no_pool_returns_none(self):
        adapter = GoogleMapsAdapter(lines=[json.dumps(GMAPS_ENTRY)])
        out = asyncio.run(run_ingestion(adapter, pool=None))
        # get_pool() may or may not exist in test env; both paths must not raise
        assert out is None or isinstance(out, dict)


# ── API surface ──────────────────────────────────────────────────────────────


class TestIngestEndpoints:
    def _client(self):
        from api.v1 import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_ingest_gmaps_happy_path(self, monkeypatch):
        from storage import pg_client

        pool = _FakePool()

        async def _fake_pool():
            return pool

        monkeypatch.setattr(pg_client, "get_pool", _fake_pool)
        c = self._client()
        resp = c.post("/v1/ingest/google_maps", json={"entries": [GMAPS_ENTRY]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["provider"] == "google_maps"
        assert body["run_id"] == 42
        assert body["seen"] == 1 and body["new"] == 1

    def test_ingest_degraded_without_pool(self, monkeypatch):
        from storage import pg_client

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        c = self._client()
        resp = c.post("/v1/ingest/google_maps", json={"entries": [GMAPS_ENTRY]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False and body["seen"] == 0

    def test_ingest_unsupported_provider(self):
        c = self._client()
        resp = c.post("/v1/ingest/osm", json={"entries": [{"a": 1}]})
        assert resp.status_code == 400

    def test_ingest_empty_entries_rejected(self):
        c = self._client()
        resp = c.post("/v1/ingest/google_maps", json={"entries": []})
        assert resp.status_code == 422

    def test_ingest_runs_empty_without_pool(self, monkeypatch):
        from storage import pg_client

        async def _none():
            return None

        monkeypatch.setattr(pg_client, "get_pool", _none)
        c = self._client()
        resp = c.get("/v1/ingest/runs")
        assert resp.status_code == 200 and resp.json() == []
