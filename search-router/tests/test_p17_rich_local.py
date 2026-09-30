"""P17.1 — rich local search: open_now / min_rating / price_level / sort.

Covers the new ``LocalQuerySpec`` fields, the ranking components
(Bayesian ``rating_quality``, log1p ``popularity``, ``open_now_boost``),
the OpenSearch request-body filters + sort, the request-time open_now
post-filter, and the ``/v1/places/search`` parameter surface.

Infra-free like test_p17_serving.py: FakeOS mirrors the bool-query
filter semantics; PlaceService runs with a memory cache and a pinned
UTC+7 clock.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Any

import pytest
from serving.places.cache import PlaceCache
from serving.places.document import PlaceDocumentV1
from serving.places.os_index import build_search_body
from serving.places.query import SORT_MODES, parse_local_query
from serving.places.ranking import Candidate, RankWeights, rank
from serving.places.service import PlaceService, open_now

# Naive ``now`` values are read as UTC+7 wall time (see service.open_now).
WED_10AM = datetime(2026, 9, 30, 10, 0)  # Wednesday
WED_11PM = datetime(2026, 9, 30, 23, 0)

OPEN_DAY = {"Wednesday": ["08:00–22:00"]}
LATE_DAY = {"Wednesday": ["20:00–23:30"]}


def _doc(i: int, **kw) -> PlaceDocumentV1:
    base: dict[str, Any] = {
        "place_id": str(i),
        "business_id": None,
        "name": f"Quán P17.1 {i}",
        "normalized_name": f"quan p17.1 {i}",
        "category_ids": ["food"],
        "status": "open",
        "confidence": 0.8,
        "freshness_score": 0.9,
        "source_count": 2,
    }
    return PlaceDocumentV1(**{**base, **kw})


class FakeOS:
    """Spec-aware OpenSearch stand-in — mirrors build_search_body filters."""

    def __init__(self, docs: list[PlaceDocumentV1]):
        self.docs = list(docs)
        self.bodies: list[dict] = []

    async def search(self, spec, top_k):
        self.bodies.append(build_search_body(spec, top_k=top_k))
        out = []
        for d in self.docs:
            if spec.statuses and d.status not in spec.statuses:
                continue
            if spec.min_rating is not None and (d.rating is None or d.rating < spec.min_rating):
                continue
            if spec.price_level and d.price_level != spec.price_level:
                continue
            out.append((d, 1.0))
        return out[:top_k]

    async def get_doc(self, place_id):
        return next((d for d in self.docs if d.place_id == str(place_id)), None)


async def _nopool():
    return None


def _service(docs: list[PlaceDocumentV1], *, now: datetime = WED_10AM) -> PlaceService:
    return PlaceService(
        os_index=FakeOS(docs),
        cache=PlaceCache(redis_enabled=False),
        pool_getter=_nopool,
        clock=lambda: now,
    )


# ─── spec parsing ────────────────────────────────────────────────────────


class TestSpecParsing:
    def test_new_fields_default_none(self):
        spec = parse_local_query(q="cafe")
        assert spec.open_now is None
        assert spec.min_rating is None
        assert spec.price_level is None
        assert spec.sort is None

    def test_open_now_bool(self):
        assert parse_local_query(open_now=True).open_now is True
        assert parse_local_query(open_now=False).open_now is False

    def test_min_rating(self):
        assert parse_local_query(min_rating=4.5).min_rating == 4.5
        # Non-positive ratings carry no filter intent — dropped, not clamped.
        assert parse_local_query(min_rating=0).min_rating is None
        assert parse_local_query(min_rating=-2).min_rating is None

    def test_price_level(self):
        assert parse_local_query(price_level="₫₫").price_level == "₫₫"
        assert parse_local_query(price_level="$$").price_level == "$$"
        assert parse_local_query(price_level="   ").price_level is None
        assert parse_local_query(price_level=None).price_level is None

    def test_sort_vocabulary(self):
        for mode in SORT_MODES:
            assert parse_local_query(sort=mode).sort == mode
        assert parse_local_query(sort="RATING").sort == "rating"
        assert parse_local_query(sort="bogus").sort is None
        assert parse_local_query(sort="").sort is None


# ─── ranking components ──────────────────────────────────────────────────


class TestRankComponents:
    def test_bayesian_rating_quality(self):
        """5.0 from 1 review must not outrank 4.8 from 2500 — shrinkage."""
        thin = Candidate(doc=_doc(1, rating=5.0, review_count=1))
        proven = Candidate(doc=_doc(2, rating=4.8, review_count=2500))
        rank([thin, proven], query_category=None)
        # (5*1 + 10*3.5)/11 / 5 ≈ 0.727  vs  (4.8*2500 + 10*3.5)/2510 / 5 ≈ 0.959
        assert thin.components["rating_quality"] == pytest.approx(0.7273, abs=1e-3)
        assert proven.components["rating_quality"] == pytest.approx(0.9589, abs=1e-3)
        assert proven.components["rating_quality"] > thin.components["rating_quality"]

    def test_rating_quality_prior_when_unrated(self):
        c = Candidate(doc=_doc(1))
        rank([c], query_category=None)
        # No data → the corpus prior: 3.5/5 = 0.7.
        assert c.components["rating_quality"] == pytest.approx(0.7, abs=1e-6)

    def test_popularity_log1p(self):
        c = Candidate(doc=_doc(1, review_count=999))
        rank([c], query_category=None)
        assert c.components["popularity"] == pytest.approx(
            math.log1p(999) / math.log1p(1000), abs=1e-6
        )
        assert 0.0 <= c.components["popularity"] <= 1.0
        zero = Candidate(doc=_doc(2))
        rank([zero], query_category=None)
        assert zero.components["popularity"] == 0.0
        big = Candidate(doc=_doc(3, review_count=50_000))
        rank([big], query_category=None)
        assert big.components["popularity"] == 1.0  # saturates, never >1

    def test_open_now_boost_component(self):
        open_c = Candidate(doc=_doc(1, open_now=True))
        closed_c = Candidate(doc=_doc(2, open_now=False))
        unknown_c = Candidate(doc=_doc(3))
        rank([open_c, closed_c, unknown_c], query_category=None)
        assert open_c.components["open_now_boost"] == 1.0
        assert closed_c.components["open_now_boost"] == 0.0
        assert unknown_c.components["open_now_boost"] == 0.5

    def test_new_weights_tunable(self):
        w = RankWeights()
        assert w.rating_quality > 0 and w.popularity > 0 and w.open_now_boost > 0
        over = RankWeights.from_json('{"rating_quality": 0.2, "bogus": 9}')
        assert over.rating_quality == 0.2 and over.text == RankWeights().text
        total = (
            w.text
            + w.distance
            + w.category
            + w.confidence
            + w.freshness
            + w.sources
            + w.status
            + w.rating_quality
            + w.popularity
            + w.open_now_boost
        )
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_sort_rating_orders_by_quality(self):
        cands = [
            Candidate(doc=_doc(1, rating=4.0, review_count=10)),
            Candidate(doc=_doc(2, rating=4.9, review_count=200)),
            Candidate(doc=_doc(3, rating=3.5)),
        ]
        out = rank(cands, query_category=None, sort="rating")
        assert [c.doc.place_id for c in out] == ["2", "1", "3"]

    def test_sort_popularity_orders_by_reviews(self):
        cands = [
            Candidate(doc=_doc(1, review_count=5)),
            Candidate(doc=_doc(2, review_count=900)),
            Candidate(doc=_doc(3, review_count=60)),
        ]
        out = rank(cands, query_category=None, sort="popularity")
        assert [c.doc.place_id for c in out] == ["2", "3", "1"]

    def test_sort_distance_orders_nearest_first(self):
        cands = [
            Candidate(doc=_doc(1), distance_m=900),
            Candidate(doc=_doc(2), distance_m=50),
            Candidate(doc=_doc(3), distance_m=None),
        ]
        out = rank(cands, query_category=None, sort="distance")
        assert [c.doc.place_id for c in out] == ["2", "1", "3"]


# ─── OpenSearch request body ─────────────────────────────────────────────


class TestSearchBody:
    def test_min_rating_range_filter(self):
        spec = parse_local_query(min_rating=4.5)
        body = build_search_body(spec, top_k=50)
        assert {"range": {"rating": {"gte": 4.5}}} in body["query"]["bool"]["filter"]

    def test_price_level_term_filter(self):
        spec = parse_local_query(price_level="₫₫")
        body = build_search_body(spec, top_k=50)
        assert {"term": {"price_level": "₫₫"}} in body["query"]["bool"]["filter"]

    def test_open_now_is_not_an_index_filter(self):
        # open_now resolves against the live clock — it post-filters
        # candidates at request time, never an index-side clause.
        spec = parse_local_query(open_now=True)
        body = build_search_body(spec, top_k=50)
        assert body["query"]["bool"]["filter"] == [{"terms": {"status": sorted(spec.statuses)}}]

    def test_sort_rating_desc(self):
        body = build_search_body(parse_local_query(sort="rating"), top_k=50)
        assert body["sort"] == [{"rating": {"order": "desc", "missing": "_last"}}]

    def test_sort_popularity_desc(self):
        body = build_search_body(parse_local_query(sort="popularity"), top_k=50)
        assert body["sort"] == [{"review_count": {"order": "desc", "missing": "_last"}}]

    def test_sort_distance_needs_geo(self):
        body = build_search_body(parse_local_query(sort="distance", lat=10.7, lon=106.7), top_k=50)
        assert "_geo_distance" in body["sort"][0]
        # No geo anchor → nothing to sort by distance on.
        assert "sort" not in build_search_body(parse_local_query(sort="distance"), top_k=50)


# ─── service: request-time filters ───────────────────────────────────────


class TestServiceFilters:
    def test_min_rating_drops_low_rated(self):
        docs = [
            _doc(1, rating=4.8),
            _doc(2, rating=3.0),  # below the floor
            _doc(3),  # unrated → cannot satisfy >=4.5
        ]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(min_rating=4.5))
        assert [r["place_id"] for r in rows] == [1]

    def test_price_level_filters(self):
        docs = [_doc(1, price_level="₫"), _doc(2, price_level="₫₫"), _doc(3)]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(price_level="₫₫"))
        assert [r["place_id"] for r in rows] == [2]

    def test_open_now_true_keeps_only_open(self):
        docs = [
            _doc(1, opening_hours=OPEN_DAY),  # open at Wed 10:00
            _doc(2, opening_hours=LATE_DAY),  # closed at Wed 10:00
            _doc(3),  # no hours → unknown, not "open"
        ]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(open_now=True))
        assert [r["place_id"] for r in rows] == [1]
        assert rows[0]["open_now"] is True

    def test_open_now_false_keeps_only_closed(self):
        docs = [_doc(1, opening_hours=OPEN_DAY), _doc(2, opening_hours=LATE_DAY)]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(open_now=False))
        assert [r["place_id"] for r in rows] == [2]

    def test_open_now_verdict_follows_the_clock(self):
        """The same query flips with time — nothing is baked into the doc."""
        docs = [_doc(1, opening_hours=OPEN_DAY)]
        day_rows, _ = asyncio.run(_service(docs, now=WED_10AM).search(open_now=True))
        night_rows, _ = asyncio.run(_service(docs, now=WED_11PM).search(open_now=True))
        assert [r["place_id"] for r in day_rows] == [1]
        assert night_rows == []
        # and open_now() agrees with the row verdicts
        assert open_now(OPEN_DAY, now=WED_10AM) is True
        assert open_now(OPEN_DAY, now=WED_11PM) is False

    def test_combined_filters(self):
        docs = [
            _doc(1, rating=4.6, price_level="₫₫", opening_hours=OPEN_DAY),
            _doc(2, rating=4.9, price_level="₫₫", opening_hours=LATE_DAY),
            _doc(3, rating=4.7, price_level="₫", opening_hours=OPEN_DAY),
        ]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(min_rating=4.5, price_level="₫₫", open_now=True))
        assert [r["place_id"] for r in rows] == [1]

    def test_sort_rating_service_level(self):
        docs = [
            _doc(1, rating=4.0, review_count=10),
            _doc(2, rating=4.9, review_count=200),
            _doc(3, rating=3.5),
        ]
        svc = _service(docs)
        rows, _ = asyncio.run(svc.search(sort="rating"))
        assert [r["place_id"] for r in rows] == [2, 1, 3]


# ─── API surface ─────────────────────────────────────────────────────────


class TestAPISurface:
    def test_places_search_accepts_and_forwards_new_params(self, monkeypatch):
        """``/v1/places/search?min_rating=4.5&open_now=true`` must bind and
        reach the service unchanged."""
        import api.v1 as api_v1

        seen: dict[str, Any] = {}

        class _Spy:
            async def search(self, **kw):
                seen.update(kw)
                return [], type(
                    "M", (), {"lanes": [], "degraded": [], "cache_hit": False, "total_ms": 0.0}
                )()

        monkeypatch.setattr(api_v1, "_get_places_service", lambda: _Spy())
        out = asyncio.run(
            api_v1.places_search(
                q="phở", min_rating=4.5, open_now=True, price_level="₫₫", sort="rating"
            )
        )
        assert out == []
        assert seen["min_rating"] == 4.5
        assert seen["open_now"] is True
        assert seen["price_level"] == "₫₫"
        assert seen["sort"] == "rating"

    def test_places_search_new_params_default_absent(self, monkeypatch):
        import api.v1 as api_v1

        seen: dict[str, Any] = {}

        class _Spy:
            async def search(self, **kw):
                seen.update(kw)
                return [], type(
                    "M", (), {"lanes": [], "degraded": [], "cache_hit": False, "total_ms": 0.0}
                )()

        monkeypatch.setattr(api_v1, "_get_places_service", lambda: _Spy())
        asyncio.run(api_v1.places_search(q="cafe"))
        assert seen["min_rating"] is None
        assert seen["open_now"] is None
        assert seen["price_level"] is None
        assert seen["sort"] is None
