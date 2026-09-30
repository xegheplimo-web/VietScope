"""OSM PBF adapter via pyosmium (P15.1 production path).

``osmium`` (pyosmium) is the preferred backend for nationwide ingestion:
it handles node/way/relation decoding, dense-node expansion, and way
node-location resolution in C++ — the stdlib reader in ``ingestion/pbf.py``
remains the zero-dependency fallback.

Elements stream through ``osmium.FileProcessor`` (pyosmium ≥ 3.7).
Ways emit member-coordinate centroids; relations emit unlocated
(member geometry belongs to P16 shape assembly) — the record, tags
and member list are preserved either way.

Install: ``osmium`` is an optional dependency — the stdlib fallback
engages automatically when it isn't installed.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingestion.adapters.osm_pbf import _is_poi, record_from_tags
from ingestion.base import IngestionContext, RawPlaceRecord, SourceProbe


def _tags(obj: Any) -> dict[str, str]:
    return {t.k: t.v for t in obj.tags}


def _dataset_meta(path: Path) -> dict[str, Any]:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    return {"file": path.name, "sha256": sha.hexdigest(), "size_bytes": path.stat().st_size}


_MEMBER_KIND = {"n": "node", "w": "way", "r": "relation"}


def _stream_elements(
    path: Path,
) -> Iterator[tuple[str, int, dict[str, str], Any, Any, list[tuple[str, int, str]]]]:
    """Yield (kind, id, tags, lat, lon, member_refs) lazily."""
    import osmium  # type: ignore[import-not-found]  # optional production backend

    fp = osmium.FileProcessor(str(path)).with_locations()
    for obj in fp:
        if isinstance(obj, osmium.osm.Node):
            t = _tags(obj)
            if _is_poi(t):
                yield "node", obj.id, t, obj.location.lat, obj.location.lon, []
        elif isinstance(obj, osmium.osm.Way):
            t = _tags(obj)
            if not _is_poi(t):
                continue
            pts = [(nd.location.lat, nd.location.lon) for nd in obj.nodes if nd.location.valid()]
            members = [("node", nd.ref, "") for nd in obj.nodes]
            if pts:
                lat = sum(p[0] for p in pts) / len(pts)
                lon = sum(p[1] for p in pts) / len(pts)
            else:
                lat = lon = None
            yield "way", obj.id, t, lat, lon, members
        elif isinstance(obj, osmium.osm.Relation):
            t = _tags(obj)
            if _is_poi(t):
                members = [
                    (_MEMBER_KIND.get(str(m.type), "node"), m.ref, m.role or "")
                    for m in obj.members
                ]
                yield "relation", obj.id, t, None, None, members


class OsmiumPbfAdapter:
    """pyosmium-backed OSM bootstrap: nodes, ways (centroid), relations.

    Resume: pyosmium streams internally without exposed offsets, so a
    crashed run restarts the file — the merge's (provider, external_id)
    dedup keeps replays idempotent and observations log the re-sighting.
    """

    name = "osm"
    adapter_version = "osmium-v1"

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self.source_dataset: dict[str, Any] = {}

    async def probe(self) -> SourceProbe:
        try:
            import osmium  # type: ignore[import-not-found]  # optional
        except ImportError:
            return SourceProbe(ok=False, provider=self.name, detail="pyosmium not installed")
        if not hasattr(osmium, "FileProcessor"):
            return SourceProbe(
                ok=False, provider=self.name, detail="pyosmium <3.7 (needs FileProcessor)"
            )
        if self._path and self._path.exists():
            return SourceProbe(ok=True, provider=self.name, detail=str(self._path))
        return SourceProbe(ok=False, provider=self.name, detail="pbf path missing or unreadable")

    async def ingest(self, context: IngestionContext) -> AsyncIterator[RawPlaceRecord]:
        path = self._path or Path(str(context.param("pbf", "")))
        fetched = datetime.now(UTC)
        self.source_dataset = _dataset_meta(path)
        for kind, osm_id, tags, lat, lon, members in _stream_elements(path):
            yield record_from_tags(
                kind,
                osm_id,
                tags,
                lat=lat,
                lon=lon,
                fetched_at=fetched,
                member_refs=members,
            )
        context.checkpoint["stage"] = "done"


__all__ = ["OsmiumPbfAdapter"]
