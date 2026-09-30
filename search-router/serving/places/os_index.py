"""P17 OpenSearch lane — the ``places`` index and its query builders.

Owns the concrete-index/alias layout (``places`` → ``places_v1_NNNNNN``)
that makes full rebuilds atomic, plus the search/autocomplete request
bodies the ``LocalQuerySpec`` compiles to. Reads and writes go through
the existing ``opensearch.client.OpenSearchClient`` (sync opensearch-py,
wrapped with ``asyncio.to_thread``).

Never touches Postgres and never fabricates results: a lane failure
raises ``PlaceIndexUnavailable`` so the service can fall back to the
canonical PostGIS path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from config import settings

from serving.places.document import PLACE_DOCUMENT_VERSION, PlaceDocumentV1
from serving.places.projection import doc_from_index_source, doc_to_index_source
from serving.places.query import GEO_FIELD, LocalQuerySpec

logger = logging.getLogger(__name__)

_MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "opensearch" / "mappings"
_MAPPING_PATH = _MAPPINGS_DIR / "places.json"

# A short refresh interval keeps incremental indexing visible quickly; the
# mapping sets 5s which suits both interactive and bulk lanes.
BULK_BATCH = 500


class PlaceIndexUnavailable(Exception):
    """Raised when the OpenSearch lane cannot answer (connection/index)."""


def _mapping_body() -> dict[str, Any]:
    return json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))


def build_search_body(spec: LocalQuerySpec, *, top_k: int) -> dict[str, Any]:
    """Compile a ``LocalQuerySpec`` into an OpenSearch request body.

    Lane shape:

    - ``must`` — text relevance (exact-folded boost, phrase, best_fields
      over name/aliases/normalized, fuzzy folded fallback); absent text →
      match_all so geo/category browsing still works.
    - ``filter`` — status set, category, admin unit, geo radius/bbox,
      plus the P17.1 rich filters (``rating`` range, ``price_level``
      term). ``open_now`` is deliberately absent: it is a verdict about
      *now* that post-filters candidates at request time, not an
      index-side clause. Filters are non-scoring: ranking stays in the
      fusion layer.
    """
    filters: list[dict[str, Any]] = []
    if spec.statuses:
        filters.append({"terms": {"status": sorted(spec.statuses)}})
    if spec.category:
        filters.append({"term": {"category_ids": spec.category}})
    if spec.admin_unit_id is not None:
        filters.append({"term": {"admin_unit_id": str(spec.admin_unit_id)}})
    if spec.min_rating is not None:
        filters.append({"range": {"rating": {"gte": spec.min_rating}}})
    if spec.price_level:
        filters.append({"term": {"price_level": spec.price_level}})
    if spec.has_geo:
        filters.append(
            {
                "geo_distance": {
                    "distance": f"{spec.radius_m}m",
                    GEO_FIELD: {"lat": spec.lat, "lon": spec.lon},
                }
            }
        )
    if spec.bbox:
        min_lon, min_lat, max_lon, max_lat = spec.bbox
        filters.append(
            {
                "geo_bounding_box": {
                    GEO_FIELD: {
                        "top_left": {"lat": max_lat, "lon": min_lon},
                        "bottom_right": {"lat": min_lat, "lon": max_lon},
                    }
                }
            }
        )

    must: list[dict[str, Any]] = []
    if spec.text:
        should: list[dict[str, Any]] = [
            # exact folded-name boost ("Phở Thìn" == "pho thin")
            {"term": {"name.exact": {"value": spec.text, "boost": 8.0}}},
            {"match_phrase": {"name": {"query": spec.q_raw, "boost": 4.0}}},
            {
                "multi_match": {
                    "query": spec.q_raw,
                    "fields": [
                        "name^3",
                        "name.folded^2.5",
                        "normalized_name^2",
                        "aliases^2",
                    ],
                    "type": "best_fields",
                }
            },
            # fuzzy fallback on folded fields — typo/diacritic tolerance
            {
                "multi_match": {
                    "query": spec.text,
                    "fields": ["name.folded^2", "normalized_name^2", "aliases"],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                }
            },
        ]
        must.append({"bool": {"should": should, "minimum_should_match": 1}})
    else:
        must.append({"match_all": {}})

    body: dict[str, Any] = {
        "size": top_k,
        "track_scores": True,
        "query": {"bool": {"must": must, "filter": filters}},
    }
    # Retrieval order decides which candidates make the top_k cut — the
    # fusion layer re-sorts the final response either way. P17.1 ``sort``
    # overrides the default hint; ``distance`` needs a geo anchor, and
    # geo-only queries keep their distance ordering as before.
    geo_sort = {
        "_geo_distance": {
            GEO_FIELD: {"lat": spec.lat, "lon": spec.lon},
            "order": "asc",
            "unit": "m",
        }
    }
    if spec.sort == "distance" and spec.has_geo:
        body["sort"] = [geo_sort]
    elif spec.sort == "rating":
        body["sort"] = [{"rating": {"order": "desc", "missing": "_last"}}]
    elif spec.sort == "popularity":
        body["sort"] = [{"review_count": {"order": "desc", "missing": "_last"}}]
    elif spec.has_geo and not spec.text:
        body["sort"] = [geo_sort]
    return body


def build_autocomplete_body(
    q_folded: str, *, limit: int, lat: float | None, lon: float | None
) -> dict[str, Any]:
    """Prefix-completion query over edge-ngram fields + optional geo decay."""
    inner: dict[str, Any] = {
        "bool": {
            "must": [
                {
                    "bool": {
                        "should": [
                            {"match_bool_prefix": {"name.ac": {"query": q_folded, "boost": 3.0}}},
                            {"match_bool_prefix": {"normalized_name.ac": {"query": q_folded}}},
                            {"match_bool_prefix": {"aliases.ac": {"query": q_folded}}},
                            {
                                "match_phrase_prefix": {
                                    "name.folded": {"query": q_folded, "boost": 1.5}
                                }
                            },
                        ],
                        "minimum_should_match": 1,
                    }
                }
            ],
            "filter": [{"terms": {"status": ["open", "temporarily_closed", "unknown"]}}],
        }
    }
    if lat is not None and lon is not None:
        query: dict[str, Any] = {
            "function_score": {
                "query": inner,
                "functions": [
                    {
                        "gauss": {
                            GEO_FIELD: {
                                "origin": {"lat": lat, "lon": lon},
                                "scale": "2km",
                                "decay": 0.5,
                            }
                        }
                    }
                ],
                "boost_mode": "multiply",
            }
        }
    else:
        query = inner
    return {"size": limit, "query": query}


class PlaceOSIndex:
    """OpenSearch operations for the places index.

    ``index`` is the *alias* name (default ``places``); concrete indices
    are ``<alias>_v<doc_version>`` then ``<alias>_v<doc_version>_g<gen>``
    for rebuilds, so a full reindex never interrupts reads.
    """

    def __init__(self, client: Any | None = None, index: str | None = None):
        self._client = client  # OpenSearchClient; lazy by design
        self.alias = index or getattr(settings, "opensearch_index_places", "places")
        self._default_concrete = f"{self.alias}_v{PLACE_DOCUMENT_VERSION}"

    # ── plumbing ─────────────────────────────────────────────────────────

    def _os(self):
        if self._client is None:
            from opensearch.client import OpenSearchClient

            self._client = OpenSearchClient()
        return self._client._get_client()

    async def available(self) -> bool:
        try:
            return await asyncio.to_thread(self._os().ping)
        except Exception:
            return False

    # ── index topology ───────────────────────────────────────────────────

    async def ensure(self) -> str:
        """Create the concrete index + alias if missing. Returns the write
        index name (the alias — reads/writes always target it)."""

        def _ensure() -> None:
            client = self._os()
            if client.indices.exists_alias(name=self.alias):
                return
            if not client.indices.exists(index=self._default_concrete):
                client.indices.create(index=self._default_concrete, body=_mapping_body())
            client.indices.put_alias(index=self._default_concrete, name=self.alias)

        try:
            await asyncio.to_thread(_ensure)
            return self.alias
        except Exception as exc:
            raise PlaceIndexUnavailable(f"ensure failed: {exc}") from exc

    async def create_generation(self, generation: int) -> str:
        """Create the next-generation concrete index (for full rebuilds)."""
        name = f"{self.alias}_v{PLACE_DOCUMENT_VERSION}_g{generation}"

        def _create() -> None:
            client = self._os()
            if not client.indices.exists(index=name):
                client.indices.create(index=name, body=_mapping_body())

        try:
            await asyncio.to_thread(_create)
            return name
        except Exception as exc:
            raise PlaceIndexUnavailable(f"create_generation failed: {exc}") from exc

    async def swap_alias(self, new_concrete: str) -> list[str]:
        """Atomically move the alias to ``new_concrete``; returns the old
        concrete index names so the caller can drop them after success."""

        def _swap() -> list[str]:
            client = self._os()
            old: list[str] = []
            if client.indices.exists_alias(name=self.alias):
                old = sorted(client.indices.get_alias(name=self.alias).keys())
            actions = [{"remove": {"index": name, "alias": self.alias}} for name in old]
            actions.append({"add": {"index": new_concrete, "alias": self.alias}})
            client.indices.update_aliases(body={"actions": actions})
            return old

        try:
            return await asyncio.to_thread(_swap)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"alias swap failed: {exc}") from exc

    async def reindex(self, source: str, dest: str) -> int:
        """Copy all documents from ``source`` concrete index to ``dest`` via
        the OpenSearch ``_reindex`` API (server-side, no client round-trips).

        Used by mapping migrations: the destination index is created with the
        fresh mapping (e.g. ``price_level: keyword``), documents are copied
        in-place, then the alias is swapped atomically.
        """

        def _reindex() -> int:
            client = self._os()
            resp = client.reindex(body={"source": {"index": source}, "dest": {"index": dest}})
            total = resp.get("total", 0)
            if isinstance(total, dict):
                return int(total.get("documents", 0))
            return int(total)

        try:
            return await asyncio.to_thread(_reindex)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"reindex failed: {exc}") from exc

    async def drop_index(self, name: str) -> None:
        def _drop() -> None:
            client = self._os()
            if client.indices.exists(index=name) and name != self.alias:
                with contextlib.suppress(Exception):
                    client.indices.delete(index=name)

        await asyncio.to_thread(_drop)

    async def current_concrete(self) -> str | None:
        def _get() -> str | None:
            client = self._os()
            if client.indices.exists_alias(name=self.alias):
                return sorted(client.indices.get_alias(name=self.alias).keys())[-1]
            if client.indices.exists(index=self.alias):
                return self.alias
            return None

        try:
            return await asyncio.to_thread(_get)
        except Exception:
            return None

    # ── document ops ─────────────────────────────────────────────────────

    async def upsert_docs(
        self, docs: list[PlaceDocumentV1], *, index: str | None = None
    ) -> dict[str, Any]:
        """Bulk upsert; returns ``{indexed, failed, errors}``."""
        if not docs:
            return {"indexed": 0, "failed": 0, "errors": []}
        target = index or self.alias

        def _bulk() -> dict[str, Any]:
            client = self._os()
            actions: list[dict[str, Any]] = []
            for d in docs:
                actions.append({"index": {"_index": target, "_id": d.place_id}})
                actions.append(doc_to_index_source(d))
            resp = client.bulk(body=actions, params={"refresh": "false"})
            items = resp.get("items", [])
            errors = [
                it.get("index", {}) for it in items if it.get("index", {}).get("status", 200) >= 300
            ]
            return {
                "indexed": len(items) - len(errors),
                "failed": len(errors),
                "errors": errors[:20],
            }

        try:
            return await asyncio.to_thread(_bulk)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"bulk upsert failed: {exc}") from exc

    async def refresh(self, index: str | None = None) -> None:
        try:
            await asyncio.to_thread(self._os().indices.refresh, index=index or self.alias)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"refresh failed: {exc}") from exc

    async def delete(self, place_id: str, *, index: str | None = None) -> bool:
        def _delete() -> bool:
            client = self._os()
            try:
                client.delete(
                    index=index or self.alias, id=str(place_id), params={"refresh": "true"}
                )
                return True
            except Exception as exc:
                # 404 → already absent (idempotent delete)
                if getattr(exc, "status_code", None) == 404:
                    return False
                raise

        try:
            return await asyncio.to_thread(_delete)
        except PlaceIndexUnavailable:
            raise
        except Exception as exc:
            raise PlaceIndexUnavailable(f"delete failed: {exc}") from exc

    async def delete_ids(self, place_ids: list[str]) -> int:
        if not place_ids:
            return 0

        def _del() -> int:
            client = self._os()
            resp = client.delete_by_query(
                index=self.alias,
                body={"query": {"terms": {"place_id": [str(p) for p in place_ids]}}},
                params={"conflicts": "proceed", "refresh": "true"},
            )
            return int(resp.get("deleted", 0))

        try:
            return await asyncio.to_thread(_del)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"delete_ids failed: {exc}") from exc

    async def get_doc(self, place_id: str) -> PlaceDocumentV1 | None:
        def _get() -> PlaceDocumentV1 | None:
            client = self._os()
            try:
                resp = client.get(index=self.alias, id=str(place_id))
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    return None
                raise
            src = resp.get("_source")
            return doc_from_index_source(src) if src else None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"get failed: {exc}") from exc

    # ── queries ──────────────────────────────────────────────────────────

    async def search(
        self, spec: LocalQuerySpec, *, top_k: int
    ) -> list[tuple[PlaceDocumentV1, float | None]]:
        """Run the compiled query; returns (doc, os_score) pairs."""

        def _search() -> list[tuple[PlaceDocumentV1, float | None]]:
            client = self._os()
            resp = client.search(index=self.alias, body=build_search_body(spec, top_k=top_k))
            hits = resp.get("hits", {}).get("hits", [])
            out = []
            for h in hits:
                src = h.get("_source")
                if not src:
                    continue
                out.append((doc_from_index_source(src), h.get("_score")))
            return out

        try:
            return await asyncio.to_thread(_search)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"search failed: {exc}") from exc

    async def autocomplete(
        self, q_folded: str, *, limit: int, lat: float | None, lon: float | None
    ) -> list[tuple[PlaceDocumentV1, float | None]]:
        def _ac() -> list[tuple[PlaceDocumentV1, float | None]]:
            client = self._os()
            resp = client.search(
                index=self.alias,
                body=build_autocomplete_body(q_folded, limit=limit, lat=lat, lon=lon),
            )
            out = []
            for h in resp.get("hits", {}).get("hits", []):
                src = h.get("_source")
                if src:
                    out.append((doc_from_index_source(src), h.get("_score")))
            return out

        try:
            return await asyncio.to_thread(_ac)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"autocomplete failed: {exc}") from exc

    async def all_ids(self, *, batch_size: int = 2000) -> set[str]:
        """Every ``place_id`` in the index (scroll)."""

        def _scroll() -> set[str]:
            client = self._os()
            ids: set[str] = set()
            resp = client.search(
                index=self.alias,
                body={
                    "size": batch_size,
                    "query": {"match_all": {}},
                    "_source": ["place_id"],
                },
                params={"scroll": "2m"},
            )
            scroll_id = resp.get("_scroll_id")
            try:
                while True:
                    hits = resp.get("hits", {}).get("hits", [])
                    if not hits:
                        break
                    for h in hits:
                        pid = (h.get("_source") or {}).get("place_id") or h.get("_id")
                        if pid is not None:
                            ids.add(str(pid))
                    resp = client.scroll(scroll_id=scroll_id, params={"scroll": "2m"})
            finally:
                if scroll_id:
                    with contextlib.suppress(Exception):
                        client.clear_scroll(scroll_id=scroll_id)
            return ids

        try:
            return await asyncio.to_thread(_scroll)
        except Exception as exc:
            raise PlaceIndexUnavailable(f"all_ids failed: {exc}") from exc

    async def stats(self) -> dict[str, Any]:
        """Doc count + store size for observability."""

        def _stats() -> dict[str, Any]:
            client = self._os()
            concrete = None
            if client.indices.exists_alias(name=self.alias):
                concrete = sorted(client.indices.get_alias(name=self.alias).keys())
            name = concrete[-1] if concrete else self.alias
            if not client.indices.exists(index=name):
                return {"index": name, "exists": False}
            st = client.indices.stats(index=name)
            idx = st.get("indices", {}).get(name, {})
            prim = idx.get("primaries", {})
            return {
                "index": name,
                "exists": True,
                "docs": prim.get("docs", {}).get("count", 0),
                "store_bytes": prim.get("store", {}).get("size_in_bytes", 0),
            }

        try:
            return await asyncio.to_thread(_stats)
        except Exception as exc:
            return {"index": self.alias, "exists": False, "error": str(exc)}


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0
