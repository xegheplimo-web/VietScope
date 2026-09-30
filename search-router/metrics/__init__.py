"""Search metrics — feedback & evaluation loop.

Exports:
    metrics_store — singleton MetricsStore backed by SQLite.
    record_query  — convenience wrapper to log a search/answer call.
    quality_score — compute a 0-1 quality score from a query record.
"""

from metrics.store import MetricsStore, metrics_store, quality_score, record_query

__all__ = [
    "MetricsStore",
    "metrics_store",
    "record_query",
    "quality_score",
]
