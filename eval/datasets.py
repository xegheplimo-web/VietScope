"""Dataset loading for Search Quality Eval Harness.

Each dataset is a JSONL file in ``eval/datasets/``. Every line is one labeled
query:

    {"query": "...", "expected_urls": ["vnexpress.net", "https://.../article"],
     "category": "vi_general|current_events|technical|code|local_business|ambiguous|adversarial",
     "region": "vn|global", "time_sensitive": true}

``expected_urls`` entries may be bare domains or full URLs (see matching.py).
``time_sensitive`` is optional (default false) and marks queries whose best
answers must be fresh (e.g. "giá vàng hôm nay") — it is only a label used by
the freshness datasets; the loader stays backward-compatible with wave 7B rows.
``expected_facts`` (P13) is an optional list of short strings the answer text
should contain (accent-folded substring match); queries without it simply skip
the answer_correctness metric.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

DATASETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets")

VALID_CATEGORIES = {
    "vi_general",
    "current_events",
    "technical",
    "code",
    "local_business",
    "ambiguous",
    "adversarial",
    # P12 VN verticals
    "vn_news",
    "vn_government",
    "vn_legal",
    "vn_market",
    "vn_product",
    "vn_company",
    "vn_places",
    "vn_community",
    "vn_research",
}


@dataclass
class EvalQuery:
    query: str
    expected_urls: list[str] = field(default_factory=list)
    category: str = "vi_general"
    region: str = "vn"
    time_sensitive: bool = False
    expected_facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "expected_urls": list(self.expected_urls),
            "category": self.category,
            "region": self.region,
            "time_sensitive": self.time_sensitive,
            "expected_facts": list(self.expected_facts),
        }


def query_name(q: EvalQuery) -> str:
    """Stable short label for a query (truncated to 60 chars)."""
    return (q.query or "query")[:60]


def list_datasets() -> list[str]:
    """Names of all .jsonl datasets in the datasets dir (sorted).

    One level of subdirectories is walked so grouped suites like
    ``vietnam/news.jsonl`` surface as ``vietnam/news``.
    """
    if not os.path.isdir(DATASETS_DIR):
        return []
    names: list[str] = []
    for f in sorted(os.listdir(DATASETS_DIR)):
        p = os.path.join(DATASETS_DIR, f)
        if os.path.isfile(p) and f.endswith(".jsonl"):
            names.append(f[:-6])
        elif os.path.isdir(p):
            for g in sorted(os.listdir(p)):
                gp = os.path.join(p, g)
                if os.path.isfile(gp) and g.endswith(".jsonl"):
                    names.append(f"{f}/{g[:-6]}")
    return names


def dataset_path(name: str) -> str:
    """Resolve a dataset name to a file path (or return the path verbatim)."""
    if os.path.isfile(name):
        return name
    p = os.path.join(DATASETS_DIR, name if name.endswith(".jsonl") else f"{name}.jsonl")
    if os.path.isfile(p):
        return p
    raise FileNotFoundError(f"Dataset '{name}' not found. Available: {list_datasets() or 'none'}")


def load_dataset(name: str) -> list[EvalQuery]:
    """Load a dataset into a list of EvalQuery."""
    path = dataset_path(name)
    queries: list[EvalQuery] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            query = (obj.get("query") or "").strip()
            if not query:
                raise ValueError(f"{path}:{lineno}: missing 'query'")
            category = (obj.get("category") or "vi_general").strip()
            if category not in VALID_CATEGORIES:
                raise ValueError(
                    f"{path}:{lineno}: invalid category {category!r} "
                    f"(valid: {sorted(VALID_CATEGORIES)})"
                )
            ts = obj.get("time_sensitive", False)
            ts = ts.strip().lower() in ("1", "true", "yes") if isinstance(ts, str) else bool(ts)
            queries.append(
                EvalQuery(
                    query=query,
                    expected_urls=[str(u) for u in (obj.get("expected_urls") or [])],
                    category=category,
                    region=(obj.get("region") or "vn").strip() or "vn",
                    time_sensitive=ts,
                    expected_facts=[str(f) for f in (obj.get("expected_facts") or [])],
                )
            )
    if not queries:
        raise ValueError(f"Dataset '{name}' is empty")
    return queries


def summary(dataset: list[EvalQuery]) -> dict:
    """High-level stats about a dataset (for the report header)."""
    by_cat: dict[str, int] = {}
    by_region: dict[str, int] = {}
    for q in dataset:
        by_cat[q.category] = by_cat.get(q.category, 0) + 1
        by_region[q.region] = by_region.get(q.region, 0) + 1
    return {
        "queries": len(dataset),
        "by_category": by_cat,
        "by_region": by_region,
        "time_sensitive": sum(1 for q in dataset if q.time_sensitive),
    }
