"""Ingest-time validation gate + normalizations (P15).

Cheap, per-record checks at the staging door — NOT fuzzy matching (that's
P16). A failed record is rejected to the DLQ with a reason, raw payload
intact, and never blocks the batch.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ingestion.base import RawPlaceRecord

# Plausible Vietnam extent — generous, matches the P14B geometry box.
_VN_LAT_MIN, _VN_LAT_MAX = 6.0, 24.0
_VN_LON_MIN, _VN_LON_MAX = 100.0, 112.0  # mainland only; islands allowed by caller
_VN_LON_MAX_ISLANDS = 119.5  # Trường Sa / Hoàng Sa outposts

_PHONE_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str | None:
    """VN phone → canonical +84 form, digits preserved.

    0901234567 / +84901234567 / 84 901234567 → +84901234567.
    Returns None when the digits are implausible as a VN number.
    """
    digits = _PHONE_DIGITS.sub("", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("84"):
        digits = "+" + digits
    elif digits.startswith("0"):
        digits = "+84" + digits[1:]
    elif digits and not digits.startswith("+"):
        digits = "+" + digits if len(digits) > 10 else "0" + digits
    body = digits.lstrip("+")
    if not (8 <= len(body) <= 13):
        return None
    return digits if digits.startswith("+") else "+" + digits


def canonical_website(raw: str) -> str | None:
    """Lowercase scheme+host, strip fragment/trailing slash."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlparse(raw)
    except ValueError:
        return None
    if not p.netloc:
        return None
    path = p.path.rstrip("/")
    return f"{p.scheme.lower() or 'https'}://{p.netloc.lower()}{path}"


def validate(rec: RawPlaceRecord) -> list[str]:
    """Reject reasons; empty list = stageable. Raw is preserved regardless."""
    errs: list[str] = []
    if rec.raw_payload.get("__parse_error__") is not None:
        return ["parse_error"]
    if not (rec.raw_name or "").strip():
        errs.append("missing_name")
    # external_id is optional per-source (UNIQUE allows NULLs); identity is
    # only truly absent when neither name nor id exists.
    if not (rec.raw_name or "").strip() and not (rec.external_id or "").strip():
        errs.append("missing_identity")
    if rec.observed_at is None:
        errs.append("missing_observed_at")
    if rec.lat is not None or rec.lon is not None:
        if rec.lat is None or rec.lon is None:
            errs.append("partial_coordinates")
        elif not (-90.0 <= rec.lat <= 90.0):
            errs.append("invalid_latitude")
        elif not (-180.0 <= rec.lon <= 180.0):
            errs.append("invalid_longitude")
        elif not (
            _VN_LAT_MIN <= rec.lat <= _VN_LAT_MAX and _VN_LON_MIN <= rec.lon <= _VN_LON_MAX_ISLANDS
        ):
            errs.append("coords_outside_vietnam")
    if rec.raw_phone and normalize_phone(rec.raw_phone) is None:
        errs.append("implausible_phone")
    if rec.raw_website and canonical_website(rec.raw_website) is None:
        errs.append("malformed_website")
    return errs
