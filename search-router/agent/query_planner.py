"""Query Planner — LLM-driven sub-query generation (VI + EN).

The LLM proposes focused sub-queries; code owns the invariants (original
query first, schema validation, dedupe, per-mode cap, bilingual coverage).
A deterministic heuristic fallback keeps the pipeline alive when no LLM is
configured or the call fails.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core.inference_gateway import ModelRole
from pipeline.rag import llm_chat
from research_models.research_state import SearchIntent

# Depth → max queries (deterministic policy; the LLM never picks the count).
_DEPTH_MAX_QUERIES = {"none": 1, "quick": 3, "normal": 6, "deep": 10}

_VALID_LANGS = {"vi", "en", "auto"}
_VI_MARKS = re.compile(
    r"[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]",
    re.IGNORECASE,
)


@dataclass
class PlannedQuery:
    """One sub-query emitted by the planner."""

    query: str
    lang: str = "auto"  # vi | en | auto
    purpose: str = ""  # original | sub_question | entity | site_scoped | ...


@dataclass
class QueryPlan:
    """Planner output: ordered sub-queries plus provenance."""

    original: str
    queries: list[PlannedQuery] = field(default_factory=list)
    source: str = "heuristic"  # llm | heuristic

    @property
    def texts(self) -> list[str]:
        return [q.query for q in self.queries]

    @property
    def languages(self) -> set[str]:
        return {q.lang for q in self.queries if q.lang in ("vi", "en")}


def detect_lang(text: str) -> str:
    """Cheap language hint: Vietnamese tone marks → ``vi``, else ``en``."""
    return "vi" if _VI_MARKS.search(text or "") else "en"


def _max_queries_for(intent: SearchIntent, max_queries: int | None) -> int:
    if max_queries is not None:
        return max(1, max_queries)
    return _DEPTH_MAX_QUERIES.get(intent.depth, 6)


def _dedupe_keep_order(items: list[PlannedQuery]) -> list[PlannedQuery]:
    seen: set[str] = set()
    out: list[PlannedQuery] = []
    for item in items:
        key = item.query.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_llm_queries(raw: str) -> list[PlannedQuery] | None:
    """Validate the planner's JSON output into ``PlannedQuery`` objects.

    Accepts ``{"queries": [{"query", "lang", "purpose"}]}`` or a bare list of
    strings/objects. Returns ``None`` on any schema violation so callers can
    fall back to the heuristic path.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    entries = data.get("queries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return None

    parsed: list[PlannedQuery] = []
    for entry in entries:
        if isinstance(entry, str):
            text, lang, purpose = entry, "auto", ""
        elif isinstance(entry, dict):
            text = entry.get("query") or entry.get("q") or ""
            lang = str(entry.get("lang") or "auto").lower()
            purpose = str(entry.get("purpose") or "")
        else:
            continue
        text = str(text).strip()
        if len(text) < 2 or len(text) > 200:
            continue
        if lang not in _VALID_LANGS:
            lang = "auto"
        if lang == "auto":
            lang = detect_lang(text)
        parsed.append(PlannedQuery(query=text, lang=lang, purpose=purpose))

    return parsed if parsed else None


async def _llm_plan(query: str, intent: SearchIntent, limit: int) -> list[PlannedQuery] | None:
    """Ask the LLM for a bilingual sub-query plan; ``None`` on failure."""
    system_prompt = (
        "You are a search query planner. Decompose the user's question into "
        "focused web search queries that maximize recall. ALWAYS produce "
        "queries in BOTH Vietnamese and English when the topic benefits from "
        "both (local news, Vietnamese entities, comparisons); single-language "
        "plans are only acceptable for purely English or purely Vietnamese "
        "topics. Use entity pairs, site-scoped lookups, and topic-specific "
        "variants. Return ONLY a JSON object: "
        '{"queries": [{"query": "...", "lang": "vi|en", "purpose": "..."}]}. '
        "No prose, no explanation."
    )
    user_prompt = (
        f"Question: {query}\n"
        f"Depth: {intent.depth}\n"
        f"Max queries: {limit}\n"
        f"Categories: {', '.join(intent.categories)}\n"
    )
    if intent.preferred_domains:
        user_prompt += (
            "Preferred domains (use site: scoped queries): "
            + ", ".join(intent.preferred_domains[:3])
            + "\n"
        )
    if intent.freshness in ("day", "week"):
        user_prompt += "Prefer recent/time-sensitive sub-queries.\n"
    if intent.official_first:
        user_prompt += "Prioritize official / government sources.\n"

    raw = await llm_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=800,
        json_mode=True,
        role=ModelRole.PLANNER,
    )
    if not raw:
        return None
    return _parse_llm_queries(raw)


def _heuristic_plan(query: str, intent: SearchIntent) -> list[PlannedQuery]:
    """Deterministic fallback: entity/quoted/site-scoped variants."""
    lang = detect_lang(query)
    planned = [PlannedQuery(query=query.strip(), lang=lang, purpose="original")]

    quoted = re.findall(r'"([^"]+)"', query)
    words = query.split()
    entities = [w for i, w in enumerate(words) if i > 0 and w[0].isupper() and len(w) > 2]
    if len(entities) >= 2:
        planned.append(
            PlannedQuery(
                query=f'"{entities[0]}" "{entities[1]}"',
                lang=lang,
                purpose="entity_pair",
            )
        )

    topic_keywords = ["lừa đảo", "online", "vụ án", "tội phạm"]
    for entity in entities[:2]:
        for topic in topic_keywords:
            if topic in query:
                planned.append(
                    PlannedQuery(query=f'"{entity}" {topic}', lang=lang, purpose="topic")
                )

    if intent.official_first:
        for entity in quoted[:2] + entities[:2]:
            for domain in ("congan.bacgiang.gov.vn", "cand.com.vn", "baobacgiang.vn"):
                planned.append(
                    PlannedQuery(
                        query=f'site:{domain} "{entity}"',
                        lang="vi",
                        purpose="site_scoped",
                    )
                )

    return planned


async def plan_queries(
    query: str,
    intent: SearchIntent,
    *,
    max_queries: int | None = None,
) -> QueryPlan:
    """Produce an ordered sub-query plan for ``query``.

    LLM first (bilingual VI+EN), deterministic heuristic as fallback. The
    original question is always the first entry and the plan is capped by
    ``max_queries`` (or the intent depth default).
    """
    limit = _max_queries_for(intent, max_queries)

    source = "llm"
    planned = await _llm_plan(query, intent, limit)
    if not planned:
        source = "heuristic"
        planned = _heuristic_plan(query, intent)

    # Deterministic invariants — code owns them, not the LLM.
    original = query.strip()
    planned = [p for p in planned if p.query.strip().lower() != original.lower()]
    planned.insert(0, PlannedQuery(query=original, lang=detect_lang(original), purpose="original"))
    planned = _dedupe_keep_order(planned)[:limit]

    return QueryPlan(original=original, queries=planned, source=source)
