"""SQLite-backed metrics store for the feedback & evaluation loop.

Schema (single table `query_log`):
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    timestamp       TEXT    (ISO-8601 UTC)
    query           TEXT
    endpoint        TEXT    (/search | /answer | /research | /code_search | /fetch)
    query_type      TEXT    (web | news | image | research | code | answer)
    results_count   INTEGER
    latency_ms      INTEGER
    providers       TEXT    (comma-separated provider names)
    cache_hit       INTEGER (0 | 1)
    quality_score   REAL    (0.0 - 1.0, NULL if not computed)
    coverage        REAL    (0.0 - 1.0, NULL if N/A)
    relevance       REAL    (0.0 - 1.0, NULL if N/A)
    dedup_ratio     REAL    (0.0 - 1.0, NULL if N/A)
    error           TEXT    (NULL on success, error message on failure)

Design notes:
- Uses stdlib `sqlite3` only (no extra dependency).
- Thread-safe via a module-level `threading.Lock` (FastAPI runs sync endpoints
  in a threadpool, and async endpoints call this from sync context).
- DB file path comes from `settings.metrics_db_path` (env `METRICS_DB_PATH`);
  the default lives in the per-user data dir (`%LOCALAPPDATA%/search-hub` on
  Windows, `~/.local/share/search-hub` otherwise) so writes never dirty the
  source tree.
- `WAL` journal mode for better concurrent read/write.
- All writes are best-effort: a metrics failure must NEVER break a search.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import settings

# ── Config ────────────────────────────────────────────────────────────────────

_DB_PATH = Path(settings.metrics_db_path)
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Latency target in ms for the quality-score latency component.
_LATENCY_TARGET_MS = 5000
# Minimum results considered "good coverage".
_COVERAGE_MIN_RESULTS = 3


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ── Store ─────────────────────────────────────────────────────────────────────


class MetricsStore:
    """Thread-safe SQLite metrics store.

    A single shared instance (`metrics_store`) is used app-wide. The DB schema
    is created lazily on first use and is idempotent.
    """

    def __init__(self, db_path: str | Path = _DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()

    # -- internal -----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            try:
                conn = self._conn()
                try:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS query_log (
                            id            INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp     TEXT    NOT NULL,
                            query         TEXT    NOT NULL,
                            endpoint      TEXT    NOT NULL,
                            query_type    TEXT    NOT NULL DEFAULT '',
                            results_count INTEGER NOT NULL DEFAULT 0,
                            latency_ms    INTEGER NOT NULL DEFAULT 0,
                            providers     TEXT    NOT NULL DEFAULT '',
                            cache_hit     INTEGER NOT NULL DEFAULT 0,
                            quality_score REAL,
                            coverage      REAL,
                            relevance     REAL,
                            dedup_ratio   REAL,
                            error         TEXT
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_query_log_ts ON query_log(timestamp)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_query_log_endpoint ON query_log(endpoint)"
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                # Metrics must never block startup.
                pass

    # -- public API ---------------------------------------------------------

    def record(
        self,
        *,
        query: str,
        endpoint: str,
        query_type: str = "",
        results_count: int = 0,
        latency_ms: int = 0,
        providers: str = "",
        cache_hit: bool = False,
        quality_score: float | None = None,
        coverage: float | None = None,
        relevance: float | None = None,
        dedup_ratio: float | None = None,
        error: str | None = None,
    ) -> int | None:
        """Insert a query-log row. Returns row id or None on failure."""
        try:
            with self._lock:
                conn = self._conn()
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO query_log (
                            timestamp, query, endpoint, query_type,
                            results_count, latency_ms, providers, cache_hit,
                            quality_score, coverage, relevance, dedup_ratio, error
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            _utc_now_iso(),
                            query[:1000],
                            endpoint,
                            query_type,
                            int(results_count),
                            int(latency_ms),
                            providers,
                            1 if cache_hit else 0,
                            quality_score,
                            coverage,
                            relevance,
                            dedup_ratio,
                            error,
                        ),
                    )
                    conn.commit()
                    return cur.lastrowid
                finally:
                    conn.close()
        except Exception:
            return None

    def recent_queries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent `limit` query-log rows (newest first)."""
        try:
            conn = self._conn()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM query_log ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            return []

    def quality_overview(self) -> dict[str, Any]:
        """Aggregate quality metrics across all logged queries."""
        try:
            conn = self._conn()
            try:
                row = conn.execute(
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
                return {
                    "total_queries": row[0] or 0,
                    "scored_queries": row[1] or 0,
                    "avg_quality": round(row[2] or 0.0, 4),
                    "avg_latency_ms": round(row[3] or 0.0, 1),
                    "avg_results": round(row[4] or 0.0, 2),
                    "avg_coverage": round(row[5] or 0.0, 4),
                    "avg_relevance": round(row[6] or 0.0, 4),
                    "avg_dedup_ratio": round(row[7] or 0.0, 4),
                    "cache_hits": row[8] or 0,
                    "errors": row[9] or 0,
                }
            finally:
                conn.close()
        except Exception:
            return {
                "total_queries": 0,
                "scored_queries": 0,
                "avg_quality": 0.0,
                "avg_latency_ms": 0.0,
                "avg_results": 0.0,
                "avg_coverage": 0.0,
                "avg_relevance": 0.0,
                "avg_dedup_ratio": 0.0,
                "cache_hits": 0,
                "errors": 0,
            }

    def quality_trend(self, days: int = 7) -> list[dict[str, Any]]:
        """Daily quality trend for the last `days` days (oldest first)."""
        try:
            conn = self._conn()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        substr(timestamp, 1, 10) AS day,
                        COUNT(*) AS queries,
                        COUNT(quality_score) AS scored,
                        COALESCE(AVG(quality_score), 0) AS avg_quality,
                        COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                        COALESCE(AVG(results_count), 0) AS avg_results,
                        COALESCE(AVG(coverage), 0) AS avg_coverage,
                        COALESCE(AVG(relevance), 0) AS avg_relevance,
                        SUM(cache_hit) AS cache_hits,
                        SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
                    FROM query_log
                    WHERE timestamp >= datetime('now', ?)
                    GROUP BY substr(timestamp, 1, 10)
                    ORDER BY day ASC
                    """,
                    (f"-{int(days)} days",),
                ).fetchall()
                return [
                    {
                        "day": r["day"],
                        "queries": r["queries"],
                        "scored": r["scored"],
                        "avg_quality": round(r["avg_quality"] or 0.0, 4),
                        "avg_latency_ms": round(r["avg_latency_ms"] or 0.0, 1),
                        "avg_results": round(r["avg_results"] or 0.0, 2),
                        "avg_coverage": round(r["avg_coverage"] or 0.0, 4),
                        "avg_relevance": round(r["avg_relevance"] or 0.0, 4),
                        "cache_hits": r["cache_hits"] or 0,
                        "errors": r["errors"] or 0,
                    }
                    for r in rows
                ]
            finally:
                conn.close()
        except Exception:
            return []


# ── Singleton ─────────────────────────────────────────────────────────────────

metrics_store = MetricsStore()


# ── Quality scoring ───────────────────────────────────────────────────────────


def quality_score(
    *,
    results_count: int = 0,
    latency_ms: int = 0,
    relevance_scores: list[float] | None = None,
    domains: list[str] | None = None,
    error: str | None = None,
) -> tuple[float, float, float, float]:
    """Compute a composite quality score (0.0 - 1.0).

    Returns a tuple ``(quality, coverage, relevance, dedup_ratio)`` so callers
    can persist the individual components alongside the composite.

    Components (each 0.0 - 1.0, weighted):
      coverage   (0.30) — how many sources were returned vs. a minimum.
      relevance  (0.30) — mean of the top-N rerank scores (or 0 if none).
      latency    (0.25) — 1.0 at 0ms, decays to 0 at 2x target, clipped.
      dedup      (0.15) — fraction of *unique* domains (1 - duplication).

    On error, quality is forced to 0.0.
    """
    if error:
        return 0.0, 0.0, 0.0, 0.0

    # Coverage: saturate at _COVERAGE_MIN_RESULTS.
    coverage = min(results_count / _COVERAGE_MIN_RESULTS, 1.0) if results_count > 0 else 0.0

    # Relevance: mean of provided scores (already 0-1 from reranker).
    if relevance_scores:
        relevance = sum(relevance_scores) / len(relevance_scores)
        relevance = max(0.0, min(1.0, relevance))
    else:
        relevance = 0.0

    # Latency: linear decay from 1.0 at 0ms to 0.0 at 2x target.
    if latency_ms <= 0:
        latency_score = 1.0
    elif latency_ms >= _LATENCY_TARGET_MS * 2:
        latency_score = 0.0
    else:
        latency_score = 1.0 - (latency_ms / (_LATENCY_TARGET_MS * 2))

    # Dedup: fraction of unique domains.
    if domains:
        unique = len({d for d in domains if d})
        total = len([d for d in domains if d])
        dedup_ratio = unique / total if total else 1.0
    else:
        dedup_ratio = 1.0

    quality = 0.30 * coverage + 0.30 * relevance + 0.25 * latency_score + 0.15 * dedup_ratio
    quality = max(0.0, min(1.0, quality))
    return (
        round(quality, 4),
        round(coverage, 4),
        round(relevance, 4),
        round(dedup_ratio, 4),
    )


# ── Convenience ───────────────────────────────────────────────────────────────


def record_query(
    *,
    query: str,
    endpoint: str,
    query_type: str = "",
    results_count: int = 0,
    latency_ms: int = 0,
    providers: str = "",
    cache_hit: bool = False,
    relevance_scores: list[float] | None = None,
    domains: list[str] | None = None,
    error: str | None = None,
) -> int | None:
    """Compute quality + record a query in one call.

    This is the primary entry point used by the FastAPI endpoints. It never
    raises — metrics failures are swallowed to protect the search path.
    """
    q, cov, rel, dedup = quality_score(
        results_count=results_count,
        latency_ms=latency_ms,
        relevance_scores=relevance_scores,
        domains=domains,
        error=error,
    )
    return metrics_store.record(
        query=query,
        endpoint=endpoint,
        query_type=query_type,
        results_count=results_count,
        latency_ms=latency_ms,
        providers=providers,
        cache_hit=cache_hit,
        quality_score=q,
        coverage=cov,
        relevance=rel,
        dedup_ratio=dedup,
        error=error,
    )
