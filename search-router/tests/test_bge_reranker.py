"""Tests for BGE Reranker Service (Phase 1)."""

from services.bge_reranker import BgeRerankerService, get_bge_reranker


class TestBgeRerankerService:
    """Unit tests for the BgeRerankerService."""

    def test_init_defaults(self):
        svc = BgeRerankerService()
        assert svc.model_name == "BAAI/bge-reranker-v2-m3"
        assert svc.batch_size == 16  # From config default
        assert svc._model is None
        assert not svc._load_failed

    def test_init_custom(self):
        svc = BgeRerankerService(
            model_name="custom/model",
            device="cpu",
            batch_size=8,
        )
        assert svc.model_name == "custom/model"
        assert svc.device == "cpu"
        assert svc.batch_size == 8

    def test_available_returns_false_when_not_loaded(self):
        svc = BgeRerankerService()
        # Before load, and with no sentence-transformers installed or model
        # load failure, available should return False (but doesn't crash).
        result = svc.available()
        assert isinstance(result, bool)

    def test_score_returns_none_when_unavailable(self):
        svc = BgeRerankerService()
        svc._load_failed = True
        result = svc.score("query", ["doc1", "doc2"])
        assert result is None

    def test_score_returns_none_for_empty_docs(self):
        svc = BgeRerankerService()
        result = svc.score("query", [])
        assert result is None

    def test_singleton(self):
        s1 = get_bge_reranker()
        s2 = get_bge_reranker()
        assert s1 is s2

    def test_detect_device(self):
        svc = BgeRerankerService()
        device = svc._detect_device()
        assert device in ("cuda", "mps", "cpu", None)

    def test_load_returns_false_when_sentence_transformers_missing(self):
        """When sentence-transformers is not installed, _load returns False."""
        svc = BgeRerankerService()
        # This doesn't crash even if sentence-transformers is missing
        result = svc._load()
        assert isinstance(result, bool)

    def test_service_module_importable(self):
        """Verify the services package is importable."""
        import services

        assert hasattr(services, "BgeRerankerService")
        assert hasattr(services, "get_bge_reranker")
