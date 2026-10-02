"""LOCAL-1 — bounded local discovery helpers.

Deterministic, I/O-free building blocks for ``api.v1.business_search``:

* ``expand_local_query`` — bounded category/locality query expansion.
  No LLM, no network: regex + a static Vietnamese synonym table only.
* ``locality_of`` — trailing locality tokens after the category phrase.
* ``evaluate_local_quality`` / ``local_quality_sufficient`` — the recall
  gate deciding whether a request widens to the slow lanes.
* ``dedupe_local_candidates`` — cross-lane candidate merge with field
  enrichment and source-count accounting.
* ``tag_lane_origin`` — origin/verified/location_precision stamping.
* ``run_local_lane`` — degrade-safe timed wrapper around one lane coroutine.

Matching is done on accent-folded tokens so queries typed without
diacritics still expand, while emitted variants always carry diacritics —
VN engines need tone marks for recall.
"""

from __future__ import annotations

import difflib
import math
import re
import time
from collections.abc import Awaitable
from urllib.parse import urlparse

from models import BusinessEntity

from core.business_entity import category_for
from core.entity_resolver import fold
from observability.prometheus import observe_local_expansion, observe_local_lane

_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\D+")
_TRAIL_CHARS = " \t\n\r?.!"

# surface forms → substitute synonyms, ordered most-specific first; the
# longest trigger match wins so "quán ăn đêm" beats "quán ăn".
_CATEGORY_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("quán ăn đêm", "quán ăn khuya", "ăn đêm", "ăn khuya", "đồ ăn đêm", "đồ ăn khuya"),
        ("quán ăn khuya", "nhà hàng mở muộn", "quán nhậu"),
    ),
    (
        ("quán ăn", "nhà hàng", "restaurant", "quán cơm", "quán phở", "quán bún"),
        ("nhà hàng", "quán nhậu"),
    ),
    (("quán nhậu", "bar"), ("quán ăn", "nhà hàng")),
    (("quán cà phê", "cà phê", "cafe", "coffee"), ("quán cà phê", "cà phê")),
    (("nhà thuốc", "tiệm thuốc", "hiệu thuốc", "pharmacy"), ("hiệu thuốc", "tiệm thuốc")),
    (("cây xăng", "trạm xăng", "xăng dầu"), ("xăng dầu", "trạm xăng")),
    (("khách sạn", "hotel"), ("nhà nghỉ",)),
    (("nhà nghỉ", "guest house"), ("khách sạn",)),
    (("siêu thị", "supermarket"), ("tạp hóa", "cửa hàng tiện lợi")),
    (("cửa hàng tiện lợi", "tạp hóa"), ("siêu thị", "cửa hàng")),
    (("cửa hàng", "shop", "store"), ("siêu thị", "tạp hóa")),
    (("chợ", "marketplace"), ("siêu thị",)),
    (("phòng khám", "trạm y tế", "clinic"), ("bệnh viện",)),
    (("bệnh viện", "hospital"), ("phòng khám",)),
    (("nha khoa", "dentist"), ("phòng khám",)),
    (("thú y", "veterinary"), ("phòng khám",)),
    (("ngân hàng", "bank", "atm"), ("atm", "ngân hàng")),
    (("trường học", "school"), ("đại học",)),
    (("đại học", "university"), ("trường học",)),
    (("salon", "hairdresser", "tiệm tóc"), ("spa",)),
    (("spa",), ("salon", "tiệm tóc")),
)

_FOLDED_GROUPS = tuple(
    (tuple(fold(t) for t in triggers), synonyms) for triggers, synonyms in _CATEGORY_EXPANSIONS
)

# folded proximity/connector tokens stripped from the front of a locality tail
_PROXIMITY_TOKENS = frozenset({"gan", "o", "tai", "quanh", "near", "around", "trong", "khu", "vuc"})

_LOCAL_LANE_ORIGINS = frozenset({"canonical", "osm_local", "osm_live"})

_NAME_SIM_DOMAIN = 0.60
_NAME_SIM_GEO = 0.88
_NAME_SIM_ADDR = 0.90
_ADDR_SIM = 0.85
_GEO_MERGE_M = 100.0
_EARTH_RADIUS_M = 6_371_000.0

_SCALAR_FILL = ("phone", "hours", "website", "address", "description", "category")
_POPULATED_FIELDS = _SCALAR_FILL + (
    "name",
    "lat",
    "lon",
    "rating",
    "price_level",
    "source_url",
)

# ─── Specialty / product intent ─────────────────────────────────────────────
# Maps a detected specialty to its surface-form variants for bounded
# expansion.  When a specialty is present, generic category matches do NOT
# satisfy the quality gate — only entities whose name/description/address
# mention the specialty count as useful.

_SPECIALTY_LEXICON: dict[str, tuple[str, ...]] = {
    "giò chả": ("giò chả", "giò lụa", "chả lụa", "chả giò", "bánh chả"),
    "phở": ("phở", "bún phở", "phở bò", "phở gà"),
    "bún chả": ("bún chả", "chả cá", "bún chả cá"),
    "bánh mì": ("bánh mì", "bánh mì trứng", "bánh mì ốp la"),
    "cà phê": ("cà phê", "coffee", "cafe"),
    "bánh sinh nhật": ("bánh sinh nhật", "bánh kem", "bánh ngọt"),
    "sắt thép": ("sắt thép", "vật liệu xây", "thép"),
}


# (specialty, variants, folded forms) — matching happens on folded token
# spans so accentless queries ("gio cha", "nha thuoc") resolve too.
_FOLDED_SPECIALTY: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = tuple(
    (specialty, variants, tuple(dict.fromkeys(fold(f) for f in (specialty,) + variants)))
    for specialty, variants in _SPECIALTY_LEXICON.items()
)


def _span_end(tokens: list[str], forms: tuple[str, ...]) -> int | None:
    """End index of the longest folded multi-token ``forms`` match in ``tokens``."""
    best_end: int | None = None
    best_len = 0
    for form in forms:
        ft = form.split()
        n = len(ft)
        if not n or n <= best_len:
            continue
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == ft:
                best_len, best_end = n, i + n
    return best_end


def extract_specialty(query: str) -> tuple[str | None, tuple[str, ...]]:
    """Detect specialty/product intent from the query (deterministic, no LLM).

    Returns ``(specialty, variants)`` or ``(None, ())``.
    """
    fold_tokens = fold(query).split()
    for specialty, variants, folded_forms in _FOLDED_SPECIALTY:
        if _span_end(fold_tokens, folded_forms) is not None:
            return specialty, variants
    return None, ()


def entity_supports_specialty(
    entity: BusinessEntity, specialty: str, variants: tuple[str, ...]
) -> bool:
    """True when the entity's name/description/address mention the specialty."""
    all_tokens = _norm_text(f"{entity.name} {entity.description} {entity.address}").split()
    forms = tuple(fold(f) for f in (specialty,) + variants)
    return _span_end(all_tokens, forms) is not None


def _clean(query: str) -> str:
    return _WS_RE.sub(" ", (query or "").strip()).rstrip(_TRAIL_CHARS)


def _match_category(tokens: list[str]) -> tuple[int, int, tuple[str, ...]] | None:
    """Longest trigger token-span; ties prefer the earliest start, then the
    earlier group (groups are ordered most-specific first)."""
    best: tuple[tuple[int, int, int], int, int, tuple[str, ...]] | None = None
    for gi, (triggers, synonyms) in enumerate(_FOLDED_GROUPS):
        for trig in triggers:
            tt = trig.split()
            n = len(tt)
            for i in range(len(tokens) - n + 1):
                if tokens[i : i + n] == tt:
                    rank = (n, -i, -gi)
                    if best is None or rank > best[0]:
                        best = (rank, i, i + n, synonyms)
    if best is None:
        return None
    _, start, end, synonyms = best
    return start, end, synonyms


def _locality_tail(query: str) -> str:
    """Tokens after the matched category span, minus leading proximity words."""
    q = _clean(query)
    if not q:
        return ""
    orig_tokens = q.split()
    fold_tokens = fold(q).split()
    m = _match_category(fold_tokens)
    if m is not None:
        _, end, _ = m
    else:
        # No broad-category phrase — locality trails the specialty span.
        specialty, variants = extract_specialty(query)
        if not specialty:
            return ""
        end = _span_end(fold_tokens, tuple(fold(f) for f in (specialty,) + variants))
        if not end:
            return ""
    tail = list(orig_tokens[end:])
    ftail = list(fold_tokens[end:])
    while ftail and ftail[0] in _PROXIMITY_TOKENS:
        tail.pop(0)
        ftail.pop(0)
    return " ".join(tail)


def locality_of(query: str) -> str:
    """The trailing locality phrase of a local query ("" when none detected)."""
    return _locality_tail(query)


def expand_local_query(query: str, *, max_variants: int = 8) -> list[str]:
    """Original query first, then bounded category + locality variants.

    ``max_variants`` caps the total list (8 by default — never a
    categories × localities cartesian product). Pure: regex + dict lookups.

    When a specialty is detected, specialty variants are prioritized over
    generic category synonyms — the web lane needs the specialty term to
    find relevant businesses.
    """
    original = _clean(query)
    if not original:
        return [query]

    fold_tokens = fold(original).split()
    m = _match_category(fold_tokens)
    specialty, specialty_variants = extract_specialty(query)
    if (m is None and not specialty) or max_variants <= 1:
        return [original]

    orig_tokens = original.split()
    if m is not None:
        _, end, synonyms = m
    else:
        # No broad-category phrase — locality trails the specialty span so
        # standalone specialty queries ("giò chả Yên Dũng") still expand.
        end = _span_end(
            fold_tokens, tuple(fold(f) for f in (specialty,) + specialty_variants)
        ) or len(fold_tokens)
        synonyms = ()
    tail = list(orig_tokens[end:])
    ftail = list(fold_tokens[end:])
    while ftail and ftail[0] in _PROXIMITY_TOKENS:
        tail.pop(0)
        ftail.pop(0)
    locality = " ".join(tail)

    # Specialty variants take priority over generic category synonyms
    if specialty:
        candidates = [f"{v} {locality}" for v in specialty_variants[:3]] if locality else []
        candidates += list(specialty_variants[:4])
    else:
        # locality-qualified synonyms first — they pin the category to the place,
        # which is the higher-recall shape for the web lane.
        candidates = [f"{syn} {locality}" for syn in synonyms[:3]] if locality else []
        candidates += list(synonyms[:4])

    out = [original]
    seen = {fold(original)}
    for v in candidates:
        key = fold(v)
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    out = out[:max_variants]
    if len(out) > 1:
        observe_local_expansion()
    return out


_FOODISH_CATEGORIES = frozenset({"restaurant", "cafe", "food", "bar", "store", "convenience"})


def _category_compatible(cat: str, target: str) -> bool:
    """Same taxonomy comparison as the service path (food-family groups)."""
    if not cat or not target:
        return False
    if cat == target:
        return True
    return cat in _FOODISH_CATEGORIES and target in _FOODISH_CATEGORIES


def _query_relevant(e: BusinessEntity, target_category: str) -> bool:
    """The entity's own evidence supports the query's category intent."""
    if not target_category:
        return False
    if _category_compatible((e.category or "").strip(), target_category):
        return True
    inferred = category_for(f"{e.name} {e.description} {e.address}")
    return _category_compatible(inferred, target_category)


def evaluate_local_quality(
    entities: list[BusinessEntity],
    *,
    requested_limit: int,
    query: str = "",
    category: str = "",
    specialty: str | None = None,
    specialty_variants: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """The local recall gate.

    ``useful`` = named entities that carry coordinates AND (a category OR a
    canonical/local-lane origin). When ``query``/``category`` is given the
    entity must also look relevant to that intent — nearby-but-unrelated
    canonical rows must not close the gate. When a specialty is present,
    generic category matches do NOT count — only entities whose
    name/description/address mention the specialty are useful.

    Sufficient when ``useful >= min(5, limit)``.
    """
    target = min(5, max(0, requested_limit))
    target_category = ""
    if category:
        # req.category accepts both taxonomy values ("pharmacy") and
        # surface forms ("nhà thuốc") — classify first, keep raw fallback.
        target_category = category_for(category) or category.strip().lower()
    if not target_category and query:
        target_category = category_for(query)
    useful = 0
    for e in entities or []:
        if not (e.name or "").strip():
            continue
        if e.lat is None or e.lon is None:
            continue
        if specialty:
            # Specialty query: only specialty-supporting entities are useful
            if entity_supports_specialty(e, specialty, specialty_variants):
                useful += 1
        elif query or category:
            if _query_relevant(e, target_category):
                useful += 1
        elif (e.category or "").strip() or e.origin in _LOCAL_LANE_ORIGINS:
            useful += 1
    if useful >= target:
        return True, "sufficient"
    return False, "insufficient_useful"


def local_quality_sufficient(entities: list[BusinessEntity], *, requested_limit: int) -> bool:
    return evaluate_local_quality(entities, requested_limit=requested_limit)[0]


def _norm_text(s: str | None) -> str:
    return _WS_RE.sub(" ", fold(s or "")).strip()


def _sim(a: str | None, b: str | None) -> float:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _norm_phone(phone: str | None) -> str:
    """Digits only, with a written +84/0084/bare-84 MSISDN folded to 0…"""
    if not phone:
        return ""
    digits = _DIGITS_RE.sub("", phone)
    if not digits:
        return ""
    if digits.startswith("0084"):
        return "0" + digits[4:]
    if digits.startswith("84") and (phone.strip().startswith("+") or len(digits) == 11):
        return "0" + digits[2:]
    return digits


def _domain(url: str | None) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    return (urlparse(u).hostname or "").removeprefix("www.")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _same_entity(a: BusinessEntity, b: BusinessEntity) -> bool:
    pa, pb = _norm_phone(a.phone), _norm_phone(b.phone)
    if pa and pa == pb:
        return True
    da, db = _domain(a.website), _domain(b.website)
    if da and da == db and _sim(a.name, b.name) >= _NAME_SIM_DOMAIN:
        return True
    name_sim = _sim(a.name, b.name)
    if (
        name_sim >= _NAME_SIM_GEO
        and a.lat is not None
        and a.lon is not None
        and b.lat is not None
        and b.lon is not None
        and _haversine_m(a.lat, a.lon, b.lat, b.lon) <= _GEO_MERGE_M
    ):
        return True
    return bool(
        name_sim >= _NAME_SIM_ADDR
        and _norm_text(a.address)
        and _norm_text(b.address)
        and _sim(a.address, b.address) >= _ADDR_SIM
    )


def _populated(e: BusinessEntity) -> int:
    return sum(1 for f in _POPULATED_FIELDS if getattr(e, f, None) not in (None, ""))


def _pick_base(
    first: BusinessEntity, later: BusinessEntity
) -> tuple[BusinessEntity, BusinessEntity]:
    """First entity keeps identity — except a strictly richer later entity
    that brings coordinates the first one lacks."""
    if (
        _populated(later) > _populated(first)
        and (first.lat is None or first.lon is None)
        and later.lat is not None
        and later.lon is not None
    ):
        return later, first
    return first, later


def _fill(base: BusinessEntity, other: BusinessEntity) -> None:
    for f in _SCALAR_FILL:
        if getattr(base, f) in (None, "") and getattr(other, f) not in (None, ""):
            setattr(base, f, getattr(other, f))
    if base.lat is None and base.lon is None and other.lat is not None and other.lon is not None:
        base.lat, base.lon = other.lat, other.lon


def dedupe_local_candidates(entities: list[BusinessEntity]) -> list[BusinessEntity]:
    """Merge cross-lane duplicates in input order, enriching survivors.

    The surviving entity keeps its own ``source_url``; each merge records
    the duplicate's distinct URL in ``supporting_source_count``. No field is
    ever overwritten with an empty value and lat/lon are never invented.
    """
    survivors: list[BusinessEntity] = []
    src_sets: list[set[str]] = []
    for e in entities or []:
        hit = None
        for i, s in enumerate(survivors):
            if _same_entity(s, e):
                hit = i
                break
        if hit is None:
            survivors.append(e)
            src_sets.append({e.source_url} if e.source_url else set())
            continue
        s = survivors[hit]
        base, other = _pick_base(s, e)
        _fill(base, other)
        urls = src_sets[hit]
        for u in (s.source_url, e.source_url):
            if u:
                urls.add(u)
        base.supporting_source_count = max(1, len(urls))
        if base is not s:
            survivors[hit] = base
    return survivors


def tag_lane_origin(entities: list[BusinessEntity], origin: str, *, locality: str = "") -> None:
    """Stamp the LOCAL-1 output contract on one lane's results, in place."""
    for e in entities or []:
        e.origin = origin
        e.verified = origin == "canonical"
        if origin in _LOCAL_LANE_ORIGINS:
            e.location_precision = "exact"
        elif (e.address or "").strip():
            e.location_precision = "street"
        else:
            e.location_precision = "area" if locality.strip() else "unknown"


async def run_local_lane(name: str, coro: Awaitable[list]) -> tuple[str, list, float]:
    """Await one retrieval lane; any exception degrades to an empty list.

    Returns ``(name, results, finished_at)`` — ``finished_at`` is a
    ``perf_counter`` timestamp used for first-result latency.
    """
    start = time.perf_counter()
    try:
        results = await coro
    except Exception:  # noqa: BLE001 — a dead lane must not fail the request
        results = []
    finished = time.perf_counter()
    results = list(results or [])
    observe_local_lane(name, finished - start, len(results))
    return name, results, finished
