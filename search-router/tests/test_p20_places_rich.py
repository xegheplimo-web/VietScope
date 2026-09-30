"""P2.0 — rich place cards: raw_payload promotion, projection passthrough,
serve-time open_now/map_url derivation, and the places:read scope.

All infra-free: promotion runs against DictCanonicalStore, derivation is
pure functions, scopes are pure mapping. The MCP half lives in
test_p11_mcp.py (TestSearchPlaces + registration surface).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from security.apikeys import required_scope
from serving.places.document import PlaceDocumentV1
from serving.places.projection import project_row
from serving.places.ranking import Candidate
from serving.places.service import PlaceService, _map_url, open_now

NOW = datetime(2026, 9, 30, tzinfo=UTC)  # a Wednesday
# 2026-09-28 is a Monday — naive `now` args are read as UTC+7 wall time.
MON_10AM = datetime(2026, 9, 28, 10, 0)
MON_11PM = datetime(2026, 9, 28, 23, 0)
TUE_10AM = datetime(2026, 9, 29, 10, 0)  # Tuesday
TUE_6PM = datetime(2026, 9, 29, 18, 0)
SAT_1AM = datetime(2026, 10, 3, 1, 0)  # Saturday
SAT_NOON = datetime(2026, 10, 3, 12, 0)
SAT_11PM = datetime(2026, 10, 3, 23, 0)
SUN_11AM = datetime(2026, 10, 4, 11, 0)  # Sunday
SUN_1PM = datetime(2026, 10, 4, 13, 0)


# ─── scope mapping ──────────────────────────────────────────────────────


class TestPlacesScope:
    def test_places_search_and_autocomplete_scoped(self):
        assert required_scope("/v1/places/search", "GET") == "places:read"
        assert required_scope("/v1/places/autocomplete", "GET") == "places:read"

    def test_place_detail_is_places_read(self):
        # P17.1: places/{id} maps to places:read via route-template match —
        # the detail endpoint no longer falls back to admin:debug.
        assert required_scope("/v1/places/123", "GET") == "places:read"
        # Nested/unknown places paths are not covered by the template.
        assert required_scope("/v1/places/123/extra", "GET") == "admin:debug"

    def test_reindex_not_places_read(self):
        # POST-only route — admin-gated like every other unmapped POST.
        assert required_scope("/v1/places/reindex", "POST") == "admin:debug"


# ─── open_now parser ────────────────────────────────────────────────────


class TestOpenNow:
    def test_within_range(self):
        hours = {"Monday": ["8:00–22:00"]}
        assert open_now(hours, now=MON_10AM) is True
        assert open_now(hours, now=MON_11PM) is False

    def test_hyphen_and_padded_range(self):
        assert open_now({"Monday": ["08:00-22:00"]}, now=MON_10AM) is True

    def test_full_day_range(self):
        assert open_now({"Monday": ["00:00–23:59"]}, now=MON_10AM) is True
        assert open_now({"Monday": ["00:00–24:00"]}, now=MON_11PM) is True
        assert open_now({"Monday": ["Open 24 hours"]}, now=MON_10AM) is True

    def test_day_without_entry_is_unknown(self):
        # Monday-only hours carry no information about Tuesday — a missing
        # day is unknown, never a guessed "closed".
        hours = {"Monday": ["8:00–22:00"]}
        assert open_now(hours, now=datetime(2026, 9, 29, 10, 0)) is None  # Tuesday

    def test_explicit_empty_day_is_closed(self):
        hours = {"Monday": [], "Tuesday": ["8:00–22:00"]}
        assert open_now(hours, now=MON_10AM) is False

    def test_missing_or_garbage_is_none(self):
        assert open_now(None, now=MON_10AM) is None
        assert open_now({}, now=MON_10AM) is None
        assert open_now("garbage", now=MON_10AM) is None
        assert open_now({"Monday": ["không rõ"]}, now=MON_10AM) is None
        assert open_now({"nonsense": {"x": 1}}, now=MON_10AM) is None

    def test_overnight_range(self):
        hours = {"Monday": ["18:00–02:00"]}
        assert open_now(hours, now=MON_11PM) is True  # pre-midnight side
        assert open_now(hours, now=datetime(2026, 9, 29, 1, 0)) is True  # Tue tail
        # The tail ended at 02:00 and Tuesday itself has no entry → unknown.
        assert open_now(hours, now=datetime(2026, 9, 29, 3, 0)) is None

    def test_aware_now_converted_to_vn(self):
        # 10:00 UTC = 17:00 UTC+7 — inside a 16:00–20:00 VN range.
        hours = {"Monday": ["16:00–20:00"]}
        aware = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
        assert open_now(hours, now=aware) is True


class TestOpenNowVietnamese:
    """Corpus fixtures (R6 probe): the Google Maps scrape ran -lang vi, so
    every place's opening_hours keys days in Vietnamese and uses
    "Mở cửa cả ngày" / "Đóng cửa" value strings."""

    def test_vn_weekday_range(self):
        hours = {"Thứ Ba": ["09:00–17:00"]}  # Tuesday
        assert open_now(hours, now=TUE_10AM) is True
        # Entry exists but the minute falls outside → closed, not unknown.
        assert open_now(hours, now=TUE_6PM) is False

    def test_vn_closed_string_is_closed(self):
        hours = {"Thứ Hai": ["Đóng cửa"]}  # Monday
        assert open_now(hours, now=MON_10AM) is False
        assert open_now(hours, now=MON_11PM) is False
        # English "Closed" is the same explicit statement.
        assert open_now({"Monday": ["Closed"]}, now=MON_10AM) is False
        # Days with no entry stay unknown — closed ≠ missing.
        assert open_now(hours, now=TUE_10AM) is None

    def test_vn_open_all_day(self):
        hours = {"Thứ Hai": ["Mở cửa cả ngày"]}  # Monday
        assert open_now(hours, now=MON_10AM) is True
        assert open_now(hours, now=MON_11PM) is True

    def test_vn_day_key_case_and_whitespace(self):
        assert open_now({"thứ ba": ["09:00–17:00"]}, now=TUE_10AM) is True
        assert open_now({"  CHỦ NHẬT ": ["09:00–17:00"]}, now=SUN_11AM) is True

    def test_mixed_vn_en_days(self):
        hours = {"Thứ Bảy": ["08:00–22:00"], "Sunday": ["10:00–12:00"]}
        assert open_now(hours, now=SAT_NOON) is True
        assert open_now(hours, now=SAT_11PM) is False
        assert open_now(hours, now=SUN_11AM) is True
        assert open_now(hours, now=SUN_1PM) is False

    def test_vn_overnight_tail(self):
        # Friday 18:00–02:00 spills its post-midnight tail into Saturday.
        hours = {"Thứ Sáu": ["18:00–02:00"]}
        assert open_now(hours, now=SAT_1AM) is True
        # Tail over and no Saturday entry → unknown, not closed.
        assert open_now(hours, now=datetime(2026, 10, 3, 3, 0)) is None


# ─── map_url ────────────────────────────────────────────────────────────


class TestMapUrl:
    def test_coords(self):
        assert (
            _map_url(21.0321, 105.8523)
            == "https://www.google.com/maps/search/?api=1&query=21.0321,105.8523"
        )

    def test_missing_coord(self):
        assert _map_url(None, 105.0) is None
        assert _map_url(21.0, None) is None
        assert _map_url(None, None) is None


# ─── document / API contract defaults ────────────────────────────────────


class TestContractDefaults:
    def test_document_new_fields_default(self):
        doc = PlaceDocumentV1(place_id="1", business_id=None, name="X")
        assert doc.rating is None
        assert doc.review_count is None
        assert doc.price_level is None
        assert doc.open_now is None
        assert doc.map_url is None
        assert doc.primary_image_url is None
        assert doc.images == []
        # serialized payload carries the keys (P16 shape stays valid)
        d = doc.to_dict()
        for k in ("rating", "review_count", "price_level", "open_now", "map_url"):
            assert k in d

    def test_place_out_new_fields_default(self):
        from api.v1 import PlaceOut

        out = PlaceOut(
            place_id=1,
            canonical_name="X",
            status="open",
            confidence=0.5,
            source_count=1,
        )
        assert out.rating is None and out.review_count is None
        assert out.price_level is None and out.open_now is None
        assert out.map_url is None and out.primary_image_url is None
        assert out.images == []


# ─── projection passthrough ─────────────────────────────────────────────


def _canon_row(**kw) -> dict[str, Any]:
    base = {
        "place_id": 7,
        "business_id": None,
        "canonical_name": "Cà Phê Giảng",
        "normalized_name": "ca phe giang",
        "canonical_category": "food",
        "address": None,
        "phone": None,
        "website": None,
        "website_domain": None,
        "opening_hours": None,
        "lat": 21.03,
        "lon": 105.85,
        "admin_unit_id": None,
        "status": "open",
        "confidence": 0.8,
        "source_count": 1,
        "last_seen": NOW,
        "updated_at": NOW,
    }
    return {**base, **kw}


class TestProjectionRichFields:
    def test_row_columns_reach_document(self):
        doc = project_row(
            _canon_row(
                rating=4.5,
                review_count=120,
                price_level="₫₫",
                primary_image_url="https://img.example/a.jpg",
                images=["https://img.example/a.jpg", "https://img.example/b.jpg"],
            )
        )
        assert doc.rating == 4.5
        assert doc.review_count == 120
        assert doc.price_level == "₫₫"
        assert doc.primary_image_url == "https://img.example/a.jpg"
        assert doc.images == ["https://img.example/a.jpg", "https://img.example/b.jpg"]

    def test_missing_columns_default(self):
        doc = project_row(_canon_row())
        assert doc.rating is None and doc.review_count is None
        assert doc.price_level is None and doc.primary_image_url is None
        assert doc.images == []

    def test_images_jsonb_str(self):
        doc = project_row(_canon_row(images='["https://i/1.jpg"]'))
        assert doc.images == ["https://i/1.jpg"]
        # primary falls back to the first image URL when the column is NULL
        assert doc.primary_image_url == "https://i/1.jpg"
        assert project_row(_canon_row(images="not json")).images == []


# ─── serve-time derivation in row assembly ──────────────────────────────


class TestServeDerivation:
    def test_out_row_carries_rich_fields(self):
        every_day = {
            d: ["00:00–23:59"]
            for d in (
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            )
        }
        doc = PlaceDocumentV1(
            place_id="3",
            business_id=None,
            name="Quán X",
            lat=21.03,
            lon=105.85,
            opening_hours=every_day,
            rating=4.2,
            review_count=9,
            price_level="₫",
            images=["https://i/x.jpg"],
        )
        row = PlaceService._out_row(
            Candidate(doc=doc, os_score=1.0, lane="opensearch"), debug=False
        )
        assert row["rating"] == 4.2 and row["review_count"] == 9
        assert row["price_level"] == "₫"
        assert row["open_now"] is True
        assert row["map_url"].endswith("query=21.03,105.85")
        assert row["images"] == ["https://i/x.jpg"]

    def test_out_row_null_when_no_data(self):
        doc = PlaceDocumentV1(place_id="4", business_id=None, name="Y")
        row = PlaceService._out_row(
            Candidate(doc=doc, os_score=1.0, lane="opensearch"), debug=False
        )
        assert row["open_now"] is None and row["map_url"] is None
        assert row["rating"] is None and row["images"] == []


# ─── resolver promotion ─────────────────────────────────────────────────

GMAPS_PAYLOAD = {
    "link": "https://maps.google.com/?cid=12345",
    "cid": "12345",
    "title": "Cà Phê Giảng",
    "categories": ["Coffee shop"],
    "open_hours": {"Monday": ["8:00–22:00"]},
    "review_count": 120,
    "review_rating": 4.5,
    "price_range": "₫₫",
    "photos": ["https://img.example/a.jpg", {"url": "https://img.example/b.jpg"}],
    "thumbnail": "not-a-url",
}


def _staged(i: int, **kw) -> dict[str, Any]:
    base = {
        "id": i,
        "provider": "google_maps",
        "external_id": f"ChIJ{i}",
        "raw_name": f"Quán {i}",
        "raw_address": "1 Đường X",
        "raw_phone": None,
        "raw_website": None,
        "raw_category": "Cafe",
        "raw_hours": None,
        "lat": 21.03,
        "lon": 105.85,
        "admin_unit_id": 101,
        "observed_at": NOW,
        "raw_payload": None,
    }
    return {**base, **kw}


async def _feed(rows: list[dict]):
    for r in rows:
        yield r


def _run(rows):
    from resolution.runner import run_resolution
    from resolution.store import DictCanonicalStore

    store = DictCanonicalStore()
    asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
    return store


class TestRichPromotion:
    def test_gmaps_payload_promotes(self):
        store = _run([_staged(1, raw_payload=GMAPS_PAYLOAD)])
        p = store.places[1]
        assert p.rating == 4.5
        assert p.review_count == 120
        assert p.price_level == "₫₫"
        assert p.images == ["https://img.example/a.jpg", "https://img.example/b.jpg"]
        assert p.primary_image_url == "https://img.example/a.jpg"
        # provenance recorded through the existing machinery
        fields = {r.field for r in store.provenance if r.chosen}
        assert {"rating", "review_count", "price_level", "images"} <= fields

    def test_payload_missing_keys_stays_null(self):
        store = _run([_staged(1, raw_payload={"cid": "9"})])
        p = store.places[1]
        assert p.rating is None and p.review_count is None
        assert p.price_level is None and p.images is None

    def test_no_payload_no_crash(self):
        store = _run([_staged(1)])
        assert store.places[1].rating is None

    def test_invalid_values_dropped(self):
        store = _run(
            [
                _staged(
                    1,
                    raw_payload={
                        "review_rating": 9.9,  # out of 0..5 range
                        "review_count": -3,
                        "price_range": "   ",
                        "photos": ["ftp://x", 123],
                    },
                )
            ]
        )
        p = store.places[1]
        assert p.rating is None and p.review_count is None
        assert p.price_level is None and p.images is None

    def test_jsonb_str_payload(self):
        # asyncpg hands jsonb back as str without a registered codec
        store = _run([_staged(1, raw_payload=json.dumps(GMAPS_PAYLOAD))])
        assert store.places[1].rating == 4.5

    def test_string_review_count_coerced(self):
        store = _run([_staged(1, raw_payload={"review_count": "1,234"})])
        assert store.places[1].review_count == 1234

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (2147483648, None),  # int4 overflow → dropped, not promoted
            (10**400, None),  # overflows float() → dropped, not an abort
            (float("inf"), None),
            (float("nan"), None),
            (-1, None),
            (3.7, None),  # non-integral → dropped, not truncated
            # float() collapses these to an in-range int; exact-decimal
            # validation must still see them as non-integral
            ("1.00000000000000001", None),
            ("2147483647.0000001", None),
            (2147483647, 2147483647),  # int4 max still promotes
            # strict thousands-grouping is the only comma form promoted
            ("1,234", 1234),
            ("12,345", 12345),
            ("1,234,567", 1234567),
            # malformed comma placements drop the field — no strip-and-promote
            ("1,2", None),
            ("1e,3", None),
            ("1,,234", None),
            (",1234", None),
            ("1234,", None),
        ],
    )
    def test_review_count_int4_bounds(self, raw, expected):
        # canonical_places.review_count is Postgres int4 — only finite,
        # integral, in-range values may promote; the rest become NULL.
        store = _run([_staged(1, raw_payload={"review_count": raw})])
        assert store.places[1].review_count == expected

    def test_poisoned_rich_fields_do_not_abort_run(self):
        """Overflow / non-finite payload values are dropped field-wise —
        the resolution run itself still completes and persists the place."""
        from resolution.runner import run_resolution
        from resolution.store import DictCanonicalStore

        store = DictCanonicalStore()
        result = asyncio.run(
            run_resolution(
                None,
                store=store,
                sources_feed=_feed(
                    [
                        _staged(
                            1,
                            raw_payload={
                                "review_count": 2147483648,
                                "review_rating": float("nan"),
                            },
                        ),
                        # float(10**400) raises OverflowError — the rating
                        # must still drop field-wise, never abort the run
                        _staged(
                            2,
                            raw_payload={
                                "review_count": 10**400,
                                "review_rating": 10**400,
                            },
                        ),
                    ]
                ),
            )
        )
        assert result["status"] == "done"
        p = store.places[1]
        assert p.review_count is None and p.rating is None
        p2 = store.places[2]
        assert p2.review_count is None and p2.rating is None

    def test_competing_sources_vote(self):
        """Two contributors → the weighted winner lands on canonical."""
        rows = [
            _staged(
                1,
                provider="osm",
                raw_name="Quán 1",
                external_id="n1",
                observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                raw_payload={"review_rating": 3.0, "review_count": 10},
            ),
            _staged(
                2,
                raw_name="Quán 1",
                observed_at=NOW,
                raw_payload={"review_rating": 4.8, "review_count": 250},
            ),
        ]
        store = _run(rows)
        assert len(store.places) == 1
        assert store.places[1].rating == 4.8
        # both candidates kept in provenance
        ratings = [r for r in store.provenance if r.field == "rating"]
        assert len(ratings) == 2 and sum(r.chosen for r in ratings) == 1
