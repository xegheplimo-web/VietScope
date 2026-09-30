"""P16 entity-resolution tests: normalize, match, merge, provenance, resume.

All resolution logic is exercised against DictCanonicalStore — the Pg
store shares the same code path; SQL is asserted structurally where it
matters (keyset paging, provenance upsert).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from resolution.match import (
    MERGE_THRESHOLD,
    RESOLVER_VERSION,
    NormSource,
    PlaceNorm,
    geo_score,
    haversine_m,
    score_pair,
)
from resolution.normalize import (
    CategoryResolver,
    canonical_category,
    categories_related,
    name_signature,
    norm_address,
    norm_name,
    norm_phone,
    website_domain,
)
from resolution.provenance import confidence_from, map_canonical, resolve_fields
from resolution.runner import _contrib, run_resolution
from resolution.store import DictCanonicalStore

NOW = datetime(2026, 9, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=400)


def _row(i: int, **kw) -> dict:
    base = {
        "id": i,
        "provider": "osm",
        "external_id": f"osm_node:{i}",
        "raw_name": f"Place {i}",
        "raw_address": "1 Đường X, Hà Nội",
        "raw_phone": None,
        "raw_website": None,
        "raw_category": None,
        "raw_hours": None,
        "lat": 21.03,
        "lon": 105.85,
        "admin_unit_id": 101,
        "observed_at": NOW,
    }
    return {**base, **kw}


async def _feed(rows: list[dict]):
    for r in rows:
        yield r


def _run(rows: list[dict], **kw):
    return asyncio.run(run_resolution(None, sources_feed=_feed(rows), **kw))


class TestNormalize:
    def test_diacritics_and_case_fold(self):
        assert norm_name("Quán Ăn Vặt Cô Ba") == "an vat co ba"
        assert norm_name("QUAN AN VAT CO BA") == "an vat co ba"

    def test_business_prefixes_stripped(self):
        assert norm_name("Công ty TNHH ABC") == "abc"
        assert norm_name("Siêu thị Mini Mart") == "mini mart"
        assert norm_name("Nhà thuốc Minh Anh") == "minh anh"

    def test_punctuation_collapses(self):
        assert norm_name("ABC-Mart, Chi nhánh 2!") == "abc mart chi nhanh 2"

    def test_name_signature_order_independent(self):
        assert name_signature("pho thin lo duc") == name_signature("lo duc pho thin")

    def test_phone_and_domain(self):
        assert norm_phone("0901 234 567") == "+84901234567"
        assert norm_phone("+84 901-234-567") == "+84901234567"
        assert website_domain("https://WWW.Pho-Thin.vn/menu") == "pho-thin.vn"

    def test_address_normalize(self):
        assert norm_address("1 Đường X,   Hà Nội") == "1 duong x ha noi"

    def test_category_mapping(self):
        assert canonical_category("restaurant") == "food"
        assert canonical_category("Nhà thuốc tây") == "health"
        assert canonical_category("pharmacy") == "health"
        assert canonical_category(None, {"types": ["lodging", "point_of_interest"]}) == "lodging"
        assert canonical_category("zzz_unknown") is None

    def test_category_mapping_google_vietnamese(self):
        """gosom -lang vi emits Vietnamese category labels."""
        assert canonical_category("Hiệu thuốc") == "health"
        assert canonical_category("Bác sĩ khoa nhi") == "health"
        assert canonical_category("Dịch vụ y tế địa phương") == "health"
        assert canonical_category("Gara ô tô") == "transport"
        assert canonical_category("Bãi rửa xe ô tô") == "transport"
        assert canonical_category("Khu ăn uống") == "food"
        assert canonical_category("Đại lý xe máy điện") == "retail"
        assert canonical_category("Hiệu sách") == "retail"
        assert canonical_category("Rạp chiếu phim") == "culture"

    def test_category_resolver_overlay(self):
        """DB mappings layer over built-ins: provider row > '*' > static."""
        cats = CategoryResolver()
        cats.add("google_maps", "quay thuoc", "health")
        cats.add("*", "sieu thi mini", "retail")
        # provider-specific label only applies to that provider
        assert cats.resolve("Quầy thuốc", provider="google_maps") == "health"
        assert cats.resolve("Quầy thuốc", provider="osm") is None
        # wildcard applies everywhere
        assert cats.resolve("Siêu thị mini", provider="osm") == "retail"
        assert cats.resolve("Siêu thị mini") == "retail"
        # provider row wins over wildcard for the same key
        cats.add("*", "cafe", "food")
        cats.add("google_maps", "cafe", "lodging")
        assert cats.resolve("cafe", provider="google_maps") == "lodging"
        assert cats.resolve("cafe", provider="osm") == "food"

    def test_category_resolver_unknowns(self):
        """Unmapped raw labels accumulate as (provider, folded_key) counts —
        the ops signal for which mapping rows to add after a crawl."""
        cats = CategoryResolver()
        assert cats.resolve("Tiệm vàng", provider="google_maps") is None
        assert cats.resolve("tiệm vàng", provider="google_maps") is None
        assert cats.resolve("Tiệm vàng", provider="osm") is None
        assert cats.unknowns[("google_maps", "tiem vang")] == 2
        assert cats.unknowns[("osm", "tiem vang")] == 1
        rows = cats.unknown_rows()
        assert ("google_maps", "tiem vang", "Tiệm vàng", 2) in rows
        # payload hints still rescue the bucket but the raw label remains
        # logged — it needs its own mapping row
        assert (
            cats.resolve("Cơ sở ký túc xá", provider="g", payload={"types": ["lodging"]})
            == "lodging"
        )
        assert cats.unknowns[("g", "co so ky tuc xa")] == 1
        # a mapped label and a missing label leave unknowns alone
        cats.resolve("Nhà thuốc", provider="g")
        cats.resolve(None, provider="g", payload={"types": ["pharmacy"]})
        assert len(cats.unknowns) == 3

    def test_unknown_categories_in_run_summary(self):
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_category="Tiệm vàng"),
            _row(2, raw_category="Hiệu thuốc"),
        ]
        out = _run(rows, store=store)
        assert out["status"] == "done"
        assert out["unknown_categories"] == 1

    def test_category_relatedness(self):
        assert categories_related("food", "food") == 1.0
        assert categories_related("food", "retail") == 0.6
        assert categories_related("food", "government") == 0.1
        assert categories_related(None, "food") == 0.4

    def test_norm_status(self):
        from resolution.normalize import norm_status

        assert norm_status("OPERATIONAL") == "open"
        assert norm_status("CLOSED_TEMPORARILY") == "temporarily_closed"
        assert norm_status("CLOSED_PERMANENTLY") == "permanently_closed"
        assert norm_status("Tạm đóng cửa") == "temporarily_closed"
        assert norm_status("đã đóng cửa") == "permanently_closed"
        assert norm_status("TEMPORARILY CLOSED - covid") == "temporarily_closed"
        assert norm_status("open") == "open"
        assert norm_status(None) is None
        assert norm_status("gibberish") is None

    def test_gmaps_extract_status(self):
        from ingestion.adapters.gmaps import _extract_status

        assert _extract_status({"business_status": "OPERATIONAL"}) == "OPERATIONAL"
        assert _extract_status({"permanently_closed": True}) == "CLOSED_PERMANENTLY"
        assert _extract_status({"temporarily_closed": True}) == "CLOSED_TEMPORARILY"
        assert _extract_status({"status": "  Open "}) == "Open"
        assert _extract_status({}) is None

    def test_osm_status_lifecycle_tags(self):
        from ingestion.adapters.osm_pbf import _osm_status

        assert _osm_status({"disused:amenity": "restaurant"}) == "CLOSED_PERMANENTLY"
        assert _osm_status({"opening_hours": "closed"}) == "CLOSED_PERMANENTLY"
        assert _osm_status({"temporary_closed": "yes"}) == "CLOSED_TEMPORARILY"
        assert _osm_status({"amenity": "cafe"}) is None


class TestMatch:
    def _src(self, **kw) -> NormSource:
        base = {
            "record_id": 1,
            "provider": "osm",
            "external_id": "n1",
            "norm_name": "pho thin",
            "name_sig": "pho thin",
            "tokens": frozenset({"pho", "thin"}),
            "norm_address": "",
            "phone": None,
            "domain": None,
            "category": None,
            "lat": 21.03,
            "lon": 105.85,
            "admin_unit_id": 1,
            "observed_at": NOW,
            "fields": {},
        }
        return NormSource(**{**base, **kw})

    def _place(self, **kw) -> PlaceNorm:
        base = {
            "place_id": 9,
            "norm_name": "pho thin",
            "tokens": frozenset({"pho", "thin"}),
            "phone": None,
            "domain": None,
            "category": None,
            "lat": 21.0301,
            "lon": 105.8501,
            "admin_unit_id": 1,
        }
        return PlaceNorm(**{**base, **kw})

    def test_same_place_scores_high(self):
        s, force = score_pair(self._src(), self._place())
        assert s >= MERGE_THRESHOLD or force

    def test_distinct_place_scores_low(self):
        s, force = score_pair(
            self._src(norm_name="cafe giang", tokens=frozenset({"cafe", "giang"})),
            self._place(lat=20.5, lon=106.2),
        )
        assert s < MERGE_THRESHOLD and not force

    def test_same_phone_forces_match(self):
        _, force = score_pair(
            self._src(phone="+84901112222", norm_name="other name", tokens=frozenset({"x"})),
            self._place(phone="+84901112222", lat=21.0301, lon=105.8501),
        )
        assert force == "phone"

    def test_same_phone_far_apart_no_merge(self):
        """Chain hotline: identical phone but pins kilometres apart —
        geography contradicts identity, so no force and no merge."""
        s, force = score_pair(
            self._src(phone="+84901112222"),
            self._place(phone="+84901112222", lat=21.28, lon=106.19),
        )
        assert not force
        assert s < MERGE_THRESHOLD

    def test_same_name_close_by_forces_match(self):
        _, force = score_pair(
            self._src(lat=21.0301, lon=105.8501),
            self._place(lat=21.0302, lon=105.8502),
        )
        assert force == "name_geo"

    def test_geo_decay(self):
        assert geo_score(None) == 0.4
        assert geo_score(10) == 1.0
        assert geo_score(1000) == -1.0
        assert geo_score(200) == -1.0
        assert 0 < geo_score(100) < 1
        assert haversine_m(21.0, 105.0, 21.0, 105.001) > 100


class TestResolution:
    def test_cross_provider_merge(self):
        """Same place via OSM + Google collapses into one canonical place."""
        rows = [
            _row(1, provider="osm", external_id="osm_node:1", raw_name="Phở Thìn"),
            _row(
                2,
                provider="google_maps",
                external_id="ChIJabc",
                raw_name="Quán Phở Thìn",
                raw_phone="0901 234 567",
            ),
        ]
        out = _run(rows)
        assert out["status"] == "done"
        assert out["created"] == 1
        assert out["merged"] == 1

    def test_distinct_places_stay_separate(self):
        rows = [
            _row(1, raw_name="Cafe Giang", lat=21.03, lon=105.85),
            _row(2, external_id="osm_node:2", raw_name="Bệnh viện Bạch Mai", lat=21.00, lon=105.84),
        ]
        store = DictCanonicalStore()
        out = asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert out["created"] == 2 and out["merged"] == 0
        assert len(store.places) == 2

    def test_re_resolution_idempotent(self):
        """Running the same batch twice produces no new places."""
        rows = [_row(1), _row(2, provider="google_maps", external_id="ChIJx")]
        store = DictCanonicalStore()
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        n1 = len(store.places)
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == n1  # relinked / re-resolved, never duped

    def test_re_resolution_no_external_id(self):
        """Id-less records relink via source_record_id, never duplicate."""
        rows = [_row(1, external_id=None)]
        store = DictCanonicalStore()
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 1
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 1
        assert asyncio.run(store.find_place_by_record(1)) is not None

    def test_link_conflict_folds_into_winner(self):
        """Record already linked to another place resolves into the winner."""
        rows = [_row(1, external_id=None, raw_name="Totally Different")]
        store = DictCanonicalStore()
        winner = asyncio.run(
            store.create_place(
                {
                    "canonical_name": "W",
                    "normalized_name": "w",
                    "business_id": 1,
                    "canonical_category": None,
                    "address": None,
                    "normalized_address": None,
                    "phone": None,
                    "website": None,
                    "opening_hours": None,
                    "lat": None,
                    "lon": None,
                    "admin_unit_id": None,
                    "status": "open",
                    "confidence": 0.5,
                    "source_count": 1,
                }
            )
        )
        asyncio.run(store.link_source(winner, 1, "osm", None, 0))
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 1  # no orphan created
        assert asyncio.run(store.find_place_by_record(1)) == winner

    def test_same_brand_two_locations_one_business(self):
        """100 branches of one brand → one canonical_businesses row."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_name="Thế Giới Di Động", raw_category="electronics", lat=21.03, lon=105.85),
            _row(
                2,
                raw_name="The Gioi Di Dong",
                raw_category="mobile_phone",
                lat=10.77,
                lon=106.70,
                admin_unit_id=202,
            ),
        ]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 2  # different spots → different places
        assert len(store.businesses) == 1  # ...but one shared business
        assert {p.business_id for p in store.places.values()} == {1}

    def test_chain_branches_shared_hotline_stay_separate(self):
        """6 Long Châu branches share the hotline + brand name but sit
        0.8–2.2 km apart — geography vetoes the merge; six places, one
        business. Regression for the pilot over-merge."""
        store = DictCanonicalStore()
        coords = [
            (21.2724, 106.1944),
            (21.2798, 106.1881),
            (21.2735, 106.1917),
            (21.2852, 106.1968),
            (21.2696, 106.2012),
            (21.2739, 106.1990),
        ]
        rows = [
            _row(
                i + 1,
                provider="google_maps",
                external_id=f"ChIJ{i}",
                raw_name="Nhà Thuốc FPT Long Châu",
                raw_phone="1800 6928",
                raw_category="pharmacy",
                lat=lat,
                lon=lon,
            )
            for i, (lat, lon) in enumerate(coords)
        ]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 6
        assert len(store.businesses) == 1

    def test_link_decision_metadata_recorded(self):
        """Every link records matcher version + score + reason so a
        future matcher upgrade can tell which decisions it wrote."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_name="Phở Thìn", raw_phone="0901 234 567"),
            _row(
                2,
                provider="google_maps",
                external_id="ChIJ1",
                raw_name="Quán Phở Thìn",
                raw_phone="0901 234 567",
            ),
        ]
        out = asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert out["status"] == "done"
        m1 = store.link_meta[1]
        assert m1["resolution_reason"] == "create"
        assert m1["matcher_version"] == RESOLVER_VERSION
        m2 = store.link_meta[2]
        assert m2["resolution_reason"] in ("weighted", "force:name_geo", "force:phone")
        assert m2["resolution_score"] is not None

    def test_audit_flags_implausible_merge(self):
        """Linked records spanning beyond the same-place radius flag the
        canonical place as a suspicious merge."""
        store = DictCanonicalStore()
        asyncio.run(
            store.create_place(
                {
                    "canonical_name": "W",
                    "normalized_name": "w",
                    "business_id": 1,
                    "canonical_category": None,
                    "address": None,
                    "normalized_address": None,
                    "phone": None,
                    "website": None,
                    "opening_hours": None,
                    "lat": None,
                    "lon": None,
                    "admin_unit_id": None,
                    "status": "open",
                    "confidence": 0.5,
                    "source_count": 1,
                    "resolution_run_id": 0,
                }
            )
        )
        store.seed_source(1, _row(1, lat=21.03, lon=105.85))
        store.seed_source(2, _row(2, provider="google_maps", lat=21.30, lon=106.20))
        asyncio.run(store.link_source(1, 1, "osm", None, 0))
        asyncio.run(store.link_source(1, 2, "google_maps", "x", 0))
        out = asyncio.run(store.audit_merges(0))
        assert out["suspicious"] == 1
        assert store.places[1].suspicious_merge
        assert "geo_span" in store.places[1].suspicious_reason

    def test_audit_ignores_clean_merge(self):
        """A tight cluster of duplicate sightings stays unflagged."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_name="Phở Thìn", lat=21.03, lon=105.85),
            _row(
                2,
                provider="google_maps",
                external_id="c1",
                raw_name="Phở Thìn",
                lat=21.03005,
                lon=105.85005,
            ),
        ]
        out = asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert out["suspicious"] == 0
        assert not store.places[1].suspicious_merge

    def test_same_name_no_shared_evidence_separate_businesses(self):
        """Two unrelated 'Minh Anh' shops (different category+region) must
        not collapse into one business just because the name folds equal."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_name="Nhà Thuốc Minh Anh", raw_category="pharmacy", lat=21.03, lon=105.85),
            _row(
                2,
                raw_name="Cửa Hàng Minh Anh",
                raw_category="convenience",
                lat=10.77,
                lon=106.70,
                admin_unit_id=202,
            ),
        ]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert len(store.places) == 2
        assert len(store.businesses) == 2  # name alone is not brand evidence
        assert len({p.business_id for p in store.places.values()}) == 2

    def test_status_from_source_not_default_open(self):
        """Provider-reported closure must not be overwritten by 'open'."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_status="CLOSED_PERMANENTLY"),
            _row(2, raw_status="CLOSED_TEMPORARILY", external_id="n2"),
        ]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        statuses = {p.status for p in store.places.values()}
        assert statuses == {"permanently_closed", "temporarily_closed"}

    def test_status_stale_confidence_decays_fast(self):
        """A 400-day-old 'closed' loses to a fresh 'open' on status field."""
        store = DictCanonicalStore()
        rows = [
            _row(1, raw_status="CLOSED_PERMANENTLY", observed_at=OLD),
            _row(
                2,
                provider="google_maps",
                external_id="n1",
                raw_name="Place 1",  # same name + coords → merges
                raw_status="OPERATIONAL",
                observed_at=NOW,
            ),
        ]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        p = store.places[1]
        assert p.status == "open"

    def test_corroboration_uses_signatures(self):
        """'Công Ty ABC' vs 'CONG TY ABC' count as the same value."""
        from resolution.provenance import resolve_fields

        def src(i: int, name: str) -> NormSource:
            return NormSource(
                record_id=i,
                provider="osm",
                external_id=None,
                norm_name="cong ty abc",
                name_sig="abc cong ty",
                tokens=frozenset({"cong", "ty", "abc"}),
                norm_address="",
                phone=None,
                domain=None,
                category=None,
                lat=None,
                lon=None,
                admin_unit_id=None,
                observed_at=NOW,
                fields={"name": name},
            )

        agreeing = [src(1, "Công Ty ABC"), src(2, "CONG TY ABC")]
        _, prov_same = resolve_fields(1, agreeing, {}, now=NOW)
        # same signature → each gets corroboration 1.0 (same weight)
        assert prov_same[0].weight == prov_same[1].weight

        distinct = [src(1, "Công Ty ABC"), src(2, "Quán Khác Xyz")]
        _, prov_diff = resolve_fields(1, distinct, {}, now=NOW)
        # distinct signatures → corroboration 0.5 → lower weight than agreement
        assert prov_same[0].weight > prov_diff[0].weight
        assert abs(prov_same[0].weight - prov_diff[0].weight) == pytest.approx(
            0.1 * (1.0 - 0.5), abs=0.001
        )

    def test_provenance_winner_and_confidence(self):
        """Google's newer phone beats OSM's stale one; provenance keeps both."""
        policies = {
            "google_maps": {"authority": {"phone": 0.9}},
            "osm": {"authority": {"phone": 0.3}},
        }
        store = DictCanonicalStore()
        rows = [
            _row(
                1,
                provider="osm",
                external_id="n1",
                raw_name="Phở Thìn",
                raw_phone="0901 234 567",
                observed_at=OLD,
            ),
            _row(
                2,
                provider="google_maps",
                external_id="g1",
                raw_name="Phở Thìn",
                raw_phone="0987 654 321",
                observed_at=NOW,
            ),
        ]
        asyncio.run(
            run_resolution(
                None,
                store=store,
                sources_feed=_feed(rows),
            )
        )
        # rerun with policies via internal _apply — simulate manual check
        contribs = [_contrib(r) for r in store.sources.values()]
        fields, prov = resolve_fields(1, contribs, policies, now=NOW)
        chosen_phone = [r for r in prov if r.field == "phone" and r.chosen][0]
        assert chosen_phone.provider == "google_maps"
        assert fields["phone"] == "+84987654321"
        assert confidence_from(prov) > 0

    def test_map_canonical_columns(self):
        fields = {"name": "A", "phone": "+849", "location": {"lat": 1.0, "lon": 2.0}}
        out = map_canonical(fields)
        assert out["canonical_name"] == "A" and out["lat"] == 1.0

    def test_resume_cursor_skips_processed(self):
        store = DictCanonicalStore()
        rows = [_row(i) for i in range(1, 6)]
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        n1 = len(store.places)
        # simulate resume at cursor=3: only 4,5 are re-fed
        out = asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows), since_id=3))
        assert out["cursor"] == 5
        assert out["scanned"] == 2
        assert len(store.places) == n1  # no dups on resume


class TestRunnerStore:
    def test_pg_candidates_sql_blocks(self):
        """Blocking SQL uses admin/geo/phone/domain — not a full scan."""
        import inspect

        import resolution.store as st

        src = inspect.getsource(st.PgCanonicalStore.candidates)
        assert "ST_DWithin" in src and "admin_unit_id" in src and "LIMIT" in src

    def test_paging_sql_is_keyset(self):
        from resolution.runner import _PAGE_SQL

        assert "id > $1" in _PAGE_SQL and "ORDER BY id" in _PAGE_SQL
        assert "OFFSET" not in _PAGE_SQL.upper()

    def test_failed_feed_status(self):
        async def boom():
            yield _row(1)
            raise RuntimeError("feed died")

        out = asyncio.run(run_resolution(None, store=DictCanonicalStore(), sources_feed=boom()))
        assert out["status"] == "failed"
        assert "fatal" in out["errors"]

    def test_source_records_without_coords_still_resolve(self):
        rows = [_row(1, lat=None, lon=None, raw_name="Tiệm vàng A", raw_phone="0901234567")]
        store = DictCanonicalStore()
        asyncio.run(run_resolution(None, store=store, sources_feed=_feed(rows)))
        assert store.places[1].lat is None
        assert store.places[1].phone == "+84901234567"


def test_provenance_row_json_serializable():
    """ProvRow values land in jsonb — must always serialize."""
    src = _contrib(_row(1, raw_hours={"raw": "8-20"}))
    fields, prov = resolve_fields(1, [src], {}, now=NOW)
    for r in prov:
        json.dumps(r.value)  # raises if not serializable
    assert fields["hours"] == {"raw": "8-20"}
