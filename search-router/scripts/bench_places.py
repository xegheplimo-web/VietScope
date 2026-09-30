#!/usr/bin/env python3
"""P17 benchmark — fixture seeding + latency/throughput measurement.

Seeds synthetic canonical places (marked ``p17bench`` in
``normalized_address``), full-rebuilds an isolated ``places_bench``
OpenSearch index (never the production alias), drives the real
``PlaceService`` read path through a scenario suite, reports
p50/p95/p99, per-lane timings, cache hit rate, and throughput — then
cleans every fixture row/doc unless ``--keep``.

Examples:
    uv run python -m scripts.bench_places --size 10000
    uv run python -m scripts.bench_places --size 100000 --iters 60 --keep

Nothing here mutates canonical data semantics — fixture rows are plain
canonical_places inserts deleted in ``finally``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

_SR = Path(__file__).resolve().parents[1]
if str(_SR) not in sys.path:
    sys.path.insert(0, str(_SR))

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402

# Ho Chi Minh City center — fixture cloud anchor (deliberately far from
# the Hanoi coordinates used by the P16 endpoint tests).
HCMC = (10.7769, 106.7009)
HANOI = (21.0278, 105.8342)
DANANG = (16.0544, 108.2022)
CENTERS = [HCMC, HANOI, DANANG]

CATEGORIES = [
    "food",
    "retail",
    "health",
    "lodging",
    "education",
    "transport",
    "finance",
    "services",
    "worship",
    "tourism",
]
NAME_TEMPLATES = [
    "Nhà thuốc P17 {i}",
    "Quán cà phê P17 {i}",
    "Siêu thị P17 {i}",
    "Khách sạn P17 {i}",
    "Trạm xăng P17 {i}",
    "Phòng khám P17 {i}",
    "Bến xe P17 {i}",
    "Cửa hàng P17 {i}",
    "Chùa P17 {i}",
    "Ngân hàng P17 {i}",
]


def _norm(raw: str) -> str:
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", raw.replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_SEED_SQL = """
INSERT INTO canonical_places
    (business_id, canonical_name, normalized_name, canonical_category,
     address, normalized_address, phone, website, website_domain,
     opening_hours, lat, lon, location, admin_unit_id, status,
     confidence, source_count, last_seen, updated_at)
SELECT CASE WHEN g % 5 = 0 THEN $2::bigint ELSE NULL END,
       name || ' ' || g, norm_name || g, cat,
       'P17BENCH ' || g || ' Bench Ward',
       'p17bench ' || g || ' bench ward',
       CASE WHEN g % 3 = 0 THEN '+8490' || lpad((g % 10000000)::text, 7, '0') END,
       CASE WHEN g % 4 = 0 THEN 'https://p17bench-' || (g % 977) || '.example.vn' END,
       CASE WHEN g % 4 = 0 THEN 'p17bench-' || (g % 977) || '.example.vn' END,
       CASE WHEN g % 2 = 0 THEN jsonb_build_object('raw', '8-20') END,
       lat, lon, ST_SetSRID(ST_MakePoint(lon, lat), 4326),
       admin_id, st, conf, scount, seen, seen
FROM (
    SELECT g,
           (ARRAY[{names}])[g % {n_names} + 1] AS name,
           (ARRAY[{norms}])[g % {n_names} + 1] AS norm_name,
           (ARRAY[{cats}])[g % {n_cats} + 1] AS cat,
           (ARRAY[{admin_ids}])[g % {n_admin} + 1]::bigint AS admin_id,
           (ARRAY['open','open','open','open','open','open','open','open',
                  'temporarily_closed','unknown','permanently_closed','open'])[g % 12 + 1] AS st,
           ((g % 97)::float / 97.0) AS conf,
           1 + (g % 3) AS scount,
           now() - ((g % 400) || ' days')::interval AS seen,
           {lat} + ((g % {grid}) - {half}) * 0.0009
                  + (g / {grid}) % {grid} * 0.00002 AS lat,
           {lon} + (g / {grid}) % {grid} * 0.0009
                  + ((g % {grid}) - {half}) * 0.00002 AS lon
    FROM generate_series(1, $1) g
) s
RETURNING place_id
"""


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return round(s[k], 2)


async def _seed(pool, size: int, admin_ids: list[int], biz_id: int | None) -> tuple[int, int]:
    """INSERT..SELECT generate_series — deterministic fixture, one query."""
    names = ",".join("'" + t.format(i="").rstrip().replace("'", "''") + "'" for t in NAME_TEMPLATES)
    # folded name prefixes — every template ends in "{i}", so the folded
    # prefix + ``g`` reproduces fold_text(name) without SQL diacritics work.
    norms = ",".join("'" + _norm(t.format(i="")) + " '" for t in NAME_TEMPLATES)
    cats = ",".join(f"'{c}'" for c in CATEGORIES)
    admins = ",".join(str(a) for a in admin_ids)
    # Spread points on a sqrt-sized grid around each center; rotate centers.
    grid = int(math.sqrt(size)) + 1
    ids: list[int] = []
    for clat, clon in CENTERS:
        sql = _SEED_SQL.format(
            names=names,
            norms=norms,
            n_names=len(NAME_TEMPLATES),
            cats=cats,
            n_cats=len(CATEGORIES),
            admin_ids=admins,
            n_admin=len(admin_ids),
            grid=grid,
            half=grid // 2,
            lat=clat,
            lon=clon,
        )
        rows = await pool.fetch(sql, size // len(CENTERS), biz_id)
        ids.extend(int(r["place_id"]) for r in rows)
    return min(ids), max(ids)


async def _seed_admin(pool) -> list[int]:
    """Three fixture admin units: squares around each center."""
    ids = []
    for i, (clat, clon) in enumerate(CENTERS):
        r = await pool.fetchrow(
            """INSERT INTO administrative_units
               (code, name, normalized_name, type, admin_level, status,
                source, geometry)
               VALUES ($1, $2, $3, 'phuong', 3, 'current', 'p17bench',
                ST_SetSRID(ST_GeomFromText($4), 4326))
               RETURNING unit_id""",
            f"P17B{i}",
            f"P17 Bench Ward {i}",
            f"p17 bench ward {i}",
            (
                f"POLYGON(({clon - 0.22} {clat - 0.22}, {clon + 0.22} {clat - 0.22},"
                f" {clon + 0.22} {clat + 0.22}, {clon - 0.22} {clat + 0.22},"
                f" {clon - 0.22} {clat - 0.22}))"
            ),
        )
        ids.append(int(r["unit_id"]))
    return ids


async def _cleanup(pool, os_index, place_range, admin_ids, biz_id, concrete_names):
    # marker-based deletes run even when the seed crashed midway
    await pool.execute(
        "DELETE FROM place_sources WHERE provider = 'p17bench' OR place_id IN"
        " (SELECT place_id FROM canonical_places"
        "  WHERE normalized_address LIKE 'p17bench%')"
    )
    await pool.execute("DELETE FROM place_source_records WHERE provider = 'p17bench'")
    await pool.execute(
        "DELETE FROM canonical_places WHERE normalized_address LIKE 'p17bench%'"
        " OR address LIKE 'P17BENCH %'"
    )
    if admin_ids:
        await pool.execute(
            "DELETE FROM administrative_units WHERE unit_id = ANY($1::bigint[])",
            admin_ids,
        )
    if biz_id:
        await pool.execute(
            "DELETE FROM canonical_businesses WHERE business_id = $1 AND"
            " display_name LIKE 'P17 Bench%'",
            biz_id,
        )
    for name in concrete_names:
        await os_index.drop_index(name)
    await pool.execute("DELETE FROM serving_index_state WHERE index_name = 'places_bench'")


def _scenarios(size: int, admin_id: int, sample_pid: int):
    return [
        ("text_only", {"q": "nhà thuốc p17"}),
        ("geo_only", {"lat": HCMC[0], "lon": HCMC[1], "radius_m": 3000}),
        ("text_geo", {"q": "p17", "lat": HCMC[0], "lon": HCMC[1], "radius_m": 3000}),
        ("near_me_intent", {"q": "nhà thuốc gần tôi", "lat": HCMC[0], "lon": HCMC[1]}),
        ("category", {"category": "health", "lat": HCMC[0], "lon": HCMC[1], "radius_m": 5000}),
        ("admin_area", {"admin_unit_id": admin_id}),
        ("status_open", {"status": "open", "lat": HCMC[0], "lon": HCMC[1], "radius_m": 5000}),
        ("bbox", {"bbox": f"{HCMC[1] - 0.05},{HCMC[0] - 0.05},{HCMC[1] + 0.05},{HCMC[0] + 0.05}"}),
        (
            "autocomplete",
            {"_kind": "autocomplete", "q": "nha thuoc p1", "lat": HCMC[0], "lon": HCMC[1]},
        ),
        ("by_id", {"_kind": "detail", "place_id": sample_pid}),
    ]


async def _run_bench(pool, size: int, iters: int, keep: bool) -> dict:
    from serving.places.cache import PlaceCache
    from serving.places.indexer import PlaceIndexer
    from serving.places.os_index import PlaceOSIndex
    from serving.places.service import PlaceService

    os_index = PlaceOSIndex(index="places_bench")
    cache = PlaceCache(namespace="places_bench")

    admin_ids: list[int] = []
    biz_id: int | None = None
    place_range = None
    concrete_names: list[str] = []
    try:
        admin_ids = await _seed_admin(pool)
        biz = await pool.fetchrow(
            "INSERT INTO canonical_businesses (display_name, normalized_name)"
            " VALUES ('P17 Bench Corp', 'p17 bench corp') RETURNING business_id"
        )
        biz_id = int(biz["business_id"]) if biz else None
        place_range = await _seed(pool, size, admin_ids, biz_id)
        await pool.execute(
            "INSERT INTO place_source_records (provider, external_id, raw_name,"
            " observed_at) SELECT 'p17bench', 'b' || g,"
            " 'NT P17 variant ' || g, now() FROM generate_series(1, $1) g",
            max(1, size // 50),
        )
        recs = await pool.fetch("SELECT id FROM place_source_records WHERE provider = 'p17bench'")
        lo = place_range[0]
        for i, r in enumerate(recs):
            await pool.execute(
                "INSERT INTO place_sources (place_id, source_record_id,"
                " provider, external_id) VALUES ($1, $2, 'p17bench', $3)"
                " ON CONFLICT DO NOTHING",
                lo + i * 50,
                r["id"],
                f"b{i}",
            )

        indexer = PlaceIndexer(pool, os_index=os_index, cache=cache)
        t0 = time.perf_counter()
        await indexer.ensure()
        rebuild = await indexer.rebuild(batch_size=2000)
        index_ms = round((time.perf_counter() - t0) * 1000)
        stats = await os_index.stats()

        async def _pool_getter():
            return pool

        service = PlaceService(os_index=os_index, cache=cache, pool_getter=_pool_getter)
        sample_pid = lo + size // 2

        report: dict = {
            "fixture_size": size,
            "rebuild": rebuild,
            "index": stats,
            "index_ms": index_ms,
            "scenarios": {},
        }

        concrete = await os_index.current_concrete()
        concrete_names = [concrete] if concrete else []

        for name, kw in _scenarios(size, admin_ids[0], sample_pid):
            lats: list[float] = []
            lane_ms = {"os": [], "pg": [], "fusion": []}
            hits = 0
            cold: dict = {}
            for _ in range(iters):
                # deterministic query drift so identical keys still hit cache
                kw2 = dict(kw)
                kind = kw2.pop("_kind", "search")
                if kind == "autocomplete":
                    rows, meta = await service.autocomplete(**kw2)
                elif kind == "detail":
                    res = await service.get_place(kw2["place_id"])
                    meta = res.meta
                else:
                    rows, meta = await service.search(**kw2)
                lats.append(meta.total_ms)
                hits += 1 if meta.cache_hit else 0
                lane_ms["os"].append(meta.os_ms)
                lane_ms["pg"].append(meta.pg_ms)
                lane_ms["fusion"].append(meta.fusion_ms)
                if not meta.cache_hit and not cold:
                    cold = {
                        "total_ms": meta.total_ms,
                        "os_ms": meta.os_ms,
                        "pg_ms": meta.pg_ms,
                        "fusion_ms": meta.fusion_ms,
                        "lanes": meta.lanes,
                        "degraded": meta.degraded,
                    }
            report["scenarios"][name] = {
                "p50_ms": _percentile(lats, 50),
                "p95_ms": _percentile(lats, 95),
                "p99_ms": _percentile(lats, 99),
                "mean_ms": round(statistics.fmean(lats), 2),
                "cache_hit_rate": round(hits / max(1, iters), 3),
                "cold": cold,
                "os_p50_ms": _percentile(lane_ms["os"], 50),
                "pg_p50_ms": _percentile(lane_ms["pg"], 50),
                "fusion_p50_ms": _percentile(lane_ms["fusion"], 50),
            }

        # throughput: sequential + concurrent mixed queries
        seq_n = min(200, iters * 4)
        t = time.perf_counter()
        for i in range(seq_n):
            await service.search(q=f"p17 {i % 97}", lat=HCMC[0], lon=HCMC[1], radius_m=4000)
        seq_s = time.perf_counter() - t

        conc_n = min(400, iters * 8)
        t = time.perf_counter()
        chunk = 25
        for off in range(0, conc_n, chunk):
            await asyncio.gather(
                *[
                    service.search(q=f"p17 {i % 131}", lat=HCMC[0], lon=HCMC[1], radius_m=4000)
                    for i in range(off, min(off + chunk, conc_n))
                ]
            )
        conc_s = time.perf_counter() - t

        report["throughput"] = {
            "sequential_qps": round(seq_n / seq_s, 1),
            "concurrent25_qps": round(conc_n / conc_s, 1),
        }
        return report
    finally:
        if not keep:
            await _cleanup(pool, os_index, place_range, admin_ids, biz_id, concrete_names)


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P17 places serving benchmark")
    ap.add_argument("--size", type=int, default=10_000)
    ap.add_argument("--iters", type=int, default=40, help="iterations per scenario")
    ap.add_argument("--keep", action="store_true", help="keep fixture data + index")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args(argv)

    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("no database DSN configured", file=sys.stderr)
        return 2

    import asyncpg

    pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=12)
    try:
        report = await _run_bench(pool, args.size, args.iters, args.keep)
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
