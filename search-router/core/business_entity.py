"""Business entity extraction from web content.

Provides both an LLM-driven structured extraction path and a deterministic
regex fallback that tolerates Vietnamese text. The entry points never raise.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

from models import BusinessEntity

from core.entity_resolver import fold

logger = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore(4)

# Vietnamese + common locale terms for contact/address blocks.
_ADDRESS_PREFIXES = re.compile(
    r"(?:địa\s*chỉ|address|located\s*at|địa\s*điểm|chỗ|nằm\s*tại)\s*[:\-]?\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.UNICODE,
)

_PHONE_RE = re.compile(
    r"(?:\+84\s?\d{8,10}"
    r"|\+84\s?\(?\d{1,3}\)?\s?\d{3}\s?\d{3}\s?\d{3}"
    r"|0\d{2}\s?\d{3,4}\s?\d{3,4}"
    r"|0\d{3}\s?\d{3}\s?\d{3})"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_WEBSITE_RE = re.compile(r"https?://[^\s\"\)\]<>]+", re.IGNORECASE)

_RATING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*5\b")

_HOURS_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})")

_COORDS_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")

_BUSINESS_CATEGORIES = {
    "cafe": "cafe",
    "coffee": "cafe",
    "quán cà phê": "cafe",
    "cà phê": "cafe",
    "quán ăn": "restaurant",
    "nhà hàng": "restaurant",
    "restaurant": "restaurant",
    "quán phở": "restaurant",
    "quán cơm": "restaurant",
    "quán bún": "restaurant",
    "quán nhậu": "bar",
    "bar": "bar",
    "khách sạn": "hotel",
    "hotel": "hotel",
    "nhà nghỉ": "guest_house",
    "siêu thị": "supermarket",
    "supermarket": "supermarket",
    "tạp hóa": "convenience",
    "cửa hàng tiện lợi": "convenience",
    "cửa hàng": "store",
    "shop": "store",
    "store": "store",
    "chợ": "marketplace",
    "nhà thuốc": "pharmacy",
    "hiệu thuốc": "pharmacy",
    "pharmacy": "pharmacy",
    "phòng khám": "clinic",
    "trạm y tế": "clinic",
    "bệnh viện": "hospital",
    "nha khoa": "dentist",
    "thú y": "veterinary",
    "ngân hàng": "bank",
    "bank": "bank",
    "atm": "bank",
    "trạm xăng": "fuel",
    "cây xăng": "fuel",
    "trường học": "school",
    "đại học": "university",
    "salon": "hairdresser",
    "spa": "spa",
    "quán": "restaurant",
}

# Category → OSM tag as (key, value): value=None means "any value of the
# key" (Overpass ``["shop"]``, SQL ``shop IS NOT NULL``). Categories
# outside the map fall back to the generic ``("amenity", None)`` filter.
_OSM_TAG = {
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "bar": ("amenity", "bar"),
    "pharmacy": ("amenity", "pharmacy"),
    "clinic": ("amenity", "clinic"),
    "hospital": ("amenity", "hospital"),
    "dentist": ("amenity", "dentist"),
    "veterinary": ("amenity", "veterinary"),
    "bank": ("amenity", "bank"),
    "fuel": ("amenity", "fuel"),
    "school": ("amenity", "school"),
    "university": ("amenity", "university"),
    "marketplace": ("amenity", "marketplace"),
    "supermarket": ("shop", "supermarket"),
    "convenience": ("shop", "convenience"),
    "store": ("shop", None),
    "hotel": ("tourism", "hotel"),
    "guest_house": ("tourism", "guest_house"),
    "hairdresser": ("shop", "hairdresser"),
}

# Folded markers so accentless text ("nha thuoc", "cay xang") classifies
# into the same taxonomy — mirrors the accent-folded matching contract of
# the local-discovery path.
_FOLDED_BUSINESS_CATEGORIES = tuple(
    (fold(marker), cat) for marker, cat in _BUSINESS_CATEGORIES.items()
)


_EXAMPLE_JSON = """
{
  "name": "Phở Hòa Pasteur",
  "category": "restaurant",
  "address": "260C Pasteur, Phường 8, Quận 3, TP. Hồ Chí Minh",
  "phone": "028 3829 2083",
  "hours": "06:00 - 23:00",
  "rating": 4.2,
  "price_level": "$$",
  "website": "https://phohoa.com",
  "lat": 10.7818,
  "lon": 106.6862,
  "description": "Famous phở restaurant in Ho Chi Minh City."
}
""".strip()


async def extract_business(
    content: str,
    query: str,
    llm=None,
    source_url: str = "",
) -> BusinessEntity | None:
    """Extract a single business entity from ``content``.

    If ``llm`` is configured and has an API key, an LLM is asked to produce a
    JSON object matching the BusinessEntity schema. Otherwise, deterministic
    regex heuristics are used. Returns ``None`` when nothing meaningful can be
    extracted or on any error.
    """
    if not content or not content.strip():
        return None
    try:
        if llm is not None and getattr(llm, "api_key", None):
            entity = await _extract_with_llm(content, query, llm)
            if entity is not None:
                if not entity.source_url:
                    entity.source_url = source_url
                return entity
        return _extract_with_regex(content, query, source_url=source_url)
    except Exception as exc:
        logger.warning("extract_business failed: %s", exc)
        return None


async def extract_business_batch(
    contents: Iterable[str],
    query: str,
    llm=None,
    source_urls: Iterable[str] | None = None,
) -> list[BusinessEntity]:
    """Extract business entities from multiple content items in parallel."""
    contents = list(contents)
    urls = list(source_urls or [""] * len(contents))
    if len(urls) < len(contents):
        urls.extend([""] * (len(contents) - len(urls)))

    async def _one(item: tuple[str, str]) -> BusinessEntity | None:
        text, url = item
        async with _SEMAPHORE:
            return await extract_business(text, query, llm=llm, source_url=url)

    results = await asyncio.gather(*(_one((c, u)) for c, u in zip(contents, urls, strict=False)))
    return [r for r in results if r is not None]


async def _extract_with_llm(content: str, query: str, llm) -> BusinessEntity | None:
    schema_hint = (
        "BusinessEntity with fields: name (string), category (string), "
        "address (string), phone (string or null), hours (string or null), "
        "rating (number 0-5 or null), price_level (string or null), "
        "website (string or null), lat (number or null), lon (number or null), "
        "description (string), and no markdown."
    )
    user_prompt = (
        "Extract the business information from the following text about a local "
        f"place in Vietnam matching the query '{query}'. "
        "Return a single JSON object with these fields. Use null for missing data.\n\n"
        f"Example:\n{_EXAMPLE_JSON}\n\n"
        f"Text:\n{content[:6000]}"
    )
    try:
        data = await llm.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You extract structured local business data. "
                        "Return ONLY a JSON object. No markdown, no explanation."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            schema_hint=schema_hint,
            max_tokens=1024,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("LLM business extraction failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    try:
        return _dict_to_entity(data)
    except Exception:
        return None


def _extract_with_regex(content: str, query: str, source_url: str = "") -> BusinessEntity | None:
    text = content.strip()
    if not text:
        return None

    name = _extract_name(text, query)
    address = _extract_address(text)
    phone = _first_match(_PHONE_RE, text)
    email = _first_match(_EMAIL_RE, text)
    website = _extract_website(text, source_url)
    rating = _extract_rating(text)
    hours = _extract_hours(text)
    category = _infer_category(text, query)
    lat, lon = _extract_coords(text)
    description = _extract_description(text)

    # If we cannot identify at least a name and some business signal, skip.
    if not name and not phone and not website and not email and not address:
        return None
    if not name:
        name = query.strip() or "Unknown"

    return BusinessEntity(
        name=name,
        category=category,
        address=address,
        phone=phone,
        hours=hours,
        rating=rating,
        price_level=None,
        website=website,
        lat=lat,
        lon=lon,
        description=description,
        source_url=source_url,
    )


def _extract_name(text: str, query: str) -> str:
    # Look for a short first heading or line that is not metadata.
    for line in text.splitlines()[:5]:
        line = line.strip().strip("#*-=–")
        if not line:
            continue
        lower = line.lower()
        if any(lower.startswith(p) for p in ("địa chỉ", "address", "phone", "tel", "email")):
            continue
        if 3 <= len(line) <= 120:
            return line
    # Fall back to a quoted name in the text.
    m = re.search(r'["\']([^"\']{3,80})["\']', text)
    if m:
        return m.group(1).strip()
    return query.strip() or ""


def _extract_address(text: str) -> str:
    m = _ADDRESS_PREFIXES.search(text)
    if m:
        addr = m.group(1).strip()
        # Truncate at the end of the first sentence or line.
        addr = re.split(r"[\n;]|(?<=[.])\s+", addr, maxsplit=1)[0]
        return addr.strip()
    return ""


def _extract_website(text: str, source_url: str) -> str | None:
    urls = _WEBSITE_RE.findall(text)
    for url in urls:
        if source_url and url == source_url:
            continue
        # Skip common social media tracking; otherwise keep first non-source URL.
        return url
    return None


def _extract_rating(text: str) -> float | None:
    m = _RATING_RE.search(text)
    if not m:
        return None
    try:
        return max(0.0, min(5.0, float(m.group(1))))
    except ValueError:
        return None


def _extract_hours(text: str) -> str | None:
    m = _HOURS_RE.search(text)
    if m:
        return f"{m.group(1)} - {m.group(2)}"
    return None


def _extract_coords(text: str) -> tuple[float | None, float | None]:
    for m in _COORDS_RE.finditer(text):
        try:
            lat = float(m.group(1))
            lon = float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                # Reject obviously swapped coords by requiring lat near query.
                return lat, lon
        except ValueError:
            continue
    return None, None


def _infer_category(text: str, query: str) -> str:
    combined = f"{query} {text[:500]}".lower()
    for marker, cat in _BUSINESS_CATEGORIES.items():
        if marker in combined:
            return cat
    folded = fold(combined)
    for marker, cat in _FOLDED_BUSINESS_CATEGORIES:
        if marker in folded:
            return cat
    return ""


def category_for(query: str) -> str:
    """Public: classify a business/places query into the VN taxonomy."""
    return _infer_category("", query)


def osm_tag_for(query: str) -> str:
    """OSM tag filter for a query's inferred category (Overpass syntax)."""
    key, value = _OSM_TAG.get(category_for(query), ("amenity", None))
    return f'["{key}"="{value}"]' if value else f'["{key}"]'


def osm_tag_kv(query: str) -> tuple[str, str | None]:
    """Category → (tag key, value) pair for the ``osm_pois`` SQL lane (P14).

    ``value`` of ``None`` means "any value under this key"."""
    return _OSM_TAG.get(category_for(query), ("amenity", None))


def _extract_description(text: str) -> str:
    # Use the first non-trivial paragraph as description.
    for para in text.split("\n\n"):
        para = para.strip()
        if para and len(para) > 20:
            return para[:500].strip()
    return text[:500].strip()


def _first_match(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0).strip() if m else None


def _dict_to_entity(data: dict) -> BusinessEntity:
    raw = dict(data.items())
    # Map common aliases.
    name = raw.get("name") or raw.get("title") or raw.get("business_name") or ""
    address = raw.get("address") or raw.get("location") or ""
    category = raw.get("category") or raw.get("type") or ""
    phone = raw.get("phone") or raw.get("telephone") or raw.get("contact")
    hours = raw.get("hours") or raw.get("opening_hours") or raw.get("open_hours")
    website = raw.get("website") or raw.get("url") or raw.get("web")
    description = raw.get("description") or raw.get("summary") or ""
    lat = _to_float(raw.get("lat")) if "lat" in raw else _to_float(raw.get("latitude"))
    lon = _to_float(raw.get("lon")) if "lon" in raw else _to_float(raw.get("longitude"))
    rating = _to_float(raw.get("rating"))
    price_level = raw.get("price_level") or raw.get("price_range") or None
    source_url = raw.get("source_url") or ""
    return BusinessEntity(
        name=name,
        category=category,
        address=address,
        phone=phone,
        hours=hours,
        rating=rating,
        price_level=price_level,
        website=website,
        lat=lat,
        lon=lon,
        description=description,
        source_url=source_url,
    )


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
