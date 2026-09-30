"""VN administrative lookup (P14) — local geo anchors from hub-postgres.

``administrative_units`` + ``administrative_aliases`` (migration 006) are
seeded by ``python -m db.seed_admin`` from ``db/seeds/vn_admin_units.json``
— the temporal administrative graph: 34 current provincial units plus the
pre-2025 geography kept as historical units, so dissolved names ("Bà Rịa
- Vũng Tàu") still resolve — via successor aliases — instead of falling
back to a wrong centroid. Current-era units carry their boundary in
``geometry`` (P14B); the anchor point is its centroid.

Lookups fold accents (``core.entity_resolver.fold``) so "Bac Ninh",
"tỉnh Bắc Ninh" and "Bắc Ninh" hit the same row. The graph is small
(~15k units) — the folded index is cached in-process for
``_CACHE_TTL_S``; every failure (no DSN, missing table, DB error)
degrades to ``None``, never raises.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.entity_resolver import fold
from storage import pg_client

from services.geo import GeoPoint

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 300.0
_index: dict[str, AdminDivision] | None = None
_index_built_at = 0.0


@dataclass(frozen=True)
class AdminDivision:
    """A resolvable administrative unit with its anchor point."""

    code: str
    name: str
    type: str  # province | municipality | former_province | ward | ...
    lat: float
    lon: float
    current: bool = True
    aliases: tuple[str, ...] = field(default_factory=tuple)


_LOAD_SQL = """
SELECT u.code, u.name, u.type,
       ST_Y(ST_Centroid(u.geometry)) AS lat,
       ST_X(ST_Centroid(u.geometry)) AS lon,
       (u.valid_to IS NULL) AS current,
       COALESCE(array_agg(a.alias ORDER BY a.alias)
                FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
FROM administrative_units u
LEFT JOIN administrative_aliases a ON a.unit_id = u.unit_id
GROUP BY u.unit_id
ORDER BY (u.valid_to IS NULL) DESC, u.type, u.code
"""


# Folded administrative prefixes — stripped before the exact lookup so
# "tỉnh Bắc Ninh" hits the same row as "Bắc Ninh".
_ADMIN_PREFIX_RE = re.compile(
    r"^(?:tinh|thanh pho|tp\.?|pho|phuong|xa|thi xa|thi tran|quan|huyen|dac khu)\s+"
)


def match_forms(name: str, aliases: Iterable[str] = ()) -> list[str]:
    """Folded surface forms for a unit — name + aliases, deduped.

    Each surface also contributes its admin-prefix-stripped form so a
    stored "Thành phố Bắc Ninh" answers a bare "Bắc Ninh" query."""
    forms: list[str] = []
    seen: set[str] = set()
    for surface in (name, *aliases):
        f = fold(surface)
        for form in (f, _ADMIN_PREFIX_RE.sub("", f)):
            if form and form not in seen:
                seen.add(form)
                forms.append(form)
    return forms


async def _get_index() -> dict[str, AdminDivision] | None:
    """folded form → division; rebuilt lazily, None when DB is absent."""
    global _index, _index_built_at
    if _index is not None and time.monotonic() - _index_built_at < _CACHE_TTL_S:
        return _index
    pool = await pg_client.get_pool()
    if pool is None:
        return None
    try:
        rows = await pool.fetch(_LOAD_SQL)
    except Exception as exc:  # noqa: BLE001 — missing table / DB error
        logger.info("administrative_units load failed: %s", exc)
        return None
    index: dict[str, AdminDivision] = {}
    for r in rows:
        if r["lat"] is None or r["lon"] is None:
            continue
        div = AdminDivision(
            code=r["code"],
            name=r["name"],
            type=r["type"] or "",
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            current=bool(r["current"]),
            aliases=tuple(r["aliases"] or ()),
        )
        for form in match_forms(div.name, div.aliases):
            index.setdefault(form, div)
    _index, _index_built_at = index, time.monotonic()
    return _index


def _candidates(text: str) -> list[str]:
    folded = fold(text)
    out = [folded] if folded else []
    stripped = _ADMIN_PREFIX_RE.sub("", folded)
    if stripped and stripped != folded:
        out.append(stripped)
    return out


async def lookup_admin(text: str) -> AdminDivision | None:
    """Resolve ``text`` to an administrative unit, or ``None``.

    Exact folded match first (with admin prefixes stripped); then a
    contained match on the longest surface form, so "bà rịa vũng tàu"
    beats "vũng tàu" and current units win over former ones on ties
    (the index loads them first).
    """
    index = await _get_index()
    if not index:
        return None
    for cand in _candidates(text):
        hit = index.get(cand)
        if hit is not None:
            return hit
    padded = f" {fold(text)} "
    if len(padded) <= 2:
        return None
    best: AdminDivision | None = None
    best_len = -1
    for form, div in index.items():
        if len(form) <= best_len:
            continue
        if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", padded):
            best, best_len = div, len(form)
    return best


async def admin_anchor(text: str) -> GeoPoint | None:
    """``text`` → anchor ``GeoPoint`` for the geo-search lanes."""
    div = await lookup_admin(text)
    if div is None:
        return None
    return GeoPoint(
        name=div.name,
        lat=div.lat,
        lon=div.lon,
        display_name=f"{div.name}, Vietnam",
    )


def _reset_cache() -> None:
    """Test hook — drop the cached index."""
    global _index, _index_built_at
    _index, _index_built_at = None, 0.0
