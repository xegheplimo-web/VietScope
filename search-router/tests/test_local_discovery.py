"""LOCAL-1 — bounded local discovery unit tests (deterministic, no I/O)."""

import pytest
from core.local_discovery import (
    _category_compatible,
    _query_relevant,
    dedupe_local_candidates,
    evaluate_local_quality,
    expand_local_query,
    extract_specialty,
    local_quality_sufficient,
    locality_of,
    tag_lane_origin,
)
from models import BusinessEntity


def _ent(name: str, **kw) -> BusinessEntity:
    return BusinessEntity(name=name, **kw)


# ─── expand_local_query ──────────────────────────────────────────────────────


class TestExpandLocalQuery:
    def test_yen_dung_late_night(self):
        out = expand_local_query("quán ăn đêm Yên Dũng")
        assert out[0] == "quán ăn đêm Yên Dũng"
        assert 1 < len(out) <= 8
        assert any("khuya" in v for v in out[1:])
        assert any(v.endswith("Yên Dũng") for v in out[1:])
        lowered = [v.lower() for v in out]
        assert len(lowered) == len(set(lowered))
        assert all(v.strip() for v in out)

    def test_empty_and_blank(self):
        assert expand_local_query("") == [""]
        assert expand_local_query("   ") == ["   "]

    def test_cafe_no_locality(self):
        out = expand_local_query("cafe")
        assert out[0] == "cafe"
        assert len(out) <= 8
        assert all(v.strip() for v in out)

    @pytest.mark.parametrize(
        "query,synonyms",
        [
            ("quán ăn", ("nhà hàng", "quán nhậu")),
            ("cafe", ("quán cà phê", "cà phê")),
            ("nhà thuốc", ("hiệu thuốc", "tiệm thuốc")),
            ("cây xăng", ("xăng dầu", "trạm xăng")),
            ("ăn khuya", ("quán ăn khuya", "nhà hàng mở muộn", "quán nhậu")),
        ],
    )
    def test_category_synonyms(self, query, synonyms):
        out = expand_local_query(query)
        assert any(any(s in v for s in synonyms) for v in out[1:])

    def test_max_variants_respected(self):
        assert len(expand_local_query("quán ăn đêm Yên Dũng", max_variants=4)) <= 4
        assert expand_local_query("quán ăn đêm Yên Dũng", max_variants=1) == [
            "quán ăn đêm Yên Dũng"
        ]

    def test_no_category_match(self):
        assert expand_local_query("latest python release") == ["latest python release"]

    def test_trailing_punct_and_whitespace(self):
        out = expand_local_query("  nhà thuốc Yên Dũng?  ")
        assert out[0] == "nhà thuốc Yên Dũng"

    def test_accentless_query_still_expands(self):
        out = expand_local_query("nha thuoc Yen Dung")
        assert out[0] == "nha thuoc Yen Dung"
        assert any("thuốc" in v for v in out[1:])


class TestLocalityOf:
    def test_trailing_locality(self):
        assert locality_of("quán ăn đêm Yên Dũng") == "Yên Dũng"

    def test_proximity_marker_stripped(self):
        assert locality_of("nhà thuốc gần chợ Bến Thành") == "chợ Bến Thành"

    def test_no_locality(self):
        assert locality_of("quán ăn") == ""

    def test_no_category(self):
        assert locality_of("Yên Dũng") == ""


# ─── local_quality_sufficient ────────────────────────────────────────────────


def _useful(name: str, i: int = 0, **kw) -> BusinessEntity:
    kw.setdefault("category", "restaurant")
    return _ent(name, lat=10.0 + i * 0.01, lon=106.0, **kw)


class TestLocalQualityGate:
    def test_five_useful_satisfies_limit_20(self):
        ents = [_useful(f"POI {i}", i) for i in range(5)]
        assert local_quality_sufficient(ents, requested_limit=20) is True

    def test_four_useful_fails_limit_20(self):
        ents = [_useful(f"POI {i}", i) for i in range(4)]
        assert local_quality_sufficient(ents, requested_limit=20) is False

    def test_one_useful_satisfies_limit_1(self):
        assert local_quality_sufficient([_useful("A")], requested_limit=1) is True

    def test_named_without_geo_not_useful(self):
        ents = [_ent("A", category="cafe"), _ent("B", category="cafe"), _ent("C")]
        assert local_quality_sufficient(ents, requested_limit=20) is False

    def test_empty_is_insufficient(self):
        assert local_quality_sufficient([], requested_limit=20) is False

    def test_canonical_origin_counts_without_category(self):
        # A canonical-lane entity with coords but no category still counts.
        ents = [_useful("A", category="")]
        ents[0].origin = "canonical"
        assert local_quality_sufficient(ents, requested_limit=1) is True

    def test_web_origin_without_category_not_useful(self):
        ents = [_useful("A", category="")]
        ents[0].origin = "web_discovery"
        assert local_quality_sufficient(ents, requested_limit=1) is False

    def test_reason_strings(self):
        ok, reason = evaluate_local_quality([_useful("A")], requested_limit=1)
        assert ok is True and reason == "sufficient"
        ok, reason = evaluate_local_quality([], requested_limit=20)
        assert ok is False and reason == "insufficient_useful"


# ─── dedupe_local_candidates ─────────────────────────────────────────────────


class TestDedupeLocalCandidates:
    def test_same_phone_merges(self):
        a = _ent("Quán A", phone="+84 987 654 321", source_url="https://a.com/x")
        b = _ent("Quán A Khuya", phone="0987 654 321", source_url="https://b.com/y")
        out = dedupe_local_candidates([a, b])
        assert len(out) == 1
        assert out[0].supporting_source_count == 2

    def test_same_domain_and_similar_name_merges(self):
        a = _ent("Phở Hòa Pasteur", website="https://phohoa.example.com")
        b = _ent("Phở Hòa Pasteur Q3", website="http://www.phohoa.example.com/menu")
        assert len(dedupe_local_candidates([a, b])) == 1

    def test_similar_name_close_geo_merges(self):
        a = _ent("Cafe Mộc", lat=10.0, lon=106.0)
        b = _ent("Cafe Mộc.", lat=10.0005, lon=106.0005)  # ~78 m away
        assert len(dedupe_local_candidates([a, b])) == 1

    def test_similar_name_far_geo_keeps_both(self):
        a = _ent("Cafe Mộc", lat=10.0, lon=106.0)
        b = _ent("Cafe Mộc.", lat=10.05, lon=106.0)  # ~5.5 km away
        assert len(dedupe_local_candidates([a, b])) == 2

    def test_similar_name_similar_address_merges(self):
        a = _ent("Nhà Hàng Yên Dũng", address="TDP 3, Yên Dũng, Bắc Ninh")
        b = _ent("Nhà hàng Yên Dũng 2", address="TDP 3, Yên Dũng, Bac Ninh")
        assert len(dedupe_local_candidates([a, b])) == 1

    def test_motivating_yen_dung_case_merges(self):
        # Real-world dup: same restaurant surfaced under short/long names —
        # the shared phone is the merge key.
        a = _ent("Nhà Hàng Yên Dũng", phone="0901 234 567", address="Yên Dũng")
        b = _ent("Nhà hàng Yên Dũng - Đặc Sản Trâu Tươi", phone="+84901234567")
        out = dedupe_local_candidates([a, b])
        assert len(out) == 1
        assert out[0].name == "Nhà Hàng Yên Dũng"

    def test_distinct_entities_untouched(self):
        a = _ent("Cafe A", lat=10.0, lon=106.0)
        b = _ent("Nhà thuốc B", lat=10.0, lon=106.0)
        assert len(dedupe_local_candidates([a, b])) == 2

    def test_merge_fills_empty_fields(self):
        a = _ent("Cafe X", address="12 Lê Lợi", source_url="https://a.com/x")
        b = _ent(
            "Cafe X",
            address="12 Lê Lợi",
            phone="0901234567",
            hours="08:00 - 22:00",
            website="https://cafex.vn",
            source_url="https://b.com/y",
        )
        out = dedupe_local_candidates([a, b])
        assert len(out) == 1
        e = out[0]
        assert e.phone == "0901234567"
        assert e.hours == "08:00 - 22:00"
        assert e.website == "https://cafex.vn"
        assert e.source_url == "https://a.com/x"  # surviving identity kept
        assert e.supporting_source_count == 2

    def test_merge_never_overwrites_with_empty(self):
        a = _ent(
            "Cafe X",
            address="12 Lê Lợi",
            phone="0111222333",
            website="https://a.vn",
        )
        b = _ent("Cafe X", address="12 Lê Lợi")
        out = dedupe_local_candidates([a, b])
        assert len(out) == 1
        assert out[0].phone == "0111222333"
        assert out[0].website == "https://a.vn"

    def test_merge_never_invents_geo(self):
        a = _ent("Cafe X", address="12 Lê Lợi")
        b = _ent("Cafe X", address="12 Lê Lợi")
        out = dedupe_local_candidates([a, b])
        assert out[0].lat is None and out[0].lon is None

    def test_richer_later_entity_becomes_base(self):
        sparse = _ent("Nhà Hàng Yên Dũng", address="12 Lê Lợi")
        rich = _ent(
            "Nhà hàng Yên Dũng 2",
            address="12 Lê Lợi",
            phone="0901234567",
            website="https://cafex.vn",
            lat=10.0,
            lon=106.0,
        )
        out = dedupe_local_candidates([sparse, rich])
        assert len(out) == 1
        assert out[0].name == "Nhà hàng Yên Dũng 2"
        assert out[0].lat == 10.0 and out[0].lon == 106.0

    def test_same_source_url_counts_once(self):
        a = _ent("Cafe X", address="12 Lê Lợi", source_url="https://a.com/x")
        b = _ent("Cafe X", address="12 Lê Lợi", source_url="https://a.com/x")
        out = dedupe_local_candidates([a, b])
        assert out[0].supporting_source_count == 1


# ─── origin / verified / precision contract ──────────────────────────────────


class TestLaneOriginContract:
    def test_canonical_is_verified_exact(self):
        e = _ent("POI")
        tag_lane_origin([e], "canonical")
        assert e.origin == "canonical"
        assert e.verified is True
        assert e.location_precision == "exact"

    def test_osm_lanes_exact_not_verified(self):
        e = _ent("POI")
        tag_lane_origin([e], "osm_local")
        assert e.origin == "osm_local" and e.verified is False
        assert e.location_precision == "exact"
        tag_lane_origin([e], "osm_live")
        assert e.origin == "osm_live" and e.location_precision == "exact"

    def test_web_precision(self):
        street = _ent("POI", address="12 Lê Lợi")
        area = _ent("POI")
        unknown = _ent("POI")
        tag_lane_origin([street], "web_discovery", locality="Yên Dũng")
        tag_lane_origin([area], "web_discovery", locality="Yên Dũng")
        tag_lane_origin([unknown], "web_discovery", locality="")
        assert street.location_precision == "street"
        assert area.location_precision == "area"
        assert unknown.location_precision == "unknown"
        assert street.origin == "web_discovery" and street.verified is False

    def test_new_fields_have_defaults(self):
        e = _ent("POI")
        assert e.origin == ""
        assert e.verified is False
        assert e.location_precision == "unknown"
        assert e.supporting_source_count == 1


# ─── query-aware quality gate + folded matching ───────────────────────────────


class TestQueryAwareQualityGate:
    """evaluate_local_quality with query/category must filter by relevance."""

    def test_category_param_classified(self):
        ents = [
            _ent("Nhà Thuốc A", category="pharmacy", lat=21.21, lon=106.14),
            _ent("Nhà Thuốc B", category="pharmacy", lat=21.22, lon=106.15),
            _ent("Nhà Thuốc C", category="pharmacy", lat=21.23, lon=106.16),
        ]
        ok, reason = evaluate_local_quality(ents, requested_limit=3, category="nhà thuốc")
        assert ok is True and reason == "sufficient"

    def test_irrelevant_entities_do_not_close_gate(self):
        ents = [
            _ent("Nhà Thuốc A", category="pharmacy", lat=21.21, lon=106.14),
            _ent("Nhà Thuốc B", category="pharmacy", lat=21.22, lon=106.15),
            _ent("Nhà Thuốc C", category="pharmacy", lat=21.23, lon=106.16),
        ]
        # restaurant intent + pharmacy rows → not useful → insufficient
        ok, reason = evaluate_local_quality(ents, requested_limit=3, query="quán ăn Yên Dũng")
        assert ok is False and reason == "insufficient_useful"

    def test_inferred_category_from_name(self):
        ents = [
            _ent("Nhà Thuốc Tâm An", category="", lat=21.21, lon=106.14),
            _ent("Nhà Thuốc Bình An", category="", lat=21.22, lon=106.15),
            _ent("Hiệu Thuốc C", category="", lat=21.23, lon=106.16),
        ]
        ok, _ = evaluate_local_quality(ents, requested_limit=3, query="nhà thuốc Yên Dũng")
        assert ok is True

    def test_brand_query_named_entity_counts(self):
        ents = [
            _ent("Circle K - Hàng Bài", category="convenience", lat=21.21, lon=106.14),
            _ent("StarMart", category="convenience", lat=21.22, lon=106.15),
        ]
        ok, reason = evaluate_local_quality(ents, requested_limit=1, query="circle k gần tôi")
        assert ok is True
        assert reason == "sufficient"

    def test_brand_query_unnamed_entities_do_not_close_gate(self):
        ents = [
            _ent("StarMart A", category="convenience", lat=21.21, lon=106.14),
            _ent("7-Eleven B", category="convenience", lat=21.22, lon=106.15),
            _ent("Minimart C", category="store", lat=21.23, lon=106.16),
        ]
        ok, reason = evaluate_local_quality(ents, requested_limit=3, query="circle k gần tôi")
        assert ok is False
        assert reason == "insufficient_useful"


class TestCategoryHelpers:
    def test_empty_inputs(self):
        assert _category_compatible("", "restaurant") is False
        assert _category_compatible("cafe", "") is False
        assert _query_relevant(_ent("A"), "", "") is False
        assert _query_relevant(_ent("A"), "circle k", "") is False

    def test_food_family_compatible(self):
        assert _category_compatible("cafe", "restaurant") is True
        assert _category_compatible("pharmacy", "restaurant") is False

    def test_query_relevant_inferred(self):
        e = _ent("Nhà Thuốc Tâm An", category="", description="bán thuốc")
        assert _query_relevant(e, "", "pharmacy") is True
        assert _query_relevant(e, "", "restaurant") is False

    def test_query_relevant_by_name(self):
        e = _ent("Circle K - Hoàn Kiếm", category="convenience")
        assert _query_relevant(e, "circle k gần tôi", "") is True
        other = _ent("StarMart - Hoàn Kiếm", category="convenience")
        assert _query_relevant(other, "circle k gần tôi", "") is False


class TestFoldedSpecialty:
    def test_accentless_specialty_matches(self):
        specialty, variants = extract_specialty("gio cha Yen Dung")
        assert specialty == "giò chả"
        assert variants

    def test_standalone_specialty_expands(self):
        out = expand_local_query("giò chả Yên Dũng")
        assert out[0] == "giò chả Yên Dũng"
        assert len(out) > 1

    def test_no_specialty(self):
        specialty, variants = extract_specialty("quán ăn Hà Nội")
        assert specialty is None and variants == ()
