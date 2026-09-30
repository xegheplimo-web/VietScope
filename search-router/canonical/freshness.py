"""Metadata freshness scoring and query cache-lifetime hints."""

import contextlib
import json
import re
import unicodedata
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from email.utils import parsedate_to_datetime
from typing import Any

_UPDATED_KEYS = {
    "articlemodifiedtime",
    "datemodified",
    "lastmodified",
    "modifiedat",
    "ogupdatedtime",
    "updatedat",
    "updatedtime",
}
_PUBLISHED_KEYS = {
    "articlepublishedtime",
    "datepublished",
    "ogpublishedtime",
    "publicationdate",
    "publishedat",
    "publisheddate",
    "publishedtime",
}
_FIRST_SEEN_KEYS = {"firstseen", "firstseenat"}
_JSON_LD_KEYS = {"jsonld", "ldjson", "structureddata"}
_NEW_HASH_FLAGS = {
    "contentchanged",
    "contenthashisnew",
    "contenthashnew",
    "iscontenthashnew",
    "isnewcontent",
}
_CURRENT_HASH_KEYS = {"contenthash", "currentcontenthash", "latestcontenthash"}
_PREVIOUS_HASH_KEYS = {
    "lastcontenthash",
    "oldcontenthash",
    "previouscontenthash",
    "priorcontenthash",
}


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _metadata_items(value: Any) -> Iterator[tuple[str, Any]]:
    """Yield normalized metadata keys, including meta-tag and JSON-LD forms."""

    seen: set[int] = set()

    def walk(node: Any, parent_key: str = "") -> Iterator[tuple[str, Any]]:
        if isinstance(node, (dict, list)):
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)

        if isinstance(node, dict):
            tag_name = node.get("property") or node.get("name") or node.get("itemprop")
            if tag_name and "content" in node:
                yield _key(tag_name), node["content"]

            for raw_key, child in node.items():
                normalized_key = _key(raw_key)
                if not isinstance(child, (dict, list)):
                    yield normalized_key, child
                if normalized_key in _JSON_LD_KEYS and isinstance(child, str):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        child = json.loads(child)
                yield from walk(child, normalized_key)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child, parent_key)

    yield from walk(value)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _has_new_content_hash(items: list[tuple[str, Any]]) -> bool:
    current_hashes: list[str] = []
    previous_hashes: list[str] = []
    for key, value in items:
        if key in _NEW_HASH_FLAGS and (
            value is True or str(value).strip().casefold() in {"1", "true", "new", "changed", "yes"}
        ):
            return True
        if key in _CURRENT_HASH_KEYS and value:
            current_hashes.append(str(value))
        elif key in _PREVIOUS_HASH_KEYS and value:
            previous_hashes.append(str(value))
    return bool(current_hashes and previous_hashes and current_hashes[-1] != previous_hashes[-1])


def _age_score(age_days: float) -> float:
    if age_days <= 1:
        return 0.95
    if age_days <= 7:
        return 0.90
    if age_days <= 30:
        return 0.80
    if age_days <= 90:
        return 0.65
    if age_days <= 180:
        return 0.55
    if age_days <= 365:
        return 0.40
    if age_days <= 730:
        return 0.25
    return 0.10


def freshness_score(doc_meta: dict) -> float:
    """Score document freshness in the inclusive range ``0.0`` to ``1.0``.

    Modified and published dates take precedence over ``first_seen_at`` so a
    newly crawled old article is not treated as newly published. Recognized
    dates may appear directly, in nested JSON-LD, or as HTML meta-tag records.
    Missing or invalid dates receive the neutral score ``0.5``. A confirmed
    changed content hash adds a small bonus.
    """

    if not isinstance(doc_meta, dict):
        return 0.5

    items = list(_metadata_items(doc_meta))
    primary_dates: list[datetime] = []
    first_seen_dates: list[datetime] = []
    for key, value in items:
        parsed = _parse_datetime(value)
        if parsed is None:
            continue
        if key in _UPDATED_KEYS or key in _PUBLISHED_KEYS:
            primary_dates.append(parsed)
        elif key in _FIRST_SEEN_KEYS:
            first_seen_dates.append(parsed)

    dates = primary_dates or first_seen_dates
    if dates:
        now = datetime.now(UTC)
        age_days = max(0.0, (now - max(dates)).total_seconds() / 86_400)
        score = _age_score(age_days)
    else:
        score = 0.5

    if _has_new_content_hash(items):
        score += 0.1
    return min(1.0, max(0.0, score))


def _fold_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKD", query.casefold()).replace("đ", "d")
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_marks).split())


def _contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in phrases)


def query_ttl_hint(query: str) -> str:
    """Suggest a coarse cache TTL class from Vietnamese or English intent."""

    if not isinstance(query, str):
        return "days"
    text = _fold_query(query)
    if not text:
        return "days"

    # Local availability is useful for hours, even when a user adds "now".
    if _contains_phrase(
        text,
        (
            "dang mo",
            "gan day",
            "gan toi",
            "gio mo cua",
            "local",
            "mo cua",
            "near me",
            "open now",
            "quanh day",
        ),
    ):
        return "hours"
    if _contains_phrase(
        text,
        (
            "bay gio",
            "breaking",
            "current",
            "gio",
            "hien tai",
            "hom nay",
            "latest",
            "moi nhat",
            "news",
            "now",
            "tin moi",
            "tin tuc",
            "today",
            "tonight",
            "vua ra mat",
        ),
    ):
        return "minutes"
    if _contains_phrase(
        text,
        (
            "api reference",
            "cach su dung",
            "cach dung",
            "cu phap",
            "documentation",
            "how to",
            "huong dan",
            "syntax",
            "tutorial",
        ),
    ):
        return "static"
    return "days"
