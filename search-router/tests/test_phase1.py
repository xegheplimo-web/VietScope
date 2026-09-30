"""Tests for Phase 1 modules: url_normalize, url_pre_rank, contextual_chunking.

(``core.query_understanding_v2.QueryUnderstandingL2`` and
``pipeline.citation_validator.CitationValidator`` were dead code —
tests-only — deleted in phase-0 dedup T1b.  Canonical:
``core.query_understanding`` and ``evidence.verifier``.)
"""

from pipeline.contextual_chunking import Chunk, ContextualChunker
from pipeline.url_normalize import NormalizedURL, URLNormalizer
from pipeline.url_pre_rank import URLPreRanker

# ─── URLNormalizer ──────────────────────────────────────────────────────────


class TestURLNormalizer:
    def setup_method(self):
        self.normalizer = URLNormalizer()

    def test_basic_normalization(self):
        result = self.normalizer.normalize("https://www.example.com/path/")
        assert result is not None
        assert result.canonical == "https://example.com/path"
        assert result.domain == "example.com"

    def test_tracking_params_stripped(self):
        result = self.normalizer.normalize("https://example.com/path?utm_source=google&id=123")
        assert result is not None
        assert "utm_source" not in result.canonical
        assert "id=123" in result.canonical

    def test_www_removed(self):
        result = self.normalizer.normalize("https://www.example.com/path")
        assert result is not None
        assert result.domain == "example.com"

    def test_private_ip_blocked(self):
        result = self.normalizer.normalize("http://192.168.1.1/admin")
        assert result is not None
        assert result.is_private is True

    def test_localhost_blocked(self):
        result = self.normalizer.normalize("http://localhost:8080/test")
        assert result is not None
        assert result.is_private is True

    def test_invalid_url(self):
        result = self.normalizer.normalize("not a url")
        assert result is None

    def test_dedupe(self):
        urls = [
            self.normalizer.normalize("https://example.com/path"),
            self.normalizer.normalize("https://example.com/path"),
            self.normalizer.normalize("https://example.com/other"),
        ]
        urls = [u for u in urls if u]
        deduped = self.normalizer.dedupe(urls)
        assert len(deduped) == 2

    def test_filter_blocked(self):
        urls = [
            self.normalizer.normalize("https://example.com/path"),
            self.normalizer.normalize("http://192.168.1.1/admin"),
            self.normalizer.normalize("https://example.com/other"),
        ]
        urls = [u for u in urls if u]
        filtered = self.normalizer.filter_blocked(urls)
        assert len(filtered) == 2


# ─── URLPreRanker ───────────────────────────────────────────────────────────


class TestURLPreRanker:
    def setup_method(self):
        self.ranker = URLPreRanker(intent="pricing_lookup", entities=["Qwen Image 2.1"])

    def test_rank_urls(self):
        urls = [
            NormalizedURL(
                original="https://help.aliyun.com/pricing",
                canonical="https://help.aliyun.com/pricing",
                domain="help.aliyun.com",
                path="/pricing",
                query="",
            ),
            NormalizedURL(
                original="https://blog.example.com/qwen-pricing",
                canonical="https://blog.example.com/qwen-pricing",
                domain="blog.example.com",
                path="/qwen-pricing",
                query="",
            ),
        ]
        ranked = self.ranker.rank(urls, top_n=10)
        assert len(ranked) == 2
        assert ranked[0].url.domain == "help.aliyun.com"

    def test_diversity_quota(self):
        urls = [
            NormalizedURL(
                original=f"https://example.com/page{i}",
                canonical=f"https://example.com/page{i}",
                domain="example.com",
                path=f"/page{i}",
                query="",
            )
            for i in range(5)
        ]
        ranked = self.ranker.rank(urls, top_n=10)
        # Should limit to 2 per domain for non-research intent
        assert len(ranked) <= 2


# ─── ContextualChunker ──────────────────────────────────────────────────────


class TestContextualChunker:
    def setup_method(self):
        self.chunker = ContextualChunker(chunk_size=100, chunk_overlap=20)

    def test_basic_chunking(self):
        text = "This is a test. " * 50
        chunks = self.chunker.chunk(text)
        assert len(chunks) > 0
        assert all(c.text for c in chunks)

    def test_header_path(self):
        text = "# Title\n\n## Section 1\n\nContent here.\n\n## Section 2\n\nMore content."
        chunks = self.chunker.chunk(text)
        assert len(chunks) > 0
        # First chunk should have header path
        assert chunks[0].heading_path == ["Title", "Section 1"]

    def test_table_detection(self):
        text = "| Col1 | Col2 |\n|------|------|\n| A    | B    |"
        chunks = self.chunker.chunk(text)
        assert len(chunks) > 0
        assert chunks[0].dom_role == "table"

    def test_code_detection(self):
        text = "```python\ndef hello():\n    print('hello')\n```"
        chunks = self.chunker.chunk(text)
        assert len(chunks) > 0
        assert chunks[0].dom_role == "code"

    def test_header_prefix(self):
        chunk = Chunk(
            chunk_id="test",
            text="Some content",
            heading_path=["Title", "Section"],
        )
        prefixed = self.chunker.add_header_prefix(chunk)
        assert "[Title > Section]" in prefixed
