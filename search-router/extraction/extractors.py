"""Content extractors: one fetched body → ``ExtractedDocument``.

Two implementations:

- ``TrafilaturaExtractor`` — main-content + metadata extraction for real
  HTML (text/html, application/xhtml+xml). Trafilatura is imported lazily
  so the module loads (and ``available`` reports False) on hosts/images
  where the dependency is absent.
- ``RawTextExtractor`` — passthrough for bodies that are already main
  text: Firecrawl-rendered markdown and text/plain. No structural
  extraction is attempted — the body IS the content.

Extractors are synchronous and CPU-bound; ``ExtractionService`` runs
them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from extraction.models import ExtractedDocument

logger = logging.getLogger(__name__)


class ContentExtractor(Protocol):
    """Synchronous body→document extractor."""

    method: str
    version: str

    def extract(self, content: str, *, url: str) -> ExtractedDocument | None: ...


def _clean(value: Any) -> str | None:
    """Normalize an extractor field: blank/None-literal → None."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text != "None" else None


def _parse_date(value: Any) -> datetime | None:
    """Trafilatura dates arrive as 'YYYY-MM-DD' or ISO strings."""
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class TrafilaturaExtractor:
    """HTML main-content + metadata extraction via trafilatura.

    Scope is deliberately narrow: HTML → clean text + metadata. It does
    not fetch, does not store, does not know the document schema.
    """

    method = "trafilatura"

    def __init__(self) -> None:
        try:
            import trafilatura
        except ImportError:
            self._traf = None
            self.version = "trafilatura-missing"
            logger.warning("trafilatura not installed — HTML extraction disabled")
        else:
            self._traf = trafilatura
            self.version = f"trafilatura-{trafilatura.__version__}"

    @property
    def available(self) -> bool:
        return self._traf is not None

    def extract(self, content: str, *, url: str) -> ExtractedDocument | None:
        if self._traf is None or not content:
            return None
        raw = self._traf.extract(
            content,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_formatting=False,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        text = _clean(data.get("text"))
        if not text:
            return None
        return ExtractedDocument(
            title=_clean(data.get("title")),
            text=text,
            author=_clean(data.get("author")),
            published_at=_parse_date(data.get("date")),
            description=_clean(data.get("excerpt")),
            language=_clean(data.get("language")),
            site_name=_clean(data.get("source-hostname")),
            extraction_method=self.method,
            extraction_version=self.version,
            word_count=len(text.split()),
            metadata={
                k: v
                for k, v in data.items()
                if k not in ("text", "raw_text", "body", "commentsbody", "comments")
                and v not in (None, "", "None")
            },
        )


_MD_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


class RawTextExtractor:
    """Passthrough for bodies that are already main text.

    Used for ``text/markdown`` (the Firecrawl render lane) and
    ``text/plain`` — formats where Trafilatura has nothing to extract
    from but the content is still index-worthy.
    """

    method = "raw_passthrough"
    version = "passthrough-1.0"

    def extract(self, content: str, *, url: str) -> ExtractedDocument | None:
        text = (content or "").strip()
        if not text:
            return None
        title = None
        heading = _MD_HEADING_RE.search(text[:2000])
        if heading:
            title = heading.group(1).strip()
        return ExtractedDocument(
            title=title,
            text=text,
            canonical_url=url,
            extraction_method=self.method,
            extraction_version=self.version,
            word_count=len(text.split()),
        )
