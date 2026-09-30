import pytest
from ranking.authority import authority_score, classify_source_type


def test_vnexpress_is_major_publication_and_high():
    assert authority_score("vnexpress.net") > 0.8
    assert classify_source_type("vnexpress.net") == "major_publication"


def test_foody_is_specialist_and_medium():
    assert 0.7 < authority_score("foody.vn") < 0.8
    assert classify_source_type("foody.vn") == "specialist_blog"


def test_giatot24gio_is_low_and_unknown_seo():
    assert authority_score("giatot24gio.com") < 0.25
    assert classify_source_type("giatot24gio.com") == "unknown_seo_site"


def test_hoclaixetphcm_is_low_and_unknown_seo():
    assert authority_score("hoclaixetphcm.com") < 0.25
    assert classify_source_type("hoclaixetphcm.com") == "unknown_seo_site"


def test_reuters_spoof_is_low():
    assert authority_score("reuters.com.evil.example") <= 0.3
    assert classify_source_type("reuters.com.evil.example") == "unknown_seo_site"


def test_reuters_subdomain_is_high():
    assert authority_score("sub.reuters.com") > 0.8
    assert classify_source_type("sub.reuters.com") == "major_publication"


def test_docs_subdomain_is_high_and_official():
    assert authority_score("docs.python.org") > 0.8
    assert classify_source_type("docs.python.org") == "official"


def test_github_is_vendor():
    assert authority_score("github.com") > 0.8
    assert classify_source_type("github.com") == "vendor_website"


def test_arxiv_is_research():
    assert authority_score("arxiv.org") > 0.9
    assert classify_source_type("arxiv.org") == "research_paper"


def test_unknown_site_gets_default():
    assert authority_score("some-unknown-blog.xyz") == pytest.approx(0.3)
    assert classify_source_type("some-unknown-blog.xyz") == "unknown_seo_site"
