import re
from urllib.parse import urlparse

SCORES = {
    "official": 1.0,
    "government": 1.0,
    "research_paper": 0.95,
    "major_publication": 0.9,
    "vendor_website": 0.85,
    "specialist_blog": 0.75,
    "forum": 0.6,
    "reddit": 0.55,
    "unknown_seo_site": 0.3,
}

DOMAIN_MAP = {
    # VN major publications
    "vnexpress.net": ("major_publication", 0.88),
    "tuoitre.vn": ("major_publication", 0.88),
    "thanhnien.vn": ("major_publication", 0.88),
    "dantri.com.vn": ("major_publication", 0.85),
    "zingnews.vn": ("major_publication", 0.85),
    "vietnamnet.vn": ("major_publication", 0.85),
    "kenh14.vn": ("major_publication", 0.72),
    "cafef.vn": ("major_publication", 0.75),
    "cafebiz.vn": ("major_publication", 0.75),
    "nld.com.vn": ("major_publication", 0.85),
    "nhandan.vn": ("major_publication", 0.9),
    "qdnd.vn": ("major_publication", 0.85),
    "vtv.vn": ("major_publication", 0.85),
    "laodong.vn": ("major_publication", 0.85),
    "baomoi.com": ("major_publication", 0.8),
    "voh.com.vn": ("major_publication", 0.8),
    # VN official company / gold-jewellery vendors (official pricing pages)
    "sjc.com.vn": ("vendor_website", 0.9),
    "btmc.vn": ("vendor_website", 0.85),
    "phuquy.com.vn": ("vendor_website", 0.85),
    "pnj.com.vn": ("vendor_website", 0.9),
    "doji.vn": ("vendor_website", 0.85),
    # VN government / state media
    "chinhphu.vn": ("government", 1.0),
    "baochinhphu.vn": ("government", 1.0),
    "vietnam.gov.vn": ("government", 1.0),
    "mof.gov.vn": ("government", 1.0),
    # VN food/review
    "foody.vn": ("specialist_blog", 0.72),
    "lozi.vn": ("specialist_blog", 0.72),
    "mytour.vn": ("specialist_blog", 0.72),
    "toplist.vn": ("specialist_blog", 0.7),
    "amthucbinhxuyen.com": ("specialist_blog", 0.72),
    # International major publications
    "reuters.com": ("major_publication", 0.9),
    "apnews.com": ("major_publication", 0.9),
    "bloomberg.com": ("major_publication", 0.9),
    "nytimes.com": ("major_publication", 0.9),
    "theguardian.com": ("major_publication", 0.9),
    "washingtonpost.com": ("major_publication", 0.9),
    "ft.com": ("major_publication", 0.9),
    "cnn.com": ("major_publication", 0.72),
    "techcrunch.com": ("major_publication", 0.75),
    # Research / academic
    "arxiv.org": ("research_paper", 0.95),
    "ieee.org": ("research_paper", 0.95),
    "papers.ssrn.com": ("research_paper", 0.95),
    "pubmed.ncbi.nlm.nih.gov": ("research_paper", 0.95),
    # Vendor / official
    "github.com": ("vendor_website", 0.85),
    "openai.com": ("vendor_website", 0.85),
    "developer.mozilla.org": ("official", 1.0),
    "docs.docker.com": ("official", 1.0),
    "wikipedia.org": ("major_publication", 0.8),
    # Forums / community
    "stackoverflow.com": ("forum", 0.6),
    "reddit.com": ("reddit", 0.55),
    "quora.com": ("specialist_blog", 0.55),
    # Blog platforms
    "medium.com": ("specialist_blog", 0.75),
    "substack.com": ("specialist_blog", 0.75),
}

# VN legal / official-document sources (P5-VN). Mapping to existing type
# vocabulary keeps ``classify_source_type``'s contract unchanged.
DOMAIN_MAP.update(
    {
        "vbpl.vn": ("government", 1.0),
        "vanban.chinhphu.vn": ("government", 1.0),
        "congbao.chinhphu.vn": ("government", 1.0),
        "xaydungchinhsach.chinhphu.vn": ("government", 1.0),
        "thuvienphapluat.vn": ("major_publication", 0.85),
        "luatvietnam.vn": ("major_publication", 0.8),
        "vietnamplus.vn": ("major_publication", 0.9),
        "congthuong.vn": ("major_publication", 0.8),
        "vietstock.vn": ("major_publication", 0.75),
        "tinnhanhchungkhoan.vn": ("major_publication", 0.75),
        "vneconomy.vn": ("major_publication", 0.78),
        # VN forums / community
        "otofun.net": ("forum", 0.55),
        "vozforums.com": ("forum", 0.55),
        "webtretho.com": ("forum", 0.5),
        "tinhte.vn": ("specialist_blog", 0.7),
        # VN ecommerce / retail (authoritative for own listings & prices)
        "thegioididong.com": ("vendor_website", 0.8),
        "cellphones.com.vn": ("vendor_website", 0.8),
        "fptshop.com.vn": ("vendor_website", 0.8),
        "shopee.vn": ("vendor_website", 0.72),
        "tiki.vn": ("vendor_website", 0.72),
        "lazada.vn": ("vendor_website", 0.7),
    }
)

CONTENT_FARM_DOMAINS = {
    "giatot24gio.com",
    "hoclaixetphcm.com",
}

# ── Per-vertical authority (P5-VN) ─────────────────────────────────────────
# Authority depends on intent — one ``domain_score`` is wrong: a VN auto
# forum is weak on law but useful on product opinions. Keys are domains;
# values map ``SourceType`` lane names to a score override. Lanes absent
# here fall back to the domain's base score. Verified/legal-first sources
# still win their lane; forums stay low on legal/government.

VN_VERTICAL_SCORES: dict[str, dict[str, float]] = {
    # First-party legal texts — max on legal/government, plain news elsewhere.
    "vbpl.vn": {"legal": 1.0, "government": 1.0, "document": 1.0, "news": 0.6},
    "vanban.chinhphu.vn": {"legal": 1.0, "government": 1.0, "document": 1.0, "news": 0.6},
    "congbao.chinhphu.vn": {"legal": 1.0, "government": 1.0, "document": 1.0, "news": 0.6},
    "xaydungchinhsach.chinhphu.vn": {
        "legal": 1.0,
        "government": 1.0,
        "document": 1.0,
        "news": 0.75,
    },
    "thuvienphapluat.vn": {"legal": 0.92, "government": 0.85, "document": 0.9, "news": 0.5},
    "luatvietnam.vn": {"legal": 0.88, "government": 0.8, "document": 0.85, "news": 0.5},
    # State media — near-official for government/administrative, news too.
    "nhandan.vn": {"government": 0.95, "administrative": 0.9, "legal": 0.8, "news": 0.9},
    "vietnamplus.vn": {"government": 0.9, "administrative": 0.85, "legal": 0.75, "news": 0.88},
    # Business / market verticals.
    "congthuong.vn": {"business": 0.85, "market": 0.85, "news": 0.75},
    "cafef.vn": {"market": 0.85, "finance": 0.8, "business": 0.75, "news": 0.65},
    "vneconomy.vn": {"market": 0.82, "finance": 0.8, "business": 0.78, "news": 0.65},
    "vietstock.vn": {"market": 0.85, "finance": 0.85, "business": 0.75, "news": 0.6},
    "tinnhanhchungkhoan.vn": {"market": 0.85, "finance": 0.8, "news": 0.6},
    # Vendor price pages — official for their own market prices (gold, retail).
    "sjc.com.vn": {"market": 0.95, "product": 0.9, "ecommerce": 0.7},
    "pnj.com.vn": {"market": 0.92, "product": 0.9, "ecommerce": 0.7},
    "doji.vn": {"market": 0.9, "product": 0.85, "ecommerce": 0.7},
    # Places / local.
    "foody.vn": {"places": 0.85, "business": 0.7, "news": 0.4},
    "tripadvisor.com": {"places": 0.8, "news": 0.3},
    # Product / ecommerce.
    "thegioididong.com": {"product": 0.85, "ecommerce": 0.8, "news": 0.4},
    "cellphones.com.vn": {"product": 0.85, "ecommerce": 0.8, "news": 0.4},
    "fptshop.com.vn": {"product": 0.82, "ecommerce": 0.78, "news": 0.4},
    "shopee.vn": {"ecommerce": 0.72, "product": 0.6, "news": 0.25},
    "tiki.vn": {"ecommerce": 0.72, "product": 0.6, "news": 0.25},
    "lazada.vn": {"ecommerce": 0.7, "product": 0.55, "news": 0.25},
    # Community — useful on product opinions, weak on law/government.
    "otofun.net": {"forum": 0.7, "product": 0.7, "social": 0.6, "legal": 0.1, "government": 0.1},
    "vozforums.com": {"forum": 0.65, "social": 0.6, "product": 0.55, "legal": 0.1},
    "webtretho.com": {"forum": 0.6, "social": 0.55, "product": 0.5, "legal": 0.1},
    "tinhte.vn": {"product": 0.75, "forum": 0.65, "news": 0.5, "legal": 0.15},
    "reddit.com": {"forum": 0.55, "social": 0.55, "legal": 0.15, "government": 0.1},
}

# Source metadata (P5-VN spec): official flag, publisher, ownership,
# geography, update cadence — keyed by domain for downstream use
# (authority explanation, corpus seeding, freshness policy).
VN_SOURCE_META: dict[str, dict[str, str | bool]] = {
    "chinhphu.vn": {
        "official": True,
        "publisher": "Báo điện tử Chính phủ",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "realtime",
    },
    "vbpl.vn": {
        "official": True,
        "publisher": "Cổng thông tin điện tử về văn bản quy phạm pháp luật",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "daily",
    },
    "congbao.chinhphu.vn": {
        "official": True,
        "publisher": "Công báo",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "daily",
    },
    "thuvienphapluat.vn": {
        "official": False,
        "publisher": "Thư Viện Pháp Luật",
        "ownership": "private",
        "geography": "VN",
        "update_frequency": "daily",
    },
    "nhandan.vn": {
        "official": True,
        "publisher": "Báo Nhân Dân",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "realtime",
    },
    "vietnamplus.vn": {
        "official": True,
        "publisher": "VietnamPlus / TTXVN",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "realtime",
    },
    "vnexpress.net": {
        "official": False,
        "publisher": "VnExpress",
        "ownership": "private",
        "geography": "VN",
        "update_frequency": "realtime",
    },
    "congthuong.vn": {
        "official": True,
        "publisher": "Báo Công Thương (Bộ Công Thương)",
        "ownership": "state",
        "geography": "VN",
        "update_frequency": "daily",
    },
    "cafef.vn": {
        "official": False,
        "publisher": "CafeF",
        "ownership": "private",
        "geography": "VN",
        "update_frequency": "realtime",
    },
}

_CONTENT_FARM_RE = re.compile(r"(giatot|24h|24gio|nhanh|pro|top\d+|seo)", re.IGNORECASE)

# ── Vietnamese ranking boost ──────────────────────────────────────────────────
# Applied when the query language is Vietnamese (lang="vi"): prefer local VN
# sources. A plain .vn TLD earns a strong boost; well-known VN platforms earn
# a smaller boost. When a domain is both .vn and well-known, the larger boost
# wins (no double counting).

VN_TLD_BOOST = 1.5
VN_FAMOUS_BOOST = 1.0

VN_FAMOUS_DOMAINS = {
    "foody.vn",
    "pasgo.vn",
    "lozi.vn",
    "vietnamnet.vn",
    "vnexpress.net",
    "dantri.com.vn",
    "zingnews.vn",
    "thanhnien.vn",
    "tuoitre.vn",
    "baomoi.com",
    "thegioididong.com",
    "shopee.vn",
    "tiki.vn",
    "google.com/maps",  # host part "google.com" matches any google.com URL
    "maps.google.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "tripadvisor.com",
    "booking.com",
    "agoda.com",
    "now.vn",
    "baemin.vn",
    "shopeefood.vn",
    "grabfood.vn",
}


def _normalize(domain: str) -> str:
    d = domain.lower().strip().strip(".")
    if "://" in d:
        d = urlparse(d).netloc
    d = d.removeprefix("www.")
    return d.split(":")[0]


def _is_domain(name: str, d: str) -> bool:
    return d == name or d.endswith(f".{name}")


def _is_content_farm(d: str) -> bool:
    if d in CONTENT_FARM_DOMAINS:
        return True
    return bool(_CONTENT_FARM_RE.search(d))


def classify_source_type(domain: str) -> str:
    d = _normalize(domain)
    labels = d.split(".")

    if labels and (
        labels[-1] in {"gov", "edu", "mil"} or (len(labels) >= 2 and labels[-2] in {"gov", "edu"})
    ):
        return "government" if "gov" in labels else "research_paper"

    if labels and labels[0] == "docs" and not _is_content_farm(d):
        return "official"

    for name, (stype, _) in DOMAIN_MAP.items():
        if _is_domain(name, d):
            return stype

    if _is_content_farm(d):
        return "unknown_seo_site"

    # .vn TLD is registry-controlled (requires business license) — a legal VN
    # site we have not yet mapped earns specialist_blog rather than unknown.
    if d.endswith(".vn"):
        return "specialist_blog"

    return "unknown_seo_site"


def _authority_score(domain: str) -> float:
    d = _normalize(domain)
    labels = d.split(".")

    if labels and (
        labels[-1] in {"gov", "edu", "mil"} or (len(labels) >= 2 and labels[-2] in {"gov", "edu"})
    ):
        return 1.0

    if labels and labels[0] == "docs" and not _is_content_farm(d):
        return 0.85

    for name, (_, score) in DOMAIN_MAP.items():
        if _is_domain(name, d):
            return score

    if _is_content_farm(d):
        return 0.18

    # Registry-controlled .vn → modest baseline instead of unknown.
    if d.endswith(".vn"):
        return 0.7

    return SCORES["unknown_seo_site"]


def _is_known_vn_domain(d: str) -> bool:
    """Match a normalized domain against the famous-VN-domain list.

    Entries that carry a path (e.g. "google.com/maps") are matched by their
    host part, so any google.com URL counts as a known VN source.
    """
    for entry in VN_FAMOUS_DOMAINS:
        host = entry.split("/")[0]
        if _is_domain(host, d):
            return True
    return False


def vn_boost(domain: str) -> float:
    """Extra score for Vietnamese sources.

    - ``.vn`` TLD          → ``VN_TLD_BOOST`` (1.5)
    - famous VN platform   → ``VN_FAMOUS_BOOST`` (1.0)
    - both                 → the larger of the two (no double counting)
    """
    d = _normalize(domain)
    boost = 0.0
    if d.endswith(".vn"):
        boost = VN_TLD_BOOST
    if _is_known_vn_domain(d):
        boost = max(boost, VN_FAMOUS_BOOST)
    return boost


def authority_source_meta(domain: str) -> dict[str, str | bool]:
    """Official/publisher/ownership metadata for a domain (may be empty)."""
    d = _normalize(domain)
    for name, meta in VN_SOURCE_META.items():
        if _is_domain(name, d):
            return dict(meta)
    return {}


def _vertical_override(domain: str, vertical: str | None) -> float | None:
    if not vertical:
        return None
    d = _normalize(domain)
    lane = str(vertical)
    for name, lanes in VN_VERTICAL_SCORES.items():
        if _is_domain(name, d):
            return lanes.get(lane)
    return None


def authority_for(domain: str, vertical: str | None = None, lang: str = "en") -> float:
    """Intent-dependent authority score (P5-VN).

    ``vertical`` is a ``SourceType`` lane name (``news``, ``legal``,
    ``forum``, …). When the domain carries a per-lane override in
    ``VN_VERTICAL_SCORES`` it wins; otherwise the base ``_authority_score``
    applies — so the API degrades to the legacy scalar for unknown lanes
    and unmapped domains. Vietnamese-language queries still get
    ``vn_boost`` on top, matching ``authority_score``.
    """
    override = _vertical_override(domain, vertical)
    score = override if override is not None else _authority_score(domain)
    if lang and str(lang).lower().startswith("vi"):
        score += vn_boost(domain)
    return score


def authority_score(domain: str, lang: str = "en") -> float:
    """Authority score for a domain (0.0-1.0 by default).

    Backward compatible with the original single-argument call. When
    ``lang`` is Vietnamese (starts with "vi"), ``vn_boost`` is added on top
    so local VN sources rank higher for Vietnamese queries.
    """
    return authority_for(domain, lang=lang)
