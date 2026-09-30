"""Google Maps discovery adapter (P15).

The scraper (github.com/gosom/google-maps-scraper) is an *external worker*:
it runs in its own container/process and emits NDJSON lines shaped like
its ``Entry`` struct. This adapter only reads that file/stream — Search-
Hub never shells out to the scraper, so a crash/CAPTCHA/layout change or
proxy rotation in the worker never touches core.

Longitude caveat: upstream's JSON tag is misspelled ``"longtitude"`` but
``Entry.MarshalJSON`` also emits ``"longitude"`` — accept either.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingestion.base import IngestionContext, RawPlaceRecord, SourceProbe

logger = logging.getLogger(__name__)


class GoogleMapsAdapter:
    """Streams RawPlaceRecord from scraper NDJSON.

    Context parameters:
      ``ndjson`` — path to a line-delimited JSON file (required unless
      ``lines`` is injected for tests).
      ``lines`` — in-memory iterable of JSON strings (tests/API upload).
    Checkpoint: ``{"offset": N}`` byte offset for resume.
    """

    name = "google_maps"
    adapter_version = "gosom-scraper-v1"  # P15.1: adapter, not dataset, version

    def __init__(self, lines: Iterable[str] | None = None):
        self._lines = lines
        self.source_dataset: dict[str, Any] = {}

    async def probe(self) -> SourceProbe:
        if self._lines is not None:
            return SourceProbe(ok=True, provider=self.name, detail="lines injected")
        return SourceProbe(ok=True, provider=self.name, detail="file-based; probe at ingest")

    async def ingest(self, context: IngestionContext) -> AsyncIterator[RawPlaceRecord]:
        fetched = datetime.now(UTC)
        offset = int(context.checkpoint.get("offset", 0) or 0)
        sha = hashlib.sha256()
        size = 0

        if self._lines is not None:
            stream: Iterable[str] = self._lines
        else:
            path = Path(str(context.param("ndjson", "")))
            f = path.open("r", encoding="utf-8", errors="replace")
            if offset:
                f.seek(offset)
            stream = f
            self.source_dataset = {
                "file": path.name,
                # resuming mid-file fingerprints the tail this run read
                "hash_scope": "from_checkpoint" if offset else "full",
            }

        pos = offset
        for line in stream:
            raw = line.encode("utf-8", errors="replace")
            sha.update(raw)
            size += len(raw)
            self.source_dataset.update({"sha256": sha.hexdigest(), "size_bytes": size})
            pos += len(raw)
            context.checkpoint["offset"] = pos
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # Runner counts parse failures; surface as an unusable record.
                yield RawPlaceRecord(
                    provider=self.name,
                    raw_payload={"__parse_error__": stripped[:2000]},
                    observed_at=None,
                    fetched_at=fetched,
                )
                continue
            rec = entry_to_record(obj, fetched_at=fetched)
            if rec is None:
                yield RawPlaceRecord(
                    provider=self.name,
                    raw_payload=obj if isinstance(obj, dict) else {"__raw__": stripped[:2000]},
                    observed_at=None,
                    fetched_at=fetched,
                )
                continue
            yield rec


_STATUS_KEYS = (
    "business_status",
    "status",
    "operational_status",
    "open_status",
)


def _extract_status(obj: dict[str, Any]) -> str | None:
    """Provider-reported operational status, verbatim.

    gosom/Maps scrapes variously emit ``business_status`` ('OPERATIONAL',
    'CLOSED_TEMPORARILY', 'CLOSED_PERMANENTLY'), ``status``, or boolean
    ``permanently_closed``/``temporarily_closed`` flags. Return the first
    non-empty value so the resolution layer can canonicalize it.
    """
    if obj.get("permanently_closed") is True:
        return "CLOSED_PERMANENTLY"
    if obj.get("temporarily_closed") is True:
        return "CLOSED_TEMPORARILY"
    for k in _STATUS_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def entry_to_record(obj: dict[str, Any], *, fetched_at: datetime) -> RawPlaceRecord | None:
    """Map one scraper ``Entry`` object → RawPlaceRecord.

    Identity precedence: place_id > cid > data_id > link — recorded as
    ``external_id`` + ``external_id_type`` for (provider, id) dedupe.
    """
    if not isinstance(obj, dict):
        return None

    title = _s(obj.get("title"))
    ext_id = _s(obj.get("place_id")) or _s(obj.get("cid")) or _s(obj.get("data_id"))
    ext_type = (
        "google_place_id"
        if _s(obj.get("place_id"))
        else ("google_cid" if _s(obj.get("cid")) else "google_data_id")
    )
    if not ext_id:
        ext_id, ext_type = _s(obj.get("link")), "google_link"
    if not title and not ext_id:
        return None  # nothing downstream can key on this

    lat = _f(obj.get("latitude"))
    lon = _f(obj.get("longitude", obj.get("longtitude")))

    address = _s(obj.get("address"))
    ca = obj.get("complete_address")
    if not address and isinstance(ca, dict):
        address = (
            ", ".join(
                _s(ca.get(k))
                for k in ("street", "borough", "city", "state", "country")
                if _s(ca.get(k))
            )
            or None
        )

    cats = obj.get("categories") if isinstance(obj.get("categories"), list) else []
    category = _s(obj.get("category")) or (_s(cats[0]) if cats else None)

    observed = fetched_at  # scraper rows carry no observation timestamp
    return RawPlaceRecord(
        provider=GoogleMapsAdapter.name,
        external_id=ext_id,
        external_id_type=ext_type,
        source_url=_s(obj.get("link")) or None,
        raw_name=title or None,
        raw_address=address,
        raw_phone=_s(obj.get("phone")) or None,
        raw_website=_s(obj.get("web_site") or obj.get("website")) or None,
        raw_category=category,
        raw_hours=obj.get("open_hours") if isinstance(obj.get("open_hours"), dict) else None,
        raw_status=_extract_status(obj),
        lat=lat,
        lon=lon,
        raw_payload=obj,
        observed_at=observed,
        fetched_at=fetched_at,
    )


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
