#!/usr/bin/env python3
"""P1: Baseline latency measurement.

Đo latency thực tế cho 4 endpoints chính với 10 query VN đại diện.
3 lần đo mỗi query → P50/P95. Ghi kết quả vào baseline/P1_latency.json.

Usage (from search-router/):
    python scripts/bench_latency.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL = "http://localhost:8888"
REPEATS = 3
TIMEOUT_S = 30.0

# 10 query VN đại diện (mix web/places/news/research)
QUERIES = [
    # Web search
    {"endpoint": "/v1/search", "query": "cà phê ngon Hà Nội", "type": "web"},
    {"endpoint": "/v1/search", "query": "thời tiết Hà Nội hôm nay", "type": "web"},
    {"endpoint": "/v1/search", "query": "giá vàng hôm nay", "type": "web"},
    # Places — GET with query params (q), returns a bare list
    {"endpoint": "/v1/places/search", "method": "get", "q": "bún chả Hà Nội", "type": "places"},
    {"endpoint": "/v1/places/search", "method": "get", "q": "tiệm thuốc gần đây", "type": "places"},
    # News
    {"endpoint": "/v1/news", "query": "tin tức mới nhất", "type": "news"},
    # Fetch — the read endpoint, POST {"url": ...}
    {"endpoint": "/v1/read", "url": "https://vnexpress.net", "type": "fetch"},
    # Research
    {"endpoint": "/v1/research", "query": "tình hình kinh tế Việt Nam 2026", "type": "research"},
    {"endpoint": "/v1/research", "query": "chính sách tiền mới nhất", "type": "research"},
    {"endpoint": "/v1/search", "query": "khách sạn Nha Trang", "type": "web"},
]


async def measure_endpoint(
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    method: str = "post",
    repeats: int = REPEATS,
) -> dict[str, Any]:
    """Đo latency một endpoint với N repeats."""
    latencies: list[float] = []
    errors: list[str] = []
    result_count = 0

    for _ in range(repeats):
        try:
            t0 = time.perf_counter()
            if method == "get":
                resp = await client.get(
                    f"{BASE_URL}{endpoint}",
                    params=payload,
                    timeout=TIMEOUT_S,
                )
            else:
                resp = await client.post(
                    f"{BASE_URL}{endpoint}",
                    json=payload,
                    timeout=TIMEOUT_S,
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if resp.status_code == 200:
                # Failed probes must not skew p50/p95 — count them as errors.
                latencies.append(elapsed_ms)
                data = resp.json()
                if isinstance(data, list):
                    # e.g. GET /v1/places/search returns a bare list
                    result_count = len(data)
                elif endpoint == "/v1/read":
                    result_count = 1 if data.get("passages") else 0
                else:
                    result_count = len(data.get("results", []))
            else:
                errors.append(f"HTTP {resp.status_code}")
        except Exception as e:
            errors.append(str(e)[:100])

    if not latencies:
        return {
            "endpoint": endpoint,
            "error": errors[0] if errors else "no data",
            "p50_ms": None,
            "p95_ms": None,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "result_count": 0,
            "errors": len(errors),
        }

    latencies.sort()
    n = len(latencies)
    p50_idx = int(n * 0.5)
    p95_idx = min(int(n * 0.95), n - 1)

    return {
        "endpoint": endpoint,
        "p50_ms": round(latencies[p50_idx], 1),
        "p95_ms": round(latencies[p95_idx], 1),
        "avg_ms": round(statistics.mean(latencies), 1),
        "min_ms": round(latencies[0], 1),
        "max_ms": round(latencies[-1], 1),
        "result_count": result_count,
        "errors": len(errors),
        "all_latencies": [round(x, 1) for x in latencies],
    }


async def main() -> None:
    print("🔍 P1 Baseline Latency Measurement")
    print(f"   Base URL: {BASE_URL}")
    print(f"   Repeats: {REPEATS}")
    print(f"   Queries: {len(QUERIES)}")
    print()

    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        # Health check
        try:
            resp = await client.get(f"{BASE_URL}/v1/health", timeout=5.0)
            if resp.status_code != 200:
                print(f"⚠️  Health check failed: {resp.status_code}")
                return
            print("✅ Health check OK")
        except Exception as e:
            print(f"❌ Health check failed: {e}")
            return

        for i, q in enumerate(QUERIES, 1):
            endpoint = q["endpoint"]
            method = q.get("method", "post")
            payload = {k: v for k, v in q.items() if k not in ("endpoint", "type", "method")}
            qtype = q.get("type", "unknown")

            print(
                f"  [{i}/{len(QUERIES)}] {endpoint} ({qtype}): {payload.get('query', payload.get('q', payload.get('url', '')))[:50]}"
            )

            result = await measure_endpoint(client, endpoint, payload, method=method)
            result["query"] = payload.get("query", payload.get("q", payload.get("url", "")))
            result["type"] = qtype
            results.append(result)

            if result["p50_ms"] is not None:
                print(
                    f"      P50={result['p50_ms']}ms  P95={result['p95_ms']}ms  results={result['result_count']}"
                )
            else:
                print(f"      ERROR: {result.get('error', 'unknown')}")

    # ─── Summary ──────────────────────────────────────────────────────────────

    print()
    print("=" * 70)
    print("P1 BASELINE LATENCY SUMMARY")
    print("=" * 70)

    # Group by endpoint
    by_endpoint: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        ep = r["endpoint"]
        by_endpoint.setdefault(ep, []).append(r)

    summary: dict[str, Any] = {}
    for ep, items in by_endpoint.items():
        p50s = [x["p50_ms"] for x in items if x["p50_ms"] is not None]
        p95s = [x["p95_ms"] for x in items if x["p95_ms"] is not None]
        total_results = sum(x["result_count"] for x in items)
        total_errors = sum(x["errors"] for x in items)

        summary[ep] = {
            "queries": len(items),
            "p50_ms": round(statistics.median(p50s), 1) if p50s else None,
            "p95_ms": round(statistics.median(p95s), 1) if p95s else None,
            "avg_ms": round(
                statistics.mean([x["avg_ms"] for x in items if x["avg_ms"] is not None]), 1
            )
            if any(x["avg_ms"] for x in items)
            else None,
            "total_results": total_results,
            "zero_result_queries": sum(1 for x in items if x["result_count"] == 0),
            "errors": total_errors,
        }

        print(f"\n{ep}:")
        print(f"  Queries: {len(items)}")
        if p50s:
            print(f"  P50: {summary[ep]['p50_ms']}ms")
            print(f"  P95: {summary[ep]['p95_ms']}ms")
            print(f"  Avg: {summary[ep]['avg_ms']}ms")
        print(f"  Results: {total_results} (zero-result: {summary[ep]['zero_result_queries']})")
        print(f"  Errors: {total_errors}")

    # Zero-result rate
    total_queries = len(results)
    zero_results = sum(1 for r in results if r["result_count"] == 0)
    zero_rate = (zero_results / total_queries * 100) if total_queries else 0

    print(f"\n{'=' * 70}")
    print(f"Zero-result rate: {zero_results}/{total_queries} ({zero_rate:.1f}%)")
    print(f"{'=' * 70}")

    # ─── Write artifact ───────────────────────────────────────────────────────

    artifact = {
        "name": "P1_BASELINE_LATENCY",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "base_url": BASE_URL,
        "repeats": REPEATS,
        "summary": summary,
        "zero_result_rate": round(zero_rate, 1),
        "queries": results,
    }

    out_dir = Path(__file__).resolve().parents[1] / "baseline"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "P1_latency.json"
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n✅ Artifact written: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
