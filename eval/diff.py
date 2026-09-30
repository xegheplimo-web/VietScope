"""Baseline diff — compare a new eval report against a previous run.

Each comparable metric is labelled either ``higher_is_better`` (nDCG, MRR,
Recall, Precision, freshness) or ``lower_is_better`` (latency, cost, error
rate). The CLI prints a table where every row ends with one of:

    "+"  improved vs baseline
    "-"  regressed vs baseline
    "="  unchanged
"""

from __future__ import annotations

from typing import Any

HIGHER_IS_BETTER = {
    "ndcg@10",
    "mrr",
    "recall@5",
    "recall@10",
    "recall@20",
    "precision@10",
    "freshness_ok",
    "source_diversity",
}

# latency/cost live nested under the summary; compare their own values.
NESTED_LOWER_IS_BETTER = {
    "latency_ms": ("mean_ms", "p50_ms", "p95_ms", "p99_ms"),
    "cost_usd": (),
}


def _get(summary: dict[str, Any], key: str) -> Any:
    return summary.get(key)


def comparable_metrics(new: dict[str, Any], base: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Yield (metric, new_value, base_value) pairs present in both reports."""
    out: list[tuple[str, Any, Any]] = []
    for key in HIGHER_IS_BETTER:
        if key in new and key in base:
            out.append((key, new[key], base[key]))
    # Nested lower-is-better metrics (latency distribution, cost).
    for key, subkeys in NESTED_LOWER_IS_BETTER.items():
        if key not in new or key not in base:
            continue
        if subkeys:
            for sk in subkeys:
                nv = (new.get(key) or {}).get(sk)
                bv = (base.get(key) or {}).get(sk)
                if nv is not None and bv is not None:
                    out.append((f"{key}.{sk}", nv, bv))
        else:
            nv = new.get(key)
            bv = base.get(key)
            if nv is not None and bv is not None:
                out.append((key, nv, bv))
    return out


def delta_class(metric: str, new_val: float, base_val: float) -> str:
    """Return '+' (improved), '-' (regressed), '=' (unchanged)."""
    try:
        diff = float(new_val) - float(base_val)
    except (TypeError, ValueError):
        return "="
    if abs(diff) < 1e-9:
        return "="
    if metric in HIGHER_IS_BETTER or metric.split(".")[0] in HIGHER_IS_BETTER:
        return "+" if diff > 0 else "-"
    return "-" if diff > 0 else "+"


def format_diff(new: dict[str, Any], base: dict[str, Any]) -> str:
    """Render the diff table for CLI output."""
    pairs = comparable_metrics(new, base)
    if not pairs:
        return "No comparable metrics between the two reports."

    lines = [f"{'metric':<14} {'baseline':>12} {'current':>12} {'delta':>12}  change"]
    for metric, nv, bv in pairs:
        try:
            delta = float(nv) - float(bv)
            delta_str = f"{delta:+.4f}"
        except (TypeError, ValueError):
            delta_str = "n/a"
        cls = delta_class(metric, nv, bv)
        lines.append(f"{metric:<14} {bv:>12} {nv:>12} {delta_str:>12}  {cls}")
    return "\n".join(lines)


def diff_summary(new: dict[str, Any], base: dict[str, Any]) -> dict[str, str]:
    """Machine-readable diff: metric -> '+' | '-' | '='."""
    return {m: delta_class(m, n, b) for m, n, b in comparable_metrics(new, base)}


def format_per_query_diffs(new: dict[str, Any], base: dict[str, Any], limit: int = 20) -> str:
    """Per-query nDCG@10 delta between two reports (best-effort alignment by index)."""
    npq = {r["index"]: r for r in new.get("per_query", [])}
    bpq = {r["index"]: r for r in base.get("per_query", [])}
    if not npq or not bpq:
        return ""
    lines = ["Per-query nDCG@10 delta (query index: baseline -> current  change):"]
    shown = 0
    for idx in sorted(set(npq) & set(bpq)):
        if shown >= limit:
            lines.append("  ... (truncated)")
            break
        b = bpq[idx].get("metrics", {}).get("ndcg@10", 0)
        n = npq[idx].get("metrics", {}).get("ndcg@10", 0)
        cls = delta_class("ndcg@10", n, b)
        lines.append(f"  {idx:>3}: {b:.4f} -> {n:.4f}  {n - b:+.4f}  {cls}")
        shown += 1
    return "\n".join(lines)
