"""P17 live verification — real OpenSearch + PostGIS + Redis.

Skipped unless ``E2E=1``. Needs the live local stack (hub-postgres :5433,
OpenSearch :9200, Redis :6380). Seeds a small canonical fixture (marked
``p17live``), runs the real indexer + service on a dedicated event loop,
and cleans everything in ``finally`` — idempotent, per the e2e rules.
Nothing touches env or live connections at collection time.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("E2E") != "1",
        reason="P17 live tests — set E2E=1 with the local stack up",
    ),
]

_SR = Path(__file__).resolve().parents[1]
if str(_SR) not in sys.path:
    sys.path.insert(0, str(_SR))

HCMC = (10.7769, 106.7009)

_FIXTURES = [
    ("Nhà thuốc P17 Live 1", "health", "open", 10.777, 106.701),
    ("Nhà thuốc P17 Live 2", "health", "temporarily_closed", 10.779, 106.703),
    ("Quán cà phê P17 Live", "food", "open", 10.781, 106.705),
    ("Nhà thuốc P17 Live Closed", "health", "permanently_closed", 10.775, 106.700),
    ("Bệnh viện P17 Live Far", "health", "open", 10.95, 106.90),
]


class _LiveStack:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.pool = None
        self.os_index = None
        self.indexer = None
        self.service = None
        self.admin_id: int | None = None
        self.place_ids: list[int] = []

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    async def boot(self):
        import manage_keys

        manage_keys._load_dotenv()
        dsn = os.getenv("E2E_DSN") or manage_keys._resolve_dsn(None)
        if not dsn:
            pytest.skip("no database DSN resolved")

        import asyncpg
        from serving.places.cache import PlaceCache
        from serving.places.indexer import PlaceIndexer
        from serving.places.os_index import PlaceOSIndex
        from serving.places.service import PlaceService

        try:
            self.pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.skip(f"postgres unreachable: {exc!r}")

        self.os_index = PlaceOSIndex()
        row = await self.pool.fetchrow(
            """INSERT INTO administrative_units
               (code, name, normalized_name, type, admin_level, status,
                source, geometry)
               VALUES ('P17LIVE', 'P17 Live Ward', 'p17 live ward', 'phuong',
                       3, 'current', 'p17live',
                       ST_SetSRID(ST_GeomFromText(
                        'POLYGON((106.60 10.68, 106.82 10.68,'
                        ' 106.82 10.88, 106.60 10.88, 106.60 10.68))'), 4326))
               RETURNING unit_id"""
        )
        self.admin_id = int(row["unit_id"])

        for name, cat, status, lat, lon in _FIXTURES:
            r = await self.pool.fetchrow(
                """INSERT INTO canonical_places
                   (canonical_name, normalized_name, canonical_category,
                    address, normalized_address, lat, lon, location,
                    admin_unit_id, status, confidence, source_count,
                    last_seen, updated_at)
                   VALUES ($1, $2, $3, 'P17LIVE', 'p17live', $4, $5,
                    ST_SetSRID(ST_MakePoint($5, $4), 4326), $6, $7, 0.85, 2,
                    now(), now())
                   RETURNING place_id""",
                name,
                _fold(name),
                cat,
                lat,
                lon,
                self.admin_id,
                status,
            )
            self.place_ids.append(int(r["place_id"]))

        self.indexer = PlaceIndexer(self.pool, os_index=self.os_index)
        await self.indexer.ensure()

        async def _pool():
            return self.pool

        self.service = PlaceService(
            os_index=self.os_index,
            cache=PlaceCache(namespace="places_live_test"),
            pool_getter=_pool,
        )

    async def teardown(self):
        if self.pool is not None:
            await self.pool.execute(
                "DELETE FROM canonical_places WHERE normalized_address = 'p17live'"
            )
            if self.admin_id:
                await self.pool.execute(
                    "DELETE FROM administrative_units WHERE unit_id = $1",
                    self.admin_id,
                )
        if self.os_index is not None and self.place_ids:
            await self.os_index.delete_ids([str(p) for p in self.place_ids])
        if self.pool is not None:
            await self.pool.close()


def _fold(raw: str) -> str:
    from serving.places.query import fold_text

    return fold_text(raw)


@pytest.fixture()
def live_stack():
    stack = _LiveStack()
    try:
        stack.run(stack.boot())
    except Exception:
        stack.loop.close()
        raise
    yield stack
    stack.run(stack.teardown())
    stack.loop.close()


def _rebuild(stack: _LiveStack) -> None:
    res = stack.run(stack.indexer.rebuild(batch_size=2))
    assert res["status"] == "done", res


class TestLiveIndexSync:
    def test_full_rebuild(self, live_stack):
        _rebuild(live_stack)
        stats = live_stack.run(live_stack.os_index.stats())
        assert stats["docs"] >= len(live_stack.place_ids)

    def test_incremental_roundtrip(self, live_stack):
        res = live_stack.run(live_stack.indexer.sync(batch_size=2))
        assert res["status"] == "done"
        assert res["scanned"] >= len(live_stack.place_ids)

    def test_delete_tombstone(self, live_stack):
        _rebuild(live_stack)
        pid = live_stack.place_ids[0]
        res = live_stack.run(live_stack.indexer.delete(pid))
        assert res["deleted"] is True
        doc = live_stack.run(live_stack.os_index.get_doc(str(pid)))
        assert doc is None


class TestLiveSearch:
    def test_text_geo_search(self, live_stack):
        _rebuild(live_stack)
        rows, meta = live_stack.run(
            live_stack.service.search(
                q="nhà thuốc p17 live", lat=HCMC[0], lon=HCMC[1], radius_m=3000
            )
        )
        ids = {r["place_id"] for r in rows}
        assert live_stack.place_ids[0] in ids
        assert live_stack.place_ids[3] not in ids  # permanently_closed excluded
        assert live_stack.place_ids[4] not in ids  # out of radius
        assert rows[0]["distance_m"] is not None
        assert "opensearch" in meta.lanes

    def test_admin_contains(self, live_stack):
        _rebuild(live_stack)
        rows, meta = live_stack.run(
            live_stack.service.search(admin_unit_id=live_stack.admin_id, admin_contains=True)
        )
        assert meta.lanes == ["postgis"]
        ids = {r["place_id"] for r in rows}
        assert live_stack.place_ids[0] in ids

    def test_autocomplete(self, live_stack):
        _rebuild(live_stack)
        rows, meta = live_stack.run(live_stack.service.autocomplete(q="nhà thuốc p17", limit=5))
        names = [r["name"] for r in rows]
        assert any("P17 Live" in n for n in names)

    def test_detail_and_degraded(self, live_stack):
        _rebuild(live_stack)
        res = live_stack.run(live_stack.service.get_place(live_stack.place_ids[0]))
        assert res.status == "ok"
        assert res.payload["canonical_name"] == "Nhà thuốc P17 Live 1"
        assert res.payload["sources"] is not None

        async def _none():
            return None

        live_stack.service._pool_getter = _none
        res2 = live_stack.run(live_stack.service.get_place(live_stack.place_ids[0]))
        # cached or index-served; flagged degraded when served off-index
        assert res2.status in ("ok", "unavailable")
        if res2.status == "ok":
            assert res2.payload.get("degraded") in (True, None)

    def test_os_down_postgis_lane(self, live_stack):
        _rebuild(live_stack)
        real_search = live_stack.os_index.search

        async def _down(*a, **kw):
            from serving.places.os_index import PlaceIndexUnavailable

            raise PlaceIndexUnavailable("forced down for test")

        live_stack.os_index.search = _down
        try:
            rows, meta = live_stack.run(
                live_stack.service.search(q="nhà thuốc", lat=HCMC[0], lon=HCMC[1], radius_m=3000)
            )
            assert "postgis" in meta.lanes and "opensearch" in meta.degraded
            assert any(r["place_id"] == live_stack.place_ids[0] for r in rows)
        finally:
            live_stack.os_index.search = real_search
