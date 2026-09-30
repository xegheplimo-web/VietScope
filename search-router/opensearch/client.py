"""OpenSearch client wrapper for Search-Hub.

Provides index management, document ingestion, and hybrid search
(BM25 + kNN dense → RRF fusion). Sync ``opensearch-py`` core with async
wrappers (``search``, ``upsert``, ``get_all_ids``) so pipeline/worker code can
await it without blocking the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

_MAPPINGS_DIR = Path(__file__).parent / "mappings"


class OpenSearchClient:
    """Thin wrapper around opensearch-py."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_ssl: bool | None = None,
        timeout_s: float | None = None,
    ):
        self.host = host or settings.opensearch_host
        self.port = port or settings.opensearch_port
        self.username = username or settings.opensearch_user
        self.password = password or settings.opensearch_password
        self.use_ssl = settings.opensearch_use_ssl if use_ssl is None else use_ssl
        self.timeout_s = timeout_s or settings.opensearch_timeout_s
        self._client = None

    def _get_client(self):
        """Lazy-initialize OpenSearch client."""
        if self._client is None:
            try:
                from opensearchpy import OpenSearch
            except ImportError as exc:
                raise RuntimeError(
                    "opensearch-py not installed. Install with: pip install opensearch-py"
                ) from exc

            self._client = OpenSearch(
                hosts=[{"host": self.host, "port": self.port}],
                http_auth=(self.username, self.password),
                use_ssl=self.use_ssl,
                verify_certs=False,
                ssl_show_warn=False,
                timeout=self.timeout_s,
            )
        return self._client

    def health(self) -> bool:
        """Check OpenSearch connectivity."""
        try:
            client = self._get_client()
            return client.ping()
        except Exception as exc:
            logger.warning("OpenSearch health check failed: %s", exc)
            return False

    def create_index(self, index_name: str, mappings: dict[str, Any]) -> bool:
        """Create index with mappings if not exists."""
        try:
            client = self._get_client()
            if not client.indices.exists(index=index_name):
                client.indices.create(index=index_name, body=mappings)
                logger.info("Created index: %s", index_name)
            return True
        except Exception as exc:
            logger.error("Failed to create index %s: %s", index_name, exc)
            return False

    def ensure_indices(self) -> dict[str, bool]:
        """Create the canonical indices from ``opensearch/mappings/*.json``."""
        results: dict[str, bool] = {}
        for index_name in (
            settings.opensearch_index_documents,
            settings.opensearch_index_passages,
        ):
            path = _MAPPINGS_DIR / f"{index_name}.json"
            if not path.exists():
                logger.warning("No mapping file for %s (%s)", index_name, path)
                results[index_name] = False
                continue
            try:
                mappings = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Bad mapping file %s: %s", path, exc)
                results[index_name] = False
                continue
            results[index_name] = self.create_index(index_name, mappings)
        return results

    def index_document(self, index_name: str, doc_id: str, document: dict[str, Any]) -> bool:
        """Index a single document."""
        try:
            client = self._get_client()
            client.index(index=index_name, id=doc_id, body=document, params={"refresh": "true"})
            return True
        except Exception as exc:
            logger.error("Failed to index document %s: %s", doc_id, exc)
            return False

    def bulk_index(self, index_name: str, documents: list[dict[str, Any]]) -> bool:
        """Bulk index documents."""
        try:
            client = self._get_client()
            actions = []
            for doc in documents:
                actions.append({"index": {"_index": index_name, "_id": doc["doc_id"]}})
                actions.append(doc)
            if actions:
                client.bulk(body=actions, params={"refresh": "true"})
            return True
        except Exception as exc:
            logger.error("Failed to bulk index: %s", exc)
            return False

    def search_bm25(
        self,
        index_name: str,
        query: str,
        *,
        top_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 text search."""
        try:
            client = self._get_client()
            body: dict[str, Any] = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["text_ctx^2", "title^3", "description"],
                                    "type": "best_fields",
                                }
                            }
                        ]
                    }
                },
            }
            if filters:
                body["query"]["bool"]["filter"] = filters

            resp = client.search(index=index_name, body=body)
            hits = resp.get("hits", {}).get("hits", [])
            return [
                {
                    "doc_id": hit["_id"],
                    "score": hit["_score"],
                    "source": hit["_source"],
                }
                for hit in hits
            ]
        except Exception as exc:
            logger.error("BM25 search failed: %s", exc)
            return []

    def search_knn(
        self,
        index_name: str,
        query_vector: list[float],
        *,
        top_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """kNN dense vector search."""
        try:
            client = self._get_client()
            body: dict[str, Any] = {
                "size": top_k,
                "query": {
                    "knn": {
                        "embedding": {
                            "vector": query_vector,
                            "k": top_k,
                        }
                    }
                },
            }
            if filters:
                body["query"]["knn"]["embedding"]["filter"] = filters

            resp = client.search(index=index_name, body=body)
            hits = resp.get("hits", {}).get("hits", [])
            return [
                {
                    "doc_id": hit["_id"],
                    "score": hit["_score"],
                    "source": hit["_source"],
                }
                for hit in hits
            ]
        except Exception as exc:
            logger.error("kNN search failed: %s", exc)
            return []

    def search_hybrid(
        self,
        index_name: str,
        query: str,
        query_vector: list[float],
        *,
        top_k: int = 60,
        rrf_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search: BM25 + kNN → RRF fusion."""
        bm25_results = self.search_bm25(index_name, query, top_k=top_k, filters=filters)
        knn_results = self.search_knn(index_name, query_vector, top_k=top_k, filters=filters)

        # RRF fusion
        return self._rrf_fusion(bm25_results, knn_results, k=rrf_k, top_k=top_k)

    @staticmethod
    def _rrf_fusion(
        bm25_results: list[dict[str, Any]],
        knn_results: list[dict[str, Any]],
        *,
        k: int = 60,
        top_k: int = 60,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion of two ranked lists."""
        scores: dict[str, float] = {}
        sources: dict[str, dict[str, Any]] = {}

        for rank, item in enumerate(bm25_results):
            doc_id = item["doc_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            sources[doc_id] = item["source"]

        for rank, item in enumerate(knn_results):
            doc_id = item["doc_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            if doc_id not in sources:
                sources[doc_id] = item["source"]

        # Sort by RRF score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        return [
            {
                "doc_id": doc_id,
                "score": score,
                "source": sources[doc_id],
            }
            for doc_id, score in ranked[:top_k]
        ]

    # ── Async wrappers (pipeline/worker API) ─────────────────────────────────

    async def search(
        self,
        index: str,
        query: str,
        top_k: int = 60,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Async BM25 search — the interface FederatedRetriever awaits."""
        return await asyncio.to_thread(self.search_bm25, index, query, top_k=top_k, filters=filters)

    async def upsert(
        self,
        index: str,
        doc_id: str,
        document: dict[str, Any],
    ) -> bool:
        """Async single-document upsert."""
        return await asyncio.to_thread(self.index_document, index, doc_id, document)

    async def get_all_ids(self, index: str, *, batch_size: int = 1000) -> list[str]:
        """Return every ``_id`` in an index via the scroll API."""

        def _scroll() -> list[str]:
            client = self._get_client()
            ids: list[str] = []
            resp = client.search(
                index=index,
                body={"size": batch_size, "query": {"match_all": {}}, "_source": False},
                params={"scroll": "2m"},
            )
            scroll_id = resp.get("_scroll_id")
            try:
                while True:
                    hits = resp.get("hits", {}).get("hits", [])
                    if not hits:
                        break
                    ids.extend(h["_id"] for h in hits)
                    resp = client.scroll(scroll_id=scroll_id, params={"scroll": "2m"})
            finally:
                if scroll_id:
                    with contextlib.suppress(Exception):
                        client.clear_scroll(scroll_id=scroll_id)
            return ids

        try:
            return await asyncio.to_thread(_scroll)
        except Exception as exc:
            logger.warning("get_all_ids failed for %s: %s", index, exc)
            return []

    async def delete_by_query(self, index: str, query: dict[str, Any]) -> int:
        """Delete documents matching ``query``; returns the deleted count.

        ``conflicts=proceed`` keeps a version race (a doc re-indexed mid-
        delete) from failing the whole batch — stale cleanup is
        best-effort by design.
        """

        def _delete() -> int:
            client = self._get_client()
            resp = client.delete_by_query(
                index=index,
                body={"query": query},
                params={"conflicts": "proceed", "refresh": "true"},
            )
            return int(resp.get("deleted", 0))

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.warning("delete_by_query failed for %s: %s", index, exc)
            return 0
