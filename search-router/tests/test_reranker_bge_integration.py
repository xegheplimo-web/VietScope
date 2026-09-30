"""Tests for cross-encoder reranker integration with BGE service (Phase 1)."""

from agent.reranker import CrossEncoderReranker, get_default_reranker


class TestCrossEncoderRerankerBgeIntegration:
    """Verify CrossEncoderReranker delegates to BGE service when available.

    The BGE service may not be available in test environments, so these
    tests verify the delegation logic rather than requiring model weights.
    """

    def test_bge_service_instantiation(self):
        reranker = CrossEncoderReranker()
        # _bge_service should be set (or None if services module unavailable)
        assert hasattr(reranker, "_bge_service")

    def test_bge_service_used_when_available(self):
        """When BGE service is available, score() should delegate to it."""
        reranker = CrossEncoderReranker()
        if reranker._bge_service is not None and reranker._bge_service.available():
            scores = reranker.score("test query", ["doc one", "doc two"])
            assert scores is not None
            assert len(scores) == 2
            assert all(0.0 <= s <= 1.0 for s in scores)

    def test_fallback_when_bge_service_unavailable(self):
        """When BGE service is unavailable, falls back to CrossEncoder."""
        reranker = CrossEncoderReranker()
        if reranker._bge_service is None or not reranker._bge_service.available():
            # With _load_failed set, score returns None (CrossEncoder also fails)
            reranker._load_failed = True
            result = reranker.score("query", ["doc"])
            # Should not crash
            assert result is None or isinstance(result, list)

    def test_available_returns_true_when_bge_available(self):
        reranker = CrossEncoderReranker()
        if reranker._bge_service is not None and reranker._bge_service.available():
            assert reranker.available()

    def test_get_default_reranker_singleton(self):
        r1 = get_default_reranker()
        r2 = get_default_reranker()
        assert r1 is r2


class TestBgeServiceOutputShape:
    """Verify the service returns expected shapes for pipeline consumption."""

    def test_score_returns_list_of_floats(self):
        from services.bge_reranker import BgeRerankerService

        svc = BgeRerankerService()
        if svc.available():
            scores = svc.score("query", ["doc1", "doc2", "doc3"])
            assert scores is not None
            assert len(scores) == 3
            assert all(isinstance(s, float) for s in scores)

    def test_embedding_service_output_shape(self):
        from services.bge_embedding import BgeEmbeddingService

        svc = BgeEmbeddingService()
        if svc.available():
            vectors = svc.embed(["text1", "text2"])
            assert vectors is not None
            assert len(vectors) == 2
            assert all(isinstance(v, list) for v in vectors)

    def test_pipeline_integration_mock(self):
        """Verify rerank_passages works with a mock BGE reranker."""
        from pipeline.passage_reranker import rerank_passages

        class MockReranker:
            def available(self):
                return True

            def score(self, query, docs):
                return [0.9, 0.7, 0.5][: len(docs)] if docs else None

        chunks = [
            {"chunk_text": "hello world", "source_url": "a.com"},
            {"chunk_text": "foo bar", "source_url": "b.com"},
            {"chunk_text": "test doc", "source_url": "c.com"},
        ]
        result = rerank_passages("hello", chunks, top_n=2, reranker=MockReranker())
        assert len(result) == 2
        assert result[0]["score"] > result[1]["score"]

    def test_pipeline_no_reranker_fallback(self):
        """Verify rerank_passages works without a reranker (heuristic only)."""
        from pipeline.passage_reranker import rerank_passages

        chunks = [
            {"chunk_text": "hello world test", "source_url": "a.com"},
            {"chunk_text": "foo bar baz", "source_url": "b.com"},
        ]
        result = rerank_passages("hello", chunks, top_n=2, reranker=None)
        assert len(result) == 2
        # First chunk has "hello" in it, should rank higher
        assert result[0]["source_url"] == "a.com"
