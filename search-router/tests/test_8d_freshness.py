"""TASK-8D — intent detection for live / numeric / time-sensitive queries."""

from pipeline.freshness import (
    detect_live_numeric,
    rephrase_for_widening,
)


class TestLiveNumericDetection:
    CASES = [
        ("bitcoin price", "week"),
        ("btc price today", "day"),
        ("usd/vnd rate", "week"),
        ("epl match results", "week"),
        ("gia heo hôm nay", "day"),
        ("giá vàng hôm nay", "day"),
        ("xổ số miền bắc", "week"),
        ("lịch thi đấu bóng đá", "week"),
        ("weather today", "day"),
        ("tỷ giá usd", "week"),
    ]

    def test_flags_live_numeric_queries(self):
        for q, _ in self.CASES:
            sig = detect_live_numeric(q)
            assert sig.time_sensitive is True, q
            assert sig.add_news is True, q

    def test_time_range_values(self):
        for q, expected in self.CASES:
            sig = detect_live_numeric(q)
            assert sig.time_range == expected, q

    def test_normal_queries_not_flagged(self):
        for q in [
            "cách làm bánh mì",
            "what is quantum computing",
            "so sánh iphone và samsung",
            "định nghĩa blockchain",
            "lịch sử việt nam",
        ]:
            sig = detect_live_numeric(q)
            assert sig.time_sensitive is False, q
            assert sig.time_range is None, q
            assert sig.add_news is False, q

    def test_empty_query_not_flagged(self):
        sig = detect_live_numeric("")
        assert sig.time_sensitive is False

    def test_language_detection(self):
        assert detect_live_numeric("giá vàng hôm nay").language == "vi"
        assert detect_live_numeric("bitcoin price").language == "en"
        assert detect_live_numeric("gia heo").language == "vi"

    def test_numeric_vs_live_flags(self):
        numeric = detect_live_numeric("tỷ giá usd")
        assert numeric.numeric is True
        assert numeric.live is False

        live = detect_live_numeric("xổ số hôm nay")
        assert live.live is True
        assert live.time_range == "day"


class TestEvergreenNotRestricted:
    """CRITICAL finding: evergreen queries must NOT be narrowed by time_range."""

    def test_evergreen_queries_get_no_time_range(self):
        for q in ["what is bitcoin", "bitcoin history", "API rate limiting"]:
            sig = detect_live_numeric(q)
            assert sig.time_sensitive is False, q
            assert sig.time_range is None, q
            assert sig.add_news is False, q
            assert sig.evergreen is True, q

    def test_numeric_queries_still_get_time_range(self):
        for q in ["bitcoin price", "usd/vnd rate", "gia heo", "tỷ giá usd"]:
            sig = detect_live_numeric(q)
            assert sig.time_sensitive is True, q
            assert sig.time_range == "week", q

    def test_live_marker_beats_evergreen(self):
        sig = detect_live_numeric("what is bitcoin price today")
        assert sig.live is True
        assert sig.time_sensitive is True
        assert sig.time_range == "day"


class TestIntentPrecedence:
    """MAJOR r2: explicit price/event markers must beat a bare definitional "what"."""

    def test_what_is_price_gets_week(self):
        sig = detect_live_numeric("what is bitcoin price")
        assert sig.time_sensitive is True
        assert sig.time_range == "week"
        assert sig.numeric is True

    def test_what_is_event_result_gets_week(self):
        sig = detect_live_numeric("what is EPL result")
        assert sig.time_sensitive is True
        assert sig.time_range == "week"
        assert sig.event is True

    def test_what_is_bitcoin_stays_evergreen(self):
        sig = detect_live_numeric("what is bitcoin")
        assert sig.time_sensitive is False
        assert sig.time_range is None
        assert sig.evergreen is True


class TestRephraseForWidening:
    def test_english_numeric_appends_price_term(self):
        sig = detect_live_numeric("bitcoin price")
        assert rephrase_for_widening("bitcoin price", sig) == "bitcoin price rate"

    def test_vi_numeric_appends_hom_nay(self):
        sig = detect_live_numeric("giá heo")
        assert rephrase_for_widening("giá heo", sig) == "giá heo hôm nay"

    def test_returns_none_when_all_keywords_present(self):
        q = "bitcoin price rate quoted"
        sig = detect_live_numeric(q)
        assert rephrase_for_widening(q, sig) is None

    def test_rephrase_detects_signal_when_omitted(self):
        assert rephrase_for_widening("giá vàng") == "giá vàng hôm nay"


class TestSignalSpecificWidening:
    """MAJOR finding: widening suffixes must match query intent, not leak."""

    def test_event_query_gets_no_price_suffix(self):
        sig = detect_live_numeric("epl results")
        widened = rephrase_for_widening("epl results", sig)
        assert widened is not None
        assert "price" not in widened
        assert "giá" not in widened

    def test_vietnamese_event_query_gets_no_price_suffix(self):
        sig = detect_live_numeric("bóng đá kết quả")
        widened = rephrase_for_widening("bóng đá kết quả", sig)
        assert widened is not None
        assert "giá" not in widened

    def test_toneless_vietnamese_numeric_is_vietnamese(self):
        sig = detect_live_numeric("gia heo")
        assert sig.language == "vi"
        widened = rephrase_for_widening("gia heo", sig)
        assert widened is not None
        assert "price" not in widened
        assert widened == "gia heo hôm nay"

    def test_english_evergreen_not_widened(self):
        sig = detect_live_numeric("what is bitcoin")
        assert rephrase_for_widening("what is bitcoin", sig) is None
