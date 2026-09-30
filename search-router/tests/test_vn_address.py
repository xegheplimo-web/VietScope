"""Tests for the Vietnamese address parser (core.vn_address)."""

from core.vn_address import (
    VNAddress,
    looks_like_vn_address,
    normalize_address,
    parse_vn_address,
)


class TestParseHanoi:
    def test_full_hanoi_address(self):
        addr = parse_vn_address(
            "Số 12 Ngõ 45 Trần Duy Hưng, phường Trung Hòa, quận Cầu Giấy, Hà Nội"
        )
        assert addr.house_number == "12"
        assert addr.lane == "Ngõ 45"
        assert addr.street == "Trần Duy Hưng"
        assert addr.ward == "Trung Hòa"
        assert addr.district == "Cầu Giấy"
        assert addr.city == "Thành phố Hà Nội"

    def test_hanoi_lowercase_extra_spaces(self):
        addr = parse_vn_address("số 12 ngõ 45 trần duy hưng , cầu giấy , hà nội")
        assert addr.house_number == "12"
        assert addr.street == "Trần Duy Hưng"
        assert addr.district == "Cầu Giấy"
        assert addr.city == "Thành phố Hà Nội"


class TestParseSaigon:
    def test_abbreviated_saigon(self):
        addr = parse_vn_address("55 Trần Hưng Đạo, P. Bến Thành, Q. 1, TP. HCM")
        assert addr.house_number == "55"
        assert addr.street == "Trần Hưng Đạo"
        assert addr.ward == "Bến Thành"
        assert addr.district == "1"
        assert addr.city == "Thành phố Hồ Chí Minh"

    def test_saigon_alias_sg(self):
        addr = parse_vn_address("12 Lê Lợi, Quận 1, Sài Gòn")
        assert addr.city == "Thành phố Hồ Chí Minh"
        assert addr.district == "1"

    def test_city_before_district(self):
        addr = parse_vn_address("TP. HCM, Quận 1")
        assert addr.city == "Thành phố Hồ Chí Minh"
        assert addr.district == "1"


class TestParseCanTho:
    def test_can_tho_with_street_number(self):
        addr = parse_vn_address("12A Đường 3/2, P. Xuân Khánh, Q. Ninh Kiều, Cần Thơ")
        assert addr.house_number == "12A"
        assert addr.street == "Đường 3/2"
        assert addr.ward == "Xuân Khánh"
        assert addr.district == "Ninh Kiều"
        assert addr.city == "Thành phố Cần Thơ"


class TestMissingComponents:
    def test_no_ward(self):
        addr = parse_vn_address("Trần Duy Hưng, Cầu Giấy, Hà Nội")
        assert addr.street == "Trần Duy Hưng"
        assert addr.district == "Cầu Giấy"
        assert addr.city == "Thành phố Hà Nội"
        assert addr.ward == ""

    def test_street_and_lane_only(self):
        addr = parse_vn_address("12 ngõ 45 trần duy hưng")
        assert addr.house_number == "12"
        assert addr.lane == "Ngõ 45"
        assert addr.street == "Trần Duy Hưng"
        assert addr.city == ""

    def test_empty_input(self):
        addr = parse_vn_address("")
        assert addr == VNAddress()


class TestNormalize:
    def test_normalize_title_case(self):
        assert (
            normalize_address("Số 12 ngõ 45 Trần Duy Hưng , Cầu Giấy , Hà Nội")
            == "Số 12 Ngõ 45 Trần Duy Hưng, Cầu Giấy, Hà Nội"
        )

    def test_normalize_keeps_house_letter(self):
        assert normalize_address("12a đường 3/2, p. xuân khánh") == ("12A Đường 3/2, P. Xuân Khánh")

    def test_normalize_empty(self):
        assert normalize_address("") == ""
        assert normalize_address("   ") == ""


class TestLooksLike:
    def test_full_address_true(self):
        assert looks_like_vn_address("Số 12 Ngõ 45 Trần Duy Hưng, Cầu Giấy, Hà Nội")

    def test_street_plus_city_true(self):
        assert looks_like_vn_address("Trần Hưng Đạo, TP. HCM")

    def test_keyword_only_true(self):
        assert looks_like_vn_address("quận Cầu Giấy somewhere")

    def test_english_text_false(self):
        assert not looks_like_vn_address("How to cook pho noodles at home")
        assert not looks_like_vn_address("")
