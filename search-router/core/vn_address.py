"""Vietnamese address parser — deterministic, regex-based, no LLM.

Parses free-form Vietnamese addresses into structured fields:

    "Số 12 Ngõ 45 Trần Duy Hưng, phường Trung Hòa, quận Cầu Giấy, Hà Nội"
    → house_number="12", lane="Ngõ 45", street="Trần Duy Hưng",
      ward="Trung Hòa", district="Cầu Giấy", city="Hà Nội"

    "12A Đường 3/2, P. Xuân Khánh, Q. Ninh Kiều, Cần Thơ"
    → house_number="12A", street="Đường 3/2",
      ward="Xuân Khánh", district="Ninh Kiều", city="Cần Thơ"

Supports lowercase/uppercase, abbreviated prefixes (P., Q., TP., HCM, SG),
and missing address components (partial addresses still parse gracefully).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.entity_resolver import fold as _fold_accents


@dataclass
class VNAddress:
    house_number: str = ""
    lane: str = ""
    street: str = ""
    ward: str = ""
    district: str = ""
    city: str = ""
    full: str = ""


# ── City aliases → canonical name ─────────────────────────────────────────────
#
# P14A: the table is derived from the canonical administrative seed
# (db/seeds/vn_admin_units.json — city_hints section), which carries the
# post-2025 province set plus unambiguous city names and historical
# provinces resolved through the transition graph ("bắc giang" →
# "Bắc Ninh", "bà rịa vũng tàu" → "Thành phố Hồ Chí Minh"). The hardcoded
# 63-province table it replaced could not express that history.
#
# The parser's ``city`` field is a hint for the caller; identity lives in
# the resolver's admin_unit keys, not in these canonical strings.

_SEED_PATH = Path(__file__).resolve().parents[1] / "db" / "seeds" / "vn_admin_units.json"

# Minimal fallback when the seed file is unavailable (partial checkouts,
# stripped deployments) — keeps the three biggest municipalities working.
_FALLBACK_CITY_ALIASES: dict[str, str] = {
    "hà nội": "Thành phố Hà Nội",
    "ha noi": "Thành phố Hà Nội",
    "hanoi": "Thành phố Hà Nội",
    "thành phố hồ chí minh": "Thành phố Hồ Chí Minh",
    "hồ chí minh": "Thành phố Hồ Chí Minh",
    "tphcm": "Thành phố Hồ Chí Minh",
    "tp.hcm": "Thành phố Hồ Chí Minh",
    "tp hcm": "Thành phố Hồ Chí Minh",
    "hcm": "Thành phố Hồ Chí Minh",
    "sài gòn": "Thành phố Hồ Chí Minh",
    "saigon": "Thành phố Hồ Chí Minh",
    "sg": "Thành phố Hồ Chí Minh",
    "cần thơ": "Thành phố Cần Thơ",
    "can tho": "Thành phố Cần Thơ",
    "đà nẵng": "Thành phố Đà Nẵng",
    "da nang": "Thành phố Đà Nẵng",
    "hải phòng": "Thành phố Hải Phòng",
    "hai phong": "Thành phố Hải Phòng",
    "huế": "Thành phố Huế",
    "hue": "Thành phố Huế",
}


def _load_city_aliases() -> dict[str, str]:
    """City hints from the canonical seed; fallback table when absent."""
    try:
        seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        hints = seed.get("city_hints") or {}
        if hints:
            return dict(hints)
    except Exception:
        pass
    return dict(_FALLBACK_CITY_ALIASES)


CITY_ALIASES: dict[str, str] = _load_city_aliases()

# Short aliases (≤3 chars) matched with word boundaries to avoid substring hits.
_CITY_SHORT_ALIASES = {k: v for k, v in CITY_ALIASES.items() if len(k) <= 3}
_CITY_LONG_ALIASES = {k: v for k, v in CITY_ALIASES.items() if len(k) > 3}

_WS_RE = re.compile(r"\s+")


def fold_diacritics(text: str) -> str:
    """Lowercase ASCII fold: strip tones, đ→d (shared by the admin graph)."""
    return _WS_RE.sub(" ", _fold_accents(text or "")).strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _match_city(part: str) -> tuple[bool, str]:
    """Return (is_city, canonical_name) for a single address part."""
    norm = _norm(part)
    # Strip common city prefixes first: "TP.", "Tp", "Thành phố", "Tỉnh".
    norm = re.sub(r"^(?:thành phố|thanh pho|tỉnh|tinh|tp\.?|tp)\s*", "", norm).strip()
    if not norm:
        return False, ""
    # Folded lookup — seed aliases are keyed on accent-free forms, so
    # both "hà nội" and "ha noi" resolve to the same canonical name.
    norm = fold_diacritics(norm)
    for alias, canon in _CITY_LONG_ALIASES.items():
        if alias in norm:
            return True, canon
    for alias, canon in _CITY_SHORT_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", norm):
            return True, canon
    return False, ""


# ── Segment classifiers ───────────────────────────────────────────────────────

_DISTRICT_RE = re.compile(
    r"^(?:quận|quan|huyện|huyen|thị xã|thi xa|thị trấn|thi tran|q\.)\s*",
    re.IGNORECASE,
)
_WARD_RE = re.compile(
    r"^(?:phường|phuong|xã|xa|thị trấn|thi tran|p\.)\s*",
    re.IGNORECASE,
)

_HOUSE_RE = re.compile(
    r"^(?:(?:số|so|nhà|nha|no)\s+)?(#\s*)?(\d+[a-zA-Z]?(?:\s*[/\-]\s*\d+[a-zA-Z]?)*)",
    re.IGNORECASE,
)
_LANE_RE = re.compile(
    r"^(?:(?:ngõ|ngo|hẻm|hem|kiệt|kiet|ngách|ngach|đường số|duong so)\s+)"
    r"(\d+[a-zA-Z]?)\b",
    re.IGNORECASE,
)
_STREET_PREFIX_RE = re.compile(
    r"^(?:(?:đường|duong|phố|pho|đại lộ|dai lo|quốc lộ|quoc lo|tỉnh lộ|tinh lo|"
    r"lộ|lo|đê|de)\s+)",
    re.IGNORECASE,
)


def _title(text: str) -> str:
    """Per-word title case (first letter upper).

    Words containing digits keep their letters uppercased (e.g. "12A",
    "3/2"); pure words are lowercased after the first letter.
    """
    out = []
    for w in (text or "").split():
        if not w:
            continue
        if any(ch.isdigit() for ch in w):
            out.append("".join(ch.upper() if ch.isalpha() else ch for ch in w))
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _strip_prefix(part: str, regex: re.Pattern) -> str:
    m = regex.match(part)
    if m:
        return _title(part[m.end() :])
    return ""


def _parse_head(segment: str) -> tuple[str, str, str]:
    """Parse the leading segment → (house_number, lane, street)."""
    house, lane, street = "", "", ""
    t = segment.strip()

    # House number: "Số 12", "12A", "Nhà 5", "12/3B".
    m = _HOUSE_RE.match(t)
    if m:
        house = re.sub(r"\s+", "", m.group(2))
        t = t[m.end() :].strip()

    # Lane: "Ngõ 45", "Hẻm 12", "Kiệt 2/3", "Đường số 5".
    m = _LANE_RE.match(t)
    if m:
        lane = _title(re.sub(r"\s+", " ", m.group(0)).strip())
        t = t[m.end() :].strip()

    # Street: "Trần Duy Hưng", "Đường 3/2", "Phố Hàng Bạc".
    m = _STREET_PREFIX_RE.match(t)
    if m:
        prefix = m.group(0).strip()
        rest = t[m.end() :].strip()
        street = _title(f"{prefix} {rest}") if rest else _title(prefix)
    elif t:
        street = _title(t)

    return house, lane, street


# ── Public API ────────────────────────────────────────────────────────────────


def normalize_address(text: str) -> str:
    """Normalize a Vietnamese address string.

    Collapses whitespace, fixes comma spacing, lowercases then applies
    per-word Title case.
    """
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"\s*-\s*", "-", t)
    return _title(t)


def parse_vn_address(text: str) -> VNAddress:
    """Parse a free-form Vietnamese address into a ``VNAddress`` dataclass.

    Deterministic, regex-based. Missing components (no ward/district, no
    house number, ...) parse gracefully to empty fields.
    """
    full = normalize_address(text)
    parts = [p.strip() for p in re.split(r"[,\n;]", text or "") if p.strip()]

    addr = VNAddress(full=full)
    if not parts:
        return addr

    tail = list(parts)

    # City (scan all parts, rightmost first) — supports "TP. HCM, Quận 1"
    # where the city appears before the district segment.
    for i in range(len(tail) - 1, -1, -1):
        is_city, canon = _match_city(tail[i])
        if is_city:
            addr.city = canon
            tail.pop(i)
            break

    # District ("quận Cầu Giấy", "Q. Ninh Kiều", "huyện ...").
    for i in range(len(tail) - 1, -1, -1):
        district = _strip_prefix(tail[i], _DISTRICT_RE)
        if district:
            addr.district = district
            tail.pop(i)
            break

    # Ward ("phường Trung Hòa", "P. Xuân Khánh", "xã ...").
    for i in range(len(tail) - 1, -1, -1):
        ward = _strip_prefix(tail[i], _WARD_RE)
        if ward:
            addr.ward = ward
            tail.pop(i)
            break

    # Unprefixed tail parts: assign leftover names to ward/district by position.
    if len(tail) > 1 and not addr.district:
        addr.district = _title(tail[-1])
        tail = tail[:-1]
    if len(tail) > 1 and not addr.ward:
        addr.ward = _title(tail[-1])
        tail = tail[:-1]

    # Head segment → house_number / lane / street.
    if tail:
        house, lane, street = _parse_head(tail[0])
        addr.house_number = house
        addr.lane = lane
        addr.street = street

    return addr


_ADDRESS_KEYWORD_RE = re.compile(
    r"(?i)\b(phường|phuong|xã|xa|quận|quan|huyện|huyen|thành phố|thanh pho|tỉnh|"
    r"tinh|ngõ|ngo|hẻm|hem|kiệt|kiet|đường số|duong so|p\.|q\.|tp\.?)\b"
)


def looks_like_vn_address(text: str) -> bool:
    """Heuristic check: looks like a Vietnamese address.

    True when the text parses to at least two address components, or when
    it contains a ward/district/city keyword (phường/quận/huyện/tỉnh, ...).
    """
    if not text or not text.strip():
        return False
    parsed = parse_vn_address(text)
    components = sum(
        1
        for f in (
            parsed.house_number,
            parsed.lane,
            parsed.street,
            parsed.ward,
            parsed.district,
            parsed.city,
        )
        if f
    )
    if components >= 2:
        return True
    return bool(_ADDRESS_KEYWORD_RE.search(text))
