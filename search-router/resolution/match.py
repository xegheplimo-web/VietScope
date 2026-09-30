"""P16 pairwise matching — deterministic weighted scorer.

Score = Σ w·s over name / geo / phone / website / category. Two hard
rules override the weighted score (same normalized name within 80 m, or
identical normalized phone) — a phone number is a stronger identity
signal than any weighted sum. Neither rule may fire when both sides carry
coordinates farther apart than _GEO_FAR_M: pins that distant contradict
the same-place hypothesis (chain branches share hotlines and brand
names), so far-apart sightings score geo as *negative* evidence instead
of neutral.

Normalized comparisons only: callers pass ``NormSource``/``PlaceNorm``
views built by ``resolution.normalize``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MERGE_THRESHOLD = 0.62
_AUTO_NAME_M = 80.0  # same normalized name inside this radius ⇒ merge
_GEO_NEAR_M = 30.0
_GEO_FAR_M = 200.0

WEIGHTS = {"name": 0.45, "geo": 0.25, "phone": 0.15, "website": 0.10, "category": 0.05}

RESOLVER_VERSION = "p16-v2"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geo_score(dist_m: float | None) -> float:
    if dist_m is None:
        return 0.4  # either side unlocated — neutral
    if dist_m <= _GEO_NEAR_M:
        return 1.0
    if dist_m >= _GEO_FAR_M:
        return -1.0  # contradicts same-place, not merely uninformative
    return 1.0 - (dist_m - _GEO_NEAR_M) / (_GEO_FAR_M - _GEO_NEAR_M)


def name_score(
    norm_a: str, tokens_a: frozenset[str], norm_b: str, tokens_b: frozenset[str]
) -> float:
    if not norm_a or not norm_b:
        return 0.3
    if norm_a == norm_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.3
    inter = len(tokens_a & tokens_b)
    if inter == 0:
        return 0.0
    jaccard = inter / len(tokens_a | tokens_b)
    containment = inter / min(len(tokens_a), len(tokens_b))
    return max(jaccard, containment * 0.9)


def phone_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.3
    return 1.0 if a == b else 0.0


def website_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.3
    return 1.0 if a == b else 0.0


@dataclass
class NormSource:
    """Normalized view of one place_source_records row."""

    record_id: int
    provider: str
    external_id: str | None
    norm_name: str
    name_sig: str
    tokens: frozenset[str]
    norm_address: str
    phone: str | None
    domain: str | None
    category: str | None  # canonical bucket
    lat: float | None
    lon: float | None
    admin_unit_id: int | None
    observed_at: Any
    fields: dict[str, Any]  # {field: value} for provenance writes


@dataclass
class PlaceNorm:
    """Normalized view of a canonical place (from store)."""

    place_id: int
    norm_name: str
    tokens: frozenset[str]
    phone: str | None
    domain: str | None
    category: str | None
    lat: float | None
    lon: float | None
    admin_unit_id: int | None


def score_pair(src: NormSource, place: PlaceNorm) -> tuple[float, str | None]:
    """(score, force_reason). force_reason is the fired hard rule —
    'name_geo' (same name inside 80 m) or 'phone' (identical phone
    without contradicting coordinates) — or None."""
    dist = None
    if (
        src.lat is not None
        and src.lon is not None
        and place.lat is not None
        and place.lon is not None
    ):
        dist = haversine_m(src.lat, src.lon, place.lat, place.lon)

    force = None
    if (
        src.norm_name
        and src.norm_name == place.norm_name
        and dist is not None
        and dist <= _AUTO_NAME_M
    ):
        force = "name_geo"
    elif (
        src.phone
        and place.phone
        and src.phone == place.phone
        and (dist is None or dist <= _GEO_FAR_M)
    ):
        force = "phone"
    s = (
        WEIGHTS["name"] * name_score(src.norm_name, src.tokens, place.norm_name, place.tokens)
        + WEIGHTS["geo"] * geo_score(dist)
        + WEIGHTS["phone"] * phone_score(src.phone, place.phone)
        + WEIGHTS["website"] * website_score(src.domain, place.domain)
        + WEIGHTS["category"] * _cat(src.category, place.category)
    )
    return (min(1.0, s), force)


def _cat(a: str | None, b: str | None) -> float:
    from resolution.normalize import categories_related

    return categories_related(a, b)
