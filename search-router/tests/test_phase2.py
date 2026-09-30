"""Tests for Phase 2: OpenSearch client, hybrid retrieval, RRF fusion."""

from opensearch.client import OpenSearchClient


class TestRRFFusion:
    def test_rrf_basic(self):
        bm25 = [
            {"doc_id": "A", "score": 10.0, "source": {"title": "A"}},
            {"doc_id": "B", "score": 8.0, "source": {"title": "B"}},
            {"doc_id": "C", "score": 6.0, "source": {"title": "C"}},
        ]
        knn = [
            {"doc_id": "B", "score": 0.9, "source": {"title": "B"}},
            {"doc_id": "A", "score": 0.8, "source": {"title": "A"}},
            {"doc_id": "D", "score": 0.7, "source": {"title": "D"}},
        ]
        result = OpenSearchClient._rrf_fusion(bm25, knn, k=60, top_k=3)
        assert len(result) == 3
        # A and B both appear in both lists (rank 0+1 vs 1+0 = equal RRF)
        # C only in BM25, D only in kNN — both should be in top 3
        doc_ids = [r["doc_id"] for r in result]
        assert "A" in doc_ids
        assert "B" in doc_ids

    def test_rrf_empty(self):
        result = OpenSearchClient._rrf_fusion([], [], k=60, top_k=10)
        assert result == []

    def test_rrf_single_list(self):
        bm25 = [
            {"doc_id": "A", "score": 10.0, "source": {"title": "A"}},
            {"doc_id": "B", "score": 8.0, "source": {"title": "B"}},
        ]
        result = OpenSearchClient._rrf_fusion(bm25, [], k=60, top_k=2)
        assert len(result) == 2
        assert result[0]["doc_id"] == "A"


class TestOpenSearchClient:
    def test_client_init(self):
        client = OpenSearchClient(host="localhost", port=9200)
        assert client.host == "localhost"
        assert client.port == 9200

    def test_health_unavailable(self):
        client = OpenSearchClient(host="localhost", port=9999)
        assert client.health() is False
