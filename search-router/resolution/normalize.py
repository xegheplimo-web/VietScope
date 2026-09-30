"""P16 normalization layer — Vietnamese-aware canonical field forms.

Sits between raw staging and matching: every comparison happens on
normalized values so "Quán Ăn Vặt Cô Ba" / "quan an vat co ba" and
"0901234567" / "+84 901 234 567" score as equal.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from core.vn_address import fold_diacritics
from ingestion.validate import canonical_website, normalize_phone

# Business-form stopwords: stripped from the *head* of a name — "Công ty
# TNHH ABC" and "ABC" are the same trading name. Kept deliberately small;
# trailing tokens ("Quán ABC" → "ABC") matter for identity.
_NAME_PREFIXES = re.compile(
    r"^(?:công ty|cong ty|cty|tnhh|cp|jsc|co\.?,?\s*ltd|llc|cửa hàng|cua hang|"
    r"quán|quan|tiệm|tiem|nhà hàng|nha hang|shop|store|siêu thị|sieu thi|"
    r"hiệu thuốc|hieu thuoc|nhà thuốc|nha thuoc|phòng khám|phong kham|"
    r"trạm|tram|bến|ben)\b[\s\.\-,:]*",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def norm_name(raw: str | None) -> str:
    """Canonical comparison form: fold diacritics, strip business-form
    prefixes (repeatable — "Công ty Cửa hàng ABC"), punctuation, case."""
    s = (raw or "").strip().lower()
    s = fold_diacritics(s)
    prev = None
    while prev != s:
        prev = s
        s = _NAME_PREFIXES.sub("", s).strip()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def name_tokens(norm: str) -> frozenset[str]:
    return frozenset(norm.split()) if norm else frozenset()


def name_signature(norm: str) -> str:
    """Sorted-token signature: same words any order ⇒ same signature."""
    return " ".join(sorted(norm.split()))


def norm_phone(raw: str | None) -> str | None:
    return normalize_phone(raw or "")


def norm_website(raw: str | None) -> str | None:
    return canonical_website(raw or "")


def website_domain(raw: str | None) -> str | None:
    w = norm_website(raw)
    if not w:
        return None
    host = urlparse(w).netloc
    return host[4:] if host.startswith("www.") else host


def norm_address(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    s = fold_diacritics(s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


# ── operational status ────────────────────────────────────────────────────
# Provider vocabularies (google business_status, gosom strings, OSM tags)
# → canonical: open | temporarily_closed | permanently_closed.
_STATUS_MAP: dict[str, str] = {
    "operational": "open",
    "open": "open",
    "open_for_business": "open",
    "dang hoat dong": "open",
    "mo cua": "open",
    "closed_temporarily": "temporarily_closed",
    "temporarily_closed": "temporarily_closed",
    "tam dong cua": "temporarily_closed",
    "tam ngung": "temporarily_closed",
    "ngung tam": "temporarily_closed",
    "closed_permanently": "permanently_closed",
    "permanently_closed": "permanently_closed",
    "closed": "permanently_closed",
    "dong cua": "permanently_closed",
    "da dong cua": "permanently_closed",
    "ngung hoat dong": "permanently_closed",
}
_FOLDED_STATUS = {fold_diacritics(k): v for k, v in _STATUS_MAP.items()}


def norm_status(raw: str | None) -> str | None:
    """Canonical operational status from any provider vocabulary."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s in _STATUS_MAP:
        return _STATUS_MAP[s]
    folded = fold_diacritics(s)
    if folded in _FOLDED_STATUS:
        return _FOLDED_STATUS[folded]
    # substring pass for noisy values ("TEMPORARILY CLOSED - covid")
    if "permanent" in folded:
        return "permanently_closed"
    if "temporar" in folded:
        return "temporarily_closed"
    return None


# ── category ontology ─────────────────────────────────────────────────────
# Provider vocabularies (OSM tags, Google types, free text) → canonical
# buckets. Coarse on purpose: matching uses buckets, display keeps raw.
_CATEGORY_MAP: dict[str, str] = {
    # food & drink
    "restaurant": "food",
    "cafe": "food",
    "fast_food": "food",
    "bar": "food",
    "pub": "food",
    "food_court": "food",
    "biergarten": "food",
    "ice_cream": "food",
    "bakery": "food",
    "quán ăn": "food",
    "quán nước": "food",
    "nhà hàng": "food",
    "tiệm ăn": "food",
    "quán cafe": "food",
    "cà phê": "food",
    "quán bia": "food",
    "ăn uống": "food",
    # retail
    "supermarket": "retail",
    "convenience": "retail",
    "marketplace": "retail",
    "shop": "retail",
    "mall": "retail",
    "department_store": "retail",
    "clothes": "retail",
    "shoes": "retail",
    "electronics": "retail",
    "mobile_phone": "retail",
    "furniture": "retail",
    "hardware": "retail",
    "greengrocer": "retail",
    "butcher": "retail",
    "siêu thị": "retail",
    "cửa hàng": "retail",
    "chợ": "retail",
    "tạp hóa": "retail",
    "đại lý": "retail",
    "phụ tùng": "retail",
    "điện tử": "retail",
    "điện thoại": "retail",
    "điện máy": "retail",
    "máy tính": "retail",
    "hiệu sách": "retail",
    "nhà sách": "retail",
    # health
    "pharmacy": "health",
    "hospital": "health",
    "clinic": "health",
    "doctors": "health",
    "dentist": "health",
    "veterinary": "health",
    "nhà thuốc": "health",
    "phòng khám": "health",
    "bệnh viện": "health",
    "trạm y tế": "health",
    "hiệu thuốc": "health",
    "bác sĩ": "health",
    "nha sĩ": "health",
    "y tế": "health",
    "dịch vụ y tế": "health",
    "sức khỏe": "health",
    "phòng mạch": "health",
    "châm cứu": "health",
    "dược phẩm": "health",
    # education
    "school": "education",
    "university": "education",
    "college": "education",
    "kindergarten": "education",
    "language_school": "education",
    "trường học": "education",
    "đại học": "education",
    "mầm non": "education",
    "trung tâm": "education",
    # lodging & tourism
    "hotel": "lodging",
    "guest_house": "lodging",
    "hostel": "lodging",
    "motel": "lodging",
    "resort": "lodging",
    "homestay": "lodging",
    "khách sạn": "lodging",
    "nhà nghỉ": "lodging",
    "chỗ ở": "lodging",
    "attraction": "tourism",
    "museum": "tourism",
    "viewpoint": "tourism",
    "di tích": "tourism",
    "công viên": "tourism",
    "du lịch": "tourism",
    # transport
    "fuel": "transport",
    "charging_station": "transport",
    "bus_station": "transport",
    "bus_stop": "transport",
    "parking": "transport",
    "car_wash": "transport",
    "car_repair": "transport",
    "bicycle_parking": "transport",
    "bến xe": "transport",
    "trạm xăng": "transport",
    "ga": "transport",
    "sân bay": "transport",
    "gara": "transport",
    "rửa xe": "transport",
    "sơn ô tô": "transport",
    "độ xe": "transport",
    "cứu hộ": "transport",
    # finance & services
    "bank": "finance",
    "atm": "finance",
    "money_transfer": "finance",
    "ngân hàng": "finance",
    "insurance": "finance",
    "bảo hiểm": "finance",
    # government & civic
    "townhall": "government",
    "courthouse": "government",
    "police": "government",
    "fire_station": "government",
    "post_office": "government",
    "community_centre": "government",
    "public_building": "government",
    "ủy ban": "government",
    "ubnd": "government",
    "công an": "government",
    "văn phòng chính phủ": "government",
    # personal & professional services
    "hairdresser": "services",
    "beauty": "services",
    "laundry": "services",
    "dry_cleaning": "services",
    "tailor": "services",
    "optician": "services",
    "travel_agency": "services",
    "real_estate": "services",
    "spa": "services",
    "gym": "services",
    "fitness_centre": "services",
    "kim hoàn": "services",
    "dịch vụ máy tính": "services",
    # worship & culture
    "place_of_worship": "worship",
    "church": "worship",
    "pagoda": "worship",
    "temple": "worship",
    "mosque": "worship",
    "chùa": "worship",
    "đình": "worship",
    "miếu": "worship",
    "nhà thờ": "worship",
    "theatre": "culture",
    "cinema": "culture",
    "arts_centre": "culture",
    "library": "culture",
    "rạp chiếu phim": "culture",
    "rạp hát": "culture",
    "giải trí": "culture",
    # offices & industrial
    "office": "office",
    "company": "office",
    "coworking": "office",
    "văn phòng": "office",
    "nhà xuất bản": "office",
    "factory": "industrial",
    "warehouse": "industrial",
    "xưởng": "industrial",
    # Google place types (as they appear in `types` arrays / categories)
    "lodging": "lodging",
    "food": "food",
    "store": "retail",
    "meal_takeaway": "food",
    "meal_delivery": "food",
    "liquor_store": "retail",
    "grocery_or_supermarket": "retail",
    "convenience_store": "retail",
    "drugstore": "health",
    "doctor": "health",
    "veterinary_care": "health",
    "primary_school": "education",
    "secondary_school": "education",
    "gas_station": "transport",
    "transit_station": "transport",
    "subway_station": "transport",
    "train_station": "transport",
    "airport": "transport",
    "tourist_attraction": "tourism",
    "local_government_office": "government",
    "city_hall": "government",
    "embassy": "government",
    "real_estate_agency": "services",
    "beauty_salon": "services",
    "hair_care": "services",
    "car_dealer": "retail",
    "electronics_store": "retail",
    "furniture_store": "retail",
    "hardware_store": "retail",
    "jewelry_store": "retail",
    "pet_store": "retail",
    "shoe_store": "retail",
    "clothing_store": "retail",
    "book_store": "retail",
}

# Related buckets score partial agreement in matching.
_CATEGORY_GROUPS: dict[str, frozenset[str]] = {
    "food": frozenset({"food", "retail"}),  # eateries inside shops
    "retail": frozenset({"retail", "food"}),
    "health": frozenset({"health"}),
    "education": frozenset({"education"}),
    "lodging": frozenset({"lodging", "tourism"}),
    "tourism": frozenset({"tourism", "lodging", "culture"}),
    "culture": frozenset({"culture", "tourism"}),
    "transport": frozenset({"transport"}),
    "finance": frozenset({"finance", "services"}),
    "services": frozenset({"services", "finance"}),
    "government": frozenset({"government"}),
    "worship": frozenset({"worship", "culture"}),
    "office": frozenset({"office", "industrial"}),
    "industrial": frozenset({"industrial", "office"}),
}


_FOLDED_MAP = {fold_diacritics(k): v for k, v in _CATEGORY_MAP.items()}


def _category_candidates(raw: str | None, payload: dict | None) -> list[str]:
    """Label candidates in precedence order: raw_category, then structured
    payload hints (``types`` list from Google, ``category``/``amenity``/
    ``shop`` keys from OSM tags preserved in raw_payload)."""
    cands: list[str] = []
    if raw:
        cands.append(raw)
    if payload:
        for key in ("category", "amenity", "shop", "office", "tourism", "leisure"):
            v = payload.get(key)
            if isinstance(v, str):
                cands.append(v)
        types = payload.get("types")
        if isinstance(types, list):
            cands.extend(str(t) for t in types)
    return cands


def _category_lookup(candidate: str, mapping: dict[str, str]) -> str | None:
    """Exact folded hit, else substring ('nhà thuốc tây' → 'nhà thuốc');
    longest key wins."""
    k = fold_diacritics(candidate.strip().lower())
    if k in mapping:
        return mapping[k]
    for key in sorted(mapping, key=len, reverse=True):
        if len(key) > 3 and key in k:
            return mapping[key]
    return None


def canonical_category(raw: str | None, payload: dict | None = None) -> str | None:
    """Map provider vocabulary → canonical bucket ('food','health',...).

    Checks raw_category first, then structured payload hints.
    Built-in map only — the resolution runner layers DB mappings on top
    via :class:`CategoryResolver`.
    """
    for c in _category_candidates(raw, payload):
        hit = _category_lookup(c, _FOLDED_MAP)
        if hit is not None:
            return hit
    return None


class CategoryResolver:
    """Provider-aware category lookup: DB overlay layered on the built-in map.

    ``add()`` rows come from ``source_category_mappings`` (provider '*' is a
    wildcard). Precedence: provider-specific > '*' > built-in. Raw labels
    nothing maps are counted in ``unknowns`` keyed ``(provider, key)`` —
    persisted to ``unknown_source_categories`` by the runner so a new
    provider vocabulary surfaces as rows, not silent NULLs.
    """

    def __init__(self) -> None:
        self._overlay: dict[tuple[str, str], str] = {}
        self._merged_cache: dict[str, dict[str, str]] = {}
        self.unknowns: Counter[tuple[str, str]] = Counter()
        self.samples: dict[tuple[str, str], str] = {}

    def add(self, provider: str, category_key: str, bucket: str) -> None:
        self._overlay[(provider or "*", category_key)] = bucket
        self._merged_cache.clear()

    def _merged(self, provider: str | None) -> dict[str, str]:
        p = provider or ""
        m = self._merged_cache.get(p)
        if m is None:
            m = dict(_FOLDED_MAP)
            for (prov, key), bucket in self._overlay.items():
                if prov == "*":
                    m[key] = bucket
            for (prov, key), bucket in self._overlay.items():
                if prov == p:
                    m[key] = bucket
            self._merged_cache[p] = m
        return m

    def resolve(
        self,
        raw: str | None,
        provider: str | None = None,
        payload: dict | None = None,
        *,
        log_unknown: bool = True,
    ) -> str | None:
        merged = self._merged(provider)
        result = _resolve_with(raw, payload, merged)
        raw_s = (raw or "").strip()
        # the raw label is what ops needs a row for — log its miss even if
        # a payload hint rescued the bucket. log_unknown=False on re-reads
        # (provenance recompute) so seen_count counts scanned records, not
        # resolution-internal encounters.
        if log_unknown and raw_s and _category_lookup(raw_s, merged) is None:
            pk = provider or ""
            key = fold_diacritics(raw_s.lower())
            self.unknowns[(pk, key)] += 1
            self.samples.setdefault((pk, key), raw_s[:200])
        return result

    def unknown_rows(self) -> list[tuple[str, str, str | None, int]]:
        """(provider, category_key, raw_sample, seen_count) for upsert."""
        return [
            (prov, key, self.samples.get((prov, key)), n)
            for (prov, key), n in self.unknowns.items()
        ]


def _resolve_with(raw: str | None, payload: dict | None, mapping: dict[str, str]) -> str | None:
    for c in _category_candidates(raw, payload):
        hit = _category_lookup(c, mapping)
        if hit is not None:
            return hit
    return None


def categories_related(a: str | None, b: str | None) -> float:
    """Category agreement score: same=1.0, related group=0.6, both known
    and unrelated=0.1, either unknown=0.4 (neutral)."""
    if not a or not b:
        return 0.4
    if a == b:
        return 1.0
    if b in _CATEGORY_GROUPS.get(a, frozenset()):
        return 0.6
    return 0.1
