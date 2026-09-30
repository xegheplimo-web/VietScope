"""Retrieval quality metrics.

All metric functions take a binary relevance vector ``rel`` ordered by rank
(result 0 is the highest-ranked) plus the ground-truth size (number of expected
documents) when needed. Pure functions — no I/O, no network.
"""

from __future__ import annotations

import math
from statistics import mean


def source_diversity(domains: list[str]) -> float:
    """Unique non-empty domains ÷ total non-empty domains (0.0 when empty).

    Measures how many *independent* sources a run surfaced: 1.0 means every
    result came from a distinct domain (maximally diverse), low values mean the
    engine clustered on a few domains. A single domain repeated 10 times = 0.1.
    Domains are normalized (case + www.) before counting.
    """
    from .matching import normalize_domain

    seen = [d for d in (normalize_domain(x) for x in (domains or [])) if d]
    if not seen:
        return 0.0
    return len(set(seen)) / len(seen)


def domain_overlap(domains_a: list[str], domains_b: list[str]) -> float:
    """Jaccard overlap between two result sets' domains (0.0 when either empty).

    Compares two providers (or two runs) for the same query: 1.0 = identical
    domains, 0.0 = fully disjoint. Empty set on either side scores 0.0 (no
    shared evidence to speak of). Domains are normalized before comparison.
    """
    from .matching import normalize_domain

    a = {d for d in (normalize_domain(x) for x in (domains_a or [])) if d}
    b = {d for d in (normalize_domain(x) for x in (domains_b or [])) if d}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def report_source_diversity(report: dict) -> dict:
    """Per-query + mean source diversity computed from a report's domains.

    Works on old reports too: it only needs ``per_query[].retrieved_domains``,
    present since wave 7B, so a pre-8C report JSON evaluates without breaking.
    """
    rows = report.get("per_query") or []
    per_query = [round(source_diversity(r.get("retrieved_domains") or []), 4) for r in rows]
    return {
        "mean": round(mean(per_query), 4) if per_query else 0.0,
        "queries": len(per_query),
        "per_query": per_query,
    }


def report_domain_overlap(report_a: dict, report_b: dict) -> dict:
    """Per-query + mean Jaccard domain overlap, aligned by query index.

    Use to compare two providers / two runs of the same dataset (e.g. legacy
    vs v1, or pre-engine-matrix vs post). Queries present in only one report
    are skipped; ``compared`` reports how many pairs were aligned.
    """
    a = {
        r.get("index", i): (r.get("retrieved_domains") or [])
        for i, r in enumerate(report_a.get("per_query") or [])
    }
    b = {
        r.get("index", i): (r.get("retrieved_domains") or [])
        for i, r in enumerate(report_b.get("per_query") or [])
    }
    common = sorted(set(a) & set(b))
    per_query = [round(domain_overlap(a[i], b[i]), 4) for i in common]
    return {
        "mean": round(mean(per_query), 4) if per_query else 0.0,
        "compared": len(common),
        "per_query": per_query,
    }


def _dcg(rel: list[int], k: int | None = None) -> float:
    rel = rel[:k] if k is not None else rel
    return sum(r / math.log2(i + 2) for i, r in enumerate(rel))


def _idcg(num_relevant: int, k: int) -> float:
    # Perfect ranking: all relevant docs first, then irrelevant.
    return _dcg([1] * min(num_relevant, k), k)


def ndcg_at_k(rel: list[int], num_expected: int, k: int) -> float:
    """nDCG@k (binary relevance). Returns 0.0 when no ground truth is relevant."""
    if k <= 0 or num_expected <= 0:
        return 0.0
    dcg = _dcg(rel, k)
    idcg = _idcg(num_expected, k)
    return dcg / idcg if idcg > 0 else 0.0


def mrr(rel: list[int]) -> float:
    """Mean Reciprocal Rank over one query. 0.0 when nothing is relevant."""
    for i, r in enumerate(rel):
        if r:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(result_urls: list[str], expected_urls: list[str], k: int) -> float:
    """Fraction of distinct expected docs covered by the top-k results.

    Distinct — several results matching the same expected entry count once, so
    Recall never exceeds 1.0.
    """
    from .matching import matched_expected_indexes

    expected_urls = expected_urls or []
    if not expected_urls:
        return 0.0
    covered = matched_expected_indexes((result_urls or [])[:k], expected_urls)
    return len(covered) / len(expected_urls)


def precision_at_k(rel: list[int], k: int) -> float:
    """Fraction of the top-k results that are relevant."""
    if k <= 0:
        return 0.0
    return sum(rel[:k]) / k


def freshness_ok(results: list[dict], k: int | None = None) -> float:
    """Fraction of results that carry a non-empty ``retrieved_at`` field."""
    results = results[:k] if k is not None else results
    if not results:
        return 0.0
    hits = sum(1 for r in results if (r.get("retrieved_at") or "").strip())
    return hits / len(results)


def authority_mean(domains: list[str], scorer) -> float:
    """Mean authority of unique retrieved domains (0.0 when empty/No scorer).

    ``scorer`` is a ``domain -> float`` callable following the
    ``ranking.authority`` scale (~0..2.5, higher = more authoritative) —
    see eval.vn_authority.
    """
    from .matching import normalize_domain

    seen = {normalize_domain(d) for d in (domains or []) if normalize_domain(d)}
    if not seen or scorer is None:
        return 0.0
    return round(mean(scorer(d) for d in seen), 4)


def report_authority(report: dict, scorer) -> dict:
    """Per-query + mean authority over a saved report's ``retrieved_domains``.

    Post-hoc like ``report_source_diversity`` — works on any report JSON that
    stores per-query domains. Returns a ``note`` when no scorer is available.
    """
    if scorer is None:
        return {"mean": 0.0, "queries": 0, "per_query": [], "note": "authority scorer unavailable"}
    rows = report.get("per_query") or []
    per_query = [authority_mean(r.get("retrieved_domains") or [], scorer) for r in rows]
    return {
        "mean": round(mean(per_query), 4) if per_query else 0.0,
        "queries": len(per_query),
        "per_query": per_query,
    }


def estimate_cost(queries: list[dict], cost_per_query_usd: float = 0.0) -> float:
    """Estimate total cost in USD.

    Uses an explicit ``cost_usd`` field from an API response when present,
    otherwise multiplies each query by the per-query estimate (default 0 — the
    self-hosted SearXNG path is free).
    """
    total = 0.0
    for q in queries:
        explicit = q.get("cost_usd")
        if explicit is not None:
            total += float(explicit)
        else:
            total += cost_per_query_usd
    return round(total, 6)


def _nearest_rank_pct(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile (0.0 <= pct <= 1.0) over ascending values."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, math.ceil(pct * n) - 1))
    return sorted_vals[idx]


def summarize_latencies(latencies_ms: list[float]) -> dict:
    """mean + p50/p95/p99 of per-query latencies (empty list -> all 0)."""
    if not latencies_ms:
        return {
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
        }
    sorted_vals = sorted(latencies_ms)
    return {
        "mean_ms": round(mean(latencies_ms), 2),
        "p50_ms": round(_nearest_rank_pct(sorted_vals, 0.50), 2),
        "p95_ms": round(_nearest_rank_pct(sorted_vals, 0.95), 2),
        "p99_ms": round(_nearest_rank_pct(sorted_vals, 0.99), 2),
    }


def evaluate_query(
    result_urls: list[str],
    expected_urls: list[str],
    results_meta: list[dict] | None = None,
    top_k: int = 10,
    authority_scorer=None,
) -> dict:
    """Evaluate one query's ranked result list against ground truth.

    Args:
        result_urls: ranked result URLs (position 0 = most relevant).
        expected_urls: ground-truth expected URLs/domains.
        results_meta: optional per-result metadata (for freshness_ok).
        top_k: retrieval depth to score against.

    Returns a flat dict of per-query metrics.
    """
    from .matching import domain_from_url, relevance_vector

    rel = relevance_vector(result_urls, expected_urls, top_k)
    num_expected = len(expected_urls or [])
    results_meta = results_meta or [{}] * len(result_urls)

    out = {
        "relevant_found": sum(rel),
        "num_expected": num_expected,
        "retrieved_count": len(result_urls[:top_k]),
        "ndcg@10": round(ndcg_at_k(rel, num_expected, 10), 4),
        f"ndcg@{top_k}": round(ndcg_at_k(rel, num_expected, top_k), 4),
        "mrr": round(mrr(rel), 4),
        "recall@5": round(recall_at_k(result_urls, expected_urls, 5), 4),
        "recall@10": round(recall_at_k(result_urls, expected_urls, 10), 4),
        "recall@20": round(recall_at_k(result_urls, expected_urls, 20), 4),
        "precision@10": round(precision_at_k(rel, 10), 4),
        "freshness_ok": round(freshness_ok(results_meta, top_k), 4),
        "source_diversity": round(
            source_diversity([domain_from_url(u) for u in (result_urls or [])[:top_k]]),
            4,
        ),
    }
    if authority_scorer is not None:
        out["authority"] = authority_mean(
            [domain_from_url(u) for u in (result_urls or [])[:top_k]], authority_scorer
        )
    return out
