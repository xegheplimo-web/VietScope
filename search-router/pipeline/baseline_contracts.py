"""BASELINE-V3 contract freeze (P0).

Frozen snapshots of the public contracts later phases must not silently
change: the OpenAPI surface, model schemas, DB DDL, OpenSearch mappings,
Qdrant collections, and the SSE event vocabulary.

Regenerate after an *intentional* contract change::

    python scripts/freeze_baseline.py

``tests/test_baseline_contracts.py`` rebuilds every artifact in memory and
fails on drift, so accidental contract changes break CI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
BASELINE_DIR = _SEARCH_ROUTER_DIR.parent / "baseline"

# ─── builders (deterministic; no live services required) ───────────────────


def _openapi() -> dict:
    from main import app

    return app.openapi()


def _schema_search_result() -> dict:
    from models import SearchResult

    return SearchResult.model_json_schema()


def _schema_provider_result() -> dict:
    from providers.base import ProviderResult

    return ProviderResult.model_json_schema()


def _schema_citation() -> dict:
    from models import CitationV2

    return CitationV2.model_json_schema()


def _schema_source_result() -> dict:
    from research_models.research_state import SourceResult

    return SourceResult.model_json_schema()


def _db_schema() -> str:
    parts = []
    db_dir = _SEARCH_ROUTER_DIR / "db"
    paths = [db_dir / "init.sql", *sorted((db_dir / "migrations").glob("*.sql"))]
    for path in paths:
        parts.append(f"-- {path.name}\n{path.read_text(encoding='utf-8').strip()}\n")
    return "\n".join(parts)


def _opensearch_mappings() -> dict:
    mappings_dir = _SEARCH_ROUTER_DIR / "opensearch" / "mappings"
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(mappings_dir.glob("*.json"))
    }


def _qdrant_collections() -> list[dict]:
    from qdrant.collections import ALL_COLLECTIONS

    return [asdict(c) for c in ALL_COLLECTIONS]


_EVENT_CALL = re.compile(r"""(?:_sse|_emit)\(\s*\n?\s*["']([a-z][a-z._]*)["']""")


def _sse_events() -> dict:
    names: set[str] = set()
    for path in [
        _SEARCH_ROUTER_DIR / "api" / "v1.py",
        *sorted((_SEARCH_ROUTER_DIR / "agent").glob("*.py")),
    ]:
        names.update(_EVENT_CALL.findall(path.read_text(encoding="utf-8")))
    from api.v1 import _SSE_EVENT_MAP

    return {
        "events": sorted(names),
        "legacy_to_canonical": _SSE_EVENT_MAP,
    }


# rel path under baseline/ → (builder, is_json)
BUILDERS: dict[str, tuple[Callable[[], Any], bool]] = {
    "api.openapi.json": (_openapi, True),
    "schemas/search_result.json": (_schema_search_result, True),
    "schemas/provider_result.json": (_schema_provider_result, True),
    "schemas/citation.json": (_schema_citation, True),
    "schemas/source_result.json": (_schema_source_result, True),
    "db/schema.sql": (_db_schema, False),
    "opensearch/mappings.json": (_opensearch_mappings, True),
    "qdrant/collections.json": (_qdrant_collections, True),
    "sse/events.json": (_sse_events, True),
}


def render(builder: Callable[[], Any], is_json: bool) -> str:
    """Serialize one artifact to its canonical on-disk form."""
    obj = builder()
    if is_json:
        return json.dumps(obj, indent=2, sort_keys=True) + "\n"
    return obj


def freeze(baseline_dir: Path = BASELINE_DIR) -> list[Path]:
    """Write every contract artifact; returns the paths written."""
    written = []
    for rel, (builder, is_json) in BUILDERS.items():
        path = baseline_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(builder, is_json), encoding="utf-8")
        written.append(path)
    return written


def drifted(baseline_dir: Path = BASELINE_DIR) -> list[str]:
    """Return the artifact names whose frozen file differs from the live build."""
    out = []
    for rel, (builder, is_json) in BUILDERS.items():
        path = baseline_dir / rel
        frozen = path.read_text(encoding="utf-8") if path.exists() else None
        if frozen != render(builder, is_json):
            out.append(rel)
    return out
