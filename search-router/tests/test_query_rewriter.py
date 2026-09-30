"""Tests for the Query Rewriter (wave-9)."""

from core.query_rewriter import rewrite_variants


def test_original_always_first():
    variants = rewrite_variants("latest ai news", family="news")
    assert variants[0] == "latest ai news"


def test_news_year_injection():
    variants = rewrite_variants("latest ai regulation news", family="news")
    assert any("2026" in v for v in variants)


def test_no_year_when_present():
    variants = rewrite_variants("RTX 5090 price 2025", family="web")
    assert all("2026" not in v for v in variants)


def test_comparison_splits_sides():
    variants = rewrite_variants("so sánh iPhone 17 Pro vs Galaxy S26", family="web")
    assert "iPhone 17 Pro" in variants
    assert "Galaxy S26" in variants


def test_comparison_single_side_no_split():
    variants = rewrite_variants("so sánh A và B", family="web")
    assert len(variants) == 1  # too-short sides are not real comparisons


def test_code_gets_docs_qualifier_when_missing():
    variants = rewrite_variants("python async fetch", family="code")
    assert len(variants) >= 2
    assert "docs" in variants[-1]


def test_code_no_duplicate_qualifier():
    variants = rewrite_variants("python async def fetch example", family="code")
    assert len(variants) == 1  # already has 'example', no rewrite needed


def test_definition_gets_qualifier():
    variants = rewrite_variants("what is a transformer", family="research")
    assert any("definition" in v or "meaning" in v for v in variants)


def test_howto_vietnamese():
    variants = rewrite_variants("cách viết fastapi middleware", family="web")
    assert any("ví dụ" in v for v in variants)


def test_no_more_than_three_variants():
    variants = rewrite_variants("so sánh iPhone vs Galaxy price và specs review", family="web")
    assert 1 <= len(variants) <= 3


def test_vietnamese_keeps_diacritics():
    variants = rewrite_variants("giá vàng SJC hôm nay", family="news")
    for v in variants:
        assert "á" in v or "à" in v or "ã" in v or "ạ" in v  # tones never stripped
