"""Tests for the evidence layer + v1 API router."""

from unittest.mock import AsyncMock, patch

import config
import pytest
from evidence.citation import build_passage_citations, build_passages
from evidence.claims import Claim, extract_claims, keywords
from evidence.pack import budget_for_mode, build_clusters, build_evidence_pack
from evidence.verifier import contradicts, supports, verify_claims
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import (
    EvidenceCluster,
    ProviderStatus,
    ScrapeResult,
    Source,
    VerdictStatus,
)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")


def make_source(source_id, content, title="", url="", published_at=None):
    return Source(
        source_id=source_id,
        url=url or f"https://{source_id}.example.com",
        title=title or source_id,
        content=content,
        published_at=published_at,
    )


class TestExtractClaims:
    def test_extract_informative_sentences(self):
        claims = extract_claims("OpenAI released GPT-5 in 2026. It has 2 trillion parameters.")
        assert len(claims) == 2
        assert claims[0].claim_id == "c1"
        assert "GPT-5" in claims[0].text
        assert claims[1].claim_id == "c2"

    def test_extract_empty(self):
        assert extract_claims("") == []

    def test_extract_skips_questions_and_labels(self):
        claims = extract_claims("What is GPT-5?\n\nSource: Reuters\n\nGPT-5 is a large model.")
        texts = [c.text for c in claims]
        assert not any(t.startswith("What is") for t in texts)
        assert not any(t.startswith("Source:") for t in texts)

    def test_claim_keywords(self):
        claim = Claim(
            claim_id="c1",
            text="OpenAI released GPT-5",
            keywords=keywords("OpenAI released GPT-5"),
        )
        assert "openai" in claim.keywords
        assert "gpt" in claim.keywords


class TestSupportsContradicts:
    def test_supports_overlap(self):
        assert supports("OpenAI released GPT-5", "OpenAI has released GPT-5 today")

    def test_supports_no_overlap(self):
        assert not supports("OpenAI released GPT-5", "The weather is nice today")

    def test_contradicts_negation(self):
        assert contradicts("OpenAI released GPT-5", "OpenAI did not release GPT-5")

    def test_contradicts_false_when_no_negation(self):
        assert not contradicts("OpenAI released GPT-5", "OpenAI released GPT-5")


class TestVerifyClaims:
    def test_supported(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [
            EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True),
            EvidenceCluster(cluster_id="clu2", sources=["s2"], is_independent=True),
        ]
        sources = [
            make_source("s1", "OpenAI released GPT-5 in 2026."),
            make_source("s2", "GPT-5 was released by OpenAI."),
        ]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.SUPPORTED.value
        assert set(results[0].evidence) == {"s1", "s2"}
        assert results[0].confidence >= 0.8

    def test_partially_supported(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
        sources = [make_source("s1", "OpenAI released GPT-5 in 2026.")]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.PARTIALLY_SUPPORTED.value
        assert results[0].evidence == ["s1"]

    def test_insufficient_evidence(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
        sources = [make_source("s1", "The sky is blue.")]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.INSUFFICIENT_EVIDENCE.value

    def test_contradicted(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
        sources = [make_source("s1", "OpenAI did not release GPT-5.")]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.CONTRADICTED.value
        assert results[0].sources_conflict is False

    def test_source_conflict(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [
            EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True),
            EvidenceCluster(cluster_id="clu2", sources=["s2"], is_independent=True),
        ]
        sources = [
            make_source("s1", "OpenAI released GPT-5."),
            make_source("s2", "OpenAI did not release GPT-5."),
        ]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.SOURCE_CONFLICT.value
        assert results[0].sources_conflict is True
        assert results[0].contradictions

    def test_outdated(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
        sources = [
            make_source(
                "s1",
                "OpenAI released GPT-5.",
                published_at="2020-01-01T00:00:00Z",
            )
        ]
        results = verify_claims(claims, clusters, sources=sources, max_age_days=365)
        assert results[0].status == VerdictStatus.OUTDATED.value

    def test_non_independent_cluster_not_counted(self):
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [
            EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True),
            EvidenceCluster(cluster_id="clu2", sources=["s2"], is_independent=False),
        ]
        sources = [
            make_source("s1", "OpenAI released GPT-5."),
            make_source("s2", "OpenAI released GPT-5 as well."),
        ]
        results = verify_claims(claims, clusters, sources=sources)
        assert results[0].status == VerdictStatus.PARTIALLY_SUPPORTED.value
        assert results[0].evidence == ["s1"]


class TestPassageCitations:
    def test_offsets_within_content(self):
        content = (
            "This is a long document. "
            + ("padding " * 40)
            + "OpenAI released GPT-5 in 2026."
            + (" more " * 20)
        )
        source = make_source("s1", content)
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        citations = build_passage_citations(claims, [source])
        assert len(citations) == 1
        evidence = citations[0].evidence[0]
        assert evidence["source_id"] == "s1"
        assert evidence["url"] == "https://s1.example.com"
        assert evidence["quote_start"] >= 0
        assert evidence["quote_end"] <= len(content)
        assert evidence["quote_start"] < evidence["quote_end"]
        assert "GPT-5" in evidence["quote"]

    def test_uses_verification_evidence_ids(self):
        content = "OpenAI released GPT-5 in 2026."
        source = make_source("s1", content)
        claims = [
            Claim(
                claim_id="c1",
                text="OpenAI released GPT-5",
                keywords=keywords("OpenAI released GPT-5"),
            )
        ]
        clusters = [EvidenceCluster(cluster_id="clu1", sources=["s1"], is_independent=True)]
        verifications = verify_claims(claims, clusters, sources=[source])
        citations = build_passage_citations(verifications, [source])
        assert citations[0].evidence[0]["source_id"] == "s1"

    def test_build_passages_chunks(self):
        content = "word " * 300
        source = make_source("s1", content)
        passages = build_passages(source, chunk_size=100, chunk_overlap=20)
        assert len(passages) > 1
        assert all(p.quote_start < p.quote_end for p in passages)
        assert passages[0].quote_start == 0


class TestClusters:
    def test_groups_duplicate_content(self):
        sources = [
            make_source("s1", "OpenAI released GPT-5.", title="A"),
            make_source("s2", "OpenAI released GPT-5.", title="B"),
        ]
        clusters = build_clusters(sources)
        assert len(clusters) == 1
        assert set(clusters[0].sources) == {"s1", "s2"}
        assert clusters[0].is_independent is True

    def test_distinct_content_stays_separate(self):
        sources = [
            make_source("s1", "OpenAI released GPT-5."),
            make_source("s2", "Tesla released a new car."),
        ]
        clusters = build_clusters(sources)
        assert len(clusters) == 2


class TestBuildEvidencePack:
    def test_pack_fields(self):
        answer = "OpenAI released GPT-5 in 2026."
        claims = extract_claims(answer)
        sources = [
            make_source("s1", "OpenAI released GPT-5 in 2026."),
            make_source("s2", "GPT-5 was released in 2026 by OpenAI."),
        ]
        pack = build_evidence_pack(answer, claims, sources)
        assert pack.answer == answer
        assert pack.sources == sources
        assert pack.coverage == 1.0
        assert 0 <= pack.confidence <= 1
        assert pack.budget_used.max_queries == budget_for_mode("normal").max_queries

    def test_coverage_excludes_insufficient(self):
        answer = "OpenAI released GPT-5 in 2026. The sky is blue."
        claims = extract_claims(answer)
        sources = [make_source("s1", "OpenAI released GPT-5 in 2026.")]
        pack = build_evidence_pack(answer, claims, sources)
        # Only the first claim has evidence; second is insufficient.
        assert pack.coverage == 0.5


class TestApiV1:
    @pytest.fixture
    def client(self):
        from api.v1 import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_health(self, client):
        with patch(
            "api.v1._service_statuses",
            new=AsyncMock(return_value={"searxng": "ok", "firecrawl": "ok"}),
        ):
            resp = client.get("/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_verify(self, client):
        body = {
            "claims": ["OpenAI released GPT-5"],
            "clusters": [{"cluster_id": "clu1", "sources": ["s1"], "is_independent": True}],
            "sources": [
                {
                    "source_id": "s1",
                    "url": "https://s1.example.com",
                    "content": "OpenAI released GPT-5.",
                }
            ],
        }
        resp = client.post("/v1/verify", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["claim_id"] == "c1"
        assert data[0]["status"] == VerdictStatus.PARTIALLY_SUPPORTED.value

    def test_capabilities(self, client):
        with patch(
            "api.v1._provider_statuses",
            new=AsyncMock(return_value=[ProviderStatus(name="searxng", status="ok")]),
        ):
            resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert {"fast", "normal", "deep"} <= {m.lower() for m in data["modes"]}
        assert "verification" in data["features"]

    def test_providers(self, client):
        with patch(
            "api.v1._provider_statuses",
            new=AsyncMock(return_value=[ProviderStatus(name="searxng", status="ok")]),
        ):
            resp = client.get("/v1/providers")
        assert resp.status_code == 200
        assert resp.json()[0]["name"] == "searxng"

    def test_research(self, client):
        class DummyInference:
            api_key = ""

        class DummyOrchestrator:
            inference = DummyInference()

            async def research(self, query, mode, max_hops=None, progress=None):
                from models import EvidencePack

                return EvidencePack(
                    answer="",
                    budget_used=budget_for_mode(mode),
                    sources=[
                        make_source(
                            "s1",
                            "OpenAI released GPT-5 in 2026.",
                            title="OpenAI",
                            url="https://s1.example.com",
                        )
                    ],
                    citations=[],
                )

        with patch("api.v1._get_orchestrator", return_value=DummyOrchestrator()):
            resp = client.post("/v1/research", json={"query": "GPT-5", "mode": "fast"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"]
        assert data["claims"]
        assert data["budget_used"]["max_queries"] == 2

    def test_research_stream_events(self, client):
        class DummyInference:
            api_key = ""

        class DummyOrchestrator:
            inference = DummyInference()

            async def research(self, query, mode, max_hops=None, progress=None):
                if progress:
                    await progress("planning", {"query": query})
                    await progress("searching", {"query": query})
                    await progress("fetching", {"to_fetch": 1})
                from models import EvidencePack

                return EvidencePack(
                    answer="",
                    budget_used=budget_for_mode(mode),
                    sources=[
                        make_source(
                            "s1",
                            "OpenAI released GPT-5 in 2026.",
                            title="OpenAI",
                            url="https://s1.example.com",
                        )
                    ],
                    citations=[],
                )

        with patch("api.v1._get_orchestrator", return_value=DummyOrchestrator()):
            resp = client.post("/v1/research/stream", json={"query": "GPT-5", "mode": "fast"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = [
            line.split(": ", 1)[1] for line in resp.text.splitlines() if line.startswith("event: ")
        ]
        assert {"planning", "searching", "fetching", "verifying", "answer"} <= set(events)

    def test_read(self, client):
        # SSRF guard resolve DNS — dùng IP public literal để không phụ thuộc DNS trong test
        with patch(
            "providers.firecrawl.firecrawl_scrape",
            new=AsyncMock(
                return_value=ScrapeResult(
                    url="https://s1.example.com",
                    title="Example",
                    markdown="word " * 100,
                )
            ),
        ):
            resp = client.post(
                "/v1/read",
                json={
                    "url": "http://93.184.216.34/docs",
                    "chunk_size": 50,
                    "chunk_overlap": 10,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Example"
        assert len(data["passages"]) >= 1
