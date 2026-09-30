"""Intent-aware freshness routing for the SearXNG provider path.

Wave-8D. Lightweight heuristics that flag live / numeric / time-sensitive
queries so the provider can (a) request a recent ``time_range`` from SearXNG
and (b) widen into news categories. Kept in a separate module so it does not
collide with the orchestrator / query_understanding files Devin owns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VIETNAMESE_TONES = set("áàảãạăắằẳẵặâấầẩẫậđéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵ")

# "live now" markers → tightest time window (day).
LIVE_NOW_WORDS = ["today", "now", "tonight", "live"]
LIVE_NOW_PHRASES = [
    "hôm nay",
    "hom nay",
    "hôm qua",
    "hom qua",
    "hiện tại",
    "hien tai",
    "trực tiếp",
    "truc tiep",
    "mới nhất",
    "moi nhat",
]

# numeric / price markers → request a recent window (week).
# Explicit price/rate markers only — bare asset names ("bitcoin", "eth") are
# deliberately absent so that "what is bitcoin" stays evergreen while
# "what is bitcoin price" is treated as numeric.
NUMERIC_WORDS = [
    "price",
    "prices",
    "rate",
    "rates",
    "usd",
    "vnd",
    "stock",
    "stocks",
    "nasdaq",
    "dow",
]
NUMERIC_PHRASES = [
    "giá vàng",
    "gia vang",
    "giá heo",
    "gia heo",
    "giá xăng",
    "gia xang",
    "giá gạo",
    "gia gao",
    "giá bạc",
    "gia bac",
    "giá bitcoin",
    "gia bitcoin",
    "giá bán",
    "gia ban",
    "tỷ giá",
    "ty gia",
    "ngoại tệ",
    "ngoai te",
    "lãi suất",
    "lai suat",
    "chỉ số",
    "chi so",
    "cổ phiếu",
    "co phieu",
    "how much",
]

# result / event markers → recent window (week).
RESULT_WORDS = [
    "result",
    "results",
    "score",
    "scores",
    "match",
    "fixture",
    "fixtures",
    "schedule",
    "weather",
    "forecast",
    "epl",
    "kqxs",
]
RESULT_PHRASES = [
    "xổ số",
    "xo so",
    "kết quả",
    "ket qua",
    "bóng đá",
    "bong da",
    "lịch thi đấu",
    "lich thi dau",
    "thời tiết",
    "thoi tiet",
    "dự báo thời tiết",
    "du bao thoi tiet",
    "vé số",
    "ve so",
    "premier league",
    "world cup",
    "champions league",
]

# Evergreen / definitional markers — timeless queries that must NOT be narrowed
# by a recent ``time_range`` ("what is bitcoin", "bitcoin history",
# "API rate limiting").
EVERGREEN_WORDS = [
    "what",
    "whats",
    "history",
    "histories",
    "define",
    "definition",
    "definitions",
    "meaning",
    "meanings",
    "explain",
    "explains",
    "explanation",
    "explained",
    "tutorial",
    "tutorials",
    "guide",
    "guides",
    "overview",
    "introduction",
    "intro",
    "concept",
    "concepts",
]
EVERGREEN_PHRASES = [
    "là gì",
    "la gi",
    "lịch sử",
    "lich su",
    "định nghĩa",
    "dinh nghia",
    "khái niệm",
    "khai niem",
    "hướng dẫn",
    "huong dan",
    "giới thiệu",
    "gioi thieu",
    "so sánh",
    "so sanh",
    "khác nhau",
    "khac nhau",
    "rate limit",
    "rate limiting",
]

# Toneless Vietnamese markers (ASCII-only spellings) used to classify
# diacritic-free input like "gia heo" as Vietnamese rather than English.
_VI_TONELESS_MARKERS = [
    "hom nay",
    "hom qua",
    "hien tai",
    "truc tiep",
    "moi nhat",
    "gia vang",
    "gia heo",
    "gia xang",
    "gia gao",
    "gia bac",
    "gia bitcoin",
    "gia ban",
    "ty gia",
    "ngoai te",
    "lai suat",
    "chi so",
    "co phieu",
    "xo so",
    "ket qua",
    "bong da",
    "lich thi dau",
    "thoi tiet",
    "du bao thoi tiet",
    "ve so",
]

# Signal-specific widening suffixes — price terms only for numeric intent,
# time/result terms only for event intent.
_WIDEN_SUFFIXES = {
    "numeric": {
        "en": ["price", "rate", "quoted"],
        "vi": ["hôm nay", "mới nhất", "giá", "tỷ giá"],
    },
    "event": {
        "en": ["today", "latest", "results"],
        "vi": ["kết quả", "hôm nay", "mới nhất"],
    },
    "live": {
        "en": ["today", "latest", "now"],
        "vi": ["hôm nay", "mới nhất", "trực tiếp"],
    },
}


@dataclass
class FreshnessSignal:
    time_sensitive: bool = False
    time_range: str | None = None
    add_news: bool = False
    numeric: bool = False
    live: bool = False
    event: bool = False
    evergreen: bool = False
    language: str = "en"


def _matches_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def _matches_phrase(text: str, phrase: str) -> bool:
    return phrase in text


def _detect_language(q: str) -> str:
    if set(q) & VIETNAMESE_TONES:
        return "vi"
    if any(phrase in q for phrase in _VI_TONELESS_MARKERS):
        return "vi"
    return "en"


def _is_evergreen(q: str) -> bool:
    return any(_matches_word(q, w) for w in EVERGREEN_WORDS) or any(
        _matches_phrase(q, p) for p in EVERGREEN_PHRASES
    )


def _is_numeric(q: str) -> bool:
    if any(_matches_phrase(q, p) for p in NUMERIC_PHRASES):
        return True
    for w in NUMERIC_WORDS:
        if not _matches_word(q, w):
            continue
        # "rate" inside "rate limit(ing)" is a definitional concept, not a price.
        if w in ("rate", "rates") and "rate limit" in q:
            continue
        return True
    return False


def detect_live_numeric(query: str) -> FreshnessSignal:
    q = (query or "").lower().strip()
    if not q:
        return FreshnessSignal()
    language = _detect_language(q)

    live = any(_matches_word(q, w) for w in LIVE_NOW_WORDS) or any(
        _matches_phrase(q, p) for p in LIVE_NOW_PHRASES
    )
    numeric = _is_numeric(q)
    event = any(_matches_word(q, w) for w in RESULT_WORDS) or any(
        _matches_phrase(q, p) for p in RESULT_PHRASES
    )
    evergreen = _is_evergreen(q)

    if not (live or numeric or event):
        return FreshnessSignal(language=language, evergreen=evergreen)

    # A "now" marker always wins: even "what is bitcoin price today" is live.
    if live:
        return FreshnessSignal(
            time_sensitive=True,
            time_range="day",
            add_news=True,
            numeric=numeric,
            live=True,
            event=event,
            evergreen=evergreen,
            language=language,
        )

    # Explicit price/event markers win over a bare definitional "what"
    # ("what is bitcoin price" → week, "what is EPL result" → week). Evergreen
    # queries never reach here: "what is bitcoin" has no price/event marker
    # (bare asset names are absent from NUMERIC_WORDS) and returns early above.
    return FreshnessSignal(
        time_sensitive=True,
        time_range="week",
        add_news=True,
        numeric=numeric,
        event=event,
        language=language,
    )


def rephrase_for_widening(query: str, signal: FreshnessSignal | None = None) -> str | None:
    """Return a supplemental query for thin-result widening, or ``None``.

    Suffixes are intent-specific: price terms only for numeric queries,
    time/result terms only for event queries. Evergreen (non time-sensitive)
    queries are never widened.
    """
    signal = signal or detect_live_numeric(query)
    q = (query or "").strip()
    if not q or not signal.time_sensitive:
        return None

    if signal.live:
        family = "live"
    elif signal.event:
        family = "event"
    else:
        family = "numeric"

    suffixes = _WIDEN_SUFFIXES[family][signal.language]
    low = q.lower()
    for suffix in suffixes:
        if suffix not in low:
            return f"{q} {suffix}"
    return None
