"""Deterministic-helper coverage for services/local_discovery.py.

The service class is dormant (the live path uses core/local_discovery.py),
but its pure helpers are linted and gate-checked — exercise them directly.
"""

from models import BusinessEntity
from services.local_discovery import (
    QueryIntent,
    _assess_quality,
    _category_compatible,
    _dedup_candidates,
    _entity_supports_specialty,
    _rank_candidates,
)


def _ent(name: str, **kw) -> BusinessEntity:
    return BusinessEntity(name=name, **kw)


# ─── _dedup_candidates ───────────────────────────────────────────────────────


class TestServiceDedup:
    def test_phone_merge(self):
        a = _ent("A", phone="090 123 4567")
        b = _ent("B", phone="0901234567")
        out = _dedup_candidates([a, b])
        assert len(out) == 1 and out[0].name == "A"

    def test_website_merge(self):
        a = _ent("A", website="https://abc.vn/menu")
        b = _ent("B", website="http://www.abc.vn")
        assert len(_dedup_candidates([a, b])) == 1

    def test_geo_name_merge(self):
        a = _ent("Pho 24", lat=21.2135, lon=106.1488)
        b = _ent("Phở 24", lat=21.2136, lon=106.1489)
        assert len(_dedup_candidates([a, b])) == 1

    def test_address_name_merge(self):
        a = _ent("Cafe Sáng", address="12  Lê Lợi")
        b = _ent("Cafe Sáng", address="12 Lê Lợi")
        assert len(_dedup_candidates([a, b])) == 1

    def test_distinct_kept(self):
        a = _ent("Quán A", lat=21.21, lon=106.14)
        b = _ent("Quán B", lat=21.22, lon=106.15)
        assert len(_dedup_candidates([a, b])) == 2


# ─── _category_compatible / _entity_supports_specialty ───────────────────────


class TestServiceHelpers:
    def test_category_compatible(self):
        # services variant: empty entity category is permissive
        assert _category_compatible("", "restaurant") is True
        assert _category_compatible("pharmacy", "restaurant") is False
        assert _category_compatible("cafe", "restaurant") is True

    def test_entity_supports_specialty(self):
        e = _ent("Giò Chả Bà Năm", description="đặc sản", address="Yên Dũng")
        assert _entity_supports_specialty(e, "giò chả", ["gio cha"]) is True
        other = _ent("Cafe Khuya")
        assert _entity_supports_specialty(other, "giò chả", ["gio cha"]) is False


# ─── _assess_quality / _rank_candidates ──────────────────────────────────────


def _intent(**kw) -> QueryIntent:
    base = {
        "original": "giò chả Yên Dũng",
        "broad_category": "restaurant",
        "specialty": "giò chả",
        "specialty_variants": ["gio cha"],
    }
    base.update(kw)
    return QueryIntent(**base)


class TestServiceQuality:
    def test_specialty_entities_count(self):
        ents = [
            _ent("Giò Chả A", category="restaurant", lat=21.2135, lon=106.1488, address="a"),
            _ent("Giò Chả B", category="restaurant", lat=21.2140, lon=106.1490, address="b"),
            _ent("Cafe C", category="restaurant", lat=21.2150, lon=106.1500, address="c"),
        ]
        q = _assess_quality(ents, _intent(), 21.2135, 106.1488, 10.0)
        assert q["specialty_match_count"] == 2
        assert q["exact_category_count"] == 3
        assert q["useful_count"] == 2
        assert q["sufficient"] is False  # useful < 5 and geo >= 3 fails useful floor

    def test_no_coords_gate_uses_useful_only(self):
        ents = [_ent(f"Giò Chả {i}", category="restaurant") for i in range(6)]
        q = _assess_quality(ents, _intent(), None, None, 10.0)
        assert q["sufficient"] is True

    def test_rank_specialty_boost(self):
        specialty_hit = _ent("Giò Chả Ngon", category="restaurant")
        generic = _ent("Quán Ăn", category="restaurant")
        ranked = _rank_candidates([generic, specialty_hit], _intent(), None, None, 10.0)
        assert ranked[0].name == "Giò Chả Ngon"
