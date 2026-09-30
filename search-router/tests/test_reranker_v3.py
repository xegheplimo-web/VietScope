"""Tests for the v3 reranker enhancements (PART 2).

Covers: BM25-ish scoring, phrase bonus, title boost, diversity penalty,
and backward compatibility with the original keyword/authority/searxng blend.
"""

from models import ScrapeResult, SearchResultItem
from pipeline.reranker import (
    _apply_diversity_penalty,
    _bm25_score,
    _keyword_overlap_score,
    _phrase_bonus,
    _title_boost,
    _tokenize,
    _tokenize_set,
    rerank_scraped_content,
    rerank_search_results,
)


def _mk(url, title="", desc="", score=1.0, engine="searxng"):
    return SearchResultItem(url=url, title=title, description=desc, score=score, engine=engine)


class TestBM25:
    def test_returns_zero_for_empty(self):
        assert _bm25_score([], ["a", "b"], 10) == 0.0
        assert _bm25_score(["a"], [], 10) == 0.0

    def test_nonzero_for_match(self):
        score = _bm25_score(["python", "async"], ["python", "async", "await"], 10)
        assert score > 0.0

    def test_rare_term_higher_than_common(self):
        common = _bm25_score(["the"], ["the", "the", "the"], 10, idf_map={"the": 0.1})
        rare = _bm25_score(["quark"], ["quark", "the", "the"], 10, idf_map={"quark": 5.0})
        assert rare > common


class TestPhraseBonus:
    def test_no_bonus_for_single_word(self):
        assert _phrase_bonus("python", "python is great") == 0.0

    def test_bonus_for_bigram(self):
        bonus = _phrase_bonus("python async", "python async is great")
        assert bonus > 0.0

    def test_bonus_for_trigram(self):
        bonus = _phrase_bonus("python async await", "python async await is great")
        assert bonus > 0.0

    def test_no_bonus_for_missing_phrase(self):
        assert _phrase_bonus("python async", "java threads are great") == 0.0

    def test_capped(self):
        # Many phrases should hit the cap.
        q = "python async await coroutines tasks gather"
        text = "python async await coroutines tasks gather python async await coroutines"
        bonus = _phrase_bonus(q, text)
        assert bonus <= 0.3


class TestTitleBoost:
    def test_zero_for_empty_title(self):
        assert _title_boost(["python"], "") == 0.0

    def test_nonzero_for_match(self):
        boost = _title_boost(["python", "async"], "Python Async Guide")
        assert boost > 0.0

    def test_zero_for_no_match(self):
        assert _title_boost(["python"], "Java Guide") == 0.0


class TestDiversityPenalty:
    def test_no_penalty_for_diverse(self):
        results = [
            _mk("https://a.com/1", score=0.9),
            _mk("https://b.com/1", score=0.8),
            _mk("https://c.com/1", score=0.7),
        ]
        original_scores = [r.score for r in results]
        _apply_diversity_penalty(results, top_n=3)
        # Scores unchanged when domains are diverse.
        for r, orig in zip(results, original_scores):
            assert r.score == orig

    def test_penalty_for_dominant_domain(self):
        results = [
            _mk("https://same.com/1", score=0.9),
            _mk("https://same.com/2", score=0.85),
            _mk("https://same.com/3", score=0.8),
            _mk("https://other.com/1", score=0.7),
        ]
        _apply_diversity_penalty(results, top_n=4, domain_threshold=0.5, penalty=0.15)
        # The first same.com entry keeps its score; later ones are penalized.
        same_scores = [r.score for r in results if "same.com" in r.url]
        # At least one should be lower than 0.9 (the original top).
        assert min(same_scores) < 0.9

    def test_single_result_no_op(self):
        results = [_mk("https://a.com/1", score=0.5)]
        _apply_diversity_penalty(results)
        assert results[0].score == 0.5


class TestRerankSearchResults:
    def test_preserves_results_count(self):
        results = [_mk(f"https://x{i}.com", title=f"result {i}") for i in range(5)]
        out = rerank_search_results("test query", results)
        assert len(out) == 5

    def test_relevant_result_ranks_higher(self):
        results = [
            _mk("https://a.com", title="unrelated stuff", desc="blah blah"),
            _mk(
                "https://b.com",
                title="Python async tutorial",
                desc="Learn python async programming",
            ),
        ]
        out = rerank_search_results("python async", results)
        assert "b.com" in out[0].url

    def test_backward_compat_no_diversity(self):
        """Calling with apply_diversity=False should still work."""
        results = [
            _mk("https://a.com", title="test"),
            _mk("https://b.com", title="test"),
        ]
        out = rerank_search_results("test", results, apply_diversity=False)
        assert len(out) == 2

    def test_empty_results(self):
        assert rerank_search_results("test", []) == []

    def test_scores_are_sorted_descending(self):
        results = [
            _mk(f"https://x{i}.com", title=f"result {i}", desc=f"content {i}") for i in range(5)
        ]
        out = rerank_search_results("result", results)
        scores = [r.score for r in out]
        assert scores == sorted(scores, reverse=True)


class TestRerankScrapedContent:
    def test_chunks_and_ranks(self):
        scraped = [
            ScrapeResult(
                url="https://a.com",
                title="A",
                markdown="# Python\n\nPython is great for async programming.",
            ),
            ScrapeResult(
                url="https://b.com",
                title="B",
                markdown="# Java\n\nJava is a different language entirely.",
            ),
        ]
        out = rerank_scraped_content("python async", scraped, max_chunks_per_source=2)
        assert len(out) > 0
        assert out[0]["score"] >= out[-1]["score"]

    def test_skips_errors(self):
        scraped = [
            ScrapeResult(url="https://a.com", title="A", markdown="good content", error=None),
            ScrapeResult(url="https://b.com", title="B", markdown="", error="timeout"),
        ]
        out = rerank_scraped_content("content", scraped)
        assert all(c["source_url"] == "https://a.com" for c in out)

    def test_empty_input(self):
        assert rerank_scraped_content("test", []) == []


class TestTokenization:
    def test_drops_stopwords(self):
        tokens = _tokenize("the python is a great language")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "a" not in tokens
        assert "python" in tokens

    def test_set_version(self):
        s = _tokenize_set("python async")
        assert isinstance(s, set)
        assert "python" in s

    def test_keyword_overlap_preserved(self):
        """Legacy _keyword_overlap_score still works."""
        score = _keyword_overlap_score({"python", "async"}, "python async programming")
        assert score > 0.0
