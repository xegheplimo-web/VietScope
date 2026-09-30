"""Extraction contracts — the data an extractor produces, nothing more.

``ExtractedDocument`` is deliberately storage-agnostic: it knows nothing
about Postgres columns, OpenSearch indexes, or Qdrant points. Callers
(the crawl pipeline today, an ExtractionWorker tomorrow) map it onto
their own stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ── documents.extraction_status values ──────────────────────────────────
STATUS_SUCCESS = "success"
STATUS_LOW_CONTENT = "low_content"
STATUS_LOW_QUALITY = "low_quality"
STATUS_EMPTY = "empty"
STATUS_SKIPPED_MIME = "skipped_mime"
STATUS_ERROR = "error"

# Quality-gate outcomes worth one rendered (Firecrawl) retry — the static
# body gave us nothing indexable.
RETRYABLE_STATUSES = frozenset({STATUS_EMPTY, STATUS_LOW_CONTENT, STATUS_LOW_QUALITY})

# Stored extraction_statuses that justify re-extracting unchanged content:
# 'error' is transient (dependency hiccup); deterministic statuses
# (empty/low_*/skipped_mime) only change when the content itself changes.
REEXTRACT_STATUSES = frozenset({STATUS_ERROR})

# Coarse ordering for "did the retry produce something better".
_STATUS_RANK = {
    STATUS_ERROR: -2,
    STATUS_SKIPPED_MIME: -1,
    STATUS_EMPTY: 0,
    STATUS_LOW_CONTENT: 1,
    STATUS_LOW_QUALITY: 2,
    STATUS_SUCCESS: 3,
}


def status_rank(status: str) -> int:
    return _STATUS_RANK.get(status, -2)


@dataclass(slots=True)
class ExtractedDocument:
    """Canonical extracted view of one fetched body.

    Pure extraction output — an ExtractedDocument must be producible
    without any infrastructure beyond the raw content itself.
    """

    title: str | None = None
    text: str = ""
    author: str | None = None
    published_at: datetime | None = None
    description: str | None = None
    language: str | None = None
    canonical_url: str | None = None
    site_name: str | None = None

    extraction_method: str = ""
    extraction_version: str = ""
    word_count: int = 0
    quality_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    """Outcome of one extraction attempt: status + document + provenance.

    ``document`` is populated whenever the extractor produced anything —
    including gated-out text (low_content/low_quality) — so callers can
    persist what was seen, not just what passed.
    """

    status: str
    document: ExtractedDocument | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
