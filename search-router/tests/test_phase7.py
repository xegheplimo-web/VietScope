"""Tests for Phase 7: Qdrant sparse/multivector retrieval, late interaction."""

from qdrant.multivector import (
    LateInteractionResult,
    MultiVectorPoint,
    QdrantMultiVectorRetriever,
)


class TestMultiVectorPoint:
    def test_point_creation(self):
        point = MultiVectorPoint(
            point_id="doc_001#p_001",
            dense=[0.1, 0.2],
            sparse={"0": 0.5},
            multivector=[[0.1, 0.2], [0.3, 0.4]],
            payload={"url": "https://example.com"},
        )
        assert point.point_id == "doc_001#p_001"
        assert point.dense == [0.1, 0.2]
        assert point.sparse == {"0": 0.5}
        assert point.multivector == [[0.1, 0.2], [0.3, 0.4]]

    def test_point_minimal(self):
        point = MultiVectorPoint(point_id="doc_001#p_002")
        assert point.dense is None
        assert point.sparse is None
        assert point.multivector is None


class TestLateInteractionResult:
    def test_result_creation(self):
        result = LateInteractionResult(
            point_id="doc_001#p_001",
            score=0.95,
            payload={"domain": "example.com"},
        )
        assert result.point_id == "doc_001#p_001"
        assert result.score == 0.95
        assert result.payload["domain"] == "example.com"


class TestQdrantMultiVectorRetriever:
    def test_retriever_init(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        retriever = QdrantMultiVectorRetriever(qdrant_client=client)
        assert retriever.collection == "web_passages_v1"
        assert retriever.top_k == 60

    def test_retriever_init_custom(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://qdrant:6333")
        retriever = QdrantMultiVectorRetriever(
            qdrant_client=client,
            collection="custom_v1",
            top_k=100,
        )
        assert retriever.collection == "custom_v1"
        assert retriever.top_k == 100
