"""Report building and JSON serialization for eval runs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .runner import RunResult, aggregate_metrics

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

METRIC_ORDER = [
    "ndcg@10",
    "mrr",
    "recall@5",
    "recall@10",
    "recall@20",
    "precision@10",
    "freshness_ok",
    "source_diversity",
]


def build_report(
    run_result: RunResult, dataset_name: str, note: str | None = None
) -> dict[str, Any]:
    """Assemble the full eval report (meta + summary + per-query + by-category)."""
    agg = aggregate_metrics(run_result, run_result.config.top_k)
    cfg = run_result.config

    per_category: dict[str, dict[str, float]] = {}
    for cat in {r.query.category for r in run_result.runs}:
        cat_runs = RunResult(
            config=cfg,
            dataset=[r.query for r in run_result.runs if r.query.category == cat],
            runs=[r for r in run_result.runs if r.query.category == cat],
            dataset_summary={},
        )
        per_category[cat] = aggregate_metrics(cat_runs, cfg.top_k)

    return {
        "meta": {
            "harness_version": __version__,
            "dataset": dataset_name,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endpoint": cfg.endpoint,
            "server_url": cfg.server_url,
            "top_k": cfg.top_k,
            "providers": list(cfg.providers),
            "search_type": cfg.search_type,
            "timeout_s": cfg.timeout,
            "force_mock": cfg.force_mock,
            "cost_per_query_usd": cfg.cost_per_query_usd,
            "answer_mode": getattr(cfg, "answer_mode", None),
            "note": note,
        },
        "dataset": run_result.dataset_summary,
        "summary": agg,
        "per_query": [r.to_dict() for r in run_result.runs],
        "by_category": per_category,
    }


def write_report(report: dict[str, Any], dataset_name: str, out_dir: str | None = None) -> str:
    """Write report to <out_dir>/<dataset>_<ts>.json; returns the path."""
    out_dir = out_dir or REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"{dataset_name}_{ts}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def format_summary(report: dict[str, Any]) -> str:
    """Human-readable table of the aggregate summary."""
    s = report["summary"]
    lat = s.get("latency_ms", {})
    lines = [
        "Aggregate (all queries):",
        f"  queries_evaluated : {s.get('queries_evaluated', 0)} / {s.get('total_queries', 0)}",
        f"  error_rate        : {s.get('error_rate', 0):.2%}",
        f"  mock_used         : {s.get('mock_used', 0)} query(s)",
        f"  ndcg@10           : {s.get('ndcg@10', 0):.4f}",
        f"  mrr               : {s.get('mrr', 0):.4f}",
        f"  recall@5          : {s.get('recall@5', 0):.4f}",
        f"  recall@10         : {s.get('recall@10', 0):.4f}",
        f"  recall@20         : {s.get('recall@20', 0):.4f}",
        f"  precision@10      : {s.get('precision@10', 0):.4f}",
        f"  freshness_ok      : {s.get('freshness_ok', 0):.4f}",
        f"  source_diversity  : {s.get('source_diversity', 0):.4f}",
        f"  latency mean      : {lat.get('mean_ms', 0):.1f} ms",
        f"  latency p95       : {lat.get('p95_ms', 0):.1f} ms",
        f"  cost_usd          : {s.get('cost_usd', 0):.6f}",
    ]
    return "\n".join(lines)


def format_answer_summary(report: dict[str, Any]) -> str:
    """Answer-level metric block (only meaningful on --endpoint answer runs)."""
    s = report["summary"]
    lines = ["Answer-level:"]

    def _row(label: str, key: str, pct: bool = True) -> None:
        if key not in s:
            return
        n = s.get(f"{key}_queries", s.get("queries_evaluated", 0))
        val = s[key]
        lines.append(
            f"  {label:<19}: {val:.2%}  over {n} queries"
            if pct
            else f"  {label:<19}: {val:.4f}  over {n} queries"
        )

    _row("answer_present", "answer_present")
    _row("answer_correctness", "answer_correctness")
    _row("citation_precision", "citation_precision")
    _row("citation_recall", "citation_recall")
    _row("unsupported_claims", "unsupported_claim_rate")
    _row("evidence_quote_rate", "evidence_quote_rate")
    _row("cited_src_coverage", "cited_source_coverage")
    _row("verified", "verified")
    _row("coverage", "coverage")
    return "\n".join(lines)


def format_per_query(report: dict[str, Any], limit: int | None = None) -> str:
    """Human-readable per-query rows (best for small datasets / debugging)."""
    rows = []
    header = (
        f"{'#':>3} {'category':<14} {'nDCG@10':>7} {'MRR':>6} {'R@5':>5} "
        f"{'R@10':>5} {'P@10':>5} {'fresh':>5} {'err':>3}"
    )
    rows.append(header)
    for r in report["per_query"]:
        if limit is not None and len(rows) - 1 >= limit:
            rows.append("  ... (truncated)")
            break
        m = r["metrics"]
        err = "Y" if r.get("error") else ""
        rows.append(
            f"{r['index']:>3} {r['category']:<14} {m.get('ndcg@10', 0):>7.4f} "
            f"{m.get('mrr', 0):>6.4f} {m.get('recall@5', 0):>5.3f} "
            f"{m.get('recall@10', 0):>5.3f} {m.get('precision@10', 0):>5.3f} "
            f"{m.get('freshness_ok', 0):>5.3f} {err:>3}"
        )
    return "\n".join(rows)
