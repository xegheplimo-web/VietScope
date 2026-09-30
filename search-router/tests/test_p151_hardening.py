"""P15.1 ingestion-hardening tests.

Covers the fixes the P15 audit demanded before P16:
  * OSM way + relation coverage (stdlib fallback path)
  * blob+element-granular checkpoints (a mid-blob crash skips nothing)
  * real run resume (--resume-run loads prior checkpoint/cursor/params)
  * append-only place_source_observations history
  * identity_hash vs observation_hash split (opening-hours count)
  * explicit run status + CLI exit codes on fatal runs
  * DLQ payloads always valid JSON (no mid-string truncation)
  * streaming web-corpus bridge (keyset paging, no corpus materialization)
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import zlib
from datetime import UTC, datetime

import pytest
from ingestion import pbf
from ingestion.adapters.osm_pbf import OsmPbfAdapter
from ingestion.adapters.web_corpus import WebCorpusAdapter
from ingestion.base import IngestionContext, RawPlaceRecord
from ingestion.runner import _STAGE_COLS, _dlq_payload, run_ingestion

NOW = datetime(2025, 9, 1, tzinfo=UTC)


def _ctx(run_id: int = 1, checkpoint: dict | None = None) -> IngestionContext:
    return IngestionContext(
        run_id=run_id, provider="test", parameters={}, checkpoint=checkpoint or {}
    )


async def _collect(adapter, ctx) -> list[RawPlaceRecord]:
    out: list[RawPlaceRecord] = []
    async for rec in adapter.ingest(ctx):
        out.append(rec)
    return out


# ── extended PBF encoder: nodes + ways + relations, multi-blob ────────


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
    return (n << 1) if n >= 0 else ((-n << 1) - 1)


def _fld(no: int, wire: int, val: bytes | int) -> bytes:
    tag = _v((no << 3) | wire)
    if wire == 0:
        return tag + _v(val)  # type: ignore[arg-type]
    return tag + _v(len(val)) + val  # type: ignore[arg-type]


def _pack(vals: list[int], signed: bool = False) -> bytes:
    return b"".join(_v(_zz(v)) if signed else _v(v) for v in vals)


class _St:
    def __init__(self):
        self.strs = [b""]
        self.idx = {b"": 0}

    def sid(self, s: bytes) -> int:
        if s not in self.idx:
            self.idx[s] = len(self.strs)
            self.strs.append(s)
        return self.idx[s]


def _dense_node_msg(nodes, st: _St) -> bytes:
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
            kv += [st.sid(k.encode()), st.sid(v.encode())]
        kv.append(0)
    return (
        _fld(1, 2, _pack(ids, signed=True))
        + _fld(8, 2, _pack(lats, signed=True))
        + _fld(9, 2, _pack(lons, signed=True))
        + _fld(10, 2, _pack(kv))
    )


def _way_msg(way_id: int, refs: list[int], tags: dict[str, str], st: _St) -> bytes:
    deltas = [refs[0]] + [refs[i] - refs[i - 1] for i in range(1, len(refs))]
    return (
        _fld(1, 0, way_id)
        + _fld(2, 2, _pack([st.sid(k.encode()) for k in tags]))
        + _fld(3, 2, _pack([st.sid(v.encode()) for v in tags.values()]))
        + _fld(8, 2, _pack(deltas, signed=True))
    )


def _rel_msg(
    rel_id: int, members: list[tuple[int, str, str]], tags: dict[str, str], st: _St
) -> bytes:
    tmap = {"node": 0, "way": 1, "relation": 2}
    roles = [st.sid(role.encode()) for _ref, kind, role in members]
    deltas: list[int] = []
    prev = 0
    for ref, _k, _r in members:
        deltas.append(ref - prev)
        prev = ref
    types = [tmap[kind] for _ref, kind, _r in members]
    return (
        _fld(1, 0, rel_id)
        + _fld(2, 2, _pack([st.sid(k.encode()) for k in tags]))
        + _fld(3, 2, _pack([st.sid(v.encode()) for v in tags.values()]))
        + _fld(8, 2, _pack(roles))
        + _fld(9, 2, _pack(deltas, signed=True))
        + _fld(10, 2, _pack(types))
    )


def _osm_block(elements: dict[str, list]) -> bytes:
    st = _St()
    group = b""
    if elements.get("nodes"):
        group += _fld(2, 2, _dense_node_msg(elements["nodes"], st))
    for w in elements.get("ways", []):
        group += _fld(3, 2, _way_msg(w[0], w[1], w[2], st))
    for r in elements.get("relations", []):
        group += _fld(4, 2, _rel_msg(r[0], r[1], r[2], st))
    st_msg = b"".join(_fld(1, 2, s) for s in st.strs)
    # PrimitiveBlock: stringtable (1) + primitivegroup (2) + granularity (17)
    return _fld(1, 2, st_msg) + _fld(2, 2, group) + _fld(17, 0, 100)


def _frame(btype: str, payload: bytes) -> bytes:
    blob = _fld(2, 0, len(payload)) + _fld(3, 2, zlib.compress(payload))
    header = _fld(1, 2, btype.encode()) + _fld(3, 0, len(blob))
    return struct.pack(">I", len(header)) + header + blob


def _build_pbf(blocks: list[dict[str, list]]) -> bytes:
    """Multi-OSMData PBF with per-block string tables."""
    out = _frame("OSMHeader", _fld(4, 2, b"Search-Hub test"))
    for el in blocks:
        out += _frame("OSMData", _osm_block(el))
    return out


WAY_TAGS = {"name": "Siêu Thị Winmart", "shop": "supermarket"}
REL_TAGS = {"name": "Bệnh Viện Đa Khoa", "amenity": "hospital", "type": "multipolygon"}

_FIXTURE_BLOCKS = [
    {
        "nodes": [
            (10, 21.00, 105.80, {}),  # corner node, no POI tags
            (11, 21.01, 105.81, {}),
            (12, 21.02, 105.82, {}),
            (20, 21.03, 105.85, {"name": "Cà Phê", "amenity": "cafe"}),
        ]
    },
    {
        "ways": [(500, [10, 11, 12], WAY_TAGS)],
        "relations": [(900, [(500, "way", "outer"), (10, "node", "label")], REL_TAGS)],
    },
]


class TestOsmCoverage:
    def test_way_and_relation_decoded(self):
        els = list(pbf.iter_elements(io.BytesIO(_build_pbf(_FIXTURE_BLOCKS))))
        kinds = [e.kind for e in els]
        assert kinds == ["node", "node", "node", "node", "way", "relation"]
        way = els[4].obj
        assert way.way_id == 500 and way.node_refs == [10, 11, 12]
        assert way.tags["shop"] == "supermarket"
        rel = els[5].obj
        assert rel.rel_id == 900
        assert [(m.ref, m.kind, m.role) for m in rel.members] == [
            (500, "way", "outer"),
            (10, "node", "label"),
        ]

    def test_adapter_emits_way_with_member_centroid(self, tmp_path):
        path = tmp_path / "t.osm.pbf"
        path.write_bytes(_build_pbf(_FIXTURE_BLOCKS))
        adapter = OsmPbfAdapter(path, backend="pbf")
        recs = asyncio.run(_collect(adapter, _ctx()))
        by_kind = {r.external_id_type: r for r in recs}
        assert set(by_kind) == {"osm_node", "osm_way", "osm_relation"}
        way = by_kind["osm_way"]
        assert way.external_id == "way:500"
        assert way.raw_name == "Siêu Thị Winmart"
        # centroid of member nodes 10,11,12
        assert way.lat == pytest.approx(21.01, abs=1e-4)
        assert way.lon == pytest.approx(105.81, abs=1e-4)
        assert way.raw_payload["members"] == [10, 11, 12]
        rel = by_kind["osm_relation"]
        assert rel.external_id == "relation:900"
        assert rel.lat is not None  # member-way centroid + label node
        assert rel.raw_payload["members"][0] == {"kind": "way", "ref": 500, "role": "outer"}
        assert adapter.source_dataset["sha256"]
        assert adapter.adapter_version == "osm-pbf-v2"

    def test_resume_mid_blob_loses_nothing(self):
        """iter_elements resume at (blob, index) skips exactly the prefix."""
        blob = _build_pbf([{"nodes": [(i, 21.0 + i * 0.001, 105.8, {}) for i in range(1, 9)]}])
        els = list(pbf.iter_elements(io.BytesIO(blob)))
        assert len(els) == 8
        mid = els[3]  # 0-based index 3 → resume at index 4
        resumed = list(
            pbf.iter_elements(
                io.BytesIO(blob), start_offset=mid.blob_offset, start_index=mid.index + 1
            )
        )
        assert [e.obj.node_id for e in resumed] == [e.obj.node_id for e in els[4:]]
        # and nothing from a LATER blob is skipped
        two = _build_pbf(
            [
                {"nodes": [(i, 21.0, 105.8, {}) for i in range(1, 5)]},
                {"nodes": [(i, 21.0, 105.8, {}) for i in range(5, 9)]},
            ]
        )
        all_els = list(pbf.iter_elements(io.BytesIO(two)))
        first_block2 = next(e for e in all_els if e.blob_offset != all_els[0].blob_offset)
        resumed2 = list(
            pbf.iter_elements(
                io.BytesIO(two),
                start_offset=first_block2.blob_offset,
                start_index=first_block2.index + 1,
            )
        )
        assert [e.obj.node_id for e in resumed2] == [6, 7, 8]

    def test_adapter_checkpoint_per_element_not_per_blob(self, tmp_path):
        """The checkpoint must never point past unprocessed blob elements."""
        path = tmp_path / "t.osm.pbf"
        path.write_bytes(
            _build_pbf(
                [
                    {
                        "nodes": [
                            (i, 21.0 + i * 0.001, 105.8, {"name": f"N{i}", "amenity": "cafe"})
                            for i in range(1, 6)
                        ]
                    }
                ]
            )
        )
        ctx = _ctx()
        ad = OsmPbfAdapter(path, backend="pbf")

        async def watch() -> tuple[list[dict], list[str]]:
            snaps: list[dict] = []
            ids: list[str] = []
            async for rec in ad.ingest(ctx):
                snaps.append(dict(ctx.checkpoint))
                ids.append(rec.external_id or "")
            return snaps, ids

        snaps, ids = asyncio.run(watch())
        assert ids == [f"node:{i}" for i in range(1, 6)]
        for i, snap in enumerate(snaps):
            assert snap["stage"] == "nodes"
            # each snapshot points at the just-yielded element's own index
            assert snap["index"] == i + 1

    def test_osmium_backend_propagates_metadata(self, tmp_path, monkeypatch):
        """OsmPbfAdapter delegating to osmium must surface the inner
        adapter's version/dataset, not its own v2 defaults."""
        import ingestion.adapters.osm_osmium as osm_mod
        from ingestion.adapters.osm_pbf import OsmPbfAdapter

        class _StubOsmium:
            name = "osm"
            adapter_version = "osmium-stub-v9"

            def __init__(self, path):
                self.source_dataset = {}

            async def ingest(self, ctx):
                self.source_dataset = {"file": "stub.pbf", "sha256": "x"}
                return
                yield  # pragma: no cover

        monkeypatch.setattr(osm_mod, "OsmiumPbfAdapter", _StubOsmium)
        pbf = tmp_path / "t.osm.pbf"
        pbf.write_bytes(b"")
        adapter = OsmPbfAdapter(pbf, backend="osmium")
        asyncio.run(_collect(adapter, _ctx()))
        assert adapter.adapter_version == "osmium-stub-v9"
        assert adapter.source_dataset["file"] == "stub.pbf"

    def test_done_checkpoint_short_circuits(self, tmp_path):
        path = tmp_path / "t.osm.pbf"
        path.write_bytes(_build_pbf(_FIXTURE_BLOCKS))
        ctx = _ctx(checkpoint={"stage": "done"})
        recs = asyncio.run(_collect(OsmPbfAdapter(path, backend="pbf"), ctx))
        assert recs == []


# ── runner hardening ───────────────────────────────────────────────────


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    """Records stage copies, prior-hash fetches, observation inserts."""

    def __init__(self, existing: list[dict] | None = None, fail_on: str | None = None):
        self.sql: list[str] = []
        self.copies: list[tuple[str, list[str], list[tuple]]] = []
        self.existing = existing or []
        self.fail_on = fail_on

    async def execute(self, sql: str, *args):
        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")

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
        self.copies.append((table, list(columns), list(records)))

    def transaction(self):
        return _FakeTx()


class _FakePool:
    def __init__(
        self,
        existing: list[dict] | None = None,
        prior_run: dict | None = None,
        fail_on: str | None = None,
    ):
        self.conn = _FakeConn(existing, fail_on)
        self.executed: list[tuple[str, tuple]] = []
        self.prior_run = prior_run

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
            return self.prior_run
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")


class _SeqAdapter:
    """Emits N identical-shape records; optional mid-stream crash."""

    name = "test_seq"
    adapter_version = "seq-v1"
    source_dataset = {"file": "synthetic"}

    def __init__(self, recs: list[RawPlaceRecord], crash_after: int | None = None):
        self.recs = recs
        self.crash_after = crash_after

    async def ingest(self, ctx: IngestionContext):
        start = int(ctx.checkpoint.get("i", 0) or 0)
        for i in range(start, len(self.recs)):
            ctx.checkpoint["i"] = i + 1
            if self.crash_after is not None and i >= self.crash_after:
                raise RuntimeError("adapter crashed mid-stream")
            yield self.recs[i]


def _rec(ext: str | None, name: str = "Quán", **kw) -> RawPlaceRecord:
    return RawPlaceRecord(
        provider="test_seq",
        external_id=ext,
        external_id_type="test" if ext else None,
        raw_name=name,
        raw_address="1 Đường X, Hà Nội",
        lat=21.03,
        lon=105.85,
        observed_at=NOW,
        fetched_at=NOW,
        **kw,
    )


class TestRunnerHardening:
    def test_status_done_and_observations_logged(self):
        adapter = _SeqAdapter([_rec("a"), _rec("b")])
        pool = _FakePool()
        out = asyncio.run(run_ingestion(adapter, pool=pool, batch_size=10))
        assert out["status"] == "done"
        assert out["adapter_version"] == "seq-v1"
        # one COPY, one observation append, two merges per batch
        obs = [s for s in pool.conn.sql if "place_source_observations" in s]
        assert len(obs) == 1
        # stage rows carried change_type values
        _t, cols, rows = pool.conn.copies[0]
        assert rows[0][cols.index("change_type")] == "new"

    def test_failed_run_returns_status_failed(self):
        adapter = _SeqAdapter([_rec("a")], crash_after=0)
        pool = _FakePool()
        out = asyncio.run(run_ingestion(adapter, pool=pool))
        assert out["status"] == "failed"
        finish = [a for s, a in pool.executed if "SET" in s and "status = $2" in s]
        assert finish and finish[-1][1] == "failed"

    def test_failed_run_persists_only_committed_checkpoint(self):
        """Live ctx.checkpoint outruns merged batches — the failed run's
        persisted checkpoint must stop at the last committed boundary,
        or a resume would silently skip unmerged elements."""
        adapter = _SeqAdapter([_rec(str(i)) for i in range(5)], crash_after=3)
        pool = _FakePool()
        out = asyncio.run(run_ingestion(adapter, pool=pool, batch_size=2))
        assert out["status"] == "failed"
        # records 0,1 merged (checkpoint i=2); record 2 yielded but unmerged;
        # crash at i=3 left the live checkpoint at i=4
        finish = [a for s, a in pool.executed if "status = $2" in s]
        assert finish
        persisted = json.loads(finish[-1][8])  # checkpoint arg
        assert persisted == {"i": 2}

    def test_cli_exit_code_nonzero_on_failed(self):
        from scripts.ingest import _exit_code

        assert _exit_code({"status": "done"}) == 0
        assert _exit_code({"status": "failed"}) != 0
        assert _exit_code({"status": "aborted"}) != 0
        assert _exit_code(None) != 0

    def test_resume_loads_prior_checkpoint_and_params(self):
        prior = {
            "provider": "test_seq",
            "parameters": {"province": "Bắc Ninh"},
            "checkpoint": {"i": 2},
            "cursor": "c9",
        }
        adapter = _SeqAdapter([_rec(str(i)) for i in range(4)])
        pool = _FakePool(prior_run=prior)
        out = asyncio.run(run_ingestion(adapter, pool=pool, resume_of=8421))
        assert out["status"] == "done"
        assert out["resume_of"] == 8421
        assert out["seen"] == 2  # records 0,1 skipped by checkpoint
        # merged params kept
        ins = [a for s, a in pool.executed if "INSERT INTO ingestion_runs" in s]
        assert json.loads(ins[0][1])["province"] == "Bắc Ninh"
        assert ins[0][4] == 8421  # resume_of persisted

    def test_resume_wrong_provider_rejected(self):
        prior = {"provider": "osm", "parameters": {}, "checkpoint": {}, "cursor": None}
        pool = _FakePool(prior_run=prior)
        with pytest.raises(ValueError, match="provider mismatch"):
            asyncio.run(run_ingestion(_SeqAdapter([]), pool=pool, resume_of=1))

    def test_observation_history_two_runs(self):
        """Same record across two runs → two observations, state row updated."""
        rec1 = _rec("a", raw_hours={"raw": "08:00-20:00"})
        rec2 = _rec("a", raw_hours={"raw": "08:00-22:00"})  # hours changed
        pool = _FakePool()
        asyncio.run(run_ingestion(_SeqAdapter([rec1]), pool=pool, batch_size=10))
        # second run sees the stored observation_hash → changed
        existing = [{"external_id": "a", "observation_hash": rec1.observation_hash()}]
        pool2 = _FakePool(existing=existing)
        out = asyncio.run(run_ingestion(_SeqAdapter([rec2]), pool=pool2, batch_size=10))
        assert out["changed"] == 1 and out["unchanged"] == 0
        obs_inserts = [s for s in pool2.conn.sql if "place_source_observations" in s]
        assert len(obs_inserts) == 1
        _t, cols, rows = pool2.conn.copies[0]
        assert rows[0][cols.index("change_type")] == "changed"

    def test_opening_hours_count_in_change_detection(self):
        a = _rec("x", raw_hours={"raw": "08:00-20:00"})
        b = _rec("x", raw_hours={"raw": "08:00-22:00"})
        assert a.identity_hash() == b.identity_hash()  # same place
        assert a.observation_hash() != b.observation_hash()  # but it changed

    def test_status_change_counts_as_changed(self):
        a = _rec("x", raw_status="OPERATIONAL")
        b = _rec("x", raw_status="CLOSED_PERMANENTLY")
        assert a.identity_hash() == b.identity_hash()  # same place
        assert a.observation_hash() != b.observation_hash()  # status changed

    def test_identity_stable_across_mutable_drift(self):
        a = _rec(None, raw_phone="0901")
        b = _rec(None, raw_phone="0902", raw_hours={"raw": "24/7"})
        assert a.identity_hash() == b.identity_hash()
        assert a.observation_hash() != b.observation_hash()

    def test_dlq_oversized_payload_stays_valid_json(self):
        big = {"blob": "x" * 200_000}
        rec = _rec("a", raw_payload=big)
        out = asyncio.run(_dlq_payload(None, 42, rec))
        parsed = json.loads(out)  # must be valid JSON — the P15 bug broke this
        assert parsed["truncated"] is True
        assert parsed["size_bytes"] > 32_000
        assert len(out) <= 32_000

    def test_dlq_normal_payload_inline(self):
        rec = _rec("a", raw_payload={"ok": True})
        out = asyncio.run(_dlq_payload(None, 42, rec))
        assert json.loads(out) == {"ok": True}

    def test_stage_cols_include_hashes_and_change_type(self):
        assert "identity_hash" in _STAGE_COLS
        assert "observation_hash" in _STAGE_COLS
        assert "change_type" in _STAGE_COLS

    def test_no_id_merge_uses_identity_hash(self):
        import ingestion.runner as runner

        assert "ON CONFLICT (provider, identity_hash)" in runner._MERGE_NO_ID
        assert "idx_psr_identity_noid" in runner._MERGE_NO_ID_INDEX


# ── web corpus streaming ──────────────────────────────────────────────


class _CorpusPool:
    """Keyset-paged fake for the documents table."""

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.fetch_calls: list[tuple] = []

    async def fetch(self, sql, after, limit):
        self.fetch_calls.append((after, limit))
        page = [d for d in self.docs if d["doc_id"] > after][:limit]
        return page


_DOC = {
    "doc_id": "doc_1",
    "canonical_url": "https://x.vn/",
    "title": "X",
    "metadata": {"telephone": "0901", "name": "Quán X"},
}


class TestCorpusStream:
    def test_keyset_paging_streams_all(self):
        docs = [dict(_DOC, doc_id=f"doc_{i}") for i in range(5)]
        pool = _CorpusPool(docs)
        ctx = _ctx()
        recs = asyncio.run(_collect(WebCorpusAdapter(pool=pool, page_size=2), ctx))
        assert len(recs) == 5
        # paged 3 fetches (2+2+1) + terminator fetch
        assert pool.fetch_calls == [("", 2), ("doc_1", 2), ("doc_3", 2), ("doc_4", 2)]
        assert ctx.checkpoint["after"] == "doc_4"

    def test_resume_from_doc_cursor(self):
        docs = [dict(_DOC, doc_id=f"doc_{i}") for i in range(5)]
        pool = _CorpusPool(docs)
        ctx = _ctx(checkpoint={"after": "doc_2"})
        recs = asyncio.run(_collect(WebCorpusAdapter(pool=pool, page_size=10), ctx))
        assert [r.external_id for r in recs] == ["doc_3", "doc_4"]

    def test_docs_iterable_lazy(self):
        """A generator must not be materialized — records stream through."""

        def gen():
            for i in range(3):
                yield dict(_DOC, doc_id=f"g_{i}")

        recs = asyncio.run(_collect(WebCorpusAdapter(docs=gen()), _ctx()))
        assert len(recs) == 3

    def test_docs_iterable_resume_skips_seen(self):
        docs = [dict(_DOC, doc_id=f"d_{i}") for i in range(4)]
        ctx = _ctx(checkpoint={"docs_seen": 2})
        recs = asyncio.run(_collect(WebCorpusAdapter(docs=docs), ctx))
        assert [r.external_id for r in recs] == ["d_2", "d_3"]
        assert ctx.checkpoint["docs_seen"] == 4
