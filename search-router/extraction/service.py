"""ExtractionService — MIME dispatch + quality gate + provenance.

The pipeline-facing surface of the extraction engine. It owns three
decisions the extractors themselves must not make:

1. Which extractor handles a MIME type (HTML → Trafilatura, markdown/
   plain → passthrough, everything else is skipped — a PDF/JSON/image
   parser slots in here later, not inside an extractor).
2. The quality gate that decides whether extracted text is index-worthy
   (``extraction_status``).
3. The provenance record — who produced each field, from which snapshot,
   with what confidence — so later extractors (JSON-LD, adapters) can
   dispute fields instead of being treated as truth.

Called by the crawl pipeline *after* the raw snapshot is durable; a
future queue-driven ExtractionWorker can call the same service against
``document_snapshots`` without touching extractor logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import settings

from extraction.extractors import ContentExtractor, RawTextExtractor, TrafilaturaExtractor
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

logger = logging.getLogger(__name__)

# Mimes routed to the HTML extractor. Everything else either passes
# through (already main text) or is skipped (no extractor yet).
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_MIMES = frozenset({"text/plain", "text/markdown"})

# Quality-score heuristic (0..1): word count saturates at ~400 words,
# text density (text/source ratio) saturates at ~30%. Transparent and
# tunable — real ranking signal lives downstream, this only gates junk.
_DENSITY_SATURATION = 0.30
_WORD_SATURATION = 400

# Fields carried into the per-field provenance record.
_PROVENANCE_FIELDS = ("title", "author", "published_at", "description", "site_name", "language")


def quality_score(text: str, source_len: int) -> float:
    """Blend text volume and text/source density into a 0..1 score."""
    if not text:
        return 0.0
    words = len(text.split())
    density = len(text) / max(source_len, 1)
    score = 0.6 * min(words / _WORD_SATURATION, 1.0) + 0.4 * min(density / _DENSITY_SATURATION, 1.0)
    return round(score, 3)


class ExtractionService:
    """Dispatch + gate + provenance around pluggable content extractors."""

    def __init__(
        self,
        *,
        html_extractor: ContentExtractor | None = None,
        text_extractor: ContentExtractor | None = None,
        min_chars: int | None = None,
        min_quality: float | None = None,
    ) -> None:
        self._html_extractor = html_extractor or TrafilaturaExtractor()
        self._text_extractor = text_extractor or RawTextExtractor()
        self._min_chars = min_chars if min_chars is not None else settings.extraction_min_chars
        self._min_quality = (
            min_quality if min_quality is not None else settings.extraction_min_quality
        )

    def extractor_for(self, mime: str) -> ContentExtractor | None:
        if mime in _HTML_MIMES:
            return self._html_extractor
        if mime in _TEXT_MIMES:
            return self._text_extractor
        return None

    async def extract(
        self,
        *,
        url: str,
        mime: str,
        content: str,
        snapshot_id: int | None = None,
    ) -> ExtractionResult:
        """Extract one decoded body; never raises.

        ``content`` is the already-decoded text of the fetched body;
        ``snapshot_id`` links the result to its raw capture for
        provenance.
        """
        base_mime = (mime or "").split(";", 1)[0].strip().lower()
        extractor = self.extractor_for(base_mime)
        if extractor is None:
            return ExtractionResult(
                status=STATUS_SKIPPED_MIME,
                provenance={"mime": base_mime, "snapshot_id": snapshot_id},
            )
        try:
            doc = await asyncio.to_thread(extractor.extract, content, url=url)
        except Exception as exc:  # noqa: BLE001 — extraction must not sink the crawl
            logger.debug("extraction failed for %s: %r", url, exc)
            return ExtractionResult(
                status=STATUS_ERROR,
                error=f"{type(exc).__name__}: {exc}",
                provenance=self._provenance(extractor, None, snapshot_id, 0.0),
            )

        if doc is None or not doc.text.strip():
            return ExtractionResult(
                status=STATUS_EMPTY,
                document=doc,
                provenance=self._provenance(extractor, doc, snapshot_id, 0.0),
            )

        score = quality_score(doc.text, len(content))
        doc.quality_score = score
        if len(doc.text) < self._min_chars:
            status = STATUS_LOW_CONTENT
        elif score < self._min_quality:
            status = STATUS_LOW_QUALITY
        else:
            status = STATUS_SUCCESS
        return ExtractionResult(
            status=status,
            document=doc,
            provenance=self._provenance(extractor, doc, snapshot_id, score),
        )

    @staticmethod
    def _provenance(
        extractor: ContentExtractor,
        doc: ExtractedDocument | None,
        snapshot_id: int | None,
        score: float,
    ) -> dict[str, Any]:
        prov: dict[str, Any] = {
            "extractor": {
                "method": extractor.method,
                "version": extractor.version,
                "snapshot_id": snapshot_id,
                "quality_score": score,
            },
            "fields": {},
        }
        if doc is not None:
            for name in _PROVENANCE_FIELDS:
                value = getattr(doc, name, None)
                if value is None:
                    continue
                prov["fields"][name] = {
                    "value": value.isoformat() if hasattr(value, "isoformat") else value,
                    "source": extractor.method,
                }
        return prov
