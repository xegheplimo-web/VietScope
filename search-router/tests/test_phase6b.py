"""Tests for Phase 6B: Qdrant client, collections, feature flags."""

from qdrant.client import QdrantClient, QdrantPoint
from qdrant.collections import (
    ALL_COLLECTIONS,
    WEB_PASSAGES_V1,
    CollectionSchema,
)


class TestQdrantClient:
    def test_client_init(self):
        client = QdrantClient(base_url="http://localhost:6333")
        assert client.base_url == "http://localhost:6333"
        assert client.timeout_ms == 300

    def test_client_init_custom_timeout(self):
        client = QdrantClient(base_url="http://qdrant:6333", timeout_ms=500)
        assert client.base_url == "http://qdrant:6333"
        assert client.timeout_ms == 500

    def test_health_unavailable(self):
        client = QdrantClient(base_url="http://localhost:9999")
        # Should return False when Qdrant is not running
        import asyncio

        result = asyncio.run(client.health())
        assert result is False

    def test_collection_exists_unavailable(self):
        client = QdrantClient(base_url="http://localhost:9999")
        import asyncio

        result = asyncio.run(client.collection_exists("test"))
        assert result is False


class TestQdrantCollections:
    def test_collection_schema(self):
        schema = CollectionSchema(
            name="test_collection",
            vector_size=1024,
            distance="Cosine",
            sparse_enabled=True,
        )
        assert schema.name == "test_collection"
        assert schema.vector_size == 1024
        assert schema.sparse_enabled is True

    def test_web_passages_schema(self):
        assert WEB_PASSAGES_V1.name == "web_passages_v1"
        assert WEB_PASSAGES_V1.vector_size == 1024
        assert WEB_PASSAGES_V1.sparse_enabled is True

    def test_all_collections(self):
        assert len(ALL_COLLECTIONS) == 3
        names = [c.name for c in ALL_COLLECTIONS]
        assert "web_passages_v1" in names
        assert "web_images_v1" in names
        assert "user_documents_v1" in names


class TestQdrantPoint:
    def test_point_creation(self):
        point = QdrantPoint(
            point_id="doc_001#p_001",
            dense=[0.1, 0.2, 0.3],
            sparse={"0": 0.5, "1": 0.3},
            payload={"url": "https://example.com", "domain": "example.com"},
        )
        assert point.point_id == "doc_001#p_001"
        assert point.dense == [0.1, 0.2, 0.3]
        assert point.sparse == {"0": 0.5, "1": 0.3}
        assert point.payload["domain"] == "example.com"

    def test_point_without_sparse(self):
        point = QdrantPoint(
            point_id="doc_001#p_002",
            dense=[0.1, 0.2],
            payload={"url": "https://example.com"},
        )
        assert point.sparse is None
        assert point.dense == [0.1, 0.2]
