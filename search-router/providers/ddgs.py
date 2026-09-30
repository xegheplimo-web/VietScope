"""DDGS search provider — no-API-key metasearch fallback (Bing, Brave, DDG, Google, …)."""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from canonical.url import canonical_url
from models import (
    RetrievalObservation,
    SearchCategory,
    SearchResultItem,
    _utc_now_iso,
    result_fingerprint,
)

from providers.searxng import VALID_TIME_RANGES, _dedupe

_DDGS_AVAILABLE = False
DDGS: Any = None

try:
    from ddgs import DDGS as _DDGS

    _DDGS_AVAILABLE = True
    DDGS = _DDGS
except ImportError:  # pragma: no cover - keep provider importable if ddgs missing
    pass

logger = logging.getLogger(__name__)

# DDGS supports d/w/m/y timelimit values.
_DDGS_TIME_RANGE = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}

# Map ISO-639-1 language codes to DDGS region tags (region-language).
# Fallback is "wt-wt" when the language is not in the map.
_LANG_REGION = {
    "ar": "ae-ar",
    "cs": "cz-cs",
    "da": "dk-da",
    "de": "de-de",
    "el": "gr-el",
    "en": "us-en",
    "es": "es-es",
    "fi": "fi-fi",
    "fr": "fr-fr",
    "he": "il-he",
    "hi": "in-hi",
    "hu": "hu-hu",
    "id": "id-id",
    "it": "it-it",
    "ja": "jp-jp",
    "ko": "kr-kr",
    "ms": "my-ms",
    "nl": "nl-nl",
    "no": "no-no",
    "pl": "pl-pl",
    "pt": "pt-pt",
    "ro": "ro-ro",
    "ru": "ru-ru",
    "sk": "sk-sk",
    "sv": "se-sv",
    "th": "th-th",
    "tr": "tr-tr",
    "uk": "ua-uk",
    "vi": "vn-vn",
    "zh": "cn-zh",
}

# Map SearchCategory -> DDGS search method. Only text/news/images/videos exist;
# the rest fall back to text.
_DDGS_CATEGORY = {
    SearchCategory.general: "text",
    SearchCategory.news: "news",
    SearchCategory.images: "images",
    SearchCategory.videos: "videos",
    SearchCategory.it: "text",
    SearchCategory.science: "text",
    SearchCategory.files: "text",
    SearchCategory.social_media: "text",
}


def _normalize_category(c: SearchCategory) -> str:
    return _DDGS_CATEGORY.get(c, "text")


def _lang_region(lang: str) -> str:
    """Return the DDGS region tag closest to the requested language."""
    return _LANG_REGION.get(lang.lower(), "wt-wt")


def _thumbnail(raw: dict) -> str | None:
    """Extract a thumbnail URL from a DDGS result, coercing non-strings to None."""
    for key in ("thumbnail", "image", "images", "icon"):
        value = raw.get(key)
        if isinstance(value, str):
            return value or None
        if value is not None and not isinstance(value, str):
            logger.warning(
                "DDGS result %r has non-string thumbnail at %r: %r",
                raw.get("title", "???"),
                key,
                value,
            )
    return None


def _build_observation(
    provider: str, engine: str, rank: int, retrieved_at: str
) -> RetrievalObservation:
    return RetrievalObservation(
        provider=provider, engine=engine, rank=rank, retrieved_at=retrieved_at
    )


def _parse_text(r: dict, rank: int = 1, retrieved_at: str = "") -> SearchResultItem:
    # DDGS does not expose the upstream engine; we record the provider as ddgs.
    url = r.get("href") or r.get("url") or ""
    title = r.get("title") or ""
    description = r.get("body") or ""
    c_url = canonical_url(url)
    return SearchResultItem(
        url=url,
        canonical_url=c_url,
        title=title,
        description=description,
        fingerprint=result_fingerprint(c_url, title, description),
        score=0.0,
        engine=r.get("source") or "ddgs",
        category="text",
        published_date=None,
        thumbnail=_thumbnail(r),
        retrieval_observations=[_build_observation("ddgs", "ddgs", rank, retrieved_at)],
    )


def _parse_news(r: dict, rank: int = 1, retrieved_at: str = "") -> SearchResultItem:
    url = r.get("url") or r.get("href") or ""
    title = r.get("title") or ""
    description = r.get("body") or ""
    c_url = canonical_url(url)
    return SearchResultItem(
        url=url,
        canonical_url=c_url,
        title=title,
        description=description,
        fingerprint=result_fingerprint(c_url, title, description),
        score=0.0,
        engine=r.get("source") or "ddgs",
        category="news",
        published_date=r.get("date"),
        thumbnail=_thumbnail(r),
        retrieval_observations=[_build_observation("ddgs", "ddgs", rank, retrieved_at)],
    )


def _parse_images(r: dict, rank: int = 1, retrieved_at: str = "") -> SearchResultItem:
    url = r.get("url") or r.get("href") or r.get("image") or ""
    title = r.get("title") or ""
    description = r.get("source") or ""
    c_url = canonical_url(url)
    return SearchResultItem(
        url=url,
        canonical_url=c_url,
        title=title,
        description=description,
        fingerprint=result_fingerprint(c_url, title, description),
        score=0.0,
        engine=r.get("source") or "ddgs",
        category="images",
        published_date=None,
        thumbnail=_thumbnail(r),
        retrieval_observations=[_build_observation("ddgs", "ddgs", rank, retrieved_at)],
    )


def _parse_videos(r: dict, rank: int = 1, retrieved_at: str = "") -> SearchResultItem:
    url = r.get("url") or r.get("href") or r.get("embed_url") or ""
    title = r.get("title") or ""
    description = r.get("description") or r.get("content") or ""
    c_url = canonical_url(url)
    return SearchResultItem(
        url=url,
        canonical_url=c_url,
        title=title,
        description=description,
        fingerprint=result_fingerprint(c_url, title, description),
        score=0.0,
        engine=r.get("source") or "ddgs",
        category="videos",
        published_date=None,
        thumbnail=_thumbnail(r),
        retrieval_observations=[_build_observation("ddgs", "ddgs", rank, retrieved_at)],
    )


_PARSERS = {
    "text": _parse_text,
    "news": _parse_news,
    "images": _parse_images,
    "videos": _parse_videos,
}


def _parse_results(raw: Iterable[dict], category: str) -> list[SearchResultItem]:
    parser = _PARSERS.get(category, _parse_text)
    results: list[SearchResultItem] = []
    retrieved_at = _utc_now_iso()
    for rank, r in enumerate(raw, start=1):
        if not r:
            continue
        try:
            results.append(parser(r, rank=rank, retrieved_at=retrieved_at))
        except Exception as exc:
            logger.warning("DDGS %s parser dropped a result: %s", category, exc)
    return results


def _search_sync(
    category: str,
    query: str,
    max_results: int,
    safesearch: str,
    timelimit: str | None,
    lang: str = "en",
) -> tuple[list[SearchResultItem], str | None]:
    if not _DDGS_AVAILABLE or DDGS is None:
        logger.warning("ddgs library is not installed; returning empty result")
        return [], "ddgs library not installed"

    try:
        with DDGS(timeout=10) as client:
            raw = getattr(client, category)(
                query=query,
                region=_lang_region(lang),
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=max_results,
                backend="auto",
            )
            return _parse_results(raw, category), None
    except Exception as exc:
        logger.warning("ddgs %s search failed for %r: %s", category, query, exc)
        return [], f"{type(exc).__name__}: {exc}"


async def ddgs_search(
    query: str,
    categories: list[SearchCategory] | None = None,
    max_results: int = 10,
    lang: str = "en",
    safe: bool = False,
    time_range: str | None = None,
    call_error: list[str] | None = None,
) -> list[SearchResultItem]:
    """Search via DDGS. Async wrapper around the sync ddgs library."""
    if not query:
        return []

    if time_range is not None and time_range not in VALID_TIME_RANGES:
        raise ValueError(
            f"invalid time_range: {time_range!r} "
            f"(expected one of {sorted(VALID_TIME_RANGES)} or None)"
        )

    cats = list(categories or [SearchCategory.general])
    methods: list[str] = []
    seen: set[str] = set()
    for c in cats:
        m = _normalize_category(c)
        if m not in seen:
            methods.append(m)
            seen.add(m)
    if not methods:
        methods = ["text"]

    timelimit = _DDGS_TIME_RANGE.get(time_range) if time_range else None
    safesearch = "on" if safe else "off"

    all_results: list[SearchResultItem] = []
    errors: list[str] = []
    for method in methods:
        results, err = await asyncio.to_thread(
            _search_sync,
            method,
            query,
            max_results,
            safesearch,
            timelimit,
            lang,
        )
        if err:
            errors.append(err)
        all_results.extend(results)
    if call_error is not None and errors:
        call_error.append(errors[0])

    all_results = _dedupe(all_results)
    return all_results[:max_results]


async def ddgs_health() -> bool:
    """DDGS needs no API key; configured if the library is importable."""
    return _DDGS_AVAILABLE
