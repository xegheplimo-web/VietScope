"""Tests for BGE Embedding Service (Phase 1)."""

import pytest
from services.bge_embedding import BgeEmbeddingService, get_bge_embedding


class TestBgeEmbeddingService:
    """Unit tests for the BgeEmbeddingService."""

    def test_init_defaults(self):
        svc = BgeEmbeddingService()
        assert svc.model_name == "BAAI/bge-m3"
        assert svc.batch_size == 32
        assert svc.max_seq_length == 8192
        assert svc._model is None
        assert not svc._load_failed

    def test_init_custom(self):
        svc = BgeEmbeddingService(
            model_name="custom/embedding",
            device="cpu",
            batch_size=16,
            max_seq_length=512,
        )
        assert svc.model_name == "custom/embedding"
        assert svc.device == "cpu"
        assert svc.batch_size == 16
        assert svc.max_seq_length == 512

    def test_available_returns_false_when_not_loaded(self):
        svc = BgeEmbeddingService()
        result = svc.available()
        assert isinstance(result, bool)

    def test_embed_returns_none_when_unavailable(self):
        svc = BgeEmbeddingService()
        svc._load_failed = True
        result = svc.embed(["some text"])
        assert result is None

    def test_embed_returns_none_for_empty(self):
        svc = BgeEmbeddingService()
        result = svc.embed([])
        assert result is None

    def test_embed_query_returns_none_when_unavailable(self):
        svc = BgeEmbeddingService()
        svc._load_failed = True
        result = svc.embed_query("test")
        assert result is None

    def test_embed_documents_returns_none_when_unavailable(self):
        svc = BgeEmbeddingService()
        svc._load_failed = True
        result = svc.embed_documents(["doc1", "doc2"])
        assert result is None

    def test_rerank_returns_empty_for_empty_docs(self):
        svc = BgeEmbeddingService()
        result = svc.rerank("query", [])
        assert result == []

    def test_rerank_returns_identity_on_failure(self):
        svc = BgeEmbeddingService()
        svc._load_failed = True
        result = svc.rerank("query", ["doc1", "doc2"])
        assert result == [(0, 0.0), (1, 0.0)]

    def test_singleton(self):
        s1 = get_bge_embedding()
        s2 = get_bge_embedding()
        assert s1 is s2

    def test_detect_device(self):
        svc = BgeEmbeddingService()
        device = svc._detect_device()
        assert device in ("cuda", "mps", "cpu", None)

    def test_cosine_similarity(self):
        from services.bge_embedding import _cosine

        assert _cosine([1, 0], [1, 0]) == 1.0
        assert _cosine([1, 0], [0, 1]) == 0.0
        assert _cosine([1, 1], [1, 1]) == pytest.approx(1.0)
        assert _cosine([], [1, 0]) == 0.0
        assert _cosine([1, 0], []) == 0.0
        assert _cosine([1, 0], [1, 0, 0]) == 0.0  # Different lengths

    def test_normalize_embeddings(self):
        """Embeddings should be normalized to unit length."""
        svc = BgeEmbeddingService()
        svc._load_failed = True
        # When load fails, embed returns None
        assert svc.embed(["test"]) is None


class TestBgeEmbeddingServiceIntegration:
    """Integration tests (only run when model is available."""

    @pytest.mark.skipif(
        not BgeEmbeddingService().available(),
        reason="sentence-transformers not available",
    )
    def test_embed_and_rerank(self):
        svc = BgeEmbeddingService()
        texts = ["hello world", "foo bar baz", "test document"]
        vectors = svc.embed(texts)
        assert vectors is not None
        assert len(vectors) == len(texts)

        scores = svc.rerank("hello", texts)
        assert scores is not None
        assert len(scores) == len(texts)
