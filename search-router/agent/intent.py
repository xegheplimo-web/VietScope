"""Intent Analyzer — Phân tích ý định tìm kiếm."""

from research_models.research_state import SearchIntent

# Keywords that signal specific search depths
QUICK_KEYWORDS = {
    "what is",
    "who is",
    "define",
    "meaning",
    "là gì",
    "ai là",
    "định nghĩa",
}
DEEP_KEYWORDS = {
    "so sánh",
    "phân tích",
    "nghiên cứu",
    "compare",
    "analyze",
    "research",
    "review",
    "đánh giá",
}
NEWS_KEYWORDS = {
    "hôm nay",
    "mới nhất",
    "tin tức",
    "today",
    "latest",
    "news",
    "vừa ra",
    "recent",
}
SHOPPING_KEYWORDS = {
    "giá",
    "price",
    "mua",
    "buy",
    "đáng mua",
    "best",
    "tốt nhất",
    "rẻ",
    "expensive",
}
TECH_KEYWORDS = {"version", "release", "update", "api", "library", "framework", "model"}

# Official domains for different query types
OFFICIAL_DOMAINS = {
    "legal": ["luatvietnam.vn", "thuvienphapluat.vn", "chinhphu.vn"],
    "news": ["vnexpress.net", "tuoitre.vn", "dantri.com.vn", "thanhnien.vn"],
    "tech": ["github.com", "docs.python.org", "stackoverflow.com"],
    "government": ["chinhphu.vn", "*.gov.vn"],
}


def analyze_intent(query: str) -> SearchIntent:
    """Analyze search query to determine intent.

    Examples:
        "What is Python?" → no_web / optional
        "Giá vàng hôm nay?" → live search
        "Tìm vụ án Phạm Chí Nghị" → deep research
        "RTX 5090 giá bao nhiêu?" → shopping/current
        "Qwen3.8 mới nhất?" → technical + recent
    """
    query_lower = query.lower()

    # Determine depth
    if any(kw in query_lower for kw in QUICK_KEYWORDS):
        depth = "quick"
    elif any(kw in query_lower for kw in DEEP_KEYWORDS):
        depth = "deep"
    else:
        depth = "normal"

    # Determine freshness
    if any(kw in query_lower for kw in NEWS_KEYWORDS):
        freshness = "day"
    elif any(kw in query_lower for kw in SHOPPING_KEYWORDS):
        freshness = "week"
    elif any(kw in query_lower for kw in TECH_KEYWORDS):
        freshness = "month"
    else:
        freshness = "any"

    # Determine categories
    categories = ["general"]
    if any(kw in query_lower for kw in NEWS_KEYWORDS):
        categories.append("news")

    # Determine if official sources should be prioritized
    official_first = any(
        kw in query_lower for kw in ["pháp luật", "luật", "legal", "official", "chính thức"]
    )

    # Preferred domains
    preferred_domains = []
    if any(kw in query_lower for kw in ["pháp luật", "luật", "vụ án"]):
        preferred_domains = OFFICIAL_DOMAINS["legal"]
    elif any(kw in query_lower for kw in ["tin tức", "sự kiện"]):
        preferred_domains = OFFICIAL_DOMAINS["news"]

    return SearchIntent(
        needs_web=True,
        depth=depth,
        freshness=freshness,
        categories=categories,
        preferred_domains=preferred_domains,
        official_first=official_first,
    )
