"""Phase 3 tests — extraction engine + pipeline wiring.

Unit tests cover the extractor contract and the quality gate; pipeline
tests use the shared fakes from test_crawler_pipeline plus a scripted
FakeExtraction/FakeIndexer so no trafilatura/OpenSearch/Qdrant is needed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json

import crawler.pipeline as pl
import pytest
from canonical.url import canonical_url
from crawler.fetcher import FetchResult
from crawler.pipeline import CrawlPipeline
from extraction.extractors import RawTextExtractor, TrafilaturaExtractor
from extraction.models import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_LOW_CONTENT,
    STATUS_LOW_QUALITY,
    STATUS_SKIPPED_MIME,
    STATUS_SUCCESS,
    ExtractedDocument,
    ExtractionResult,
)
from extraction.service import ExtractionService, quality_score
from test_crawler_pipeline import (
    FakeFetcher,
    FakeFrontier,
    FakeLimiter,
    FakeRobots,
    FakeStore,
    PipePool,
    _ok_result,
    _task,
)

_trafilatura_missing = pytest.mark.skipif(
    importlib.util.find_spec("trafilatura") is None,
    reason="trafilatura not installed",
)

# ─── Fixtures ────────────────────────────────────────────────────────────

RICH_HTML = """<html lang="vi"><head><title>Bai viet test</title>
<meta property="og:site_name" content="Bao Test">
<meta name="author" content="Nguyen Van A">
<meta name="description" content="Mo ta ngan cho bai viet test.">
<meta property="article:published_time" content="2026-09-20T10:00:00Z">
</head><body><article><h1>Tieu de chinh</h1>
<p>Day la noi dung chinh cua bai viet. Noi dung nay du dai de extraction
danh gia la trang co gia tri, voi nhieu cau va tu vung da dang de dat
nguong chat luong trong quality gate cua Search-Hub.</p>
<p>Doan van thu hai cung co noi dung, giup tang word count va text
density cho bai test.</p></article></body></html>"""

BOILERPLATE_HTML = (
    "<html><head><title>nav</title></head><body><nav><a href='/'>home</a>"
    "<a href='/a'>a</a></nav><script>var x=1;</script></body></html>"
)

MARKDOWN_BODY = (
    "# Tieu de markdown\n\nNoi dung markdown tu Firecrawl renderer, "
    "du dai de vuot nguong extraction quality gate. " * 4
)


def _run(coro):
    return asyncio.run(coro)


# ─── Extractor contract ─────────────────────────────────────────────────


@_trafilatura_missing
class TestTrafilaturaExtractor:
    def test_extracts_main_text_and_metadata(self):
        doc = TrafilaturaExtractor().extract(RICH_HTML, url="https://x.vn/bai-1")
        assert doc is not None
        assert doc.text
        assert doc.title == "Tieu de chinh"
        assert doc.author == "Nguyen Van A"
        assert doc.site_name == "Bao Test"
        assert doc.description == "Mo ta ngan cho bai viet test."
        assert doc.published_at is not None and doc.published_at.year == 2026
        assert doc.extraction_method == "trafilatura"
        assert doc.extraction_version.startswith("trafilatura-")
        assert doc.word_count == len(doc.text.split())

    def test_boilerplate_only_gates_out(self):
        # Trafilatura may return a few nav-link bytes — that's fine, the
        # service-level quality gate is what rejects them (low_content),
        # not the extractor contract.
        svc = ExtractionService(html_extractor=TrafilaturaExtractor())
        result = _run(
            svc.extract(url="https://x.vn/nav", mime="text/html", content=BOILERPLATE_HTML)
        )
        assert result.status in (STATUS_EMPTY, STATUS_LOW_CONTENT, STATUS_LOW_QUALITY)

    def test_empty_input_returns_none(self):
        assert TrafilaturaExtractor().extract("", url="https://x.vn") is None


class TestRawTextExtractor:
    def test_markdown_passthrough(self):
        doc = RawTextExtractor().extract(MARKDOWN_BODY, url="https://x.vn/m")
        assert doc is not None
        assert doc.text == MARKDOWN_BODY.strip()
        assert doc.title == "Tieu de markdown"
        assert doc.extraction_method == "raw_passthrough"
        assert doc.canonical_url == "https://x.vn/m"

    def test_empty_returns_none(self):
        assert RawTextExtractor().extract("   ", url="https://x.vn") is None


# ─── Quality gate + dispatch (fake extractors for determinism) ───────────


class _StubExtractor:
    """Scripted extractor: returns a fixed document."""

    def __init__(self, doc=None, method="stub"):
        self._doc = doc
        self.method = method
        self.version = f"{method}-0.0"
        self.calls: list[str] = []

    def extract(self, content: str, *, url: str) -> ExtractedDocument | None:
        self.calls.append(url)
        return self._doc


def _doc(text: str) -> ExtractedDocument:
    return ExtractedDocument(
        title="T",
        text=text,
        extraction_method="stub",
        extraction_version="stub-0.0",
        word_count=len(text.split()),
    )


class TestExtractionService:
    def test_mime_dispatch(self):
        html_ext, text_ext = _StubExtractor(), _StubExtractor()
        svc = ExtractionService(html_extractor=html_ext, text_extractor=text_ext)
        assert svc.extractor_for("text/html") is html_ext
        assert svc.extractor_for("application/xhtml+xml") is html_ext
        assert svc.extractor_for("text/markdown") is text_ext
        assert svc.extractor_for("text/plain") is text_ext
        assert svc.extractor_for("application/pdf") is None
        assert svc.extractor_for("image/png") is None

    def test_skipped_mime(self):
        svc = ExtractionService(html_extractor=_StubExtractor(_doc("x" * 500)))
        result = _run(svc.extract(url="https://x.vn/f.pdf", mime="application/pdf", content="x"))
        assert result.status == STATUS_SKIPPED_MIME
        assert result.document is None
        assert result.provenance["mime"] == "application/pdf"

    def test_empty_when_extractor_returns_none(self):
        svc = ExtractionService(html_extractor=_StubExtractor(None))
        result = _run(svc.extract(url="https://x.vn", mime="text/html", content="<html/>"))
        assert result.status == STATUS_EMPTY
        assert result.document is None

    def test_low_content_below_min_chars(self):
        svc = ExtractionService(html_extractor=_StubExtractor(_doc("short text")), min_chars=100)
        result = _run(svc.extract(url="https://x.vn", mime="text/html", content="<html/>"))
        assert result.status == STATUS_LOW_CONTENT
        assert result.document is not None  # gated text is still persisted

    def test_low_quality_below_min_quality(self):
        # ≥min_chars but few words inside a huge source → low density score.
        text = "mot hai ba bon nam sau bay tam " * 5  # ~35 words, ~190 chars
        svc = ExtractionService(
            html_extractor=_StubExtractor(_doc(text)),
            min_chars=100,
            min_quality=0.30,
        )
        result = _run(
            svc.extract(
                url="https://x.vn",
                mime="text/html",
                content="<html>" + "x" * 50_000 + "</html>",
            )
        )
        assert result.status == STATUS_LOW_QUALITY

    def test_success_and_provenance(self):
        text = "word " * 200
        svc = ExtractionService(html_extractor=_StubExtractor(_doc(text)))
        result = _run(
            svc.extract(
                url="https://x.vn",
                mime="text/html",
                content="<html>" + text + "</html>",
                snapshot_id=42,
            )
        )
        assert result.status == STATUS_SUCCESS
        assert result.document.quality_score > 0
        prov = result.provenance
        assert prov["extractor"]["method"] == "stub"
        assert prov["extractor"]["snapshot_id"] == 42
        assert prov["fields"]["title"]["value"] == "T"
        assert prov["fields"]["title"]["source"] == "stub"

    def test_extractor_exception_is_error_not_raise(self):
        class _Boom:
            method = "boom"
            version = "boom-1"

            def extract(self, content, *, url):
                raise RuntimeError("parse blew up")

        svc = ExtractionService(html_extractor=_Boom())
        result = _run(svc.extract(url="https://x.vn", mime="text/html", content="x"))
        assert result.status == STATUS_ERROR
        assert "RuntimeError" in (result.error or "")

    def test_quality_score_monotonic(self):
        assert quality_score("", 1000) == 0.0
        weak = quality_score("a b c", 50_000)
        strong = quality_score("word " * 500, 10_000)
        assert strong > weak
        assert 0.0 <= weak <= 1.0 <= 1.0


# ─── Pipeline integration ────────────────────────────────────────────────


class FakeExtraction:
    """Scripted ExtractionService stand-in — records calls, replays results."""

    def __init__(self, results: list[ExtractionResult]):
        self._results = list(results)
        self.calls: list[dict] = []

    async def extract(self, **kw) -> ExtractionResult:
        self.calls.append(kw)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class FakeIndexer:
    def __init__(self, report: dict | None = None):
        self.calls = []
        self._report = report or {
            "indexed": True,
            "indexing_status": "success",
            "embedding_status": "success",
        }

    async def __call__(self, task):
        self.calls.append(task)
        return dict(self._report)


def _success_result(text: str = "extracted " * 60) -> ExtractionResult:
    return ExtractionResult(
        status=STATUS_SUCCESS,
        document=ExtractedDocument(
            title="Extracted title",
            text=text,
            author="Tac Gia",
            site_name="Site",
            extraction_method="trafilatura",
            extraction_version="trafilatura-test",
            word_count=len(text.split()),
            quality_score=0.9,
        ),
        provenance={"extractor": {"method": "trafilatura"}},
    )


def _pipeline_x(
    *,
    fetch_result: FetchResult | Exception,
    pool: PipePool | None,
    extraction: FakeExtraction | None,
    indexer: FakeIndexer | None,
    fetcher=None,
):
    frontier = FakeFrontier()
    store = FakeStore()
    robots = FakeRobots(allowed=True)
    limiter = FakeLimiter()
    fetcher = fetcher or FakeFetcher(fetch_result)
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=robots,
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"x.vn"},
        extraction=extraction,
        indexer=indexer,
    )
    return pipe, frontier, store, pool


_URL = "https://x.vn/bai-viet"


def test_changed_doc_extracts_then_indexes():
    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    extraction = FakeExtraction([_success_result()])
    indexer = FakeIndexer()
    pipe, frontier, store, _ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=indexer
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.outcome == "changed"
    assert outcome.extraction_status == STATUS_SUCCESS
    assert outcome.indexed is True
    # Extraction got the decoded body + mime + snapshot linkage.
    assert extraction.calls[0]["mime"] == "text/html"
    assert extraction.calls[0]["snapshot_id"] == 1
    # Documents row carries extraction columns + index status.
    doc = pool.documents[canonical_url(_URL)]
    assert doc["extraction_status"] == STATUS_SUCCESS
    assert doc["main_text"]
    assert doc["extraction_method"] == "trafilatura"
    assert doc["indexing_status"] == "success"
    assert doc["embedding_status"] == "success"
    assert doc["current_snapshot_id"] == 1
    # Indexer received canonical url + extracted text.
    task = indexer.calls[0]
    assert task.url == canonical_url(_URL)
    assert task.doc_id == outcome.doc_id
    assert task.text
    assert task.source_type == "crawler"


def test_unchanged_doc_skips_extraction_and_index():
    from canonical.content import content_fingerprint

    body = _ok_result(_URL).content.decode()
    canon = canonical_url(_URL)
    pool = PipePool(
        frontier_rows=[{"url": _URL, "depth": 0}],
        documents=[
            {
                "doc_id": "doc_1",
                "canonical_url": canon,
                "content_hash": content_fingerprint(body),
                "extraction_status": STATUS_SUCCESS,
            }
        ],
    )
    extraction = FakeExtraction([_success_result()])
    indexer = FakeIndexer()
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=indexer
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.outcome == "unchanged"
    assert outcome.extraction_status is None
    assert outcome.indexed is None
    assert extraction.calls == []
    assert indexer.calls == []


def test_unchanged_doc_without_extraction_extracts_once():
    from canonical.content import content_fingerprint

    body = _ok_result(_URL).content.decode()
    canon = canonical_url(_URL)
    pool = PipePool(
        frontier_rows=[{"url": _URL, "depth": 0}],
        documents=[
            {
                "doc_id": "doc_1",
                "canonical_url": canon,
                "content_hash": content_fingerprint(body),
                # extraction_status absent → predates Phase 3
            }
        ],
    )
    extraction = FakeExtraction([_success_result()])
    indexer = FakeIndexer()
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=indexer
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.outcome == "unchanged"
    assert len(extraction.calls) == 1
    assert len(indexer.calls) == 1


def test_unchanged_doc_error_status_retries_extraction():
    from canonical.content import content_fingerprint

    body = _ok_result(_URL).content.decode()
    canon = canonical_url(_URL)
    pool = PipePool(
        frontier_rows=[{"url": _URL, "depth": 0}],
        documents=[
            {
                "doc_id": "doc_1",
                "canonical_url": canon,
                "content_hash": content_fingerprint(body),
                "extraction_status": STATUS_ERROR,
            }
        ],
    )
    extraction = FakeExtraction([_success_result()])
    indexer = FakeIndexer()
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=indexer
    )
    _run(pipe.process_one(_task(_URL)))
    assert len(extraction.calls) == 1


def test_failed_extraction_persisted_but_not_indexed():
    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    extraction = FakeExtraction(
        [ExtractionResult(status=STATUS_EMPTY, document=None, provenance={})]
    )
    indexer = FakeIndexer()
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=indexer
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.extraction_status == STATUS_EMPTY
    assert outcome.indexed is None
    assert indexer.calls == []
    # The failure is still recorded on the document row.
    doc = pool.documents[canonical_url(_URL)]
    assert doc["extraction_status"] == STATUS_EMPTY


def test_indexer_none_marks_skipped():
    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    extraction = FakeExtraction([_success_result()])
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL), pool=pool, extraction=extraction, indexer=None
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.extraction_status == STATUS_SUCCESS
    assert outcome.indexed is False
    doc = pool.documents[canonical_url(_URL)]
    assert doc["indexing_status"] == "skipped"
    assert doc["embedding_status"] == "skipped"


def test_no_extraction_service_skips_stage():
    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    pipe, *_ = _pipeline_x(fetch_result=_ok_result(_URL), pool=pool, extraction=None, indexer=None)
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.outcome == "changed"
    assert outcome.extraction_status is None
    assert not any(c[0] == pl._DOC_EXTRACTION_SQL for c in pool.calls)


def test_render_retry_when_static_extraction_fails():
    """Empty HTML extraction → render → rendered body persisted as its own
    snapshot, and the winning extraction points at THAT snapshot — never
    at the static capture it did not read (P0 provenance contract)."""

    class RenderFetcher(FakeFetcher):
        async def render(self, url):
            return FetchResult(
                ok=True,
                status=200,
                content=MARKDOWN_BODY.encode(),
                mime="text/markdown",
                final_url=url,
                via="firecrawl",
            )

    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    extraction = FakeExtraction(
        [
            ExtractionResult(status=STATUS_EMPTY, document=None, provenance={}),
            _success_result(),
        ]
    )
    indexer = FakeIndexer()
    pipe, _, store, _ = _pipeline_x(
        fetch_result=_ok_result(_URL),
        pool=pool,
        extraction=extraction,
        indexer=indexer,
        fetcher=RenderFetcher(_ok_result(_URL)),
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    assert outcome.extraction_status == STATUS_SUCCESS
    assert outcome.indexed is True
    assert len(extraction.calls) == 2
    # Second attempt extracted the rendered markdown body.
    assert extraction.calls[1]["mime"] == "text/markdown"
    # Rendered body was persisted as a separate snapshot (original=1,
    # rendered=2) — and the retry's provenance references snapshot 2.
    assert len(pool.snapshots) == 2
    assert len(store.put_keys) == 2
    assert extraction.calls[1]["snapshot_id"] == 2
    doc = pool.documents[canonical_url(_URL)]
    assert doc["current_snapshot_id"] == 2
    # Provenance written to the documents row records the fallback chain.
    extraction_writes = [args for sql, args in pool.calls if sql == pl._DOC_EXTRACTION_SQL]
    metadata = extraction_writes[-1][12]
    fallback = json.loads(metadata)["provenance"]["render_fallback"]
    assert fallback["adopted"] is True
    assert fallback["fetch_method"] == "firecrawl_render"
    assert fallback["from_snapshot_id"] == 1
    assert fallback["rendered_snapshot_id"] == 2


def test_render_retry_keeps_first_result_when_not_better():
    class RenderFetcher(FakeFetcher):
        async def render(self, url):
            return FetchResult(
                ok=True, status=200, content=b"tiny", mime="text/markdown", final_url=url
            )

    pool = PipePool(frontier_rows=[{"url": _URL, "depth": 0}])
    extraction = FakeExtraction(
        [
            ExtractionResult(status=STATUS_LOW_CONTENT, document=_doc("x" * 50)),
            ExtractionResult(status=STATUS_EMPTY, document=None),
        ]
    )
    pipe, *_ = _pipeline_x(
        fetch_result=_ok_result(_URL),
        pool=pool,
        extraction=extraction,
        indexer=FakeIndexer(),
        fetcher=RenderFetcher(_ok_result(_URL)),
    )
    outcome = _run(pipe.process_one(_task(_URL)))
    # Rendered attempt was worse — original status stands.
    assert outcome.extraction_status == STATUS_LOW_CONTENT
    # The rendered capture is still durable history, but the document
    # keeps pointing at the snapshot its extraction actually used.
    assert len(pool.snapshots) == 2
    assert extraction.calls[1]["snapshot_id"] == 2
    doc = pool.documents[canonical_url(_URL)]
    assert doc["current_snapshot_id"] == 1
