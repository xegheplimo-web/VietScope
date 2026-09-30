"""Unit tests for canonicalization, deduplication, and freshness helpers."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from canonical import (
    canonical_identity,
    canonical_url,
    cluster_sources,
    content_fingerprint,
    freshness_score,
    near_duplicate,
    query_ttl_hint,
)


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def test_scheme_variants_share_one_canonical_identity():
    urls = [
        "http://www.Example.com/story/?id=7&utm_source=newsletter",
        "https://m.example.com/story?id=7&utm_medium=email",
        "example.com/story/?utm_campaign=launch&id=7#comments",
    ]
    assert {canonical_identity(url) for url in urls} == {"example.com/story?id=7"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.example.com/", "https://example.com"),
        ("https://m.example.com/path/", "https://example.com/path"),
        # Explicit scheme is preserved — a canonical URL must stay
        # fetchable; upgrading http→https without redirect evidence can
        # produce dead links.
        ("HTTP://EXAMPLE.COM/a#section", "http://example.com/a"),
        ("http://example.com/a", "http://example.com/a"),
        # Scheme-less inputs default to https.
        ("//www.example.com/a/", "https://example.com/a"),
        ("example.com/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
    ],
)
def test_canonical_url_normalizes_web_url(url, expected):
    assert canonical_url(url) == expected


def test_canonical_url_sorts_semantic_query_and_removes_tracking():
    url = "https://example.com/watch?v=abc&utm_term=x&b=2&a=1&fbclid=tracking"
    assert canonical_url(url) == "https://example.com/watch?a=1&b=2&v=abc"


@pytest.mark.parametrize("value", ["", "   ", "not a url", "https:///missing-host"])
def test_canonical_url_rejects_empty_or_invalid_input(value):
    assert canonical_url(value) == ""


def test_content_fingerprint_normalizes_html_entities_and_whitespace():
    html = "<article><p>Xin&nbsp;chào &amp; hello</p></article>"
    text = "Xin chào & hello"
    assert content_fingerprint(html) == content_fingerprint(text)


def test_content_fingerprint_ignores_non_visible_html():
    first = "<style>hidden</style><p>Visible text</p><script>also hidden</script>"
    assert content_fingerprint(first) == content_fingerprint("Visible text")


def test_content_fingerprint_handles_self_closing_ignored_tags():
    assert content_fingerprint("<script/>Visible") == content_fingerprint("Visible")


def test_content_fingerprint_is_sha256_and_distinguishes_content():
    fingerprint = content_fingerprint("alpha")
    assert len(fingerprint) == 64
    assert fingerprint != content_fingerprint("beta")


def test_content_fingerprint_handles_empty_input():
    assert content_fingerprint("") == hashlib.sha256(b"").hexdigest()


def test_near_duplicate_accepts_equal_fingerprints():
    fingerprint = content_fingerprint("same content")
    assert near_duplicate(fingerprint, fingerprint)


def test_near_duplicate_detects_small_text_change():
    first = "mot hai ba bon nam sau bay tam chin muoi"
    second = "mot hai ba bon nam sau bay tam chin muoi moi"
    assert near_duplicate(first, second)


def test_near_duplicate_rejects_unrelated_text_and_different_hashes():
    assert not near_duplicate("red green blue", "mot hai ba")
    assert not near_duplicate(content_fingerprint("one"), content_fingerprint("two"))


def test_near_duplicate_handles_empty_text():
    assert near_duplicate("", "")
    assert not near_duplicate("", "non-empty")


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_near_duplicate_validates_threshold(threshold):
    with pytest.raises(ValueError):
        near_duplicate("one", "two", threshold)


def test_cluster_sources_groups_aliases_and_preserves_original_urls():
    urls = [
        "https://www.example.com/post/?utm_source=a",
        "http://m.example.com/post",
        "https://other.example/post",
    ]
    clusters = cluster_sources(urls)
    assert clusters == {
        "https://example.com/post": urls[:2],
        "https://other.example/post": [urls[2]],
    }


def test_cluster_sources_ignores_empty_and_repeated_aliases():
    url = "https://example.com"
    assert cluster_sources(["", url, url]) == {url: [url]}
    assert cluster_sources([]) == {}


def test_freshness_score_defaults_to_neutral_without_metadata():
    assert freshness_score({}) == 0.5
    assert freshness_score({"published_at": "not-a-date"}) == 0.5


def test_freshness_score_rewards_recent_and_penalizes_old_documents():
    recent = freshness_score({"published_at": _iso_days_ago(2)})
    old = freshness_score({"published_at": _iso_days_ago(1000)})
    assert 0.0 <= old < recent <= 1.0


def test_freshness_score_reads_json_ld_date_published():
    metadata = {"json_ld": {"@type": "NewsArticle", "datePublished": _iso_days_ago(3)}}
    assert freshness_score(metadata) == 0.9


def test_freshness_score_reads_serialized_json_ld():
    published = _iso_days_ago(3)
    metadata = {"json_ld": '{"@type":"Article","datePublished":"' + published + '"}'}
    assert freshness_score(metadata) == 0.9


def test_freshness_score_reads_meta_tag_records():
    metadata = {"meta": [{"property": "article:published_time", "content": _iso_days_ago(3)}]}
    assert freshness_score(metadata) == 0.9


def test_freshness_score_uses_first_seen_only_as_fallback():
    old_published = freshness_score(
        {"published_at": _iso_days_ago(1000), "first_seen_at": _iso_days_ago(1)}
    )
    recent_first_seen = freshness_score({"first_seen_at": _iso_days_ago(1)})
    assert old_published < recent_first_seen


def test_freshness_score_adds_bonus_for_changed_content_hash():
    assert freshness_score(
        {"content_hash": "new", "previous_content_hash": "old"}
    ) == pytest.approx(0.6)


def test_freshness_score_caps_changed_recent_content_at_one():
    score = freshness_score({"updated_at": _iso_days_ago(0), "content_hash_new": True})
    assert score == 1.0


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("tin tức hôm nay", "minutes"),
        ("tin tuc hom nay", "minutes"),
        ("latest AI news", "minutes"),
        ("mấy giờ rồi", "minutes"),
        ("quán cà phê đang mở", "hours"),
        ("quan ca phe dang mo", "hours"),
        ("restaurants near me now", "hours"),
        ("gio mo cua sieu thi", "hours"),
        ("cách dùng pathlib", "static"),
        ("cach dung pathlib", "static"),
        ("cu phap Python", "static"),
        ("Python list syntax", "static"),
        ("lịch sử Việt Nam", "days"),
        ("", "days"),
    ],
)
def test_query_ttl_hint(query, expected):
    assert query_ttl_hint(query) == expected
