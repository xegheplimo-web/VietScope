import re
from dataclasses import dataclass, field

from core.entity_resolver import resolve_entities


@dataclass
class QueryProfile:
    intent: str = "current_fact"
    entities: list[str] = field(default_factory=list)
    language: str = "en"
    time_sensitive: bool = False
    freshness_required: bool = False
    max_age: str | None = None
    preferred_sources: list[str] = field(default_factory=list)
    # P6: canonical entity IDs resolved from the query text
    # ("co:vingroup", "loc:ho_chi_minh_city", "legal:nd-254-2026").
    entity_ids: list[str] = field(default_factory=list)


class QueryUnderstanding:
    FRESHNESS_MARKERS = [
        "mới",
        "mới nhất",
        "hôm nay",
        "hiện tại",
        "ngày",
        "latest",
        "today",
        "now",
        "current",
        "news",
        "new",
    ]
    DEFINITION_MARKERS = ["là gì", "định nghĩa", "what is", "define", "meaning"]
    HOWTO_MARKERS = ["cách", "làm thế nào", "how to", "how do", "hướng dẫn"]
    COMPARISON_MARKERS = ["so sánh", "so với", "vs", "versus", "compare", "khác nhau"]
    OPINION_MARKERS = ["ý kiến", "đánh giá", "tốt không", "opinion", "review"]

    VIETNAMESE_TONES = set("áàảãạăắằẳẵặâấầẩẫậđéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵ")
    STOP_WORDS = {
        "là",
        "gì",
        "có",
        "mới",
        "nhất",
        "hôm",
        "nay",
        "của",
        "và",
        "hay",
        "không",
        "được",
        "như",
        "thế",
        "nào",
        "cách",
        "làm",
        "the",
        "is",
        "what",
        "how",
        "to",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "với",
        "so",
        "cũng",
        "cho",
        "về",
        "từ",
        "đến",
        "tại",
        "trên",
        "dưới",
    }

    CONJUNCTIONS = [
        "và",
        "and",
        "&",
        "vs",
        "với",
        "versus",
        "hoặc",
        "hay",
        "or",
    ]
    ASPECT_WORDS = [
        "giá",
        "ngày ra mắt",
        "ngày",
        "price",
        "release date",
        "specs",
        "review",
        "đánh giá",
        "hiệu năng",
        "performance",
        "thiết kế",
        "design",
        "pin",
        "battery",
        "camera",
        "màn hình",
        "display",
        "tính năng",
        "features",
    ]
    COMPARISON_PREFIXES = [
        "so sánh",
        "so với",
        "compare",
        "versus",
        "vs",
    ]

    def _has(self, text: str, markers: list[str]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text) for marker in markers)

    def analyze(self, query: str) -> QueryProfile:
        q = query.lower().strip()

        language = "vi" if (set(q) & self.VIETNAMESE_TONES) else "en"

        if self._has(q, self.COMPARISON_MARKERS):
            intent = "comparison"
        elif self._has(q, self.FRESHNESS_MARKERS):
            intent = "current_fact"
        elif self._has(q, self.DEFINITION_MARKERS):
            intent = "definition"
        elif self._has(q, self.HOWTO_MARKERS):
            intent = "howto"
        elif self._has(q, self.OPINION_MARKERS):
            intent = "opinion"
        else:
            intent = "current_fact"

        freshness_required = self._has(q, self.FRESHNESS_MARKERS)
        max_age = "24h" if freshness_required else None

        tokens = re.findall(r"\w+", q)
        entities = [t for t in tokens if len(t) > 1 and t.lower() not in self.STOP_WORDS]
        entity_ids = [e.id for e in resolve_entities(q)]

        preferred_sources = ["official", "news"] if freshness_required else ["general"]

        return QueryProfile(
            intent=intent,
            entities=entities,
            language=language,
            time_sensitive=freshness_required,
            freshness_required=freshness_required,
            max_age=max_age,
            preferred_sources=preferred_sources,
            entity_ids=entity_ids,
        )

    def _strip_comparison(self, part: str) -> str:
        stripped = part.strip()
        lower = stripped.lower()
        for prefix in sorted(self.COMPARISON_PREFIXES, key=len, reverse=True):
            if lower.startswith(prefix.lower()):
                stripped = stripped[len(prefix) :].strip()
                lower = stripped.lower()
                break
        return stripped

    def _extract_entity(self, head: str) -> str:
        entity = head.strip()
        for aspect in sorted(self.ASPECT_WORDS, key=len, reverse=True):
            pattern = rf"\b{re.escape(aspect)}\s*$"
            if re.search(pattern, entity, flags=re.IGNORECASE):
                entity = re.sub(pattern, "", entity, flags=re.IGNORECASE).strip()
                break
        return entity

    def decompose(self, query: str, max_hops: int = 2) -> list[str]:
        original = query.strip()
        if not original or max_hops <= 1:
            return [original]

        pattern = "|".join(re.escape(c) for c in self.CONJUNCTIONS)
        parts = re.split(rf"(?<!\w)(?:{pattern})(?!\w)", original, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return [original]

        if len(parts) > max_hops:
            parts = parts[: max_hops - 1] + [" ".join(parts[max_hops - 1 :])]

        parts = [self._strip_comparison(p) for p in parts]
        head = parts[0]
        entity = self._extract_entity(head)

        result = [head]
        for part in parts[1:]:
            lower = part.lower()
            is_aspect = any(lower.startswith(a.lower()) for a in self.ASPECT_WORDS)
            contains_entity = entity.lower() in lower if entity else True
            if is_aspect and entity and not contains_entity:
                part = f"{entity} {part}"
            result.append(part)

        return result
