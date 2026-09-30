"""Vector Sync Worker — dual-write passages to OpenSearch + Qdrant.

After indexing worker produces passages with embeddings, this worker
writes to both OpenSearch (lexical) and Qdrant (vector) indexes.

Failure policy:
- OpenSearch success + Qdrant fail → mark qdrant_sync_pending
- Qdrant success + OpenSearch fail → mark lexical_sync_pending
- Both fail → index retry queue
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from opensearch.client import OpenSearchClient
from qdrant.client import QdrantClient, QdrantPoint

logger = logging.getLogger(__name__)


class SyncStatus(StrEnum):
    """Sync status for a passage."""

    PENDING = "pending"
    OPENSEARCH_DONE = "opensearch_done"
    QDRANT_DONE = "qdrant_done"
    BOTH_DONE = "both_done"
    FAILED = "failed"


@dataclass
class SyncTask:
    """A single passage sync task."""

    passage_id: str
    text: str
    text_ctx: str
    embedding: list[float] | None = None
    sparse_vector: dict[str, float] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    status: SyncStatus = SyncStatus.PENDING
    retry_count: int = 0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    passage_id: str
    opensearch_ok: bool
    qdrant_ok: bool
    status: SyncStatus
    error: str = ""


class VectorSyncWorker:
    """Dual-write passages to OpenSearch and Qdrant."""

    def __init__(
        self,
        opensearch_client: OpenSearchClient,
        qdrant_client: QdrantClient,
        opensearch_index: str = "web_passages",
        qdrant_collection: str = "web_passages_v1",
        max_retries: int = 3,
        retry_delay_seconds: float = 5.0,
    ):
        self.opensearch = opensearch_client
        self.qdrant = qdrant_client
        self.opensearch_index = opensearch_index
        self.qdrant_collection = qdrant_collection
        self.max_retries = max_retries
        self.retry_delay = retry_delay_seconds
        self._pending: dict[str, SyncTask] = {}
        self._lock = asyncio.Lock()

    async def submit(self, task: SyncTask) -> None:
        """Submit a passage for dual-write."""
        async with self._lock:
            self._pending[task.passage_id] = task

    async def sync_passage(self, task: SyncTask) -> SyncResult:
        """Sync a single passage to both indexes."""
        opensearch_ok = False
        qdrant_ok = False
        errors: list[str] = []

        # Write to OpenSearch
        try:
            opensearch_ok = await self._write_opensearch(task)
        except Exception as exc:
            errors.append(f"opensearch: {exc}")

        # Write to Qdrant
        try:
            qdrant_ok = await self._write_qdrant(task)
        except Exception as exc:
            errors.append(f"qdrant: {exc}")

        # Determine status
        if opensearch_ok and qdrant_ok:
            status = SyncStatus.BOTH_DONE
        elif opensearch_ok:
            status = SyncStatus.OPENSEARCH_DONE
        elif qdrant_ok:
            status = SyncStatus.QDRANT_DONE
        else:
            status = SyncStatus.FAILED

        return SyncResult(
            passage_id=task.passage_id,
            opensearch_ok=opensearch_ok,
            qdrant_ok=qdrant_ok,
            status=status,
            error="; ".join(errors),
        )

    async def _write_opensearch(self, task: SyncTask) -> bool:
        """Write passage to OpenSearch."""
        doc = {
            "passage_id": task.passage_id,
            "text": task.text,
            "text_ctx": task.text_ctx,
            **task.payload,
        }
        # knn_vector rejects empty arrays — omit the field entirely when the
        # embedding-service is unavailable so the lexical doc still lands.
        if task.embedding:
            doc["embedding"] = task.embedding
        return await self.opensearch.upsert(
            index=self.opensearch_index,
            doc_id=task.passage_id,
            document=doc,
        )

    async def _write_qdrant(self, task: SyncTask) -> bool:
        """Write passage to Qdrant — skipped (degrades) when no vector exists."""
        if not task.embedding and not task.sparse_vector:
            return False
        point = QdrantPoint(
            point_id=task.passage_id,
            dense=task.embedding,
            sparse=task.sparse_vector,
            payload=task.payload,
        )
        return await self.qdrant.upsert_points(
            collection=self.qdrant_collection,
            points=[point],
        )

    async def process_pending(self) -> list[SyncResult]:
        """Process all pending sync tasks."""
        async with self._lock:
            tasks = list(self._pending.values())
            self._pending.clear()

        results: list[SyncResult] = []
        for task in tasks:
            result = await self.sync_passage(task)
            results.append(result)

            # Retry failed tasks
            if result.status == SyncStatus.FAILED and task.retry_count < self.max_retries:
                task.retry_count += 1
                task.last_error = result.error
                await asyncio.sleep(self.retry_delay)
                async with self._lock:
                    self._pending[task.passage_id] = task

        return results

    async def reconcile(self) -> dict[str, int]:
        """Reconcile sync status across both indexes."""
        stats = {
            "total": 0,
            "both_done": 0,
            "opensearch_only": 0,
            "qdrant_only": 0,
            "failed": 0,
        }

        # Get all passage IDs from OpenSearch
        os_ids = await self.opensearch.get_all_ids(self.opensearch_index)
        qdrant_ids = await self.qdrant.get_all_ids(self.qdrant_collection)

        all_ids = set(os_ids) | set(qdrant_ids)
        stats["total"] = len(all_ids)

        for pid in all_ids:
            in_os = pid in os_ids
            in_qdrant = pid in qdrant_ids

            if in_os and in_qdrant:
                stats["both_done"] += 1
            elif in_os:
                stats["opensearch_only"] += 1
            elif in_qdrant:
                stats["qdrant_only"] += 1
            else:
                stats["failed"] += 1

        return stats
