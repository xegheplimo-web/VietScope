#!/usr/bin/env python3
"""LOCAL-1 benchmark: before/after local business discovery.

Runs a fixed query set against POST /v1/business/search and reports
unique-useful-place recall + latency (P50/P95) + provider lane usage.

Usage (from search-router/):
    python scripts/bench_local_discovery.py --label before
    python scripts/bench_local_discovery.py --label after
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8888"
REPEATS = 1
TIMEOUT_S = 120.0

# Local/business queries — mix of covered and uncovered areas, multiple
# categories. Acceptance gate: 20-30 queries; this set is the fast subset
# plus the canonical-coverage control queries.
#
# Yên Dũng anchor: the 2025 reorganisation dissolved huyện Yên Dũng into
# phường Yên Dũng (Bắc Ninh). Coordinates below are the Nominatim-verified
# centroid of the Yên Dũng / Nếnh area — the same point is used for BEFORE
# and AFTER so the recall comparison is apples-to-apples.
_YEN_DUNG = {"lat": 21.2135, "lon": 106.1488}
_HA_NOI = {"lat": 21.0278, "lon": 105.8342}
_HCM = {"lat": 10.7769, "lon": 106.7009}
_BAC_GIANG = {"lat": 21.2731, "lon": 106.1946}
_NHA_TRANG = {"lat": 12.2388, "lon": 109.1967}

QUERIES: list[dict[str, Any]] = [
    # Uncovered area (Yên Dũng) — the motivating case
    {"query": "quán ăn Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    {"query": "quán ăn đêm Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    {"query": "quán ăn khuya Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    {"query": "cafe Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    {"query": "nhà thuốc Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    {"query": "cây xăng Yên Dũng", **_YEN_DUNG, "radius_km": 10.0},
    # Covered areas — regression control (must NOT get worse)
    {"query": "quán ăn Bắc Giang", **_BAC_GIANG, "radius_km": 5.0},
    {"query": "cafe Hà Nội", **_HA_NOI, "radius_km": 3.0},
    {"query": "khách sạn Nha Trang", **_NHA_TRANG, "radius_km": 3.0},
    {"query": "nhà thuốc Hà Nội", **_HA_NOI, "radius_km": 3.0},
    {"query": "quán phở Hà Nội", **_HA_NOI, "radius_km": 3.0},
    {"query": "quán ăn TP Hồ Chí Minh", **_HCM, "radius_km": 3.0},
]


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


async def probe(client: httpx.AsyncClient, item: dict[str, Any]) -> dict[str, Any]:
    payload = {"query": item["query"], "limit": 20}
    for key in ("lat", "lon", "radius_km", "category"):
        if key in item:
            payload[key] = item[key]
    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{BASE_URL}/v1/business/search", json=payload, timeout=TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as exc:  # noqa: BLE001
        return {
            "query": item["query"],
            "error": str(exc)[:200],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "entities": [],
            "unique": 0,
            "provider": "",
            "with_geo": 0,
        }
    if resp.status_code != 200:
        return {
            "query": item["query"],
            "error": f"HTTP {resp.status_code}",
            "elapsed_ms": round(elapsed_ms, 1),
            "entities": [],
            "unique": 0,
            "provider": "",
            "with_geo": 0,
        }
    data = resp.json()
    entities = data.get("entities") or []
    names = {_norm(e.get("name", "")) for e in entities if e.get("name")}
    with_geo = sum(1 for e in entities if e.get("lat") is not None and e.get("lon") is not None)
    return {
        "query": item["query"],
        "elapsed_ms": round(elapsed_ms, 1),
        "provider": data.get("provider", ""),
        "count": len(entities),
        "unique": len(names),
        "with_geo": with_geo,
        "entities": [
            {
                "name": e.get("name"),
                "category": e.get("category"),
                "address": e.get("address"),
                "phone": e.get("phone"),
                "lat": e.get("lat"),
                "lon": e.get("lon"),
                "source_url": e.get("source_url"),
            }
            for e in entities
        ],
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="before")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    print(f"LOCAL-1 benchmark [{args.label}] — {len(QUERIES)} queries @ {BASE_URL}")
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for i, item in enumerate(QUERIES, 1):
            r = await probe(client, item)
            results.append(r)
            print(
                f"  [{i}/{len(QUERIES)}] {item['query'][:40]:42s} "
                f"count={r.get('count', 0):3d} unique={r.get('unique', 0):3d} "
                f"geo={r.get('with_geo', 0):3d} {r.get('elapsed_ms', 0):8.1f}ms "
                f"lanes={r.get('provider', '')}"
            )

    lat = [r["elapsed_ms"] for r in results if "elapsed_ms" in r]
    lat.sort()
    total_unique = sum(r.get("unique", 0) for r in results)
    zero = sum(1 for r in results if r.get("unique", 0) == 0)
    with_geo = sum(r.get("with_geo", 0) for r in results)

    # global dedupe across all queries (name-normalized)
    all_names: set[str] = set()
    for r in results:
        for e in r.get("entities", []):
            if e.get("name"):
                all_names.add(_norm(e["name"]))

    def _pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        idx = min(int(len(vals) * p), len(vals) - 1)
        return round(vals[idx], 1)

    summary = {
        "label": args.label,
        "queries": len(results),
        "total_unique_per_query": total_unique,
        "global_unique_places": len(all_names),
        "entities_with_geo": with_geo,
        "zero_result_queries": zero,
        "p50_ms": _pct(lat, 0.5),
        "p95_ms": _pct(lat, 0.95),
        "max_ms": round(lat[-1], 1) if lat else 0.0,
    }
    print("\n" + "=" * 72)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("=" * 72)

    out_dir = (Path(__file__).resolve().parents[1] / "baseline").resolve()
    out_dir.mkdir(exist_ok=True)
    if args.out:
        # --out is a bare file name inside baseline/, never a path — the
        # allowlist rejects separators and parent references outright.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.out):
            raise SystemExit("--out must be a plain file name ([A-Za-z0-9._-], no slashes)")
        out_path = out_dir / args.out
    else:
        out_path = out_dir / f"local1_{args.label}.json"
    out_path.write_text(
        json.dumps({"summary": summary, "queries": results}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nArtifact: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
