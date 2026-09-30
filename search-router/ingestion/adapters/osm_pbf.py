"""OSM PBF adapter (P15.1) — nationwide bootstrap source, full coverage.

Reads a planet-extract ``*.osm.pbf`` (Geofabrik vietnam-latest) and emits
POI records for nodes AND ways AND relations — supermarkets, schools,
hospitals, hotels mapped as polygons/ways are no longer skipped.

Backends: ``osmium`` (pyosmium) is preferred for production when
importable; the stdlib PBF reader in ``ingestion/pbf.py`` is the fallback
and drives a four-pass flow:

  scan   — collect POI ways/relations + the node ids their geometry needs
  coords — fill needed member coordinates (restates whole on resume so
           geometry is never partial)
  nodes  — emit POI node records (resumable at blob+element granularity:
           the checkpoint never advances past unprocessed elements)
  emit   — emit way/relation records with member-coordinate centroids;
           restarts whole on resume (records dedup via merge)

Overpass stays a realtime query lane — bulk bootstrap is PBF only.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingestion.base import IngestionContext, RawPlaceRecord, SourceProbe
from ingestion.pbf import OsmNode, OsmRelation, OsmWay, iter_elements

_POI_KEYS = ("amenity", "shop", "tourism", "office", "leisure", "craft", "healthcare")


def _is_poi(tags: dict[str, str]) -> bool:
    return bool(tags.get("name")) and any(tags.get(k) for k in _POI_KEYS)


def _category(tags: dict[str, str]) -> str | None:
    for k in _POI_KEYS:
        if tags.get(k):
            return f"{k}:{tags[k]}"
    return None


def record_from_tags(
    kind: str,
    osm_id: int,
    tags: dict[str, str],
    *,
    lat: float | None,
    lon: float | None,
    fetched_at: datetime,
    member_refs: Any = None,
) -> RawPlaceRecord:
    """Shared tags→record mapping for node/way/relation POI elements."""
    street = tags.get("addr:street") or ""
    house = tags.get("addr:housenumber") or ""
    admin = tags.get("addr:district") or tags.get("addr:city") or tags.get("addr:province") or ""
    addr = ", ".join(p for p in (f"{house} {street}".strip(), admin) if p) or None
    payload: dict[str, Any] = {
        "id": osm_id,
        "kind": kind,
        "lat": lat,
        "lon": lon,
        "tags": tags,
    }
    if member_refs:
        payload["members"] = member_refs
    return RawPlaceRecord(
        provider=OsmPbfAdapter.name,
        external_id=f"{kind}:{osm_id}",
        external_id_type=f"osm_{kind}",
        source_url=f"https://www.openstreetmap.org/{kind}/{osm_id}",
        raw_name=tags.get("name"),
        raw_address=tags.get("addr:full") or addr,
        raw_phone=tags.get("phone") or tags.get("contact:phone"),
        raw_website=tags.get("website") or tags.get("contact:website") or tags.get("url"),
        raw_category=_category(tags),
        raw_hours={"raw": tags["opening_hours"]} if tags.get("opening_hours") else None,
        raw_status=_osm_status(tags),
        lat=lat,
        lon=lon,
        raw_payload=payload,
        observed_at=fetched_at,
        fetched_at=fetched_at,
    )


def _osm_status(tags: dict[str, str]) -> str | None:
    """OSM closure vocabulary → provider status.

    Lifecycle prefixes (disused:/abandoned:) mean permanently closed;
    opening_hours="closed" or temporary closure tags map to
    temporarily_closed.
    """
    if any(k.startswith("disused:") or k.startswith("abandoned:") for k in tags):
        return "CLOSED_PERMANENTLY"
    if tags.get("opening_hours") == "closed":
        return "CLOSED_PERMANENTLY"
    if tags.get("temporary_closed") in ("yes", "true", "1"):
        return "CLOSED_TEMPORARILY"
    return None


def node_to_record(node: OsmNode, *, fetched_at: datetime) -> RawPlaceRecord:
    return record_from_tags(
        "node", node.node_id, node.tags, lat=node.lat, lon=node.lon, fetched_at=fetched_at
    )


def _centroid(coords: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if not coords:
        return None, None
    return (
        sum(c[0] for c in coords) / len(coords),
        sum(c[1] for c in coords) / len(coords),
    )


def _osmium_available() -> bool:
    try:
        import osmium  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


class OsmPbfAdapter:
    """Streams POI records (node/way/relation) from a PBF extract.

    Context parameters: ``pbf`` — path to the .osm.pbf file;
    ``backend`` — ``auto``|``osmium``|``pbf`` (default auto: pyosmium when
    installed, else the stdlib multi-pass reader).

    Checkpoint: ``{"stage": ..., "offset": N, "index": I}`` — safe at
    blob/element granularity; ``scan``/``emit`` stages restart wholesale.
    """

    name = "osm"
    adapter_version = "osm-pbf-v2"

    def __init__(self, path: str | Path | None = None, *, backend: str = "auto"):
        self._path = Path(path) if path else None
        self._backend = backend
        self.source_dataset: dict[str, Any] = {}

    async def probe(self) -> SourceProbe:
        if self._path and self._path.exists():
            return SourceProbe(ok=True, provider=self.name, detail=str(self._path))
        return SourceProbe(ok=False, provider=self.name, detail="pbf path missing or unreadable")

    async def ingest(self, context: IngestionContext) -> AsyncIterator[RawPlaceRecord]:
        path = self._path or Path(str(context.param("pbf", "")))
        backend = str(context.param("backend", self._backend))
        if backend == "osmium" or (backend == "auto" and _osmium_available()):
            from ingestion.adapters.osm_osmium import OsmiumPbfAdapter

            inner = OsmiumPbfAdapter(path)
            async for rec in inner.ingest(context):
                yield rec
            # surface the backend that actually ran in run metadata
            self.adapter_version = inner.adapter_version
            self.source_dataset = inner.source_dataset
            return
        async for rec in self._ingest_fallback(path, context):
            yield rec

    # ── stdlib three-pass reader ────────────────────────────────────────

    def _dataset_meta(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        sha = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        return {
            "file": path.name,
            "sha256": sha.hexdigest(),
            "size_bytes": size,
        }

    async def _ingest_fallback(
        self, path: Path, context: IngestionContext
    ) -> AsyncIterator[RawPlaceRecord]:
        fetched = datetime.now(UTC)
        ck = context.checkpoint
        stage = ck.get("stage", "scan")
        self.source_dataset = self._dataset_meta(path)

        poi_ways: dict[int, OsmWay] = {}
        poi_relations: list[OsmRelation] = []

        # pass 1 — scan: POI ways/relations + every node id they need.
        # member_ways keeps node_refs for ALL ways referenced by POI
        # relations (their outer rings are typically untagged non-POI ways).
        with path.open("rb") as f:
            for el in iter_elements(f, want=frozenset({"way", "relation"})):
                obj = el.obj
                if el.kind == "way":
                    if _is_poi(obj.tags):
                        poi_ways[obj.way_id] = obj
                elif _is_poi(obj.tags):
                    poi_relations.append(obj)
        rel_member_way_ids = {
            m.ref for rel in poi_relations for m in rel.members if m.kind == "way"
        }
        member_ways: dict[int, OsmWay] = {}
        if rel_member_way_ids - set(poi_ways):
            with path.open("rb") as f:
                for el in iter_elements(f, want=frozenset({"way"})):
                    w: OsmWay = el.obj
                    if w.way_id in rel_member_way_ids and w.way_id not in poi_ways:
                        member_ways[w.way_id] = w
        member_ways.update(poi_ways)

        needed_nodes: set[int] = set()
        for w in member_ways.values():
            needed_nodes.update(w.node_refs)
        needed_nodes.update(m.ref for rel in poi_relations for m in rel.members if m.kind == "node")

        # pass 2 — coords: full node scan filling way/relation member
        # coordinates. NOT resumable mid-pass (a crash restarts it) —
        # checkpointing it would leave emit stage with partial geometry.
        if stage == "done":
            return
        coords: dict[int, tuple[float, float]] = {}
        if needed_nodes:
            with path.open("rb") as f:
                for el in iter_elements(f, want=frozenset({"node"})):
                    n: OsmNode = el.obj
                    if n.node_id in needed_nodes:
                        coords[n.node_id] = (n.lat, n.lon)

        # pass 3 — emit POI nodes. Resumable: the checkpoint lands on each
        # yielded element's own (blob, index) — never past unprocessed
        # elements, so a crash loses nothing already yielded but unmerged.
        start_off = int(ck.get("offset", 0) or 0) if stage == "nodes" else 0
        start_idx = int(ck.get("index", 0) or 0) if stage == "nodes" else 0
        with path.open("rb") as f:
            for el in iter_elements(
                f,
                want=frozenset({"node"}),
                start_offset=start_off,
                start_index=start_idx,
            ):
                n = el.obj
                if _is_poi(n.tags):
                    context.checkpoint["stage"] = "nodes"
                    context.checkpoint["offset"] = el.blob_offset
                    context.checkpoint["index"] = el.index + 1
                    yield node_to_record(n, fetched_at=fetched)

        # pass 4 — emit ways + relations (restarts whole on resume)
        context.checkpoint.clear()
        context.checkpoint["stage"] = "emit"
        for w in poi_ways.values():
            lat, lon = _centroid([coords[r] for r in w.node_refs if r in coords])
            yield record_from_tags(
                "way",
                w.way_id,
                w.tags,
                lat=lat,
                lon=lon,
                fetched_at=fetched,
                member_refs=w.node_refs,
            )
        for rel in poi_relations:
            pts: list[tuple[float, float]] = []
            for m in rel.members:
                if m.kind == "node" and m.ref in coords:
                    pts.append(coords[m.ref])
                elif m.kind == "way" and m.ref in member_ways:
                    w = member_ways[m.ref]
                    lat_c, lon_c = _centroid([coords[r] for r in w.node_refs if r in coords])
                    if lat_c is not None and lon_c is not None:
                        pts.append((lat_c, lon_c))
            lat, lon = _centroid(pts)
            yield record_from_tags(
                "relation",
                rel.rel_id,
                rel.tags,
                lat=lat,
                lon=lon,
                fetched_at=fetched,
                member_refs=[{"kind": m.kind, "ref": m.ref, "role": m.role} for m in rel.members],
            )
        context.checkpoint["stage"] = "done"


__all__ = ["OsmPbfAdapter", "node_to_record", "record_from_tags"]
