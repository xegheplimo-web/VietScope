"""L17 Indexing Worker — async backfill + contextual LLM prefix.

Runs after response is streamed to user:
- Upsert web_documents + web_passages (OpenSearch)
- Embed passages (batch, via embedding-service)
- Dual-write to Qdrant via VectorSyncWorker (Phase 6C)
- Compute simhash, content_hash
- Run contextual LLM prefix (Layer 3) for high-value documents
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from config import settings
from opensearch.client import OpenSearchClient
from qdrant.client import QdrantClient
from workers.vector_sync_worker import SyncTask, VectorSyncWorker

logger = logging.getLogger(__name__)


@dataclass
class IndexingTask:
    """A single indexing task."""

    doc_id: str
    url: str
    title: str
    text: str
    domain: str
    source_type: str
    published_at: str | None = None
    priority: float = 0.5
    # Optional document-level fields from the extraction pipeline —
    # indexed into web_documents when present.
    description: str | None = None
    site_name: str | None = None
    author: str | None = None
    language: str | None = None


def doc_id_for_url(url: str) -> str:
    """Deterministic document ID from a URL — dedup-safe across runs."""
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"doc_{digest}"


class IndexingWorker:
    """Async indexing worker for post-response processing.

    ``opensearch_client``/``qdrant_client`` are injectable for tests; when not
    provided they are constructed lazily from ``settings`` on first use.
    """

    def __init__(
        self,
        embedding_service_url: str | None = None,
        opensearch_url: str | None = None,
        batch_size: int = 32,
        opensearch_client: OpenSearchClient | None = None,
        qdrant_client: QdrantClient | None = None,
    ):
        self.embedding_service_url = embedding_service_url or settings.embedding_service_url
        self.opensearch_url = opensearch_url  # legacy ctor arg, unused
        self.batch_size = batch_size
        self._opensearch = opensearch_client
        self._qdrant = qdrant_client
        self._sync: VectorSyncWorker | None = None
        self._queue: asyncio.Queue[IndexingTask] = asyncio.Queue()

    @property
    def opensearch(self) -> OpenSearchClient:
        if self._opensearch is None:
            self._opensearch = OpenSearchClient()
        return self._opensearch

    @property
    def qdrant(self) -> QdrantClient:
        if self._qdrant is None:
            self._qdrant = QdrantClient()
        return self._qdrant

    @property
    def sync(self) -> VectorSyncWorker:
        if self._sync is None:
            self._sync = VectorSyncWorker(
                opensearch_client=self.opensearch,
                qdrant_client=self.qdrant,
                opensearch_index=settings.opensearch_index_passages,
                qdrant_collection=settings.qdrant_collection_passages,
            )
        return self._sync

    async def enqueue(self, task: IndexingTask) -> None:
        """Add task to indexing queue."""
        await self._queue.put(task)

    async def process_one(self, task: IndexingTask) -> dict[str, Any]:
        """Index one document end-to-end with per-stage detail.

        Returns ``{indexed, indexing_status, embedding_status, os_ok,
        passages, embedded, errors}`` — the crawl pipeline persists the
        status fields on the document row instead of inferring success
        from a missing exception. Never raises.
        """
        report: dict[str, Any] = {
            "indexed": False,
            "indexing_status": "failed",
            "embedding_status": "skipped",
            "os_ok": False,
            "passages": 0,
            "embedded": 0,
            "failed": 0,
            "stale_deleted": 0,
            "errors": [],
        }
        try:
            content_hash = hashlib.sha256(task.text.encode()).hexdigest()
            simhash = self._compute_simhash(task.text)
            report["os_ok"] = await self._index_document(task, content_hash, simhash)
            passages = self._chunk_text(task.text)
            p = await self._index_passages(task, passages)
            report.update(p)
            if report["os_ok"] and report["failed"] == 0:
                report["indexed"] = True
                report["indexing_status"] = "success"
        except Exception as exc:  # noqa: BLE001 — report, never raise
            logger.warning("Indexing failed for %s: %s", task.url, exc)
            report["errors"].append(f"{type(exc).__name__}: {exc}")
        return report

    async def process_batch(self, tasks: list[IndexingTask]) -> dict[str, Any]:
        """Process a batch of indexing tasks."""
        results = {
            "indexed": 0,
            "failed": 0,
            "errors": [],
        }

        for task in tasks:
            report = await self.process_one(task)
            if report["indexed"]:
                results["indexed"] += 1
            else:
                results["failed"] += 1
                results["errors"].extend(report["errors"])

        return results

    async def _index_document(
        self,
        task: IndexingTask,
        content_hash: str,
        simhash: str,
    ) -> bool:
        """Upsert the document record into ``web_documents``."""
        doc = {
            "doc_id": task.doc_id,
            "url": task.url,
            "canonical_url": task.url,
            "domain": task.domain,
            "title": task.title,
            "source_type": task.source_type,
            "published_at": task.published_at,
            "crawled_at": datetime.now(UTC).isoformat(),
            "content_hash": content_hash,
            "simhash": simhash,
            "word_count": len(task.text.split()),
        }
        if task.description:
            doc["description"] = task.description
        if task.site_name:
            doc["site_name"] = task.site_name
        if task.author:
            doc["author"] = task.author
        if task.language:
            doc["lang"] = task.language
        ok = await self.opensearch.upsert(
            index=settings.opensearch_index_documents,
            doc_id=task.doc_id,
            document=doc,
        )
        if not ok:
            logger.warning("web_documents upsert failed for %s", task.url)
        return ok

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Batch-embed texts via the embedding-service ``POST /embed``.

        BGE-M3 on CPU is slow (~seconds per text under contention), so the
        batch is chunked — a single oversized request would exceed the HTTP
        timeout and lose every vector instead of just the slow tail.
        """
        if not texts or not settings.embedding_service_enabled:
            return None
        chunk_size = 8
        out: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                for start in range(0, len(texts), chunk_size):
                    chunk = texts[start : start + chunk_size]
                    resp = await client.post(
                        f"{self.embedding_service_url}/embed",
                        json={"texts": chunk},
                    )
                    resp.raise_for_status()
                    vectors = resp.json().get("vectors")
                    if not vectors or len(vectors) != len(chunk):
                        logger.warning(
                            "Embedding service returned %s vectors for %s texts",
                            len(vectors) if vectors else 0,
                            len(chunk),
                        )
                        return None
                    out.extend(vectors)
            return out
        except Exception as exc:
            logger.warning("Embedding service call failed: %r", exc)
            return None

    async def _index_passages(
        self,
        task: IndexingTask,
        passages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Embed passages and dual-write via the vector sync worker.

        Returns ``{passages, embedded, failed, embedding_status,
        stale_deleted}`` so callers can persist honest per-stage status.
        Idempotent: passage IDs are deterministic (``doc_id#p_NNN`` →
        UUID5 in Qdrant), and after the new set is durable the stale tail
        from any previous, longer chunking is deleted — a URL recrawled
        N times never leaves more than one generation of vectors.
        """
        result: dict[str, Any] = {
            "passages": len(passages),
            "embedded": 0,
            "failed": 0,
            "embedding_status": "skipped",
            "stale_deleted": 0,
        }
        if not passages:
            return result

        vectors = await self._embed([p["text"] for p in passages])
        if vectors is not None:
            result["embedded"] = len(vectors)
            result["embedding_status"] = "success"
        elif settings.embedding_service_enabled:
            result["embedding_status"] = "failed"
        crawled_at = datetime.now(UTC).isoformat()

        for i, passage in enumerate(passages):
            passage_id = f"{task.doc_id}#p_{i:03d}"
            embedding = vectors[i] if vectors else None
            text = passage["text"]
            payload = {
                "doc_id": task.doc_id,
                "passage_id": passage_id,
                "canonical_url": task.url,
                "url": task.url,
                "title": task.title,
                "domain": task.domain,
                "lang": "vi"
                if re.search(
                    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
                    text.lower(),
                )
                else "en",
                "source_type": task.source_type,
                "published_at": task.published_at,
                "crawled_at": crawled_at,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "token_count": len(text.split()),
                "seq": i,
                "char_start": passage["char_start"],
                "char_end": passage["char_end"],
            }
            sync = await self.sync.sync_passage(
                SyncTask(
                    passage_id=passage_id,
                    text=text,
                    text_ctx=text,
                    embedding=embedding,
                    payload=payload,
                )
            )
            if sync.status.value == "failed":
                result["failed"] += 1
                logger.warning("passage sync failed: %s — %s", passage_id, sync.error)

        # Drop the stale tail: when new content chunks into fewer
        # passages than the previous generation, the old extras must go.
        keep_ids = [f"{task.doc_id}#p_{i:03d}" for i in range(len(passages))]
        result["stale_deleted"] = await self._delete_stale_passages(task.doc_id, keep_ids)
        return result

    async def _delete_stale_passages(self, doc_id: str, keep_ids: list[str]) -> int:
        """Delete indexed passages for ``doc_id`` not in ``keep_ids``.

        Best-effort — a cleanup failure leaves stale data (visible, not
        lost) so it logs instead of failing the index operation.
        """
        deleted = 0
        try:
            deleted += await self.opensearch.delete_by_query(
                settings.opensearch_index_passages,
                {
                    "bool": {
                        "must": [{"term": {"doc_id": doc_id}}],
                        "must_not": [{"ids": {"values": keep_ids}}],
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale OS passages cleanup failed for %s: %r", doc_id, exc)
        try:
            await self.qdrant.delete_points(
                settings.qdrant_collection_passages,
                must=[{"key": "doc_id", "match": {"value": doc_id}}],
                must_not=[{"key": "passage_id", "match": {"any": keep_ids}}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale Qdrant points cleanup failed for %s: %r", doc_id, exc)
        return deleted

    def _compute_simhash(self, text: str) -> str:
        """Compute simhash for near-duplicate detection."""
        # Simplified simhash — use hashlib for now
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _chunk_text(
        self,
        text: str,
        chunk_size: int = 800,
        overlap: int = 200,
    ) -> list[dict[str, Any]]:
        """Split text into overlapping chunks."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk_text = text[start:end]
            chunks.append(
                {
                    "passage_id": f"p_{len(chunks):03d}",
                    "text": chunk_text,
                    "char_start": start,
                    "char_end": end,
                }
            )
            start = end - overlap
        return chunks


# ─── Post-response hook (fire-and-forget) ────────────────────────────────────

_WORKER: IndexingWorker | None = None


def get_indexing_worker() -> IndexingWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = IndexingWorker()
    return _WORKER


def submit_document(
    url: str,
    title: str,
    text: str,
    *,
    published_at: str | None = None,
    source_type: str = "web",
) -> None:
    """Schedule a scraped document for indexing — never blocks the caller.

    No-ops when indexing/OpenSearch are disabled, when there is no running
    event loop (sync tests), or when the payload is empty.
    """
    if not (settings.indexing_enabled and settings.opensearch_enabled):
        return
    if not url or not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = IndexingTask(
        doc_id=doc_id_for_url(url),
        url=url,
        title=title or "",
        text=text,
        domain=urlparse(url).netloc.lower().removeprefix("www."),
        source_type=source_type,
        published_at=published_at,
    )
    worker = get_indexing_worker()

    async def _run() -> None:
        try:
            await worker.process_batch([task])
        except Exception as exc:  # noqa: BLE001 — background must not crash
            logger.warning("indexing task failed: %s", exc)

    loop.create_task(_run())
