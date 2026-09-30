"""P12 — VN benchmark datasets + authority metric."""

import pytest

from eval.datasets import list_datasets, load_dataset
from eval.metrics import authority_mean, evaluate_query, report_authority
from eval.vn_authority import load_scorer

VN_DATASETS = [
    "vietnam/general",
    "vietnam/news",
    "vietnam/government",
    "vietnam/legal",
    "vietnam/company",
    "vietnam/places",
    "vietnam/market",
    "vietnam/product",
    "vietnam/community",
    "vietnam/deep_research",
]


def test_vn_datasets_are_listed():
    names = list_datasets()
    for name in VN_DATASETS:
        assert name in names, name


@pytest.mark.parametrize("name", VN_DATASETS)
def test_vn_datasets_load_and_validate(name):
    queries = load_dataset(name)
    assert len(queries) >= 8, name
    for q in queries:
        assert q.query.strip(), name
        assert q.expected_urls, (name, q.query)
        assert q.region in {"vn", "global"}, (name, q.query)


def test_vn_dataset_categories_use_vn_taxonomy():
    cats = set()
    for name in VN_DATASETS:
        cats.update(q.category for q in load_dataset(name))
    assert cats <= {
        "vi_general",
        "vn_news",
        "vn_government",
        "vn_legal",
        "vn_market",
        "vn_product",
        "vn_company",
        "vn_places",
        "vn_community",
        "vn_research",
    }
    assert "vn_legal" in cats  # legal lane is exercised


def test_authority_mean_unique_domains():
    scorer = {"vbpl.vn": 1.0, "otofun.net": 0.3}.get
    val = authority_mean(["vbpl.vn", "vbpl.vn", "otofun.net"], scorer)
    assert val == pytest.approx(0.65, abs=0.01)


def test_authority_mean_empty_and_none():
    assert authority_mean([], None) == 0.0
    assert authority_mean(["a.vn"], None) == 0.0


def test_report_authority_from_saved_report():
    report = {
        "per_query": [
            {"retrieved_domains": ["chinhphu.vn", "vnexpress.net"]},
            {"retrieved_domains": ["otofun.net"]},
        ]
    }
    scorer = {"chinhphu.vn": 1.0, "vnexpress.net": 0.9, "otofun.net": 0.2}.get
    out = report_authority(report, scorer)
    assert out["queries"] == 2
    assert out["mean"] == pytest.approx((0.95 + 0.2) / 2, abs=0.01)


def test_report_authority_without_scorer_notes():
    out = report_authority({"per_query": [{"retrieved_domains": ["a.vn"]}]}, None)
    assert out["mean"] == 0.0
    assert "note" in out


def test_evaluate_query_adds_authority_only_with_scorer():
    urls = ["https://chinhphu.vn/x", "https://vnexpress.net/y"]
    expected = ["chinhphu.vn"]
    plain = evaluate_query(urls, expected, top_k=10)
    assert "authority" not in plain
    scorer = {"chinhphu.vn": 1.0, "vnexpress.net": 0.9}.get
    scored = evaluate_query(urls, expected, top_k=10, authority_scorer=scorer)
    assert scored["authority"] == pytest.approx(0.95, abs=0.01)


def test_load_scorer_uses_router_table_or_none():
    scorer = load_scorer()
    if scorer is None:
        pytest.skip("search-router not importable from this checkout")
    # Known VN authority rows from ranking/authority.py DOMAIN_MAP
    assert scorer("chinhphu.vn") >= scorer("unknown-blog.example")
    assert scorer("vbpl.vn") > 0


def test_load_scorer_vertical_legal():
    scorer = load_scorer(vertical="legal")
    if scorer is None:
        pytest.skip("search-router not importable from this checkout")
    # Legal vertical: official legal sources outrank general news
    assert scorer("vbpl.vn") >= scorer("otofun.net")
