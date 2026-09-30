"""Tests for Phase 6C: vector sync worker, dual-write, reconciliation."""

from workers.vector_sync_worker import (
    SyncStatus,
    SyncTask,
    VectorSyncWorker,
)


class TestSyncTask:
    def test_task_creation(self):
        task = SyncTask(
            passage_id="doc_001#p_001",
            text="Test passage",
            text_ctx="Test passage with context",
            embedding=[0.1, 0.2, 0.3],
            payload={"url": "https://example.com"},
        )
        assert task.passage_id == "doc_001#p_001"
        assert task.status == SyncStatus.PENDING
        assert task.retry_count == 0

    def test_task_with_sparse(self):
        task = SyncTask(
            passage_id="doc_001#p_002",
            text="Test",
            text_ctx="Test ctx",
            embedding=[0.1, 0.2],
            sparse_vector={"0": 0.5, "1": 0.3},
            payload={"domain": "example.com"},
        )
        assert task.sparse_vector == {"0": 0.5, "1": 0.3}


class TestVectorSyncWorker:
    def test_worker_init(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient

        os_client = OpenSearchClient(host="localhost", port=9200)
        qdrant_client = QdrantClient(base_url="http://localhost:6333")

        worker = VectorSyncWorker(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
        )
        assert worker.opensearch_index == "web_passages"
        assert worker.qdrant_collection == "web_passages_v1"
        assert worker.max_retries == 3

    def test_worker_init_custom(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient

        os_client = OpenSearchClient(host="os", port=9200)
        qdrant_client = QdrantClient(base_url="http://qdrant:6333")

        worker = VectorSyncWorker(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
            opensearch_index="custom_passages",
            qdrant_collection="custom_v1",
            max_retries=5,
        )
        assert worker.opensearch_index == "custom_passages"
        assert worker.qdrant_collection == "custom_v1"
        assert worker.max_retries == 5


class TestSyncStatus:
    def test_status_values(self):
        assert SyncStatus.PENDING == "pending"
        assert SyncStatus.OPENSEARCH_DONE == "opensearch_done"
        assert SyncStatus.QDRANT_DONE == "qdrant_done"
        assert SyncStatus.BOTH_DONE == "both_done"
        assert SyncStatus.FAILED == "failed"
