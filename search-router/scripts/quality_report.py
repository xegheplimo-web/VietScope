#!/usr/bin/env python3
"""Generate a markdown quality report from the Search Hub metrics DB.

Usage:
    python scripts/quality_report.py [--days 7] [--db PATH] [--out PATH]

Reads the SQLite metrics DB (default: $METRICS_DB_PATH, else the per-user
data dir — %LOCALAPPDATA%/search-hub on Windows, ~/.local/share/search-hub
otherwise) and writes a markdown report with:
  - Aggregate overview (total queries, avg quality, avg latency, cache hit rate)
  - Daily quality trend table + sparkline
  - Per-endpoint breakdown
  - Worst 5 queries (lowest quality score)

Exit code 0 on success, 1 if DB is empty/unreadable.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC
from pathlib import Path


def _default_db() -> Path:
    env = os.getenv("METRICS_DB_PATH")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "search-hub" / "search_metrics.db"


_DEFAULT_DB = _default_db()


def _utc_now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sparkline(values: list[float], chars: str = "▁▂▃▄▅▆▇█") -> str:
    """Render a unicode sparkline from a list of 0-1 floats."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo if hi > lo else 1.0
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(chars) - 1))
        out.append(chars[max(0, min(len(chars) - 1, idx))])
    return "".join(out)


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


def build_report(db_path: Path, days: int = 7) -> str:
    if not db_path.exists():
        return f"# Search Hub Quality Report\n\n**Error:** metrics DB not found at `{db_path}`\n"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Overview
        ov = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(quality_score) AS scored,
                COALESCE(AVG(quality_score), 0) AS avg_quality,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                COALESCE(AVG(results_count), 0) AS avg_results,
                COALESCE(AVG(coverage), 0) AS avg_coverage,
                COALESCE(AVG(relevance), 0) AS avg_relevance,
                COALESCE(AVG(dedup_ratio), 0) AS avg_dedup,
                SUM(cache_hit) AS cache_hits,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
            FROM query_log
            """
        ).fetchone()

        if not ov or ov["total"] == 0:
            return "# Search Hub Quality Report\n\n**No queries logged yet.**\n"

        # Trend
        trend = conn.execute(
            """
            SELECT
                substr(timestamp, 1, 10) AS day,
                COUNT(*) AS queries,
                COUNT(quality_score) AS scored,
                COALESCE(AVG(quality_score), 0) AS avg_quality,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                COALESCE(AVG(results_count), 0) AS avg_results,
                SUM(cache_hit) AS cache_hits,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
            FROM query_log
            WHERE timestamp >= datetime('now', ?)
            GROUP BY substr(timestamp, 1, 10)
            ORDER BY day ASC
            """,
            (f"-{days} days",),
        ).fetchall()

        # Per-endpoint
        per_ep = conn.execute(
            """
            SELECT endpoint,
                   COUNT(*) AS queries,
                   COALESCE(AVG(quality_score), 0) AS avg_quality,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                   COALESCE(AVG(results_count), 0) AS avg_results,
                   SUM(cache_hit) AS cache_hits,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
            FROM query_log
            GROUP BY endpoint
            ORDER BY queries DESC
            """
        ).fetchall()

        # Worst 5
        worst = conn.execute(
            """
            SELECT timestamp, query, endpoint, quality_score, latency_ms, results_count, error
            FROM query_log
            WHERE quality_score IS NOT NULL
            ORDER BY quality_score ASC
            LIMIT 5
            """
        ).fetchall()
    finally:
        conn.close()

    lines: list[str] = []
    lines.append("# Search Hub Quality Report")
    lines.append("")
    lines.append(f"_Generated: {_utc_now_iso()}_")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total queries | {ov['total']} |")
    lines.append(f"| Scored queries | {ov['scored']} |")
    lines.append(f"| Avg quality score | {ov['avg_quality']:.4f} |")
    lines.append(f"| Avg latency | {ov['avg_latency_ms']:.0f} ms |")
    lines.append(f"| Avg results/query | {ov['avg_results']:.2f} |")
    lines.append(f"| Avg coverage | {ov['avg_coverage']:.4f} |")
    lines.append(f"| Avg relevance | {ov['avg_relevance']:.4f} |")
    lines.append(f"| Avg dedup ratio | {ov['avg_dedup']:.4f} |")
    lines.append(f"| Cache hits | {ov['cache_hits']} ({_pct(ov['cache_hits'], ov['total'])}) |")
    lines.append(f"| Errors | {ov['errors']} ({_pct(ov['errors'], ov['total'])}) |")
    lines.append("")

    # Trend
    lines.append(f"## Quality Trend (last {days} days)")
    lines.append("")
    if trend:
        spark = _sparkline([r["avg_quality"] or 0.0 for r in trend])
        lines.append(f"Quality sparkline: `{spark}`")
        lines.append("")
        lines.append(
            "| Day | Queries | Scored | Avg Quality | Avg Latency (ms) | Avg Results | Cache Hits | Errors |"
        )
        lines.append(
            "|-----|---------|--------|-------------|------------------|-------------|------------|--------|"
        )
        for r in trend:
            lines.append(
                f"| {r['day']} | {r['queries']} | {r['scored']} | "
                f"{r['avg_quality']:.4f} | {r['avg_latency_ms']:.0f} | "
                f"{r['avg_results']:.2f} | {r['cache_hits']} | {r['errors']} |"
            )
    else:
        lines.append("_No data in the selected window._")
    lines.append("")

    # Per-endpoint
    lines.append("## Per-Endpoint Breakdown")
    lines.append("")
    lines.append(
        "| Endpoint | Queries | Avg Quality | Avg Latency (ms) | Avg Results | Cache Hits | Errors |"
    )
    lines.append(
        "|----------|---------|-------------|------------------|-------------|------------|--------|"
    )
    for r in per_ep:
        lines.append(
            f"| {r['endpoint']} | {r['queries']} | {r['avg_quality']:.4f} | "
            f"{r['avg_latency_ms']:.0f} | {r['avg_results']:.2f} | "
            f"{r['cache_hits']} | {r['errors']} |"
        )
    lines.append("")

    # Worst 5
    lines.append("## Worst 5 Queries (lowest quality)")
    lines.append("")
    if worst:
        lines.append("| Timestamp | Query | Endpoint | Quality | Latency (ms) | Results | Error |")
        lines.append("|-----------|-------|----------|---------|--------------|---------|-------|")
        for r in worst:
            q = (r["query"] or "")[:60].replace("|", "\\|")
            err = (r["error"] or "")[:40].replace("|", "\\|")
            lines.append(
                f"| {r['timestamp'][:19]} | {q} | {r['endpoint']} | "
                f"{r['quality_score']:.4f} | {r['latency_ms']} | "
                f"{r['results_count']} | {err} |"
            )
    else:
        lines.append("_No scored queries._")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Search Hub quality report")
    ap.add_argument("--days", type=int, default=7, help="Trend window in days (default 7)")
    ap.add_argument("--db", type=str, default=str(_DEFAULT_DB), help="Path to metrics SQLite DB")
    ap.add_argument("--out", type=str, default="", help="Output file (default: stdout)")
    args = ap.parse_args()

    report = build_report(Path(args.db), args.days)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
