"""P6 — Vietnamese entity resolution: canonical IDs + router steering."""

from core.entity_resolver import (
    Entity,
    fold,
    resolve_entities,
    resolve_match_forms,
)
from core.query_understanding import QueryUnderstanding
from core.source_router import SourceRouter
from providers.base import SourceType


def ids(query: str) -> list[str]:
    return [e.id for e in resolve_entities(query)]


class TestFold:
    def test_strips_tones(self):
        assert fold("Nghị Định Việt Nam") == "nghi dinh viet nam"

    def test_d_with_bar(self):
        assert fold("Đà Nẵng") == "da nang"


class TestLocationResolution:
    # User-specified example set, verbatim from the P6 spec.
    def test_saigon_surface_forms_all_resolve_to_hcm(self):
        for surface in (
            "Sài Gòn",
            "TP HCM",
            "TP.HCM",
            "Thành phố Hồ Chí Minh",
            "Ho Chi Minh City",
        ):
            assert ids(f"thời tiết {surface}") == ["loc:ho_chi_minh_city"], surface

    def test_merged_province_alias(self):
        assert ids("kẹp xe ở Thừa Thiên Huế") == ["loc:hue"]

    def test_accentless_query(self):
        assert ids("thoi tiet sai gon") == ["loc:ho_chi_minh_city"]


class TestCompanyResolution:
    def test_ticker_and_legal_form(self):
        for surface in ("Vingroup", "CTCP Tập đoàn Vingroup", "VIC"):
            assert ids(surface) == ["co:vingroup"], surface

    def test_multiple_companies_one_query(self):
        got = ids("VIC vs VCB ai mạnh hơn")
        assert set(got) == {"co:vingroup", "co:vietcombank"}

    def test_vinfast_family(self):
        assert ids("Vinfast bán bao nhiêu xe") == ["co:vinfast"]


class TestProductResolution:
    def test_iphone_variants(self):
        assert ids("iPhone 17 Pro giá bao nhiêu") == ["product:iphone_17_pro"]

    def test_vf8(self):
        assert ids("VF8 đánh giá thế nào") == ["product:vf8"]


class TestLegalDocuments:
    def test_abbreviated_decree(self):
        got = ids("nđ 254/2026 quy định gì")
        assert got == ["legal:nd-254-2026"]

    def test_full_decree_citation(self):
        got = ids("Nghị định 254/2026/NĐ-CP về hóa đơn điện tử")
        assert got == ["legal:nd-254-2026"]

    def test_different_kinds(self):
        assert ids("thông tư 12/2024") == ["legal:tt-12-2024"]
        assert ids("luật 55/2024") == ["legal:luat-55-2024"]

    def test_dedup_same_document(self):
        # Two surface forms of the same decree → one entity.
        assert ids("nđ 254/2026 và nghị định 254-2026") == ["legal:nd-254-2026"]


class TestMatchForms:
    def test_kebab_and_flat_forms(self):
        forms = resolve_match_forms("Sài Gòn")
        assert "tp-hcm" in forms and "sai-gon" in forms
        assert "ho chi minh city" in forms

    def test_legal_forms_include_number(self):
        forms = resolve_match_forms("nđ 254/2026")
        assert "254/2026" in forms


class TestRobustness:
    def test_empty_and_noise(self):
        assert resolve_entities("") == []
        assert resolve_entities("xyzzy nothing here") == []

    def test_no_partial_word_match(self):
        # "VIC" must not fire inside "convince".
        assert ids("this is quite convincing") == []

    def test_entity_is_frozen(self):
        ent = resolve_entities("Sài Gòn")[0]
        assert isinstance(ent, Entity)
        assert isinstance(ent.match_forms, frozenset)


class TestAnalyzeWiring:
    def test_entity_ids_populated(self):
        prof = QueryUnderstanding().analyze("doanh thu Vingroup")
        assert prof.entity_ids == ["co:vingroup"]

    def test_entities_tokens_unchanged(self):
        # Raw token entities are untouched — resolution adds entity_ids,
        # so downstream scoring denominators don't shift.
        prof = QueryUnderstanding().analyze("giá vàng hôm nay")
        assert "vàng" in prof.entities or "giá" in prof.entities


class TestRouterSteering:
    router = SourceRouter()
    qu = QueryUnderstanding()

    def weights(self, q: str):
        return self.router.lane_weights(q, self.qu.analyze(q))

    def test_company_entity_pins_company_lane(self):
        # "VIC" alone carries no company keyword signal — only the entity.
        w = self.weights("VIC")
        assert w[SourceType.company] == 1.0
        assert w[SourceType.business] >= 0.8
        assert w[SourceType.finance] >= 0.5

    def test_legal_entity_pins_legal_lane(self):
        # "nđ" abbreviation carries no _LEGAL_RE keyword but resolves.
        w = self.weights("nđ 254/2026 có hiệu lực chưa")
        assert w[SourceType.legal] == 1.0

    def test_location_entity_lifts_places(self):
        w = self.weights("Sài Gòn")
        assert w[SourceType.places] >= 0.5
        assert w[SourceType.administrative] >= 0.5

    def test_product_entity_lifts_product(self):
        w = self.weights("VF8")
        assert w[SourceType.product] >= 0.8

    def test_no_entity_weights_unchanged(self):
        w = self.weights("a random english query")
        assert w[SourceType.company] == 0.0
        assert w[SourceType.legal] == 0.0
