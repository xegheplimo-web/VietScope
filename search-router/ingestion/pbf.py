"""Minimal OSM PBF reader — protobuf wire format, stdlib only (P15/P15.1).

Reads ``*.osm.pbf`` streams (Geofabrik vietnam-latest.osm.pbf) without
osmium/protobuf dependencies: BlobHeader+Blob framing, zlib/lzma blobs,
PrimitiveBlock → DenseNodes/plain Node + Way + Relation decoding, with
string-table resolution and delta coordinate decoding.

Scope is POI extraction, not a general OSM engine: ``iter_elements``
yields nodes, ways and relations with blob+index positions so adapters
can checkpoint safely mid-file; geometry assembly (multipolygons,
rings) stays out — centroids are enough for staging.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

# ── protobuf wire primitives ───────────────────────────────────────────

_WIRE_VARINT, _WIRE_64, _WIRE_LEN, _WIRE_32 = 0, 1, 2, 5


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    """(value, new_pos) for a protobuf varint."""
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _zigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def _fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield (field_no, wire_type, value) over a protobuf message."""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _varint(buf, pos)
        field_no, wire = tag >> 3, tag & 0x7
        if wire == _WIRE_VARINT:
            val, pos = _varint(buf, pos)
        elif wire == _WIRE_LEN:
            size, pos = _varint(buf, pos)
            val = buf[pos : pos + size]
            pos += size
        elif wire == _WIRE_64:
            val = buf[pos : pos + 8]
            pos += 8
        elif wire == _WIRE_32:
            val = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field_no, wire, val


def _packed_varints(buf: bytes, signed: bool = False) -> Iterator[int]:
    pos = 0
    n = len(buf)
    while pos < n:
        v, pos = _varint(buf, pos)
        yield _zigzag(v) if signed else v


# ── PBF framing ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OsmNode:
    node_id: int
    lat: float
    lon: float
    tags: dict[str, str]


@dataclass(frozen=True)
class OsmWay:
    way_id: int
    node_refs: list[int]  # node ids in order (delta-decoded)
    tags: dict[str, str]


@dataclass(frozen=True)
class OsmRelMember:
    ref: int
    kind: str  # "node" | "way" | "relation"
    role: str


@dataclass(frozen=True)
class OsmRelation:
    rel_id: int
    members: list[OsmRelMember]
    tags: dict[str, str]


def read_blobs(stream: BinaryIO) -> Iterator[tuple[str, bytes, int, int]]:
    """Yield (header_type, decoded_payload, start_offset, end_offset) per blob."""
    while True:
        offset = stream.tell()
        head = stream.read(4)
        if len(head) < 4:
            return
        (hlen,) = struct.unpack(">I", head)
        header = _parse_blob_header(stream.read(hlen))
        blob = _parse_blob(stream.read(header["datasize"]))
        yield header["type"], blob, offset, stream.tell()


def _parse_blob_header(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "", "datasize": 0}
    for fno, _w, val in _fields(buf):
        if fno == 1:
            out["type"] = val.decode("utf-8", "replace")
        elif fno == 3:
            out["datasize"] = int(val)
    return out


def _parse_blob(buf: bytes) -> bytes:
    raw: bytes | None = None
    zdata: bytes | None = None
    lzma: bytes | None = None
    for fno, _w, val in _fields(buf):
        if fno == 1:
            raw = val
        elif fno == 3:
            zdata = val
        elif fno == 4:
            lzma = val
    if raw is not None:
        return raw
    if zdata is not None:
        return zlib.decompress(zdata)
    if lzma is not None:
        import lzma as _lzma

        return _lzma.decompress(lzma)
    raise ValueError("unsupported blob compression")


# ── PrimitiveBlock decoding ─────────────────────────────────────────────


def _string_table(block: bytes) -> list[bytes]:
    for fno, _w, val in _fields(block):
        if fno == 1:
            return [s for _f, _w2, s in _fields(val) if _f == 1]
    return []


def _block_params(block: bytes) -> tuple[int, int, int]:
    gran, lat_off, lon_off = 100, 0, 0
    for fno, _w, val in _fields(block):
        if fno == 17:
            gran = int(val)
        elif fno == 19:
            lat_off = _zigzag(val)
        elif fno == 20:
            lon_off = _zigzag(val)
    return gran, lat_off, lon_off


def _coord(raw: int, offset: int, gran: int) -> float:
    return 1e-9 * (offset + gran * raw)


def _dense_nodes(
    msg: bytes, st: list[bytes], gran: int, lat_off: int, lon_off: int
) -> Iterator[OsmNode]:
    ids: list[int] = []
    lats: list[int] = []
    lons: list[int] = []
    kv: list[int] = []
    for fno, _w, val in _fields(msg):
        if fno == 1:
            ids = list(_packed_varints(val, signed=True))
        elif fno == 8:
            lats = list(_packed_varints(val, signed=True))
        elif fno == 9:
            lons = list(_packed_varints(val, signed=True))
        elif fno == 10:
            kv = list(_packed_varints(val))

    nid = lat = lon = 0
    ki = 0
    for i in range(len(ids)):
        nid += ids[i]
        lat += lats[i]
        lon += lons[i]
        tags: dict[str, str] = {}
        while ki < len(kv) and kv[ki] != 0:
            k, v = kv[ki], kv[ki + 1]
            ki += 2
            if k < len(st) and v < len(st):
                tags[st[k].decode("utf-8", "replace")] = st[v].decode("utf-8", "replace")
        if ki < len(kv) and kv[ki] == 0:
            ki += 1
        yield OsmNode(
            node_id=nid,
            lat=_coord(lat, lat_off, gran),
            lon=_coord(lon, lon_off, gran),
            tags=tags,
        )


def _plain_node(
    msg: bytes, st: list[bytes], gran: int, lat_off: int, lon_off: int
) -> OsmNode | None:
    nid = lat = lon = 0
    keys: list[int] = []
    vals: list[int] = []
    for fno, w, val in _fields(msg):
        if fno == 1:
            nid = _zigzag(val)
        elif fno == 2:
            keys.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
        elif fno == 3:
            vals.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
        elif fno == 8:
            lat = _zigzag(val)
        elif fno == 9:
            lon = _zigzag(val)
    tags = {
        st[k].decode("utf-8", "replace"): st[v].decode("utf-8", "replace")
        for k, v in zip(keys, vals, strict=True)
        if k < len(st) and v < len(st)
    }
    return OsmNode(
        node_id=nid, lat=_coord(lat, lat_off, gran), lon=_coord(lon, lon_off, gran), tags=tags
    )


def _key_vals(msg: bytes, st: list[bytes], key_fno: int = 2, val_fno: int = 3) -> dict[str, str]:
    keys: list[int] = []
    vals: list[int] = []
    for fno, w, val in _fields(msg):
        if fno == key_fno:
            keys.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
        elif fno == val_fno:
            vals.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
    return {
        st[k].decode("utf-8", "replace"): st[v].decode("utf-8", "replace")
        for k, v in zip(keys, vals, strict=True)
        if k < len(st) and v < len(st)
    }


def _way(msg: bytes, st: list[bytes]) -> OsmWay:
    wid = 0
    refs: list[int] = []
    for fno, _w, val in _fields(msg):
        if fno == 1:
            wid = int(val)
        elif fno == 8:
            refs = list(_packed_varints(val, signed=True))
    out: list[int] = []
    ref = 0
    for d in refs:
        ref += d
        out.append(ref)
    return OsmWay(way_id=wid, node_refs=out, tags=_key_vals(msg, st))


_REL_TYPES = {0: "node", 1: "way", 2: "relation"}


def _relation(msg: bytes, st: list[bytes]) -> OsmRelation:
    rid = 0
    roles: list[int] = []
    memids: list[int] = []
    types: list[int] = []
    for fno, w, val in _fields(msg):
        if fno == 1:
            rid = int(val)
        elif fno == 8:
            roles.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
        elif fno == 9:
            memids.extend(_packed_varints(val, signed=True) if w == _WIRE_LEN else [val])
        elif fno == 10:
            types.extend(_packed_varints(val) if w == _WIRE_LEN else [val])
    members: list[OsmRelMember] = []
    mem = 0
    for i, d in enumerate(memids):
        mem += d
        role_sid = roles[i] if i < len(roles) else 0
        role = st[role_sid].decode("utf-8", "replace") if role_sid < len(st) else ""
        kind = _REL_TYPES.get(types[i] if i < len(types) else 0, "node")
        members.append(OsmRelMember(ref=mem, kind=kind, role=role))
    return OsmRelation(rel_id=rid, members=members, tags=_key_vals(msg, st))


@dataclass(frozen=True)
class OsmElement:
    """One decoded element + its durable position in the stream.

    ``blob_offset``/``index`` identify the element for resume: index counts
    elements of the same ``want`` set within its blob (0-based). ``blob_end``
    is the stream offset just past the blob — a checkpoint must advance to
    it only when the whole blob has been processed.
    """

    kind: str  # "node" | "way" | "relation"
    obj: Any  # OsmNode | OsmWay | OsmRelation
    blob_offset: int
    index: int
    blob_end: int


def iter_elements(
    stream: BinaryIO,
    *,
    want: frozenset[str] = frozenset({"node", "way", "relation"}),
    start_offset: int = 0,
    start_index: int = 0,
) -> Iterator[OsmElement]:
    """Yield elements with durable positions; resumable at blob+index.

    Resume contract: pass ``start_offset`` of the blob containing the last
    *processed* element and ``start_index`` one past it — elements in that
    blob up to ``start_index`` are skipped, later blobs stream whole.
    """
    for btype, payload, off, end in read_blobs(stream):
        if btype != "OSMData" or off < start_offset:
            continue
        st = _string_table(payload)
        gran, lat_off, lon_off = _block_params(payload)
        i = 0
        for fno, _w, group in _fields(payload):
            if fno != 2:
                continue
            for gf, _gw, gval in _fields(group):
                for kind, obj in _iter_group(gf, gval, st, gran, lat_off, lon_off, want):
                    if off == start_offset and i < start_index:
                        i += 1
                        continue
                    yield OsmElement(kind, obj, off, i, end)
                    i += 1


def _iter_group(
    gf: int,
    gval: bytes,
    st: list[bytes],
    gran: int,
    lat_off: int,
    lon_off: int,
    want: frozenset[str],
) -> Iterator[tuple[str, Any]]:
    if gf == 1 and "node" in want:
        node = _plain_node(gval, st, gran, lat_off, lon_off)
        if node is not None:
            yield "node", node
    elif gf == 2 and "node" in want:
        for node in _dense_nodes(gval, st, gran, lat_off, lon_off):
            yield "node", node
    elif gf == 3 and "way" in want:
        yield "way", _way(gval, st)
    elif gf == 4 and "relation" in want:
        yield "relation", _relation(gval, st)


def iter_nodes(stream: BinaryIO) -> Iterator[OsmNode]:
    """Yield every node in an OSM PBF stream (DenseNodes + plain Node)."""
    for el in iter_elements(stream, want=frozenset({"node"})):
        yield el.obj
