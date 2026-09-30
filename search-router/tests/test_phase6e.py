"""Tests for Phase 6E: federated retrieval, RRF fusion."""

from pipeline.federated_retrieval import FederatedResult, FederatedRetriever


class TestFederatedResult:
    def test_result_creation(self):
        result = FederatedResult(
            doc_ids=["doc_001", "doc_002"],
            scores=[0.5, 0.3],
            opensearch_count=10,
            qdrant_count=8,
            fused_count=12,
            latency_ms=150.0,
        )
        assert result.doc_ids == ["doc_001", "doc_002"]
        assert result.scores == [0.5, 0.3]
        assert result.opensearch_count == 10
        assert result.qdrant_count == 8
        assert result.fused_count == 12
        assert result.latency_ms == 150.0
        assert result.degraded is False

    def test_result_degraded(self):
        result = FederatedResult(
            doc_ids=["doc_001"],
            scores=[0.5],
            opensearch_count=10,
            qdrant_count=0,
            fused_count=10,
            latency_ms=100.0,
            degraded=True,
            degraded_reason="qdrant_failed",
        )
        assert result.degraded is True
        assert result.degraded_reason == "qdrant_failed"


class TestFederatedRetriever:
    def test_retriever_init(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient

        os_client = OpenSearchClient(host="localhost", port=9200)
        qdrant_client = QdrantClient(base_url="http://localhost:6333")

        retriever = FederatedRetriever(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
        )
        assert retriever.opensearch_index == "web_passages"
        assert retriever.qdrant_collection == "web_passages_v1"
        assert retriever.rrf_k == 60
        assert retriever.top_k == 60

    def test_retriever_init_custom(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient

        os_client = OpenSearchClient(host="os", port=9200)
        qdrant_client = QdrantClient(base_url="http://qdrant:6333")

        retriever = FederatedRetriever(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
            opensearch_index="custom_passages",
            qdrant_collection="custom_v1",
            rrf_k=100,
            top_k=100,
        )
        assert retriever.opensearch_index == "custom_passages"
        assert retriever.qdrant_collection == "custom_v1"
        assert retriever.rrf_k == 100
        assert retriever.top_k == 100

    def test_rrf_fuse_basic(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient, QdrantSearchResult

        os_client = OpenSearchClient(host="localhost", port=9200)
        qdrant_client = QdrantClient(base_url="http://localhost:6333")
        retriever = FederatedRetriever(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
        )

        os_results = [
            {"doc_id": "a", "score": 10.0},
            {"doc_id": "b", "score": 8.0},
            {"doc_id": "c", "score": 6.0},
        ]
        qdrant_results = [
            QdrantSearchResult(point_id="b", score=0.9),
            QdrantSearchResult(point_id="d", score=0.8),
            QdrantSearchResult(point_id="a", score=0.7),
        ]

        fused = retriever._rrf_fuse(os_results, qdrant_results)
        assert len(fused) == 4  # a, b, c, d
        # b should be first (rank 1 in OS + rank 0 in Qdrant)
        assert fused[0]["doc_id"] == "b"

    def test_rrf_fuse_empty(self):
        from opensearch.client import OpenSearchClient
        from qdrant.client import QdrantClient

        os_client = OpenSearchClient(host="localhost", port=9200)
        qdrant_client = QdrantClient(base_url="http://localhost:6333")
        retriever = FederatedRetriever(
            opensearch_client=os_client,
            qdrant_client=qdrant_client,
        )

        fused = retriever._rrf_fuse([], [])
        assert fused == []
