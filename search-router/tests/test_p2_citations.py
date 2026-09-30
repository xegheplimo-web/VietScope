"""P2 — CitationV2 end-to-end.

The research result's ``citations`` key must carry passage-level evidence:
``claim_id`` + ``evidence[{source_id, passage_id, url, quote, quote_start,
quote_end, retrieved_at}]`` — so clients can map an answer claim to the
exact supporting passage inside the fetched source document.
"""

import asyncio

import agent.orchestrator as orch
from agent.orchestrator import run_research
from evidence.citation import _claim_evidence_ids, _claim_text, build_passage_citations
from models import ScrapeResult, Source
from research_models.research_state import (
    Claim,
    EvidenceItem,
    GapResult,
    ResearchContext,
    SourceResult,
)


class _FakeReranker:
    def available(self):
        return True

    def score(self, query, docs):
        return [0.9] * len(docs)


class TestAccessors:
    """Duck-typed accessors must handle agent Claim/EvidenceItem shapes."""

    def test_claim_text_reads_claim_attr(self):
        claim = Claim(claim="Hanoi is the capital of Vietnam", verified=True)
        assert _claim_text(claim) == "Hanoi is the capital of Vietnam"

    def test_evidence_ids_from_evidence_items(self):
        ev = EvidenceItem(source_id="psg_001_2", url="https://a.example", title="t", quote="q")
        claim = Claim(claim="x", evidence=[ev])
        assert _claim_evidence_ids(claim) == ["psg_001_2"]

    def test_evidence_ids_mixed_shapes(self):
        claim = {
            "evidence": [
                "src_1",
                {"source_id": "src_2"},
                EvidenceItem(source_id="src_3", url="u", title="t", quote="q"),
            ]
        }
        assert _claim_evidence_ids(claim) == ["src_1", "src_2", "src_3"]


class TestPassageCitationShape:
    def test_dict_claim_produces_offsets(self):
        body = ("intro paragraph. " * 10) + "Hanoi is the capital of Vietnam. " + ("tail. " * 20)
        sources = [
            Source(
                source_id="psg_000_0",
                url="https://a.example/x",
                title="A",
                content=body,
            )
        ]
        claims = [
            {"claim_id": "c0", "text": "Hanoi is the capital of Vietnam", "evidence": ["psg_000_0"]}
        ]
        out = build_passage_citations(claims, sources)
        assert len(out) == 1
        cv = out[0]
        assert cv.claim_id == "c0"
        ev = cv.evidence[0]
        assert ev["source_id"] == "psg_000_0"
        assert ev["url"] == "https://a.example/x"
        assert ev["passage_id"]
        assert 0 <= ev["quote_start"] < ev["quote_end"] <= len(body)
        assert ev["quote"] == body[ev["quote_start"] : ev["quote_end"]]
        assert "capital of Vietnam" in ev["quote"]


def _stub_research(monkeypatch, claims):
    """Stub live-web + verification so run_research runs offline with known claims."""

    async def fake_retrieve(query, max_results=10, lang="vi"):
        return [
            SourceResult(
                source_id=f"s{i}",
                url=f"https://r{i}.example/page",
                title=f"result {i}",
                description="d",
                domain=f"r{i}.example",
                score=0.9 - i * 0.1,
            )
            for i in range(3)
        ]

    page_text = "The capital of Vietnam is Hanoi. It sits on the Red River. " * 30

    async def fake_read(urls, timeout=None, max_concurrent=5):
        from pipeline.reader import ReadResult

        return [
            ReadResult(url=u, success=True, text=page_text, title=f"page {u}", tier="http")
            for u in urls
        ]

    async def fake_gaps(evidence, query):
        return GapResult(known=["x"], missing=[], confidence=0.9, need_more_search=False)

    async def fake_verify(answer, evidence):
        return (
            answer,
            claims,
            {
                "claims_total": len(claims),
                "claims_verified": sum(1 for c in claims if c.verified),
            },
        )

    monkeypatch.setattr(orch, "retrieve", fake_retrieve)
    monkeypatch.setattr(orch, "read_batch", fake_read)
    monkeypatch.setattr(orch, "analyze_gaps", fake_gaps)
    monkeypatch.setattr(orch, "verify_answer", fake_verify)
    monkeypatch.setattr(orch, "get_default_reranker", lambda: _FakeReranker())


class TestRunResearchCitations:
    def test_citations_carry_passage_offsets(self, monkeypatch):
        ev = EvidenceItem(
            source_id="psg_000_0",
            url="https://r0.example/page",
            title="page",
            quote="The capital of Vietnam is Hanoi.",
            support=0.9,
        )
        claims = [Claim(claim="The capital of Vietnam is Hanoi", evidence=[ev], verified=True)]
        _stub_research(monkeypatch, claims)

        result = asyncio.run(run_research(ResearchContext(query="capital of vietnam", mode="fast")))

        assert result["citations"], "expected at least one citation"
        cit = result["citations"][0]
        assert cit["claim_id"] == "c0"
        assert cit["claim"] == "The capital of Vietnam is Hanoi"
        assert cit["verified"] is True
        assert cit["evidence_count"] >= 1
        item = cit["evidence"][0]
        for key in (
            "source_id",
            "passage_id",
            "url",
            "quote",
            "quote_start",
            "quote_end",
            "retrieved_at",
        ):
            assert key in item, f"missing evidence key: {key}"
        assert item["quote_start"] < item["quote_end"]
        assert item["quote"]

    def test_claim_without_matching_source_still_emitted(self, monkeypatch):
        ev = EvidenceItem(
            source_id="psg_000_0",
            url="https://nowhere.example",  # not among fetched pages
            title="t",
            quote="unrelated quote text",
            support=0.5,
        )
        claims = [
            Claim(claim="The capital of Vietnam is Hanoi", evidence=[ev], verified=True),
            Claim(claim="Completely unsupported statement", evidence=[], verified=False),
        ]
        _stub_research(monkeypatch, claims)

        result = asyncio.run(run_research(ResearchContext(query="q", mode="fast")))
        cits = result["citations"]
        assert len(cits) == 2
        assert cits[0]["claim_id"] == "c0" and cits[1]["claim_id"] == "c1"
        # c1 has no evidence — entry still emitted with empty evidence list
        assert cits[1]["verified"] is False
        assert cits[1]["evidence"] == []
        assert cits[1]["evidence_count"] == 0

    def test_no_claims_gives_empty_citations(self, monkeypatch):
        _stub_research(monkeypatch, [])
        result = asyncio.run(run_research(ResearchContext(query="q", mode="fast")))
        assert result["citations"] == []


class TestScrapeResultContent:
    """_build_citations reads .markdown off ScrapeResult — guard the contract."""

    def test_scrape_result_markdown_used_as_source_text(self):
        ctx = ResearchContext(query="q", mode="fast")
        body = "alpha beta gamma delta " * 40
        ctx.scraped_content = [ScrapeResult(url="https://a.example", title="A", markdown=body)]
        ev = EvidenceItem(
            source_id="psg_000_0",
            url="https://a.example",
            title="A",
            quote="alpha beta gamma",
            support=0.9,
        )
        ctx.evidence = [ev]
        ctx.claims = [Claim(claim="alpha beta gamma", evidence=[ev], verified=True)]

        out = orch._build_citations(ctx)
        assert out[0]["evidence"], "expected citation evidence"
        assert out[0]["evidence"][0]["url"] == "https://a.example"
