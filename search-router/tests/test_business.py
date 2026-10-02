"""Tests for the wave6 Local Business Search foundation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import config
import httpx
import pytest
from core.business_entity import extract_business, extract_business_batch
from core.inference_gateway import InferenceGateway, _safe_parse_json
from evidence.claims import Claim, keywords
from evidence.pack import budget_for_mode
from evidence.verifier import verify_claims_with_llm
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import BusinessEntity, EvidenceCluster, EvidencePack, Source, VerdictStatus
from pipeline.rag import _is_local_business_query
from storage.business_store import BusinessStore


@pytest.fixture(autouse=True)
def no_llm_no_db(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    # BusinessStore reads the canonical hub DSN (pg_client): settings attr
    # first, then the HUB_DATABASE_URL env — both must be cleared.
    monkeypatch.setattr(config.settings, "hub_database_url", "")
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)


# ─── BusinessEntity ──────────────────────────────────────────────────────────


def test_business_entity_model():
    e = BusinessEntity(
        name="Phở Hòa",
        address="260C Pasteur",
        phone="028 3829 2083",
        source_url="https://example.com/pho-hoa",
    )
    assert e.name == "Phở Hòa"
    assert e.phone == "028 3829 2083"


# ─── InferenceGateway ────────────────────────────────────────────────────────


def _fake_client(responses):
    """Return a mock httpx.AsyncClient with a side_effect list of responses."""
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.post = AsyncMock(side_effect=responses)
    return instance


def _mock_response(status=200, content=None, side_effect=None):
    resp = AsyncMock()
    resp.status_code = status
    resp.json = Mock(return_value=content)
    resp.raise_for_status = Mock(side_effect=side_effect)
    return resp


def test_complete_json_parses_dict():
    async def _run():
        resp = _mock_response(
            content={"choices": [{"message": {"content": '```json\n{"name": "Phở Hòa"}\n```'}}]}
        )

        with patch("httpx.AsyncClient", return_value=_fake_client([resp])):
            gw = InferenceGateway()
            gw.api_key = "key"
            result = await gw.complete_json(
                [{"role": "user", "content": "extract"}],
                schema_hint="business",
            )
            return result

    result = asyncio.run(_run())
    assert isinstance(result, dict)
    assert result["name"] == "Phở Hòa"


def test_complete_json_400_fallback():
    async def _run():
        bad = _mock_response(
            status=400,
            side_effect=httpx.HTTPStatusError(
                "Bad Request",
                request=httpx.Request("POST", "http://example.com"),
                response=AsyncMock(),
            ),
        )
        bad.json = Mock(return_value={"error": "json mode not supported"})

        good = _mock_response(
            content={"choices": [{"message": {"content": '{"name": "Phở Hòa"}'}}]}
        )

        with patch("httpx.AsyncClient", return_value=_fake_client([bad, good])):
            gw = InferenceGateway()
            gw.api_key = "key"
            result = await gw.complete_json(
                [{"role": "user", "content": "extract"}],
            )
            return result

    result = asyncio.run(_run())
    assert result == {"name": "Phở Hòa"}


def test_complete_json_no_key():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([])):
            gw = InferenceGateway()
            gw.api_key = ""
            return await gw.complete_json([{"role": "user", "content": "x"}])

    result = asyncio.run(_run())
    assert result is None


def test_embed_returns_vectors():
    async def _run():
        resp = _mock_response(
            content={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                ]
            }
        )

        with patch("httpx.AsyncClient", return_value=_fake_client([resp])):
            gw = InferenceGateway()
            gw.api_key = "key"
            return await gw.embed(["hello", "world"])

    result = asyncio.run(_run())
    assert result is not None
    assert len(result) == 2
    assert all(len(v) == 3 for v in result)


def test_safe_parse_json_tolerates_markdown():
    assert _safe_parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _safe_parse_json("plain text") is None


# ─── business_entity ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_vn_text():
    return (
        "Quán Phở Hòa Pasteur\n"
        "Địa chỉ: 260C Pasteur, Phường 8, Quận 3\n"
        "SĐT: 028 3829 2083\n"
        "Giờ mở: 06:00 - 23:00\n"
        "Rating: 4.2/5"
    )


def test_extract_business_regex(sample_vn_text):
    async def _run():
        return await extract_business(
            sample_vn_text,
            query="quán phở gần Pasteur",
            llm=None,
            source_url="https://example.com/pho-hoa",
        )

    entity = asyncio.run(_run())
    assert entity is not None
    assert "Phở Hòa" in entity.name
    assert "260C Pasteur" in entity.address
    assert entity.phone == "028 3829 2083"
    assert entity.hours == "06:00 - 23:00"
    assert entity.rating == 4.2
    assert entity.source_url == "https://example.com/pho-hoa"
    assert entity.category == "restaurant"


def test_extract_business_batch():
    async def _run():
        texts = [
            "Quán A\nSĐT: 090 123 4567",
            "Nhà hàng B\nĐịa chỉ: 12 Nguyễn Huệ",
        ]
        return await extract_business_batch(
            texts,
            query="quán ăn",
            llm=None,
            source_urls=["https://a.com", "https://b.com"],
        )

    entities = asyncio.run(_run())
    assert len(entities) == 2
    assert all(isinstance(e, BusinessEntity) for e in entities)


def test_extract_business_empty_content():
    async def _run():
        return await extract_business("", query="test", llm=None)

    assert asyncio.run(_run()) is None


# ─── storage/business_store ──────────────────────────────────────────────────


def test_business_store_disabled_without_dsn():
    store = BusinessStore()
    assert not store._available
    assert store.dsn == ""

    async def _run():
        near = await store.search_nearby(10.78, 106.68, 2.0)
        return near

    assert asyncio.run(_run()) == []


def test_business_schema_is_migration_owned():
    """H3: the businesses table DDL lives in db/migrations — the store no
    longer carries a parallel ensure_schema() bootstrap path."""
    assert not hasattr(BusinessStore, "ensure_schema")


# ─── verifier LLM path ───────────────────────────────────────────────────────


def test_verify_claims_with_llm_falls_back_to_deterministic():
    claims = [
        Claim(
            claim_id="c1",
            text="OpenAI released GPT-5",
            keywords=keywords("OpenAI released GPT-5"),
        )
    ]
    clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
    sources = [
        Source(
            source_id="s1",
            url="https://s1.example.com",
            content="OpenAI released GPT-5.",
        )
    ]

    async def _run():
        return await verify_claims_with_llm(claims, clusters, llm=None, sources=sources)

    results = asyncio.run(_run())
    assert results[0].status == VerdictStatus.PARTIALLY_SUPPORTED.value
    assert results[0].evidence == ["s1"]


# ─── api/v1 business search ──────────────────────────────────────────────────


def test_business_search_web_extract():
    from api.v1 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    class DummyInference:
        api_key = ""

    class DummyOrchestrator:
        inference = DummyInference()

        async def search(self, query, mode, progress=None):
            return EvidencePack(
                answer="",
                budget_used=budget_for_mode(mode),
                sources=[
                    Source(
                        source_id="s1",
                        url="https://example.com/pho-hoa",
                        content=(
                            "Quán Phở Hòa Pasteur\n"
                            "Địa chỉ: 260C Pasteur, Quận 3\n"
                            "SĐT: 028 3829 2083\n"
                            "Giờ mở: 06:00 - 23:00\n"
                            "Rating: 4.2/5"
                        ),
                    )
                ],
            )

    with (
        patch("api.v1._get_orchestrator", return_value=DummyOrchestrator()),
        patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
    ):
        resp = client.post(
            "/v1/business/search",
            json={
                "query": "quán phở Pasteur",
                "lat": 10.78,
                "lon": 106.68,
                "radius_km": 2.0,
                "limit": 5,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "web"
    assert len(data["entities"]) > 0
    assert "Phở Hòa" in data["entities"][0]["name"]
    assert data["entities"][0]["phone"] == "028 3829 2083"
    assert data["evidence"]


# ─── RAG local-business hint ─────────────────────────────────────────────────


def test_is_local_business_query():
    assert _is_local_business_query("quán phở gần đây")
    assert _is_local_business_query("nhà hàng ở đâu")
    assert not _is_local_business_query("latest Python release")


# ─── P7: geo anchoring + OSM lane ────────────────────────────────────────────

from core.business_entity import category_for, osm_tag_for  # noqa: E402
from services.geo import GeoPoint  # noqa: E402


class TestCategoryTaxonomy:
    def test_vn_markers(self):
        assert category_for("nhà thuốc gần đây") == "pharmacy"
        assert category_for("quán phở ngon") == "restaurant"
        assert category_for("bệnh viện") == "hospital"
        assert category_for("trạm y tế") == "clinic"
        assert category_for("chợ") == "marketplace"
        assert category_for("cây xăng") == "fuel"

    def test_osm_tag_mapping(self):
        assert osm_tag_for("nhà thuốc") == '["amenity"="pharmacy"]'
        assert osm_tag_for("quán phở") == '["amenity"="restaurant"]'
        assert osm_tag_for("siêu thị") == '["shop"="supermarket"]'
        assert osm_tag_for("khách sạn") == '["tourism"="hotel"]'

    def test_unknown_query_falls_back_to_any_amenity(self):
        assert osm_tag_for("xyzzy") == '["amenity"]'


class TestGeoAnchor:
    def _post(self, query, **extra):
        app = FastAPI()
        from api.v1 import router

        app.include_router(router)
        client = TestClient(app)
        payload = {"query": query, "limit": 5}
        payload.update(extra)
        return client.post("/v1/business/search", json=payload)

    def test_loc_entity_becomes_anchor(self):
        point = GeoPoint(
            name="Thành phố Hồ Chí Minh",
            lat=10.8231,
            lon=106.6297,
            display_name="Thành phố Hồ Chí Minh, Việt Nam",
        )
        with (
            patch("api.v1.geocode", new=AsyncMock(return_value=point)),
            patch(
                "api.v1.overpass_amenities",
                new=AsyncMock(
                    return_value=[
                        BusinessEntity(
                            name="The Coffee House",
                            category="cafe",
                            lat=10.82,
                            lon=106.63,
                            source_url="https://www.openstreetmap.org/node/1",
                        )
                    ]
                ),
            ),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post("quán cà phê ở Sài Gòn")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anchor"]["resolved_from"] == "entity"
        assert data["anchor"]["name"] == "Thành phố Hồ Chí Minh"
        assert data["lat"] == 10.8231 and data["lon"] == 106.6297
        assert data["provider"] == "osm+web"
        assert data["entities"][0]["name"] == "The Coffee House"

    def test_proximity_phrase_becomes_anchor(self):
        point = GeoPoint(
            name="chợ Bến Thành", lat=10.7725, lon=106.6980, display_name="Chợ Bến Thành"
        )
        mock_geocode = AsyncMock(return_value=point)
        with (
            patch("api.v1.geocode", new=mock_geocode),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post("nhà thuốc gần chợ Bến Thành")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anchor"]["resolved_from"] == "phrase"
        # geocode was asked to resolve the phrase tail
        called_with = mock_geocode.await_args_list[-1].args[0]
        assert "chợ Bến Thành" in called_with or "Bến Thành" in called_with

    def test_no_anchor_falls_back_to_web_only(self):
        with (
            patch("api.v1.geocode", new=AsyncMock(return_value=None)),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post("phở ngon")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anchor"] is None
        assert data["lat"] is None and data["lon"] is None
        assert data["provider"] == "web"

    def test_lat_without_lon_rejected(self):
        resp = self._post("quán phở", lat=10.78)
        assert resp.status_code == 422


class TestOverpassParsing:
    def test_elements_become_business_entities(self):
        from services.geo import overpass_amenities

        payload = {
            "elements": [
                {
                    "type": "node",
                    "id": 42,
                    "lat": 10.77,
                    "lon": 106.69,
                    "tags": {
                        "name": "Nhà Thuốc A",
                        "amenity": "pharmacy",
                        "addr:street": "Lê Lợi",
                        "opening_hours": "08:00-22:00",
                    },
                },
                {"type": "node", "id": 43, "tags": {}},  # unnamed — skipped
            ]
        }

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_Resp())):
            ents = asyncio.run(overpass_amenities(10.77, 106.69, 1.0, '["amenity"="pharmacy"]'))
        assert len(ents) == 1
        assert ents[0].name == "Nhà Thuốc A"
        assert ents[0].category == "pharmacy"
        assert ents[0].hours == "08:00-22:00"
        assert "openstreetmap.org/node/42" in ents[0].source_url

    def test_overpass_failure_degrades(self):
        from services.geo import overpass_amenities

        boom = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with patch("httpx.AsyncClient.post", new=boom):
            assert asyncio.run(overpass_amenities(10.0, 106.0, 1.0)) == []

    def test_geocode_failure_degrades(self):
        from services.geo import geocode

        boom = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch("httpx.AsyncClient.get", new=boom):
            assert asyncio.run(geocode("Sài Gòn")) is None


# ─── LOCAL-1: hedged lanes + quality gate ────────────────────────────────────


class TestLocalDiscoveryHedging:
    def _post(self, **payload):
        app = FastAPI()
        from api.v1 import router

        app.include_router(router)
        return TestClient(app).post("/v1/business/search", json=payload)

    @staticmethod
    def _poi(i: int, **kw) -> BusinessEntity:
        kw.setdefault("category", "restaurant")
        kw.setdefault("lat", 21.2135 + i * 0.001)
        kw.setdefault("lon", 106.1488)
        kw.setdefault("source_url", f"https://www.openstreetmap.org/node/{i}")
        return BusinessEntity(name=f"Quán Ăn Yên Dũng {i}", **kw)

    def test_satisfied_gate_skips_web_and_overpass(self):
        """osm_local alone meets the gate → no Overpass, no orchestrator call."""
        pois = [self._poi(i) for i in range(5)]
        overpass = AsyncMock(return_value=[])
        with (
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=pois)),
            patch("api.v1.overpass_amenities", new=overpass),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            resp = self._post(query="quán ăn Yên Dũng", lat=21.2135, lon=106.1488, limit=20)
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "osm_local"
        assert data["count"] == 5
        overpass.assert_not_awaited()
        mock_orch.return_value.search.assert_not_awaited()
        e = data["entities"][0]
        assert e["origin"] == "osm_local"
        assert e["verified"] is False
        assert e["location_precision"] == "exact"

    def test_canonical_lane_satisfies_gate(self):
        pois = [self._poi(i) for i in range(5)]
        store = Mock()
        store._available = True
        store.search_nearby = AsyncMock(return_value=pois)
        overpass = AsyncMock(return_value=[])
        with (
            patch("api.v1.BusinessStore", return_value=store),
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=[])),
            patch("api.v1.overpass_amenities", new=overpass),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            resp = self._post(query="quán ăn Yên Dũng", lat=21.2135, lon=106.1488, limit=20)
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "geo"
        assert data["entities"][0]["origin"] == "canonical"
        assert data["entities"][0]["verified"] is True
        overpass.assert_not_awaited()
        mock_orch.return_value.search.assert_not_awaited()

    def test_empty_local_lanes_widen_to_web(self):
        class DummyOrchestrator:
            inference = None

            async def search(self, query, mode, progress=None):
                return EvidencePack(
                    answer="",
                    budget_used=budget_for_mode(mode),
                    sources=[
                        Source(
                            source_id="s1",
                            url="https://example.com/pho-hoa",
                            content=(
                                "Quán Phở Hòa\nĐịa chỉ: 12 Lê Lợi, Yên Dũng\nSĐT: 0987 654 321"
                            ),
                        )
                    ],
                )

        with (
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=[])),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator", return_value=DummyOrchestrator()),
        ):
            resp = self._post(query="quán ăn Yên Dũng", lat=21.2135, lon=106.1488, limit=20)
        assert resp.status_code == 200
        data = resp.json()
        assert "web" in data["provider"]
        assert data["count"] >= 1
        assert data["entities"][0]["origin"] == "web_discovery"
        assert data["entities"][0]["location_precision"] == "street"

    def test_lane_exception_degrades_to_empty(self):
        """A lane raising must not fail the request — degrade, widen, return."""
        boom = AsyncMock(side_effect=RuntimeError("pg down"))
        with (
            patch("api.v1.osm_pois_nearby", new=boom),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator") as mock_orch,
        ):
            mock_orch.return_value.search = AsyncMock(return_value=Mock(sources=[]))
            mock_orch.return_value.inference = None
            resp = self._post(query="quán ăn Yên Dũng", lat=21.2135, lon=106.1488, limit=20)
        assert resp.status_code == 200
        assert resp.json()["provider"] == "web"

    def test_expansion_tagged_when_variant_finds_new_source(self):
        class DummyOrchestrator:
            inference = None

            async def search(self, query, mode, progress=None):
                url = (
                    "https://example.com/pho-hoa"
                    if query == "quán phở Yên Dũng"
                    else "https://variant.example.com/nha-hang"
                )
                return EvidencePack(
                    answer="",
                    budget_used=budget_for_mode(mode),
                    sources=[Source(source_id="s1", url=url, content="Quán A\nSĐT: 091")],
                )

        with (
            patch("api.v1.osm_pois_nearby", new=AsyncMock(return_value=[])),
            patch("api.v1.overpass_amenities", new=AsyncMock(return_value=[])),
            patch("api.v1._get_orchestrator", return_value=DummyOrchestrator()),
        ):
            resp = self._post(query="quán phở Yên Dũng", lat=21.2135, lon=106.1488, limit=20)
        assert resp.status_code == 200
        data = resp.json()
        assert "local_expand" in data["provider"]
        assert "web" in data["provider"]


class TestInferCategoryFolded:
    def test_accentless_text_matches_folded_marker(self):
        from core.business_entity import _infer_category

        # "quan ca phe" carries no accents — only the folded fallback hits.
        assert _infer_category("QUAN CA PHE 24H - WI-FI FREE", "") == "cafe"

    def test_accented_marker_still_first(self):
        from core.business_entity import _infer_category

        assert _infer_category("quán cà phê sân vườn", "") == "cafe"
