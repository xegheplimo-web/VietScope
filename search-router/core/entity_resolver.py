"""Vietnamese entity resolution — query-side alias canonicalization (P6).

``resolve_entities(text)`` maps surface forms to a canonical ``Entity``:
``"Sài Gòn"`` / ``"TP HCM"`` / ``"Ho Chi Minh City"`` →
``loc:ho_chi_minh_city``; ``"VIC"`` / ``"CTCP Vingroup"`` →
``co:vingroup``. Matching is accent-folded and longest-phrase-first, so
``"TP HCM"`` wins over ``"HCM"`` inside the same span and English forms
coexist with Vietnamese aliases.

Resolution is consumed by ``QueryUnderstanding.analyze`` as *expansion*:
each resolved entity contributes folded/kebab match forms to
``QueryProfile.entities`` so URL substring matching hits Vietnamese
slugs regardless of which surface form the user typed, plus the stable
``QueryProfile.entity_ids`` carrying canonical IDs. The legal-document
kind additionally canonicalizes citations — ``"nđ 254/2026"``,
``"Nghị định 254/2026/NĐ-CP"`` and ``"ng dinh so 254 nam 2026"`` all
resolve to ``legal:nd-254-2026`` whose match forms let a vbpl.vn link
match any of them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ─── accents ─────────────────────────────────────────────────────────────

_TONE_RE = re.compile(r"[̀-̣ͯ]+")
_STRIP = str.maketrans("đĐ", "dD")


def fold(text: str) -> str:
    """Lowercase + strip Vietnamese accents: ``'Nghị Định'`` → ``'nghi dinh'``."""
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(_STRIP))
    return _TONE_RE.sub("", decomposed).lower().strip()


_WS_RE = re.compile(r"[^\w]+", re.UNICODE)


def _kebab(text: str) -> str:
    return _WS_RE.sub("-", fold(text)).strip("-")


def _flat(text: str) -> str:
    return _WS_RE.sub(" ", fold(text)).strip()


# ─── Entity + alias table ────────────────────────────────────────────────


@dataclass(frozen=True)
class Entity:
    """A resolved entity: canonical identity + all known surface forms."""

    id: str  # "loc:ho_chi_minh_city", "co:vingroup", "legal:nd-254-2026"
    canonical: str
    kind: str  # location|company|person|product|legal_document|event
    aliases: tuple[str, ...] = ()
    match_forms: frozenset[str] = field(default=frozenset())


def _forms(entity: Entity) -> frozenset[str]:
    """Folded + kebab variants of canonical and every alias."""
    out: set[str] = set()
    for surface in (entity.canonical, *entity.aliases):
        flat = _flat(surface)
        kebab = _kebab(surface)
        for form in (flat, kebab):
            if form:
                out.add(form)
    return frozenset(out)


def _e(eid: str, canonical: str, kind: str, *aliases: str) -> Entity:
    ent = Entity(id=eid, canonical=canonical, kind=kind, aliases=tuple(aliases))
    object.__setattr__(ent, "match_forms", _forms(ent))
    return ent


# P6 taxonomy: PERSON / COMPANY / LOCATION(+AdministrativeUnit) /
# LEGAL DOCUMENT / PRODUCT / EVENT. Data is deliberately modest — the
# mechanism is the deliverable; rows extend by one line each.
ENTITIES: tuple[Entity, ...] = (
    # ── locations (incl. merged-province administrative units) ──────────
    _e(
        "loc:ho_chi_minh_city",
        "Thành phố Hồ Chí Minh",
        "location",
        "Sài Gòn",
        "Saigon",
        "TP HCM",
        "TPHCM",
        "TP.HCM",
        "TP. Hồ Chí Minh",
        "Thành phố HCM",
        "Ho Chi Minh City",
        "Ho Chi Minh",
    ),
    _e("loc:ha_noi", "Hà Nội", "location", "Ha Noi", "Hanoi", "Thủ đô", "Ha Noi City"),
    _e("loc:da_nang", "Đà Nẵng", "location", "Da Nang", "Danang"),
    _e("loc:hai_phong", "Hải Phòng", "location", "Hai Phong", "Haiphong"),
    _e("loc:can_tho", "Cần Thơ", "location", "Can Tho", "Cantho"),
    _e("loc:hue", "Huế", "location", "Hue", "Thừa Thiên Huế", "Thua Thien Hue"),
    _e("loc:da_lat", "Đà Lạt", "location", "Da Lat", "Dalat", "Lâm Đồng", "Lam Dong"),
    _e("loc:nha_trang", "Nha Trang", "location", "Khánh Hòa", "Khanh Hoa"),
    _e("loc:binh_duong", "Bình Dương", "location", "Binh Duong"),
    _e("loc:dong_nai", "Đồng Nai", "location", "Dong Nai", "Biên Hòa", "Bien Hoa"),
    _e("loc:bac_ninh", "Bắc Ninh", "location", "Bac Ninh"),
    _e("loc:quang_ninh", "Quảng Ninh", "location", "Quang Ninh", "Hạ Long", "Ha Long"),
    _e("loc:hai_phong_city_an_duong", "An Dương", "location"),
    # ── companies (public corp + ticker aliases) ────────────────────────
    _e("co:vingroup", "Vingroup", "company", "CTCP Tập đoàn Vingroup", "Tap doan Vingroup", "VIC"),
    _e("co:vinfast", "VinFast", "company", "Vinfast", "VF", "CTCP VinFast"),
    _e("co:fpt", "FPT", "company", "CTCP FPT", "Tập đoàn FPT", "FPT Corporation"),
    _e("co:viettel", "Viettel", "company", "Tập đoàn Viettel", "Viettel Group", "CTCP Viettel"),
    _e("co:vnpt", "VNPT", "company", "Tập đoàn VNPT"),
    _e("co:mobifone", "MobiFone", "company", "Mobifone", "Mobi", "CTCP MobiFone"),
    _e(
        "co:vinamilk", "Vinamilk", "company", "VNM", "CTCP Vinamilk", "Công ty cổ phần sữa Việt Nam"
    ),
    _e("co:sabeco", "Sabeco", "company", "SAB", "Saigon Beer", "Bia Sài Gòn"),
    _e("co:masan", "Masan Group", "company", "MSN", "Masan", "Tập đoàn Masan"),
    _e("co:novaland", "Novaland", "company", "NVL"),
    _e("co:sun_group", "Sun Group", "company", "Sungroup", "Tập đoàn Sun Group"),
    _e("co:pnj", "PNJ", "company", "CTCP PNJ", "Phú Nhuận Jewelry", "Vàng PNJ"),
    _e("co:hoaphat", "Hòa Phát", "company", "Hoa Phat", "HPG", "Hòa Phát Group", "Thép Hòa Phát"),
    _e("co:techcombank", "Techcombank", "company", "TCB", "Ngân hàng Techcombank"),
    _e(
        "co:vietcombank",
        "Vietcombank",
        "company",
        "VCB",
        "Ngân hàng Vietcombank",
        "Ngân hàng Ngoại thương Việt Nam",
    ),
    _e("co:bidv", "BIDV", "company", "Ngân hàng BIDV", "Ngân hàng Đầu tư và Phát triển Việt Nam"),
    _e("co:acb", "ACB", "company", "Ngân hàng ACB", "Ngân hàng Á Châu"),
    _e("co:sacombank", "Sacombank", "company", "STB", "Ngân hàng Sacombank"),
    _e("co:vpbank", "VPBank", "company", "VPB", "Ngân hàng VPBank"),
    _e("co:mbbank", "MB Bank", "company", "MB", "MBBank", "Ngân hàng Quân đội", "Military Bank"),
    _e("co:flc", "FLC Group", "company", "Tập đoàn FLC", "FLC"),
    _e("co:shopee_vn", "Shopee", "company", "Shopee Vietnam", "Shopee Việt Nam"),
    _e("co:tiki", "Tiki", "company", "Tiki.vn"),
    # ── products ────────────────────────────────────────────────────────
    _e(
        "product:iphone_17_pro",
        "iPhone 17 Pro",
        "product",
        "IP 17 Pro",
        "Iphone 17 Pro Max",
        "Apple iPhone 17 Pro",
    ),
    _e("product:vf8", "VinFast VF8", "product", "VF8", "VF 8", "Vinfast VF-8"),
    _e("product:vf3", "VinFast VF3", "product", "VF3", "VF 3"),
    _e("product:galaxy_s26", "Galaxy S26", "product", "Samsung Galaxy S26", "S26 Ultra"),
    _e("product:vang_sjc", "Vàng SJC", "product", "SJC", "Vàng miếng SJC", "SJC Gold"),
    # ── people & events (mechanism rows — coverage grows with data) ─────
    _e(
        "person:vo_nguyen_giap",
        "Võ Nguyên Giáp",
        "person",
        "Đại tướng Võ Nguyên Giáp",
        "Vo Nguyen Giap",
        "General Giap",
    ),
    _e(
        "event:dien_bien_phu",
        "Chiến dịch Điện Biên Phủ",
        "event",
        "Điện Biên Phủ",
        "Dien Bien Phu",
        "Battle of Dien Bien Phu",
    ),
)


# ─── legal-document resolution (pattern, not table) ──────────────────────

_DOC_KIND = {
    "nghi dinh": "nghị định",
    "nd": "nghị định",
    "nd-cp": "nghị định",
    "nghị định": "nghị định",
    "thong tu": "thông tư",
    "tt": "thông tư",
    "quyet dinh": "quyết định",
    "qd": "quyết định",
    "luat": "luật",
    "luật": "luật",
    "nghi quyet": "nghị quyết",
    "nq": "nghị quyết",
    "phap lenh": "pháp lệnh",
}
_DOC_RE = re.compile(
    r"(?:nghị\s*định|nghị\s*dinh|nghi\s*dinh|nđ(?:-cp)?|nd(?:-cp)?"
    r"|thông\s*tư|thong\s*tu|tt"
    r"|quyết\s*định|quyet\s*dinh|qđ|qd|luật|luat|nghị\s*quyết|nghi\s*quyet|nq"
    r"|pháp\s*lệnh|phap\s*lenh)"
    r"[\s\-]*(?:số\s*)?(\d{1,4})\s*(?:/|[-–])\s*(\d{4})",
    re.IGNORECASE,
)

_KIND_PREFIX = {
    "nghị định": "nd",
    "thông tư": "tt",
    "quyết định": "qd",
    "luật": "luat",
    "nghị quyết": "nq",
    "pháp lệnh": "pl",
}


def _legal_entities(text: str) -> list[Entity]:
    """``nđ 254/2026`` / ``Nghị định 254/2026/NĐ-CP`` → ``legal:nd-254-2026``."""
    out: list[Entity] = []
    seen: set[str] = set()
    for m in _DOC_RE.finditer(fold(text)):
        kind_raw = m.group(0).split()[0]
        toks = fold(m.group(0)[: m.start(1) - m.start(0)] or "").split()
        key = " ".join(t for t in toks[:2] if t not in ("so", "cp"))
        kind = _DOC_KIND.get(key) or _DOC_KIND.get(kind_raw)
        if kind is None:
            continue
        num, year = m.group(1), m.group(2)
        eid = f"legal:{_KIND_PREFIX.get(kind, kind[:2])}-{num}-{year}"
        if eid in seen:
            continue
        seen.add(eid)
        canonical = f"{kind.title()} {num}/{year}"
        ent = Entity(id=eid, canonical=canonical, kind="legal_document")
        forms = {
            f"{num}/{year}",
            f"{_flat(kind)} {num} {year}",
            f"{_kebab(kind)}-{num}-{year}",
            _kebab(canonical),
        }
        object.__setattr__(ent, "match_forms", frozenset(f for f in forms if f))
        out.append(ent)
    return out


# ─── resolution ──────────────────────────────────────────────────────────

# folded-phrase → entity, longest-phrase-first ordering at match time.
_ALIAS_INDEX: list[tuple[str, Entity]] = sorted(
    ((flat, ent) for ent in ENTITIES for flat in {fold(a) for a in (ent.canonical, *ent.aliases)}),
    key=lambda pair: -len(pair[0].split()),
)


def resolve_entities(text: str) -> list[Entity]:
    """Canonical entities found in ``text`` — deduped by id.

    Scans folded text for every known alias (longest phrase first, so a
    multi-word alias beats its own sub-phrases); legal-document citations
    resolve via pattern rather than the table. Empty text → [].
    """
    folded = fold(text)
    if not folded:
        return []
    padded = f" {folded} "
    hits: dict[str, Entity] = {}
    consumed: list[tuple[int, int]] = []

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= s or span[0] >= e for s, e in consumed)

    for alias, ent in _ALIAS_INDEX:
        for m in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", padded):
            span = m.span()
            if _free(span):
                consumed.append(span)
                hits.setdefault(ent.id, ent)
                break
    hits.update({e.id: e for e in _legal_entities(text)})
    return list(hits.values())


def resolve_match_forms(text: str) -> list[str]:
    """All match forms for resolved entities — URL-slug-friendly strings."""
    forms: list[str] = []
    seen: set[str] = set()
    for ent in resolve_entities(text):
        for form in ent.match_forms:
            if form and form not in seen:
                seen.add(form)
                forms.append(form)
    return forms
