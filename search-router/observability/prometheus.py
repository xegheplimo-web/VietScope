"""Low-cardinality Prometheus metrics for Search-Hub."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter("search_http_requests_total", "HTTP requests", ["method", "route", "status"])
REQUEST_LATENCY = Histogram(
    "search_http_request_duration_seconds",
    "HTTP request duration",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
SEARCH_REQUESTS = Counter(
    "search_requests_total", "Search operations", ["surface", "mode", "outcome"]
)
RESULT_COUNT = Histogram(
    "search_result_count", "Results returned", ["surface"], buckets=(0, 1, 2, 5, 10, 20, 50, 100)
)
ZERO_RESULTS = Counter("search_zero_results_total", "Searches with no results", ["surface", "lane"])
DEGRADED = Counter("search_degraded_total", "Degraded searches", ["reason"])
PIPELINE_DURATION = Histogram(
    "search_pipeline_duration_seconds",
    "Pipeline stage duration",
    ["stage"],
    buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
)
PROVIDER_REQUESTS = Counter(
    "search_provider_requests_total", "Provider outcomes", ["provider", "outcome"]
)
PROVIDER_RESULTS = Counter(
    "search_provider_results_total", "Provider results returned", ["provider"]
)
PROVIDER_UNIQUE_RESULTS = Counter(
    "search_provider_unique_results_total", "Provider unique results", ["provider"]
)
PROVIDER_LATENCY = Histogram(
    "search_provider_request_duration_seconds",
    "Provider latency",
    ["provider"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
)
CACHE_OPS = Counter("search_cache_operations_total", "Cache operations", ["layer", "outcome"])
COMPONENT_LATENCY = Histogram(
    "search_component_duration_seconds",
    "Pipeline component latency",
    ["component"],
    buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
)
IN_FLIGHT = Gauge("search_http_requests_in_flight", "Requests in flight")


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def observe_search(endpoint: str, mode: str, result_count: int) -> None:
    SEARCH_REQUESTS.labels(endpoint, mode, "success" if result_count else "empty").inc()
    RESULT_COUNT.labels(endpoint).observe(result_count)
    if not result_count:
        ZERO_RESULTS.labels(endpoint, "unknown").inc()


def observe_degraded(reason: str) -> None:
    # ``reason`` comes from a small fixed vocabulary in the call sites — the
    # full value is more useful than a truncated one and cardinality stays
    # bounded regardless.
    DEGRADED.labels(reason).inc()
