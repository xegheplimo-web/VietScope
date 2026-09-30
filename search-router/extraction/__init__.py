"""Extraction engine — fetched body → ExtractedDocument → index.

Public surface: ``ExtractionService`` (dispatch + gate + provenance),
``ExtractedDocument``/``ExtractionResult`` contracts, and the status
constants persisted on ``documents``.
"""

from extraction.models import (
    REEXTRACT_STATUSES,
    RETRYABLE_STATUSES,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_LOW_CONTENT,
    STATUS_LOW_QUALITY,
    STATUS_SKIPPED_MIME,
    STATUS_SUCCESS,
    ExtractedDocument,
    ExtractionResult,
    status_rank,
)
from extraction.service import ExtractionService

__all__ = [
    "REEXTRACT_STATUSES",
    "RETRYABLE_STATUSES",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_LOW_CONTENT",
    "STATUS_LOW_QUALITY",
    "STATUS_SKIPPED_MIME",
    "STATUS_SUCCESS",
    "ExtractedDocument",
    "ExtractionResult",
    "ExtractionService",
    "status_rank",
]
