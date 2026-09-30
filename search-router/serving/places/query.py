"""P17 local-place query normalization — deterministic, no LLM.

Turns ``/v1/places/*`` request parameters into a ``LocalQuerySpec`` the
OpenSearch lane, PostGIS lane and ranker all consume. Ordinary local
queries never touch an LLM: "nhà thuốc gần tôi" folds to a text term plus
a ``health`` category hint plus a geo anchor supplied by the client.

The vocabulary here is *serving-side query understanding* — it maps user
terms onto the canonical category buckets that P16 writes into
``canonical_places.canonical_category``. It deliberately does not import
``resolution/`` internals; the bucket names are part of the frozen
document contract surface (``PlaceDocumentV1.category_ids``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.entity_resolver import fold

from serving.places.document import (
    PLACE_STATUSES,
    STATUS_OPEN,
    STATUS_TEMPORARILY_CLOSED,
    STATUS_UNKNOWN,
)

MAX_LIMIT = 100
DEFAULT_RADIUS_M = 2000.0
MAX_RADIUS_M = 50_000.0

# Response ordering modes (P17.1). Absent/unknown → fusion score order;
# ``distance`` additionally needs a geo anchor to mean anything.
SORT_MODES = frozenset({"distance", "rating", "popularity"})

# Default serving set: permanently closed places exist in the index (they
# stay searchable under an explicit status filter) but never surface in an
# ordinary query.
DEFAULT_STATUSES = frozenset({STATUS_OPEN, STATUS_TEMPORARILY_CLOSED, STATUS_UNKNOWN})

# ── query-side category hints ─────────────────────────────────────────────
# Folded (accent-free, lowercase) surface forms → canonical bucket. This is
# an intent table for *queries*, not the resolver's provider-vocabulary map:
# terms chosen so a hint is a reliable signal ("nha thuoc" is always a
# pharmacy intent); ambiguous bare words are intentionally absent.
_QUERY_CATEGORY_HINTS: dict[str, str] = {
    # food & drink
    "nha hang": "food",
    "quan an": "food",
    "quan nuoc": "food",
    "quan ca phe": "food",
    "ca phe": "food",
    "cafe": "food",
    "coffee": "food",
    "pho": "food",
    "bun": "food",
    "com": "food",
    "banh mi": "food",
    "an uong": "food",
    "restaurant": "food",
    "restaurants": "food",
    "bar": "food",
    "pub": "food",
    # retail
    "sieu thi": "retail",
    "cua hang": "retail",
    "tap hoa": "retail",
    "cho": "retail",
    "shop": "retail",
    "store": "retail",
    "market": "retail",
    "supermarket": "retail",
    "mall": "retail",
    "convenience store": "retail",
    # health
    "nha thuoc": "health",
    "hieu thuoc": "health",
    "phong kham": "health",
    "benh vien": "health",
    "tram y te": "health",
    "nha khoa": "health",
    "thu y": "health",
    "pharmacy": "health",
    "drugstore": "health",
    "clinic": "health",
    "hospital": "health",
    "dentist": "health",
    # education
    "truong hoc": "education",
    "dai hoc": "education",
    "mam non": "education",
    "truong": "education",
    "school": "education",
    "university": "education",
    "kindergarten": "education",
    # lodging & tourism
    "khach san": "lodging",
    "nha nghi": "lodging",
    "homestay": "lodging",
    "hotel": "lodging",
    "hostel": "lodging",
    "resort": "lodging",
    "di tich": "tourism",
    "museum": "tourism",
    "attraction": "tourism",
    # transport
    "tram xang": "transport",
    "cay xang": "transport",
    "ben xe": "transport",
    "nha ga": "transport",
    "san bay": "transport",
    "gas station": "transport",
    "fuel": "transport",
    "parking": "transport",
    "bus station": "transport",
    "airport": "transport",
    "train station": "transport",
    # finance
    "ngan hang": "finance",
    "atm": "finance",
    "bank": "finance",
    "banking": "finance",
    # government & civic
    "uy ban": "government",
    "ubnd": "government",
    "cong an": "government",
    "buu dien": "government",
    "post office": "government",
    "police": "government",
    # services
    "cat toc": "services",
    "tiem cat toc": "services",
    "salon": "services",
    "spa": "services",
    "gym": "services",
    "phong gym": "services",
    "giat ui": "services",
    "sua chua": "services",
    "garage": "services",
    # worship & culture
    "chua": "worship",
    "nha tho": "worship",
    "dinh": "worship",
    "mieu": "worship",
    "temple": "worship",
    "pagoda": "worship",
    "church": "worship",
    "rap chieu": "culture",
    "cinema": "culture",
    "thu vien": "culture",
    "library": "culture",
}

# Proximity tail phrases stripped from the text lane — they carry intent,
# not content ("gần tôi" should not token-match place names).
_PROXIMITY_PHRASES = (
    "gan toi",
    "gan day",
    "gan nhat",
    "o day",
    "quanh day",
    "xung quanh",
    "near me",
    "nearby",
    "closest",
    "gan nhat toi",
)
_PROXIMITY_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in _PROXIMITY_PHRASES) + r")\b")
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class LocalQuerySpec:
    """Normalized local-search request — consumed by every serving lane."""

    q_raw: str | None = None  # caller text, trimmed
    text: str = ""  # folded, proximity-stripped text for matching
    tokens: tuple[str, ...] = ()

    lat: float | None = None
    lon: float | None = None
    radius_m: float = DEFAULT_RADIUS_M
    bbox: tuple[float, float, float, float] | None = None  # min_lon,min_lat,max_lon,max_lat

    category: str | None = None  # canonical bucket (explicit or hinted)
    category_hinted: bool = False  # True when inferred from text, not a param
    admin_unit_id: int | None = None
    admin_contains: bool = False  # polygon ST_Contains instead of id equality
    statuses: frozenset[str] = DEFAULT_STATUSES

    # P17.1 rich filters — all optional; None = unconstrained.
    open_now: bool | None = None  # request-time verdict, post-filtered
    min_rating: float | None = None  # rating >= floor (OS range / SQL >=)
    price_level: str | None = None  # exact keyword match ("₫₫", "$$")
    sort: str | None = None  # one of SORT_MODES; None = fusion score

    limit: int = 20
    debug: bool = False

    @property
    def has_geo(self) -> bool:
        return self.lat is not None and self.lon is not None


def fold_text(raw: str | None) -> str:
    """Accent-folded, whitespace-collapsed comparison form."""
    return _WS.sub(" ", fold(raw or "")).strip()


def hint_category(text_folded: str) -> str | None:
    """Folded query text → canonical bucket, longest hint wins."""
    best: tuple[int, str] | None = None
    for hint, bucket in _QUERY_CATEGORY_HINTS.items():
        if hint in text_folded and (best is None or len(hint) > best[0]):
            best = (len(hint), bucket)
    return best[1] if best else None


def parse_statuses(raw: str | None) -> frozenset[str]:
    """``status=`` param → canonical status set.

    Absent → the default serving set (no ``permanently_closed``).
    ``all``/``*`` → every status. Otherwise a comma list intersected with
    the canonical vocabulary; unknown values are dropped.
    """
    if raw is None or not raw.strip():
        return DEFAULT_STATUSES
    s = raw.strip().lower()
    if s in ("all", "*"):
        return PLACE_STATUSES
    out = {p.strip() for p in s.split(",") if p.strip() in PLACE_STATUSES}
    return frozenset(out) if out else DEFAULT_STATUSES


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """``bbox=minlon,minlat,maxlon,maxlat`` → tuple, or None when malformed."""
    if not raw:
        return None
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    min_lon, min_lat, max_lon, max_lat = parts
    if min_lon > max_lon or min_lat > max_lat:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def parse_local_query(
    q: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float | None = None,
    category: str | None = None,
    admin_unit_id: int | None = None,
    status: str | None = None,
    bbox: str | None = None,
    limit: int = 20,
    debug: bool = False,
    admin_contains: bool = False,
    open_now: bool | None = None,
    min_rating: float | None = None,
    price_level: str | None = None,
    sort: str | None = None,
) -> LocalQuerySpec:
    """Build the normalized spec every lane consumes."""
    q_raw = (q or "").strip() or None
    folded = fold_text(q_raw)
    text = _WS.sub(" ", _PROXIMITY_RE.sub(" ", folded)).strip()

    cat = (category or "").strip().lower() or None
    hinted = False
    if cat is None and text:
        cat = hint_category(text)
        hinted = cat is not None

    s = (sort or "").strip().lower()
    r = radius_m if radius_m and radius_m > 0 else DEFAULT_RADIUS_M
    return LocalQuerySpec(
        q_raw=q_raw,
        text=text,
        tokens=tuple(text.split()) if text else (),
        lat=lat,
        lon=lon,
        radius_m=min(r, MAX_RADIUS_M),
        bbox=parse_bbox(bbox),
        category=cat,
        category_hinted=hinted,
        admin_unit_id=admin_unit_id,
        admin_contains=admin_contains,
        statuses=parse_statuses(status),
        open_now=open_now if isinstance(open_now, bool) else None,
        min_rating=min_rating if min_rating and min_rating > 0 else None,
        price_level=(price_level or "").strip() or None,
        sort=s if s in SORT_MODES else None,
        limit=max(1, min(int(limit), MAX_LIMIT)),
        debug=debug,
    )


# Field name for the geopoint inside an indexed document.
GEO_FIELD = "location"
