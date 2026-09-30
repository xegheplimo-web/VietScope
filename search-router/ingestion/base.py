"""P15 ingestion contract — separate from SearchProvider on purpose.

``SearchProvider`` answers a user query in realtime.
``PlaceSourceAdapter`` builds the database in batches.

Every adapter streams :class:`RawPlaceRecord` — a provider-faithful,
unresolved observation. Normalization, cross-source entity resolution
and confidence scoring belong to P16, not here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceProbe:
    """Cheap reachability/health signal for a source (no data pulled)."""

    ok: bool
    provider: str
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPlaceRecord:
    """One provider observation of a place, pre-resolution."""

    provider: str
    external_id: str | None = None  # provider-native id when it has one
    external_id_type: str | None = None  # google_place_id|google_cid|osm_node|doc_id|...

    source_url: str | None = None
    raw_name: str | None = None
    raw_address: str | None = None
    raw_phone: str | None = None
    raw_website: str | None = None
    raw_category: str | None = None
    raw_hours: dict[str, Any] | None = None
    raw_status: str | None = None  # provider-reported operational status verbatim

    lat: float | None = None
    lon: float | None = None

    raw_payload: dict[str, Any] = field(default_factory=dict)

    observed_at: datetime | None = None  # when the provider truth held
    fetched_at: datetime | None = None  # when we pulled the bytes

    def _hash(self, canon: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(canon, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def identity_hash(self) -> str:
        """Stable dedup key — NOT sensitive to mutable-field drift.

        Records carrying a provider id hash that id; id-less records hash
        name+address+coordinates so a changed phone/hours never spawns a
        duplicate identity. Used only for (provider, identity_hash) dedup
        of ``external_id IS NULL`` rows — never for change detection.
        """
        if self.external_id:
            canon = {"p": self.provider, "e": self.external_id}
        else:
            canon = {
                "p": self.provider,
                "n": (self.raw_name or "").strip().lower(),
                "a": (self.raw_address or "").strip().lower(),
                "lat": round(self.lat, 6) if self.lat is not None else None,
                "lon": round(self.lon, 6) if self.lon is not None else None,
            }
        return self._hash(canon)

    def observation_hash(self) -> str:
        """Change-detection hash over every tracked mutable field.

        Covers opening hours and other mutable fields — a shop switching
        08:00–20:00 → 08:00–22:00 produces a different hash and counts as
        ``changed``, never silent ``unchanged``.
        """
        canon = {
            "p": self.provider,
            "e": self.external_id,
            "n": (self.raw_name or "").strip().lower(),
            "a": (self.raw_address or "").strip().lower(),
            "ph": (self.raw_phone or "").strip(),
            "w": (self.raw_website or "").strip().lower(),
            "c": (self.raw_category or "").strip().lower(),
            "s": (self.raw_status or "").strip().lower(),
            "h": self.raw_hours,
            "lat": round(self.lat, 6) if self.lat is not None else None,
            "lon": round(self.lon, 6) if self.lon is not None else None,
        }
        return self._hash(canon)

    def content_hash(self) -> str:
        """Deprecated alias for :meth:`observation_hash` (pre-P15.1 compat)."""
        return self.observation_hash()


@dataclass
class IngestionContext:
    """Run-scoped context handed to adapters.

    ``parameters`` carries operator intent (province, category, grid cell,
    pbf path); ``checkpoint`` is where a resumed run hands the adapter its
    last durable progress (e.g. pbf blob offset, NDJSON byte offset).
    """

    run_id: int
    provider: str
    parameters: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    cursor: str | None = None

    def param(self, key: str, default: Any = None) -> Any:
        return self.parameters.get(key, default)


class PlaceSourceAdapter(Protocol):
    """Batch acquisition contract — yields raw records, never writes."""

    name: str

    def ingest(
        self,
        context: IngestionContext,
    ) -> AsyncIterator[RawPlaceRecord]:
        """Stream raw records; resumable via ``context.checkpoint``."""
        ...

    async def probe(self) -> SourceProbe:
        """Report whether the source is reachable/usable right now."""
        ...
