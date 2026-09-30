"""Vietnamese administrative geo resolver (P14A).

Resolves free-text addresses and place names to administrative units in the
versioned gazetteer (``storage.admin_store``), including historical ones —
the transition graph carries an address written before the 2025
reorganization forward to the current two-level geography:

    "Yên Dũng, Bắc Giang"      -> huyện Yên Dũng (hist.) -> TP Bắc Giang
                                (hist.) -> phường/xã under new Bắc Ninh
    "Quận Hoàn Kiếm, Hà Nội"   -> quận Hoàn Kiếm (hist.) -> phường Hoàn
                                Kiếm + Cửa Nam (current)
    "TP Thủ Đức, TP.HCM"       -> thành phố Thủ Đức (hist.) -> the new
                                phường under Thành phố Hồ Chí Minh

Matching is accent-insensitive (``entity_resolver.fold``) and consults
unit names plus aliases (abbreviations, English names, historical forms).
Ambiguous same-name units ("Châu Thành" exists in many provinces) are
disambiguated by the surrounding segments' province/district context; a
truly ambiguous query returns ``status="ambiguous"`` with candidates.

``resolve`` never deletes or mutates history: historical units stay in the
graph and forward edges (renamed_to/merged_into/split_into/replaced_by/
boundary_changed) reach the current era; a unit with no edges inherits its
parent's transition (communes dissolved inside a dissolved district ride
the district's edges).
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from storage.admin_store import (
    AdminGraph,
    AdminRelation,
    AdminUnit,
    DictAdminStore,
    graph_from_seed,
    load_seed,
)

from core.entity_resolver import fold
from core.vn_address import parse_vn_address

_WS_RE = re.compile(r"[^\w]+", re.UNICODE)

_TYPE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"thành phố trực thuộc|thanh pho truc thuoc|"
    r"thành phố thuộc|thanh pho thuoc|"
    r"thành phố|thanh pho|tp\.?|"
    r"thị xã|thi xa|tx\.?|"
    r"thị trấn|thi tran|tt\.?|"
    r"tỉnh|tinh|"
    r"quận|quan|q\.?|"
    r"huyện|huyen|h\.?|"
    r"phường|phuong|p\.?|"
    r"xã|xa|"
    r"đặc khu|dac khu|"
    r"tỉnh thành|tinh thanh"
    r")\s+",
    re.IGNORECASE,
)

# level implied by an explicit type prefix on a segment
_LEVEL_BY_PREFIX = [
    (re.compile(r"^\s*(?:quận|quan|q\.|huyện|huyen|h\.|thị xã|thi xa|tx\.)\s", re.IGNORECASE), 2),
    (
        re.compile(
            r"^\s*(?:phường|phuong|p\.|xã|xa|đặc khu|dac khu|thị trấn|"
            r"thi tran|tt\.)\s",
            re.IGNORECASE,
        ),
        3,
    ),
    (re.compile(r"^\s*(?:tỉnh|tinh|thành phố|thanh pho|tp\.)\s", re.IGNORECASE), 1),
]

_RESOLVE_TYPES = {"merged_into", "split_into", "renamed_to", "replaced_by", "boundary_changed"}


def norm_text(text: str) -> str:
    """Query-side normalization: fold accents, drop type prefix, collapse."""
    t = _TYPE_PREFIX_RE.sub("", (text or "").strip())
    return _WS_RE.sub(" ", fold(t)).strip()


@dataclass(frozen=True)
class ResolvedHit:
    unit: AdminUnit
    via: str  # "name" | "alias:<type>" | "parent-fallback"
    segment: str  # the raw query segment that matched


@dataclass
class Resolution:
    query: str
    matched: list[AdminUnit] = field(default_factory=list)
    current: list[AdminUnit] = field(default_factory=list)
    path: list[AdminRelation] = field(default_factory=list)
    status: str = "not_found"  # resolved | ambiguous | not_found
    ambiguity: list[AdminUnit] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def provinces(self) -> list[AdminUnit]:
        return [u for u in self.current if u.admin_level == 1]

    @property
    def communes(self) -> list[AdminUnit]:
        return [u for u in self.current if u.admin_level == 3]


class GeoResolver:
    """Index over an ``AdminGraph``; deterministic, no LLM."""

    def __init__(self, graph: AdminGraph):
        self.units = graph.units
        self.relations = graph.relations
        self._children = graph.children
        self._out: dict[str, list[AdminRelation]] = {}
        for r in graph.relations:
            self._out.setdefault(r.from_key, []).append(r)
        self._name_idx: dict[str, list[str]] = {}
        for key, u in self.units.items():
            self._name_idx.setdefault(u.normalized_name, []).append(key)
        self._alias_idx: dict[str, list[tuple[str, str]]] = {}
        for a in graph.aliases:
            self._alias_idx.setdefault(a.normalized_alias, []).append((a.unit_key, a.alias_type))
        self._ancestor_cache: dict[str, tuple[str, ...]] = {}
        self._fwd_cache: dict[str, tuple[tuple[str, ...], tuple[AdminRelation, ...]]] = {}

    # ── construction helpers ─────────────────────────────────────────────

    @classmethod
    def from_seed(cls, path=None) -> GeoResolver:
        return cls(graph_from_seed(load_seed(path)))

    @classmethod
    async def from_store(cls, store) -> GeoResolver | None:
        graph = await store.graph()
        return cls(graph) if graph else None

    # ── matching ─────────────────────────────────────────────────────────

    def _match_term(self, term: str, level_hint: int | None) -> list[ResolvedHit]:
        norm = norm_text(term)
        if not norm:
            return []
        hits: list[ResolvedHit] = []
        for key in self._name_idx.get(norm, []):
            hits.append(ResolvedHit(self.units[key], "name", term))
        for key, atype in self._alias_idx.get(norm, []):
            hits.append(ResolvedHit(self.units[key], f"alias:{atype}", term))
        if level_hint and len({h.unit.admin_level for h in hits}) > 1:
            hinted = [h for h in hits if h.unit.admin_level == level_hint]
            if hinted:
                hits = hinted
        return hits

    def _segments(self, text: str) -> list[tuple[str, int | None]]:
        """Query terms from an address string: comma parts + parser fields."""
        parsed = parse_vn_address(text)
        out: list[tuple[str, int | None]] = []
        for seg in re.split(r"[,\n;|]+", text or ""):
            seg = seg.strip()
            if not seg:
                continue
            hint = None
            for rx, lvl in _LEVEL_BY_PREFIX:
                if rx.match(seg):
                    hint = lvl
                    break
            out.append((seg, hint))
        # parser positional fields carry their implied level
        for value, lvl in (
            (parsed.ward, 3),
            (parsed.district, 2),
            (parsed.city, 1),
        ):
            if value and all(norm_text(s) != norm_text(value) for s, _ in out):
                out.append((value, lvl))
        return out

    # ── graph walks ──────────────────────────────────────────────────────

    def ancestors(self, key: str) -> tuple[str, ...]:
        if key in self._ancestor_cache:
            return self._ancestor_cache[key]
        chain = []
        cur = self.units[key].parent_key
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            u = self.units.get(cur)
            if u is None:
                break
            chain.append(cur)
            cur = u.parent_key
        self._ancestor_cache[key] = tuple(chain)
        return self._ancestor_cache[key]

    def _province_key(self, key: str) -> str | None:
        if self.units[key].admin_level == 1:
            return key
        for anc in self.ancestors(key):
            if self.units[anc].admin_level == 1:
                return anc
        return None

    def _forward(self, key: str) -> tuple[tuple[str, ...], tuple[AdminRelation, ...]]:
        """Walk transition edges to the current era → (keys, edges), memoized.

        Units with no outgoing edge inherit their parent's transition —
        communes dissolved inside a dissolved district ride the district's
        edges forward.
        """
        cached = self._fwd_cache.get(key)
        if cached is not None:
            return cached
        cur = key
        while cur:
            u = self.units.get(cur)
            if u is None:
                self._fwd_cache[key] = ((), ())
                return (), ()
            if u.status == "current" and cur == key:
                self._fwd_cache[key] = ((cur,), ())
                return (cur,), ()
            if self._out.get(cur):
                break
            cur = u.parent_key
        if cur is None:
            self._fwd_cache[key] = ((), ())
            return (), ()

        out_keys: list[str] = []
        edges: list[AdminRelation] = []
        queue: deque[str] = deque([cur])
        seen: set[str] = set()
        while queue:
            k = queue.popleft()
            if k in seen:
                continue
            seen.add(k)
            u = self.units.get(k)
            if u is None:
                continue
            if u.status == "current":
                out_keys.append(k)
                continue
            rels = [r for r in self._out.get(k, []) if r.relation_type in _RESOLVE_TYPES]
            if rels:
                edges.extend(rels)
                queue.extend(r.to_key for r in rels)
            elif u.parent_key:
                queue.append(u.parent_key)  # parent fallback
        out = (tuple(out_keys), tuple(edges))
        self._fwd_cache[key] = out
        return out

    # ── public API ───────────────────────────────────────────────────────

    def resolve(self, text: str, level_hint: int | None = None) -> Resolution:
        """Resolve an address/place string to matched + current units."""
        res = Resolution(query=text)
        seg_hits: list[tuple[str, int | None, list[ResolvedHit]]] = []
        for seg, hint in self._segments(text):
            hits = self._match_term(seg, hint)
            if hits:
                seg_hits.append((seg, hint, hits))
        if not seg_hits:
            return res

        # ── province context: rightmost segment carrying a level-1 hit ────
        matched_prov: AdminUnit | None = None
        prov_seg_idx: int | None = None
        for i in range(len(seg_hits) - 1, -1, -1):
            l1 = [h for h in seg_hits[i][2] if h.unit.admin_level == 1]
            if not l1:
                continue
            matched_prov = max(l1, key=lambda h: self._ctx_score(h, seg_hits)).unit
            prov_seg_idx = i
            break

        # ── per-segment best candidate under the province context ────────
        chosen: list[ResolvedHit] = []
        ambiguous_segs: list[AdminUnit] = []
        for i, (_seg, hint, hits) in enumerate(seg_hits):
            pool = hits
            if matched_prov is not None:
                consistent = [h for h in hits if self._consistent(h.unit, matched_prov)]
                if consistent:
                    pool = consistent
            ranked = sorted(
                pool,
                key=lambda h: (
                    i == prov_seg_idx and h.unit.key == matched_prov.key,
                    h.via == "name",
                    hint is not None and h.unit.admin_level == hint,
                    h.unit.admin_level,
                ),
                reverse=True,
            )
            top = ranked[0]
            ties = [
                h
                for h in ranked
                if h.unit.key != top.unit.key
                and h.via == top.via
                and h.unit.admin_level == top.unit.admin_level
            ]
            if ties and matched_prov is None:
                ambiguous_segs.extend(h.unit for h in ranked[:5])
            chosen.append(top)

        matched_units: list[AdminUnit] = []
        for h in chosen:
            if all(u.key != h.unit.key for u in matched_units):
                matched_units.append(h.unit)
        if matched_prov is not None and all(u.key != matched_prov.key for u in matched_units):
            matched_units.append(matched_prov)
        res.matched = matched_units

        current: dict[str, AdminUnit] = {}
        edges: list[AdminRelation] = []
        for u in matched_units:
            # The unit's own transitions plus each ancestor's: a dissolved
            # district's edges reach successor communes, and its province's
            # merge edge reaches the successor province — both belong to the
            # answer ("where does this address live today?").
            for k in (u.key, *self.ancestors(u.key)):
                keys, es = self._forward(k)
                current.update({kk: self.units[kk] for kk in keys})
                edges.extend(es)
        seen_e: set[tuple] = set()
        res.path = [
            e
            for e in edges
            if not (
                (e.from_key, e.to_key, e.relation_type) in seen_e
                or seen_e.add((e.from_key, e.to_key, e.relation_type))
            )
        ]
        res.current = sorted(current.values(), key=lambda u: (-u.admin_level, u.name))

        if ambiguous_segs:
            res.status = "ambiguous"
            res.ambiguity = ambiguous_segs
            res.confidence = 0.5
        else:
            res.status = "resolved"
            res.confidence = min(
                1.0,
                0.9 + 0.1 * (all(h.via == "name" for h in chosen)) - min(0.15, 0.05 * len(edges)),
            )
        return res

    def resolve_name(self, name: str) -> Resolution:
        """Bare-name lookup — no parser hints, all levels considered."""
        res = self.resolve(name)
        return res

    async def resolve_point(self, store, lat: float, lon: float) -> list[AdminUnit]:
        """Point → current units (commune first) via the store's geometry."""
        return await store.units_containing(lat, lon)

    def _ctx_score(self, hit: ResolvedHit, seg_hits: list[tuple[str, int | None, list]]) -> int:
        """Rank a level-1 candidate by how many other segments it explains."""
        prov = hit.unit.key
        fwd, _ = self._forward(prov)
        fwd_provs = {self._province_key(t) for t in fwd}
        explained = 0
        for _seg, _hint, hs in seg_hits:
            for o in hs:
                if prov == o.unit.key or prov in self.ancestors(o.unit.key):
                    explained += 1
                    break
                ok, _e = self._forward(o.unit.key)
                if any(self._province_key(tk) in fwd_provs for tk in ok):
                    explained += 1
                    break
        return explained * 10 + (2 if hit.via == "name" else 1)

    def _consistent(self, unit: AdminUnit, prov: AdminUnit) -> bool:
        """Unit shares province lineage with the chosen province context —

        either it sits under that province, or its forward transition lands
        under that province's successor (old Bắc Giang's subtree resolves
        through new Bắc Ninh).
        """
        if prov.key in self.ancestors(unit.key) or unit.key == prov.key:
            return True
        fwd_prov, _ = self._forward(prov.key)
        fwd_provs = set(fwd_prov)
        fwd_unit, _ = self._forward(unit.key)
        return any(self._province_key(tk) in fwd_provs for tk in fwd_unit)


_DEFAULT_RESOLVER: GeoResolver | None = None


def get_resolver() -> GeoResolver:
    """Process-local resolver backed by the bundled seed (no DB needed)."""
    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = GeoResolver.from_seed()
    return _DEFAULT_RESOLVER


def dict_store() -> DictAdminStore:
    """Convenience: in-memory store over the bundled seed."""
    return DictAdminStore.from_seed()
