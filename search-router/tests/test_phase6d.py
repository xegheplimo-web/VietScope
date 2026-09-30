"""Tests for Phase 6D: Qdrant shadow mode."""

from pipeline.qdrant_shadow import QdrantShadowMode, ShadowComparison


class TestShadowComparison:
    def test_comparison_creation(self):
        comp = ShadowComparison(
            query="test query",
            query_id="q_001",
            opensearch_results=["doc_001", "doc_002"],
            qdrant_results=["doc_001", "doc_003"],
            qdrant_latency_ms=150.0,
            recall_at_50=0.5,
            candidate_overlap=0.33,
        )
        assert comp.query == "test query"
        assert comp.query_id == "q_001"
        assert comp.recall_at_50 == 0.5
        assert comp.candidate_overlap == 0.33


class TestQdrantShadowMode:
    def test_shadow_init(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)
        assert shadow.collection == "web_passages_v1"
        assert shadow.top_k == 60

    def test_shadow_init_custom(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://qdrant:6333")
        shadow = QdrantShadowMode(
            qdrant_client=client,
            collection="custom_v1",
            top_k=100,
        )
        assert shadow.collection == "custom_v1"
        assert shadow.top_k == 100

    def test_compute_recall(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)

        os_ids = ["a", "b", "c", "d", "e"]
        qdrant_ids = ["a", "b", "f", "g", "h"]
        recall = shadow._compute_recall(os_ids, qdrant_ids)
        assert recall == 0.4  # 2/5

    def test_compute_recall_empty(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)

        recall = shadow._compute_recall([], ["a", "b"])
        assert recall == 0.0

    def test_compute_overlap(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)

        os_ids = ["a", "b", "c"]
        qdrant_ids = ["b", "c", "d"]
        overlap = shadow._compute_overlap(os_ids, qdrant_ids)
        assert overlap == 0.5  # 2/4

    def test_compute_overlap_empty(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)

        overlap = shadow._compute_overlap([], ["a"])
        assert overlap == 0.0

    def test_get_summary_empty(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        shadow = QdrantShadowMode(qdrant_client=client)

        summary = shadow.get_summary()
        assert summary["total"] == 0
