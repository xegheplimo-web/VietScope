"""Tests for the semantic embedding reranker (pipeline.embeddings + rerank_semantic)."""

import pytest
from models import SearchResultItem
from pipeline.embeddings import EmbeddingReranker, _cosine
from pipeline.reranker import rerank_semantic


def _mk(url, title="", desc="", score=1.0):
    return SearchResultItem(url=url, title=title, description=desc, score=score, engine="searxng")


class TestEmbeddingRerankerAvailability:
    def test_disabled_when_no_config(self):
        rr = EmbeddingReranker(base_url="", api_key="", model="")
        assert rr.available() is False

    def test_disabled_when_model_empty(self):
        rr = EmbeddingReranker(base_url="http://x", api_key="k", model="")
        assert rr.available() is False

    def test_enabled_when_fully_configured(self):
        rr = EmbeddingReranker(base_url="http://x/v1", api_key="k", model="m")
        assert rr.available() is True


class TestEmbedFallback:
    def test_embed_returns_none_when_not_configured(self):
        rr = EmbeddingReranker(base_url="", api_key="", model="")
        assert rr.embed(["hi"]) is None

    def test_rerank_identity_without_key(self):
        rr = EmbeddingReranker(base_url="", api_key="", model="")
        docs = ["doc a", "doc b", "doc c"]
        assert rr.rerank("query", docs, top_n=2) == [0, 1, 2]

    def test_rerank_empty(self):
        rr = EmbeddingReranker(base_url="http://x", api_key="k", model="m")
        assert rr.rerank("query", [], top_n=5) == []


class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_length_mismatch(self):
        assert _cosine([1.0], [1.0, 0.0]) == 0.0


class _FakeEmbed:
    """Pretends to embed: query gets the [1,0] direction, docs ordered by
    how close their score vector is to [1,0]."""

    def __init__(self, doc_dirs):
        self.doc_dirs = doc_dirs

    def available(self):
        return True

    def embed(self, texts):
        q = [1.0, 0.0]
        out = [q]
        for t in texts[1:]:
            out.append(self.doc_dirs[t])
        return out


class TestRerankSemantic:
    def test_returns_same_list_when_unavailable(self):
        rr = EmbeddingReranker(base_url="", api_key="", model="")
        results = [_mk("https://a.com", "a"), _mk("https://b.com", "b")]
        assert rerank_semantic("q", results, rr) is results

    def test_returns_same_list_when_embed_fails(self):
        class Broken:
            def available(self):
                return True

            def embed(self, texts):
                return None

        results = [_mk("https://a.com", "a")]
        assert rerank_semantic("q", results, Broken()) is results

    def test_semantic_ordering_with_mock(self):
        # doc "a" is semantically close to the query, "b" is far away.
        rr = _FakeEmbed(
            {
                "a match": [0.99, 0.1],
                "b unrelated": [0.1, 0.99],
            }
        )
        results = [
            _mk("https://a.com", "a match"),
            _mk("https://b.com", "b unrelated"),
        ]
        out = rerank_semantic("python async", results, rr, top_n=10)
        assert [r.url for r in out] == ["https://a.com", "https://b.com"]

    def test_top_n_limits_results(self):
        rr = _FakeEmbed(
            {
                "a match": [0.99, 0.1],
                "b unrelated": [0.1, 0.99],
                "c unrelated": [0.1, 0.98],
            }
        )
        results = [
            _mk("https://a.com", "a match"),
            _mk("https://b.com", "b unrelated"),
            _mk("https://c.com", "c unrelated"),
        ]
        out = rerank_semantic("q", results, rr, top_n=2)
        assert len(out) == 2
        assert out[0].url == "https://a.com"

    def test_empty_results(self):
        rr = _FakeEmbed({})
        assert rerank_semantic("q", [], rr) == []
