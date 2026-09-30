"""Query Rewriter — expands a user query into search-engine-friendly variants.

One query rarely matches what every provider does best. The rewriter produces
2-3 *variants* of the same intent (the original first, always), so the Search
Orchestrator can fan them out to engines in parallel and the Evidence
Aggregator can merge + dedupe the combined recall.

Deterministic only (regex + query understanding, NO LLM in the hot path) —
rewriting must stay cheap and predictable.

Strategies (each only when the query calls for it):
- comparison: "A vs B / so sánh A và B" → also search each side separately.
- freshness/news: inject the current year when the query has none.
- definition: "X là gì / what is X" → add meaning/definition qualifiers.
- howto: "cách / how to" → add example/tutorial/guide qualifiers.
- code: add docs/example/API qualifiers for the detected language.
- Vietnamese queries are rewritten with the SAME diacritics (never stripped —
  tone marks are required for VN engine recall); only extra qualifiers are
  appended.
"""

from __future__ import annotations

import re
from datetime import datetime

try:
    from core.query_understanding import QueryUnderstanding

    _QU = QueryUnderstanding()
except Exception:  # pragma: no cover
    _QU = None


_MAX_VARIANTS = 3

_VI_TONES_RE = re.compile(
    r"[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịóòỏõọôốồổỗộơớờởỡợ"
    r"ùúủũụưứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)

_COMPARE_SPLIT_RE = re.compile(
    r"\b(vs\.?|versus|so sánh|so với|compare[d]?|khác nhau giữa)\b", re.IGNORECASE
)
_COMPARE_SEP_RE = re.compile(r"\b(vs\.?|versus|và|and|so với|so sánh|between)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_YEAR_HINT_RE = re.compile(
    r"\b(hôm nay|today|latest|new|mới nhất|tin nóng|breaking|năm nay|this year)\b",
    re.IGNORECASE,
)

_DEFINITION_RE = re.compile(
    r"\b(là gì|định nghĩa|nghĩa là|what is|what are|define|meaning of|explain)\b",
    re.IGNORECASE,
)
_HOWTO_RE = re.compile(
    r"\b(cách|làm thế nào|hướng dẫn|how to|how do i|tutorial)\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(
    r"\b(python|javascript|typescript|rust|golang|java|kotlin|swift|c\+\+|ruby|php|"
    r"sql|bash|react|vue|django|fastapi|flask|docker|kubernetes|api|endpoint|"
    r"middleware|function|async|decorator)\b",
    re.IGNORECASE,
)

_QUERY_WORDS = re.compile(
    r"\b(what|which|when|where|who|why|how|is|are|do|does|the|"
    r"a|an|of|to|for|in|on|at|và|của|là|gì|như|thế|nào|một)\b",
    re.IGNORECASE,
)


def _looks_vietnamese(query: str) -> bool:
    return bool(_VI_TONES_RE.search(query or ""))


def _language(query: str) -> str:
    if _looks_vietnamese(query):
        return "vi"
    return "en"


def _strip_query_noise(q: str) -> str:
    """Drop leading question words / filler that hurts keyword engines."""
    s = re.sub(
        r"^\s*(please|could you|can you|tell me|cho tôi hỏi|cho mình hỏi)\s+",
        "",
        q,
        flags=re.IGNORECASE,
    )
    return s.strip()


def _current_year() -> int:
    return datetime.now().year


def _is_compare(query: str) -> bool:
    return bool(_COMPARE_SPLIT_RE.search(query or ""))


def _split_sides(query: str) -> list[str]:
    """Return the two sides of a comparison when detectable."""
    q = _strip_query_noise(query or "")
    # Normalize Vietnamese/English connectives to 'vs' for a uniform split.
    q = re.sub(r"^\s*(so sánh|compare)\s+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\b(so sánh|compare|so với|và|and|between)\b", " vs ", q, flags=re.IGNORECASE)
    parts = [p.strip().strip("?.") for p in q.split("vs") if p.strip()]
    # Only treat as a real 2-side comparison if we found exactly two sides.
    if len(parts) == 2 and all(len(p) > 2 for p in parts):
        return parts
    return []


def rewrite_variants(
    query: str,
    *,
    family: str = "web",
    lang: str = "auto",
    max_variants: int | None = None,
) -> list[str]:
    """Produce 1-3 deterministic query variants (original always first)."""
    cap = min(max_variants or _MAX_VARIANTS, _MAX_VARIANTS)
    q = _strip_query_noise(query or "").strip()
    if not q:
        return [query or ""]

    lang = _language(q) if lang in ("auto", "") else lang
    variants: list[str] = [q]
    seen: set[str] = {q.lower()}

    def _add(v: str) -> None:
        v = re.sub(r"\s+", " ", v).strip(" ?.")
        key = v.lower()
        if v and key not in seen and len(variants) < cap:
            seen.add(key)
            variants.append(v)

    # 1. Comparison: search each side independently too (multi-query recall).
    if _is_compare(q):
        sides = _split_sides(q)
        for side in sides:
            _add(side)
        if len(variants) >= cap:
            return variants

    # 2. Definition → add qualifiers engines understand well.
    if _DEFINITION_RE.search(q) and not re.search(
        r"\b(meaning|definition|nghĩa là)\b", q, re.IGNORECASE
    ):
        _add(q + (" nghĩa là" if lang == "vi" else " meaning definition"))

    # 3. How-to → add example/tutorial/guide.
    if _HOWTO_RE.search(q) and not re.search(
        r"\b(example|tutorial|guide|ví dụ|hướng dẫn)\b", q, re.IGNORECASE
    ):
        if lang == "vi":
            _add(q + " ví dụ hướng dẫn")
        else:
            _add(q + " example tutorial")

    # 4. Code family → docs/API qualifier for the detected stack.
    if (family == "code" or _CODE_RE.search(q)) and not re.search(
        r"\b(docs|documentation|example|tutorial|tài liệu)\b", q, re.IGNORECASE
    ):
        code_qual = " docs example" if lang != "vi" else " tài liệu ví dụ"
        _add(q + code_qual)

    # 5. Freshness: inject current year when the query implies recency.
    if not _YEAR_RE.search(q) and (_YEAR_HINT_RE.search(q) or family == "news"):
        _add(q + f" {_current_year()}")

    return variants
