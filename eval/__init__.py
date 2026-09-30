"""Search Quality Eval Harness for Search Hub.

Measures retrieval quality of the Search Router against labeled datasets:
nDCG@k, MRR, Recall@k, Precision@k, freshness, latency, and estimated cost.

Usage:
    python -m eval list-datasets
    python -m eval run --dataset vi_general --top-k 10 [--baseline report.json]
"""

__version__ = "1.0.0"

from .datasets import EvalQuery, list_datasets, load_dataset, query_name
from .matching import normalize_expected, result_matches_expected
from .metrics import (
    domain_overlap,
    evaluate_query,
    freshness_ok,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    report_domain_overlap,
    report_source_diversity,
    source_diversity,
)
from .runner import QueryRun, RunConfig, run_dataset

__all__ = [
    "EvalQuery",
    "load_dataset",
    "list_datasets",
    "query_name",
    "normalize_expected",
    "result_matches_expected",
    "ndcg_at_k",
    "mrr",
    "recall_at_k",
    "precision_at_k",
    "freshness_ok",
    "evaluate_query",
    "source_diversity",
    "domain_overlap",
    "report_source_diversity",
    "report_domain_overlap",
    "run_dataset",
    "RunConfig",
    "QueryRun",
]
