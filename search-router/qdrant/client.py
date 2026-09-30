"""Qdrant client wrapper for Search-Hub.

Provides collection management, point upsert, and hybrid search
(dense + sparse vectors with payload filtering).

Point-ID policy (v2.1 §7): callers address points by the stable ``passage_id``
string (``doc_xxx#p_NNN``).  Qdrant only accepts UUID/uint64 IDs, so the wire
ID is ``UUID5(passage_id)`` and the original ``passage_id`` is stored in the
payload.  ``QdrantSearchResult.point_id`` always carries the ``passage_id``
so RRF fusion keys match OpenSearch ``_id``s.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from config import settings

logger = logging.getLogger(__name__)

# Deterministic namespace for passage_id → point UUID mapping.
_POINT_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "search-hub/passage")


def to_point_id(passage_id: str) -> str:
    """Map a stable passage_id to a deterministic Qdrant point UUID."""
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, passage_id))


@dataclass
class QdrantPoint:
    """A single Qdrant point with vectors and payload."""

    point_id: str  # stable passage_id — translated to UUID5 on the wire
    dense: list[float] | None = None
    sparse: dict[str, float] | None = None  # {index: value}
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class QdrantSearchResult:
    """Result from Qdrant search.

    ``point_id`` is the original ``passage_id`` (recovered from payload) so it
    joins cleanly with OpenSearch result IDs during RRF fusion.
    ``qdrant_id`` keeps the raw wire UUID for debugging.
    """

    point_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    qdrant_id: str = ""


class QdrantClient:
    """HTTP client for Qdrant REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_ms: int | None = None,
    ):
        self.base_url = (base_url or settings.qdrant_url).rstrip("/")
        self.timeout_ms = timeout_ms or settings.qdrant_timeout_ms
        # timeout_ms is the query-path budget. Admin/write ops (collection
        # create, upsert, scroll) can legitimately exceed it, so they get a
        # separate generous timeout.
        self._search_timeout = self.timeout_ms / 1000.0
        self._write_timeout = max(self._search_timeout, 15.0)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._write_timeout,
            )
        return self._client

    async def health(self) -> bool:
        """Check Qdrant health."""
        try:
            client = await self._get_client()
            resp = await client.get("/healthz")
            return resp.status_code == 200
        except Exception:
            return False

    async def collection_exists(self, collection: str) -> bool:
        """Check if collection exists."""
        try:
            client = await self._get_client()
            resp = await client.get(f"/collections/{collection}")
            return resp.status_code == 200
        except Exception:
            return False

    async def create_collection(
        self,
        collection: str,
        vector_size: int = 1024,
        distance: str = "Cosine",
        sparse_enabled: bool = True,
    ) -> bool:
        """Create collection with dense (+ optional sparse) vector support."""
        try:
            client = await self._get_client()
            body: dict[str, Any] = {
                "vectors": {
                    "dense": {
                        "size": vector_size,
                        "distance": distance,
                    },
                },
            }
            if sparse_enabled:
                body["sparse_vectors"] = {
                    "sparse": {
                        "index": {
                            "on_disk": True,
                        },
                    },
                }
            resp = await client.put(f"/collections/{collection}", json=body)
            if resp.status_code not in (200, 201):
                logger.warning(
                    "Qdrant create_collection %s -> %s: %s",
                    collection,
                    resp.status_code,
                    resp.text[:300],
                )
                return False
            return True
        except Exception as exc:
            logger.warning("Qdrant create_collection failed: %r", exc)
            return False

    async def upsert_points(
        self,
        collection: str,
        points: list[QdrantPoint],
    ) -> bool:
        """Upsert points into collection."""
        if not points:
            return True
        try:
            client = await self._get_client()
            body = {
                "points": [
                    {
                        "id": to_point_id(p.point_id),
                        "vector": {
                            "dense": p.dense,
                            "sparse": p.sparse,
                        }
                        if p.sparse
                        else {
                            "dense": p.dense,
                        },
                        "payload": {"passage_id": p.point_id, **p.payload},
                    }
                    for p in points
                ],
            }
            resp = await client.put(
                f"/collections/{collection}/points",
                json=body,
            )
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.warning("Qdrant upsert failed: %r", exc)
            return False

    @staticmethod
    def _to_result(r: dict[str, Any]) -> QdrantSearchResult:
        payload = r.get("payload") or {}
        return QdrantSearchResult(
            point_id=payload.get("passage_id") or str(r["id"]),
            qdrant_id=str(r["id"]),
            score=r["score"],
            payload=payload,
        )

    async def search_dense(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[QdrantSearchResult]:
        """Search by dense vector (named vector ``dense``)."""
        try:
            client = await self._get_client()
            body: dict[str, Any] = {
                # Named-vector collections require {"name", "vector"} form.
                "vector": {"name": "dense", "vector": query_vector},
                "limit": top_k,
                "with_payload": True,
                "with_vector": False,
            }
            if filters:
                body["filter"] = filters
            resp = await client.post(
                f"/collections/{collection}/points/search",
                json=body,
                timeout=self._search_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return [self._to_result(r) for r in data.get("result", [])]
        except Exception as exc:
            logger.warning("Qdrant dense search failed: %r", exc)
            return []

    async def search_sparse(
        self,
        collection: str,
        sparse_vector: dict[str, float],
        top_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[QdrantSearchResult]:
        """Search by sparse vector (named sparse vector ``sparse``)."""
        try:
            client = await self._get_client()
            body: dict[str, Any] = {
                "vector": {
                    "name": "sparse",
                    "vector": {
                        "indices": [int(k) for k in sparse_vector],
                        "values": list(sparse_vector.values()),
                    },
                },
                "limit": top_k,
                "with_payload": True,
                "with_vector": False,
            }
            if filters:
                body["filter"] = filters
            resp = await client.post(
                f"/collections/{collection}/points/search",
                json=body,
                timeout=self._search_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return [self._to_result(r) for r in data.get("result", [])]
        except Exception as exc:
            logger.warning("Qdrant sparse search failed: %r", exc)
            return []

    async def get_all_ids(self, collection: str, *, batch_size: int = 1000) -> list[str]:
        """Return every point's ``passage_id`` via the scroll API."""
        ids: list[str] = []
        try:
            client = await self._get_client()
            offset: Any = None
            while True:
                body: dict[str, Any] = {
                    "limit": batch_size,
                    "with_payload": ["passage_id"],
                    "with_vector": False,
                }
                if offset is not None:
                    body["offset"] = offset
                resp = await client.post(
                    f"/collections/{collection}/points/scroll",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json().get("result", {})
                points = data.get("points", [])
                if not points:
                    break
                for p in points:
                    payload = p.get("payload") or {}
                    ids.append(payload.get("passage_id") or str(p["id"]))
                offset = data.get("next_page_offset")
                if offset is None:
                    break
            return ids
        except Exception as exc:
            logger.warning("Qdrant get_all_ids failed: %r", exc)
            return ids

    async def delete_points(
        self,
        collection: str,
        *,
        must: list[dict[str, Any]] | None = None,
        must_not: list[dict[str, Any]] | None = None,
        point_ids: list[str] | None = None,
    ) -> bool:
        """Delete points by filter or explicit ``passage_id`` list.

        ``point_ids`` are stable passage IDs — translated to wire UUIDs
        the same way upsert does. ``wait=true`` so a follow-up recrawl
        never observes a half-deleted generation.
        """
        try:
            client = await self._get_client()
            if point_ids is not None:
                body: dict[str, Any] = {"points": [to_point_id(pid) for pid in point_ids]}
            else:
                body = {
                    "filter": {
                        "must": must or [],
                        "must_not": must_not or [],
                    }
                }
            resp = await client.post(
                f"/collections/{collection}/points/delete?wait=true",
                json=body,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Qdrant delete_points failed: %r", exc)
            return False

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
