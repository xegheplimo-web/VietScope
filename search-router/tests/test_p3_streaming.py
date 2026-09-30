"""P3 — real SSE streaming.

The research pipeline emits live state events through ``run_research(emit=)``
and streams LLM tokens via ``stream_research_answer`` — no more replaying a
finished answer in 600-char chunks.
"""

import asyncio

import agent.orchestrator as orch
import pipeline.rag as rag
from agent.orchestrator import run_research
from models import Source
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


def _sources():
    return [
        Source(
            source_id="s1",
            url="https://a.example",
            title="A",
            content="the capital of vietnam is hanoi " * 30,
        )
    ]


class TestStreamResearchAnswer:
    def test_tokens_stream_and_return_full_answer(self, monkeypatch):
        monkeypatch.setattr(rag.settings, "llm_api_key", "test-key")

        class _GW:
            async def stream(self, messages, **kw):
                for tok in ["Hello ", "world", "!"]:
                    yield tok

        monkeypatch.setattr(rag, "get_inference_gateway", lambda: _GW())
        deltas: list[str] = []

        async def collect(t):
            deltas.append(t)

        out = asyncio.run(rag.stream_research_answer("q", _sources(), on_delta=collect))
        assert deltas == ["Hello ", "world", "!"]
        # ensure_sources_section may append a Sources block — the returned
        # text is the final canonical answer, streamed text is the raw body.
        assert out.startswith("Hello world!")

    def test_no_key_emits_single_fallback_delta(self, monkeypatch):
        monkeypatch.setattr(rag.settings, "llm_api_key", "")
        deltas: list[str] = []

        async def collect(t):
            deltas.append(t)

        out = asyncio.run(rag.stream_research_answer("q", _sources(), on_delta=collect))
        assert len(deltas) == 1
        assert out == deltas[0]

    def test_stream_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(rag.settings, "llm_api_key", "test-key")

        class _GW:
            async def stream(self, messages, **kw):
                return
                yield  # pragma: no cover — async gen that yields nothing

        monkeypatch.setattr(rag, "get_inference_gateway", lambda: _GW())
        deltas: list[str] = []

        async def collect(t):
            deltas.append(t)

        out = asyncio.run(rag.stream_research_answer("q", _sources(), on_delta=collect))
        assert len(deltas) == 1  # single fallback delta
        assert out == deltas[0]

    def test_no_sources_emits_notice(self):
        deltas: list[str] = []

        async def collect(t):
            deltas.append(t)

        out = asyncio.run(rag.stream_research_answer("q", [], on_delta=collect))
        assert deltas == [out]


def _stub_pipeline(monkeypatch, *, filtered_answer=None):
    """Run run_research offline with a streaming synthesizer."""

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

    async def fake_read(urls, timeout=None, max_concurrent=5):
        from pipeline.reader import ReadResult

        return [
            ReadResult(
                url=u,
                success=True,
                text="the capital of vietnam is hanoi " * 30,
                title=f"page {u}",
                tier="http",
            )
            for u in urls
        ]

    async def fake_gaps(evidence, query):
        return GapResult(known=["x"], missing=[], confidence=0.9, need_more_search=False)

    async def fake_synth(query, claims, evidence, on_delta=None):
        if on_delta is not None:
            for tok in ["Answer ", "body ", "here."]:
                await on_delta(tok)
        return "Answer body here.", 0.8

    async def fake_verify(answer, evidence):
        claims = [
            Claim(
                claim="hanoi is the capital",
                evidence=[
                    EvidenceItem(
                        source_id="psg_000_0",
                        url="https://r0.example/page",
                        title="t",
                        quote="the capital of vietnam is hanoi",
                        support=0.9,
                    )
                ],
                verified=True,
            )
        ]
        return (
            filtered_answer if filtered_answer is not None else answer,
            claims,
            {"claims_total": 1, "claims_verified": 1, "claims_removed": 0},
        )

    monkeypatch.setattr(orch, "retrieve", fake_retrieve)
    monkeypatch.setattr(orch, "read_batch", fake_read)
    monkeypatch.setattr(orch, "analyze_gaps", fake_gaps)
    monkeypatch.setattr(orch, "synthesize_answer", fake_synth)
    monkeypatch.setattr(orch, "verify_answer", fake_verify)
    monkeypatch.setattr(orch, "get_default_reranker", lambda: _FakeReranker())


class TestRunResearchEmits:
    def test_live_event_order(self, monkeypatch):
        _stub_pipeline(monkeypatch)
        events: list[tuple[str, dict]] = []

        async def emit(ev, data):
            events.append((ev, data))

        result = asyncio.run(
            run_research(ResearchContext(query="capital of vietnam", mode="fast"), emit=emit)
        )
        names = [e for e, _ in events]
        # State events precede answer deltas; verified comes after synthesis.
        assert names[0] == "planning"
        assert "plan" in names and "search.started" in names and "search.done" in names
        assert "source" in names and "source.read" in names
        assert "evidence" in names and "synthesizing" in names
        deltas = [d["text"] for e, d in events if e == "answer.delta"]
        assert deltas == ["Answer ", "body ", "here."]
        assert names.index("synthesizing") < names.index("answer.delta") < names.index("verified")
        # streamed text equals final answer → no reconcile event needed
        assert "answer.final" not in names
        assert result["answer"]

    def test_answer_final_reconciles_filtered_text(self, monkeypatch):
        _stub_pipeline(monkeypatch, filtered_answer="FILTERED final text")
        events: list[tuple[str, dict]] = []

        async def emit(ev, data):
            events.append((ev, data))

        asyncio.run(run_research(ResearchContext(query="q", mode="fast"), emit=emit))
        finals = [d["text"] for e, d in events if e == "answer.final"]
        assert finals == ["FILTERED final text"]

    def test_no_emit_is_noop(self, monkeypatch):
        _stub_pipeline(monkeypatch)
        result = asyncio.run(run_research(ResearchContext(query="q", mode="fast")))
        assert result["answer"]

    def test_broken_sink_does_not_kill_run(self, monkeypatch):
        _stub_pipeline(monkeypatch)

        async def bad_emit(ev, data):
            raise RuntimeError("client gone")

        result = asyncio.run(run_research(ResearchContext(query="q", mode="fast"), emit=bad_emit))
        assert result["answer"]
