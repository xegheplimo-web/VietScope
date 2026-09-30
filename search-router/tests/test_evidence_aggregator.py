"""Tests for the Evidence Aggregator layer (wave-9)."""

import pytest
from models import SearchResultItem
from pipeline.evidence_aggregator import aggregate


def _item(url, title="t", desc="d", score=1.0, engine="bing", date=None):
    return SearchResultItem(
        url=url,
        title=title,
        description=desc,
        score=score,
        engine=engine,
        published_date=date,
    )


def test_aggregate_empty():
    pkg = aggregate({}, "nothing", max_results=5)
    assert pkg.sources == []
    assert pkg.aggregation.total_candidates == 0
    assert pkg.aggregation.providers_used == []


def test_aggregate_dedupes_canonical_url():
    """Same canonical URL from two providers → one source, dedup counted."""
    r1 = _item("https://docs.docker.com/engine/", score=0.9, engine="bing")
    r2 = _item("https://docs.docker.com/engine/", score=0.5, engine="qwant")
    pkg = aggregate({"searxng": [r1], "ddgs": [r2]}, "docker docs", max_results=5)
    assert len(pkg.sources) == 1
    assert pkg.aggregation.total_candidates == 2
    assert pkg.aggregation.deduplicated >= 1
    assert pkg.aggregation.providers_used == ["searxng", "ddgs"]


def test_aggregate_labels_trust_by_domain():
    docs = _item("https://docs.docker.com/engine/")
    gov = _item("https://www.gov.vn/ai-report")
    forum = _item("https://stackoverflow.com/q/1")
    reddit = _item("https://www.reddit.com/r/ai/")
    pkg = aggregate({"searxng": [docs, gov, forum, reddit]}, "ai docs", max_results=10, lang="en")
    by_url = {s.url: s for s in pkg.sources}
    assert by_url["https://docs.docker.com/engine/"].trust == "high"
    assert by_url["https://www.gov.vn/ai-report"].authority_type == "government"
    assert by_url["https://www.reddit.com/r/ai/"].trust == "low"
    assert by_url["https://stackoverflow.com/q/1"].authority_type in (
        "forum",
        "vendor_website",
    )


def test_aggregate_vn_domains_labeled():
    """Mapped VN official/vendor sites and unmapped .vn get fair labels."""
    sjc = _item("https://sjc.com.vn/gia-vang/", title="SJC giá vàng")
    gov = _item("https://chinhphu.vn/", title="Cổng TTCP")
    unmapped = _item("https://mystore123.vn/gia", title="cửa hàng")
    pkg = aggregate({"searxng": [sjc, gov, unmapped]}, "giá vàng", max_results=5, lang="vi")
    by_url = {s.url: s for s in pkg.sources}
    assert by_url["https://sjc.com.vn/gia-vang/"].authority_type == "vendor_website"
    assert by_url["https://sjc.com.vn/gia-vang/"].trust == "medium"
    assert by_url["https://chinhphu.vn/"].authority_type == "government"
    assert by_url["https://chinhphu.vn/"].trust == "high"
    assert by_url["https://mystore123.vn/gia"].authority_type == "specialist_blog"
    assert by_url["https://mystore123.vn/gia"].trust == "medium"


def test_aggregate_multi_provider_upgrades_unverified():
    """Same unknown domain from 2 providers → at least medium."""
    r1 = _item("https://randomblog.example.org/post", score=0.8, engine="bing")
    r2 = _item("https://randomblog.example.org/post", score=0.4, engine="google cse")
    pkg = aggregate({"searxng": [r1], "ddgs": [r2]}, "whatever", max_results=5)
    assert len(pkg.sources) == 1
    assert pkg.sources[0].trust in ("medium", "high")


def test_aggregate_penalizes_porn_spam():
    r = _item("https://kaskus.co.id/thread/bokep-jepang", score=0.9)
    pkg = aggregate({"searxng": [r]}, "test", max_results=5)
    assert pkg.sources[0].trust == "low"
    assert any("low-value" in reason for reason in pkg.sources[0].trust_reasons)


def test_aggregate_trust_distribution_counts():
    docs = _item("https://docs.docker.com/engine/")
    reddit = _item("https://www.reddit.com/r/docker/")
    pkg = aggregate({"searxng": [docs, reddit]}, "docker", max_results=5)
    dist = pkg.aggregation.trust_distribution
    assert dist.get("high", 0) >= 1
    assert dist.get("low", 0) >= 1


def test_aggregate_coverage_and_independent():
    r1 = _item("https://a.example.org/1")
    r2 = _item("https://b.example.org/2")
    pkg = aggregate({"searxng": [r1, r2]}, "x", max_results=4)
    assert pkg.aggregation.independent_sources == 2
    assert pkg.aggregation.coverage == pytest.approx(0.5)
