"""Source Router — adaptive fan-out (Phase 2).

Decides *which* source lanes a query needs and *which* providers should
serve them — instead of hitting every provider for every query.

    Query → lane weights (intent signals, vi+en) → provider scoring
          (lane weight × priority, health-gated) → FanOutPlan

Examples, per the federation spec:

    "OpenAI vừa ra model gì?"      → web HIGH, news VERY_HIGH, social MEDIUM
    "iPhone 17 Pro giá bao nhiêu?" → ecommerce HIGH, web HIGH, places MEDIUM
    "Nghị định mới về hóa đơn điện tử" → government/legal VERY_HIGH, web MEDIUM
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import settings
from providers.base import ProviderSpec, SearchContext, SourceType

from core.provider_health import Admission, ProviderHealthMonitor

if TYPE_CHECKING:
    from core.provider_registry import ProviderRegistry
    from core.query_understanding import QueryProfile

logger = logging.getLogger(__name__)

# Lane weight levels — OFF providers are never selected for the lane.
OFF, LOW, MEDIUM, HIGH, VERY_HIGH = 0.0, 0.25, 0.5, 0.8, 1.0

_NEWS_RE = re.compile(
    r"\b(news|today|latest|breaking|announce[ds]?|launch(?:ed)?|release[ds]?|"
    r"update[ds]?|recall|earnings)\b|"
    r"hôm nay|mới nhất|tin tức|tin nóng|vừa|ra mắt|công bố|phát hành",
    re.IGNORECASE,
)
_LEGAL_RE = re.compile(
    r"\b(decree|circular|resolution|ordinance|statute|legislation|regulation|"
    r"directive|bylaw|constitution|legal|lawsuit|compliance)\b|"
    r"nghị định|thông tư|bộ luật|luật|nghị quyết|pháp lệnh|quy định|"
    r"quyết định|chính phủ|chủ trương|công văn|hiến pháp|pháp luật|"
    r"chính sách|ban hành|gov\.vn|hóa đơn điện tử",
    re.IGNORECASE,
)
_ECOMMERCE_RE = re.compile(
    r"\b(price|cost|buy|bought|shop(?:ping)?|order|deal|discount|coupon|"
    r"cheap(?:est)?|for sale|pre-?order|specs|vs\.?)\b|"
    r"giá|bao nhiêu tiền|mua|bán|đặt hàng|sản phẩm|khuyến mãi|giảm giá|"
    r"cửa hàng|đại lý|trả góp|sale",
    re.IGNORECASE,
)
_PRODUCT_RE = re.compile(
    r"\b(iphone|samsung|galaxy|xiaomi|oppo|pixel|macbook|laptop|điện thoại|"
    r"smartphone|tablet|tai nghe|đồng hồ|rtx|gtx|cpu|gpu|máy tính|"
    # VN market products (P1 taxonomy)
    r"vinfast|vf-?\d|airblade|vision|sh[- ]?mode|winner|exciter|sirius|"
    r"xe máy|xe tay ga|ô tô|xe ô tô|xe điện)\b",
    re.IGNORECASE,
)
_MARKET_RE = re.compile(
    r"\b(stock|stocks|forex|bond|commodity|commodities|bullion|exchange rate|"
    r"interest rate|vn-?index|hsx|hnx|upcom)\b|"
    r"giá vàng|vàng|ngoại tệ|tỷ giá|usd|euro|đô la|chứng khoán|cổ phiếu|"
    r"cổ phiếu|trái phiếu|hàng hóa|xăng dầu|giá xăng|giá dầu|giá cà phê|"
    r"giá gạo|giá lúa|giá heo|giá đất|giá nhà|bất động sản|bitcoin|lợi suất",
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r"\b(company|corporation|enterprise|revenue|profit|shareholder|ipo|"
    r"listed|holding|conglomerate|tax code|business registration)\b|"
    r"doanh thu|lợi nhuận|công ty|tập đoàn|thương hiệu|sếp|ceo|founder|"
    r"hồ sơ doanh nghiệp|mã số thuế|niêm yết|cổ đông|pháp nhân|đăng ký kinh doanh|"
    r"doanh nghiệp|trụ sở công ty",
    re.IGNORECASE,
)
_FINANCE_RE = re.compile(
    r"\b(bank|banking|loan|mortgage|credit|debit|insurance|tax|budget|"
    r"investment|investor|fund|deposit|savings|inflation|gdp)\b|"
    r"tài chính|ngân hàng|vay|lãi suất|tín dụng|bảo hiểm|thuế|ngân sách|"
    r"kho bạc|đầu tư|tiết kiệm|lạm phát|tổng sản phẩm quốc nội",
    re.IGNORECASE,
)
_ADMINISTRATIVE_RE = re.compile(
    r"\b(administrative|province|district|ward|commune|boundary|merger|"
    r"province merger)\b|"
    r"tỉnh thành|quận huyện|phường xã|địa giới|sáp nhập|hành chính|"
    r"đơn vị hành chính|mã đơn vị|thành phố trực thuộc|trung ương",
    re.IGNORECASE,
)
_MEDICAL_RE = re.compile(
    r"\b(symptom|symptoms|disease|drug|dosage|dose|treatment|diagnosis|"
    r"vaccine|clinic|hospital|pharmacy|medicine|medical|surgery|cancer)\b|"
    r"bệnh|thuốc|triệu chứng|điều trị|viêm|ung thư|vaccine|tiêm|bệnh viện|"
    r"phẫu thuật|dược|sức khỏe|khám|chẩn đoán|liều dùng",
    re.IGNORECASE,
)
_DOCUMENT_RE = re.compile(
    r"\b(template|form|declaration|document|paperwork|procedure|certificate|"
    r"permit|application form)\b|"
    r"biểu mẫu|mẫu đơn|hồ sơ|văn bản|giấy tờ|thủ tục|giấy phép|"
    r"giấy chứng nhận|khai báo|mẫu cv|mẫu đơn",
    re.IGNORECASE,
)
_PLACES_RE = re.compile(
    r"\b(near me|nearby|restaurant|hotel|cafe|coffee|barber|atm|gas station|"
    r"directions|map)\b|"
    r"nhà hàng|quán|khách sạn|địa chỉ|ở đâu|gần đây|gần tôi|bản đồ|"
    r"tiệm|phở|bún|cà phê|trà sữa|siêu thị|bệnh viện|nhà thuốc",
    re.IGNORECASE,
)
_ACADEMIC_RE = re.compile(
    r"\b(arxiv|paper|journal|conference|benchmark|thesis|doi|"
    r"peer.?reviewed|study|survey of|sota|scientific|academic)\b|"
    r"nghiên cứu|học thuật|luận văn|tạp chí khoa học",
    re.IGNORECASE,
)
_CODE_RE = re.compile(
    r"\b(function|class|method|endpoint|import|export|require|module|package|"
    r"pip install|npm install|github|gitlab|stackoverflow|exception|traceback|"
    r"stack trace|snippet|implementation of|compile|runtime error|debug)\b|"
    r"```|cách (fix|sửa) lỗi|lỗi code",
    re.IGNORECASE,
)
_FORUM_RE = re.compile(
    r"\b(reddit|forum|stack ?exchange|quora|threads)\b|"
    r"diễn đàn|hỏi đáp|kinh nghiệm|mọi người|voz|tinhte|webtretho",
    re.IGNORECASE,
)
_SOCIAL_RE = re.compile(
    r"\b(tiktok|twitter|facebook|instagram|linkedin|youtube|threads|"
    r"social media)\b|"
    r"mạng xã hội|mọi người (nói|bàn|chia sẻ)",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(
    r"\b(image|images|photo|picture|logo|poster|wallpaper)\b|ảnh|hình ảnh|hình nền",
    re.IGNORECASE,
)
_VIDEO_RE = re.compile(
    r"\b(video|videos|clip|trailer|mp4|vlog)\b|"
    r"youtube|phim|clip hướng dẫn",
    re.IGNORECASE,
)
_OPINION_RE = re.compile(
    r"\b(review|reviews|opinion|worth it|should i|any good)\b|"
    r"đánh giá|có nên|tốt không|so sánh|nhận xét",
    re.IGNORECASE,
)


@dataclass
class ProviderPick:
    """One provider chosen for the fan-out, with routing provenance."""

    name: str
    spec: ProviderSpec
    weight: float  # lane_weight × spec.priority
    lane: SourceType  # strongest active lane it serves
    probe: bool = False  # half-open recovery probe
    served_types: list[SourceType] = field(default_factory=list)


@dataclass
class FanOutPlan:
    """Router output: which providers to call and why."""

    query: str
    picks: list[ProviderPick] = field(default_factory=list)
    lane_weights: dict[SourceType, float] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    reason: list[str] = field(default_factory=list)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self.picks]


# P6: entity-id prefix → minimum lane floors. A resolved entity pins
# its vertical even when the surrounding text carries no matching signal.
_ENTITY_LANE_BOOST: dict[str, tuple[tuple[SourceType, float], ...]] = {
    "co": (
        (SourceType.company, VERY_HIGH),
        (SourceType.business, HIGH),
        (SourceType.finance, MEDIUM),
    ),
    "loc": (
        (SourceType.places, MEDIUM),
        (SourceType.administrative, MEDIUM),
    ),
    "legal": (
        (SourceType.legal, VERY_HIGH),
        (SourceType.government, HIGH),
        (SourceType.document, MEDIUM),
    ),
    "product": (
        (SourceType.product, HIGH),
        (SourceType.ecommerce, MEDIUM),
    ),
    "person": ((SourceType.news, MEDIUM),),
    "event": (
        (SourceType.document, MEDIUM),
        (SourceType.news, MEDIUM),
    ),
}


class SourceRouter:
    """Maps query intent → lane weights → provider picks."""

    def __init__(self, monitor: ProviderHealthMonitor | None = None) -> None:
        self.monitor = monitor

    # ── lane weights ─────────────────────────────────────────────────────

    def lane_weights(self, query: str, profile: QueryProfile) -> dict[SourceType, float]:
        q = query or ""
        w: dict[SourceType, float] = dict.fromkeys(SourceType, OFF)
        w[SourceType.general_web] = HIGH  # backbone lane — always on
        w[SourceType.index] = MEDIUM  # own corpus — always consulted

        if profile.freshness_required or _NEWS_RE.search(q):
            w[SourceType.news] = VERY_HIGH
            w[SourceType.social] = max(w[SourceType.social], MEDIUM)

        if _LEGAL_RE.search(q):
            w[SourceType.government] = VERY_HIGH
            w[SourceType.legal] = VERY_HIGH
            w[SourceType.general_web] = max(w[SourceType.general_web], MEDIUM)
            w[SourceType.news] = max(w[SourceType.news], MEDIUM)
            w[SourceType.social] = max(w[SourceType.social], LOW)

        # Market/financial queries ("giá vàng hôm nay") mention "giá" but are
        # NOT shopping intent — market suppresses the ecommerce bump (P1).
        is_market = _MARKET_RE.search(q) is not None
        is_product = _PRODUCT_RE.search(q) is not None
        if is_market:
            w[SourceType.market] = VERY_HIGH
            w[SourceType.finance] = max(w[SourceType.finance], MEDIUM)
            w[SourceType.news] = max(w[SourceType.news], MEDIUM)

        if is_product:
            w[SourceType.product] = VERY_HIGH
            if not is_market:
                w[SourceType.ecommerce] = VERY_HIGH
                w[SourceType.places] = max(w[SourceType.places], MEDIUM)
        elif _ECOMMERCE_RE.search(q) and not is_market:
            w[SourceType.ecommerce] = HIGH
            w[SourceType.product] = MEDIUM

        if _COMPANY_RE.search(q):
            w[SourceType.company] = VERY_HIGH
            w[SourceType.business] = max(w[SourceType.business], HIGH)
            w[SourceType.finance] = max(w[SourceType.finance], MEDIUM)
            w[SourceType.news] = max(w[SourceType.news], MEDIUM)

        if _FINANCE_RE.search(q):
            w[SourceType.finance] = max(w[SourceType.finance], HIGH)
            w[SourceType.market] = max(w[SourceType.market], MEDIUM)
            w[SourceType.news] = max(w[SourceType.news], MEDIUM)

        if _ADMINISTRATIVE_RE.search(q):
            w[SourceType.administrative] = VERY_HIGH
            w[SourceType.places] = max(w[SourceType.places], MEDIUM)
            w[SourceType.government] = max(w[SourceType.government], MEDIUM)

        if _MEDICAL_RE.search(q):
            w[SourceType.medical] = HIGH
            w[SourceType.places] = max(w[SourceType.places], LOW)

        if _DOCUMENT_RE.search(q):
            w[SourceType.document] = HIGH
            w[SourceType.government] = max(w[SourceType.government], MEDIUM)
            w[SourceType.legal] = max(w[SourceType.legal], LOW)

        if _PLACES_RE.search(q):
            w[SourceType.places] = HIGH
            w[SourceType.ecommerce] = max(w[SourceType.ecommerce], LOW)

        if _ACADEMIC_RE.search(q) or profile.intent == "research":
            w[SourceType.academic] = HIGH
            w[SourceType.forum] = max(w[SourceType.forum], LOW)

        if _CODE_RE.search(q):
            w[SourceType.code] = HIGH

        if _FORUM_RE.search(q) or _OPINION_RE.search(q):
            w[SourceType.forum] = max(w[SourceType.forum], HIGH)
            w[SourceType.social] = max(w[SourceType.social], MEDIUM)
            if _OPINION_RE.search(q):
                w[SourceType.ecommerce] = max(w[SourceType.ecommerce], MEDIUM)

        if _SOCIAL_RE.search(q):
            w[SourceType.social] = max(w[SourceType.social], HIGH)

        if _IMAGE_RE.search(q):
            w[SourceType.image] = HIGH
        if _VIDEO_RE.search(q):
            w[SourceType.video] = HIGH

        if profile.intent == "comparison":
            w[SourceType.forum] = max(w[SourceType.forum], MEDIUM)
            w[SourceType.ecommerce] = max(w[SourceType.ecommerce], MEDIUM)

        # P6: resolved entity kinds steer lanes — "Vingroup doanh thu"
        # pins the company lane even though "doanh thu" alone is finance.
        for kind in {eid.split(":", 1)[0] for eid in profile.entity_ids}:
            for lane, floor in _ENTITY_LANE_BOOST.get(kind, ()):
                w[lane] = max(w[lane], floor)

        return w

    # ── provider selection ───────────────────────────────────────────────

    def plan(
        self,
        query: str,
        profile: QueryProfile,
        registry: ProviderRegistry,
        *,
        mode: str = "normal",
        remaining_queries: int = 99,
    ) -> FanOutPlan:
        weights = self.lane_weights(query, profile)
        monitor = self.monitor
        out = FanOutPlan(query=query, lane_weights=weights)

        # Wider recall on deep mode: lanes at LOW still get a shot.
        threshold = 0.15 if mode == "deep" else LOW

        candidates: list[ProviderPick] = []
        for name, _provider in registry.all():
            spec = registry.spec(name) if hasattr(registry, "spec") else None
            if spec is None:
                spec = ProviderSpec(name=name)
            if not spec.enabled:
                out.skipped.append((name, "disabled"))
                continue
            if not spec.supports(language=profile.language, country=None):
                out.skipped.append((name, "locale_mismatch"))
                continue

            active = [t for t in spec.source_types if weights.get(t, OFF) > 0]
            lane = max(
                spec.source_types,
                key=lambda t: weights.get(t, OFF),
                default=SourceType.general_web,
            )
            lane_weight = weights.get(lane, OFF)
            effective = lane_weight * max(spec.priority, 0.0)

            if spec.internal:
                # Own-index lane: always consulted — cheap, canonical, and
                # never counts against the query budget.
                candidates.append(
                    ProviderPick(name, spec, 1.0, SourceType.index, served_types=active)
                )
                continue
            if effective < threshold:
                out.skipped.append((name, f"lane_off:{lane.value}"))
                continue
            candidates.append(ProviderPick(name, spec, effective, lane, served_types=active))

        # Health gate: circuit-open providers lose their slot; half-open ones
        # go in as probes (bounded by the monitor's single-probe claim).
        gated: list[ProviderPick] = []
        for pick in candidates:
            admission = monitor.allow(pick.name) if monitor is not None else Admission.allow
            if admission == Admission.deny:
                out.skipped.append((pick.name, "circuit_open"))
                continue
            pick.probe = admission == Admission.probe
            gated.append(pick)

        # Internal lane first (cheap), then external by descending weight.
        internal = [p for p in gated if p.spec.internal]
        external = sorted([p for p in gated if not p.spec.internal], key=lambda p: -p.weight)

        cap = max(0, min(settings.provider_fanout_max_providers, remaining_queries))
        if mode == "fast":
            cap = min(cap, settings.provider_fanout_max_providers_fast)
        overflow = {p.name for p in external[cap:]}
        external = external[:cap]
        for p in candidates:
            if not p.spec.internal and p.name in overflow:
                out.skipped.append((p.name, "budget_cap"))
        out.picks = internal + external

        if not external and remaining_queries > 0:
            # Never starve the fan-out: if health/intent eliminated everything,
            # fall back to every enabled external provider (router degrades to
            # the old broadcast behaviour rather than returning nothing).
            for name, _provider in registry.all():
                if any(p.name == name for p in out.picks):
                    continue
                spec = registry.spec(name) if hasattr(registry, "spec") else None
                spec = spec or ProviderSpec(name=name)
                if not spec.enabled or spec.internal:
                    continue
                if not spec.supports(language=profile.language, country=None):
                    continue
                if monitor is not None and monitor.allow(name) == Admission.deny:
                    continue
                out.picks.append(
                    ProviderPick(
                        name,
                        spec,
                        spec.priority,
                        spec.source_types[0] if spec.source_types else SourceType.general_web,
                        served_types=[t for t in spec.source_types if weights.get(t, OFF) > 0],
                    )
                )
            if out.picks:
                out.reason.append("fallback: all enabled providers")

        lanes = [
            f"{t.value}={weights[t]:.2f}"
            for t in SourceType
            if weights.get(t, OFF) > 0 and t != SourceType.index
        ]
        out.reason.append(f"lanes[{', '.join(lanes)}]")
        out.reason.append(
            "picks[" + ", ".join(p.name + ("(probe)" if p.probe else "") for p in out.picks) + "]"
        )
        return out

    def context_for(self, pick: ProviderPick, profile: QueryProfile, mode: str) -> SearchContext:
        """Per-provider call context derived from the plan."""
        cats = sorted({t.value for t in pick.served_types})
        return SearchContext(
            source_types=list(pick.served_types),
            categories=cats,
            language=profile.language,
            freshness_required=profile.freshness_required,
            mode=mode,
            intent=profile.intent,
            probe=pick.probe,
        )
