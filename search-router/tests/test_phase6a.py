"""Phase 6A — production wiring tests.

Covers: OpenSearch client async API, Qdrant named-vector/UUID5 wire format,
'research' mode alias, OpenSearch index provider lane, L17 indexing worker
dual-write, canonical SSE events on /v1/answer.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipeline.search_modes import get_mode

# ─── Mode vocabulary ─────────────────────────────────────────────────────────


def test_research_mode_alias():
    """'research' resolves to deep (Vane quality → Search-Hub research)."""
    assert get_mode("research").name == "deep"
    assert get_mode("RESEARCH").name == "deep"


def test_search_request_accepts_vane_fields():
    from api.v1 import SearchRequest

    req = SearchRequest(
        query="qwen pricing",
        mode="research",
        sources=["web", "academic"],
        stream=True,
        history=[["human", "hi"]],
    )
    assert req.mode == "research"
    assert req.sources == ["web", "academic"]
    assert req.stream is True
    assert req.history == [["human", "hi"]]


def test_search_request_still_rejects_bad_mode():
    from api.v1 import SearchRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SearchRequest(query="x", mode="bogus")


# ─── OpenSearchClient async API ──────────────────────────────────────────────


class TestOpenSearchAsync:
    def _client(self):
        from opensearch.client import OpenSearchClient

        return OpenSearchClient()

    def test_search_awaits_bm25(self):
        client = self._client()
        client.search_bm25 = MagicMock(return_value=[{"doc_id": "d1"}])
        out = asyncio.run(client.search("web_passages", "hello", top_k=5))
        assert out == [{"doc_id": "d1"}]
        client.search_bm25.assert_called_once_with("web_passages", "hello", top_k=5, filters=None)

    def test_upsert_awaits_index_document(self):
        client = self._client()
        client.index_document = MagicMock(return_value=True)
        ok = asyncio.run(client.upsert("web_passages", "p1", {"text": "t"}))
        assert ok is True
        client.index_document.assert_called_once_with("web_passages", "p1", {"text": "t"})

    def test_index_document_sends_refresh_param(self):
        client = self._client()
        inner = MagicMock()
        client._client = inner
        assert client.index_document("web_passages", "d1", {"text": "t"}) is True
        inner.index.assert_called_once_with(
            index="web_passages", id="d1", body={"text": "t"}, params={"refresh": "true"}
        )

    def test_bulk_index_sends_refresh_param(self):
        client = self._client()
        inner = MagicMock()
        client._client = inner
        ok = client.bulk_index("web_passages", [{"doc_id": "d1"}, {"doc_id": "d2"}])
        assert ok is True
        kwargs = inner.bulk.call_args.kwargs
        assert len(kwargs["body"]) == 4 and kwargs["params"] == {"refresh": "true"}

    def test_get_all_ids_scrolls(self):
        client = self._client()
        inner = MagicMock()
        inner.search.return_value = {
            "_scroll_id": "s1",
            "hits": {"hits": [{"_id": "a"}, {"_id": "b"}]},
        }
        inner.scroll.return_value = {"hits": {"hits": []}}
        client._client = inner
        ids = asyncio.run(client.get_all_ids("web_passages"))
        assert ids == ["a", "b"]
        inner.clear_scroll.assert_called_once_with(scroll_id="s1")

    def test_get_all_ids_swallows_errors(self):
        client = self._client()
        inner = MagicMock()
        inner.search.side_effect = RuntimeError("down")
        client._client = inner
        assert asyncio.run(client.get_all_ids("web_passages")) == []


# ─── QdrantClient wire format ────────────────────────────────────────────────


class TestQdrantClient:
    def test_point_id_is_uuid5_deterministic(self):
        from qdrant.client import to_point_id

        a = to_point_id("doc_9f82#p_007")
        b = to_point_id("doc_9f82#p_007")
        assert a == b
        assert uuid.UUID(a)  # valid UUID — Qdrant rejects arbitrary strings
        assert to_point_id("other") != a

    def test_search_dense_sends_named_vector(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://x")
        mock_http = AsyncMock()
        mock_http.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "result": [{"id": "u1", "score": 0.9, "payload": {"passage_id": "doc_1#p_000"}}]
            },
        )
        mock_http.post.return_value.raise_for_status = MagicMock()
        client._client = mock_http

        out = asyncio.run(client.search_dense("web_passages_v1", [0.1] * 4))
        body = mock_http.post.call_args.kwargs["json"]
        assert body["vector"] == {"name": "dense", "vector": [0.1] * 4}
        assert out[0].point_id == "doc_1#p_000"  # passage_id recovered
        assert out[0].qdrant_id == "u1"

    def test_search_sparse_sends_named_sparse(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://x")
        mock_http = AsyncMock()
        mock_http.post.return_value = MagicMock(status_code=200, json=lambda: {"result": []})
        mock_http.post.return_value.raise_for_status = MagicMock()
        client._client = mock_http

        asyncio.run(client.search_sparse("web_passages_v1", {"7": 0.5, "9": 0.2}))
        body = mock_http.post.call_args.kwargs["json"]
        assert body["vector"]["name"] == "sparse"
        assert body["vector"]["vector"] == {"indices": [7, 9], "values": [0.5, 0.2]}

    def test_upsert_translates_to_uuid_and_keeps_passage_id(self):
        from qdrant.client import QdrantClient, QdrantPoint, to_point_id

        client = QdrantClient(base_url="http://x")
        mock_http = AsyncMock()
        mock_http.put.return_value = MagicMock(status_code=200)
        client._client = mock_http

        ok = asyncio.run(
            client.upsert_points(
                "web_passages_v1",
                [QdrantPoint(point_id="doc_1#p_000", dense=[0.1], payload={"domain": "x"})],
            )
        )
        assert ok is True
        point = mock_http.put.call_args.kwargs["json"]["points"][0]
        assert point["id"] == to_point_id("doc_1#p_000")
        assert point["payload"]["passage_id"] == "doc_1#p_000"
        assert point["payload"]["domain"] == "x"


# ─── OpenSearch index provider ───────────────────────────────────────────────


class TestOpenSearchProvider:
    def test_dedupes_by_url_keeps_best(self):
        from providers.opensearch_index import OpenSearchIndexProvider

        hits = [
            {
                "doc_id": "p1",
                "score": 5.0,
                "source": {"canonical_url": "https://a.com/x", "title": "A", "text_ctx": "t1"},
            },
            {
                "doc_id": "p2",
                "score": 9.0,
                "source": {"canonical_url": "https://a.com/x", "title": "A", "text_ctx": "t2"},
            },
            {
                "doc_id": "p3",
                "score": 3.0,
                "source": {"canonical_url": "https://b.com/y", "title": "B"},
            },
        ]
        out = OpenSearchIndexProvider._to_results(hits, 10)
        assert len(out) == 2
        assert out[0].url == "https://a.com/x" and out[0].score == 9.0 / 20.0
        assert out[0].description == "t2"

    def test_empty_when_disabled(self):
        from agent.retriever import _opensearch_lane

        with patch("agent.retriever.settings") as s:
            s.opensearch_enabled = False
            assert asyncio.run(_opensearch_lane("q", 5)) == []


# ─── VectorSyncWorker edge cases ─────────────────────────────────────────────


class TestSyncWorkerEdges:
    def test_opensearch_doc_omits_empty_embedding(self):
        from workers.vector_sync_worker import SyncTask, VectorSyncWorker

        os_client = MagicMock()
        os_client.upsert = AsyncMock(return_value=True)
        qd = MagicMock()
        worker = VectorSyncWorker(os_client, qd)

        task = SyncTask(passage_id="p1", text="t", text_ctx="tc", embedding=None)
        ok = asyncio.run(worker._write_opensearch(task))
        assert ok is True
        doc = os_client.upsert.call_args.kwargs["document"]
        assert "embedding" not in doc

    def test_qdrant_skipped_without_vectors(self):
        from workers.vector_sync_worker import SyncTask, VectorSyncWorker

        worker = VectorSyncWorker(MagicMock(), MagicMock())
        task = SyncTask(passage_id="p1", text="t", text_ctx="tc")
        assert asyncio.run(worker._write_qdrant(task)) is False


# ─── Indexing worker real path ───────────────────────────────────────────────


class TestIndexingWorkerReal:
    def test_process_batch_dual_writes(self):
        from workers.indexing_worker import IndexingTask, IndexingWorker

        os_client = MagicMock()
        os_client.upsert = AsyncMock(return_value=True)
        qd = MagicMock()
        qd.upsert_points = AsyncMock(return_value=True)

        worker = IndexingWorker(opensearch_client=os_client, qdrant_client=qd)
        worker._embed = AsyncMock(return_value=[[0.1] * 8] * 100)

        task = IndexingTask(
            doc_id="doc_1",
            url="https://a.com/x",
            title="T",
            text="word " * 300,
            domain="a.com",
            source_type="web",
        )
        out = asyncio.run(worker.process_batch([task]))
        assert out["indexed"] == 1
        # document upsert + passage upserts landed on OpenSearch
        assert os_client.upsert.await_count >= 2
        # passage points went to Qdrant with stable IDs
        flat = [
            p.point_id for call in qd.upsert_points.await_args_list for p in call.kwargs["points"]
        ]
        assert flat and all(pid.startswith("doc_1#p_") for pid in flat)

    def test_submit_document_noop_outside_loop(self):
        from workers.indexing_worker import submit_document

        # No running loop → must not raise.
        submit_document("https://a.com", "t", "text")

    def test_submit_document_disabled(self):
        from workers import indexing_worker

        with patch.object(indexing_worker.settings, "indexing_enabled", False):
            indexing_worker.submit_document("https://a.com", "t", "text")


# ─── Canonical SSE on /v1/answer ─────────────────────────────────────────────


class TestAnswerStream:
    def test_stream_emits_canonical_events(self):
        from fastapi.testclient import TestClient

        fake_result = {
            "answer": "hello world answer",
            "confidence": 0.8,
            "sources": [{"url": "https://a.com", "title": "A"}],
            "citations": [{"claim": "c1", "verified": True}],
            "verification": {"claims_total": 1, "claims_verified": 1},
            "timings": {"total": 0.1},
        }

        async def fake_run(context, language=None, freshness=None, emit=None, **kw):
            # P3: real streams come through emit — exercise the live path.
            if emit:
                await emit("planning", {"query": context.query})
                await emit("source", {"url": "https://a.com", "title": "A"})
                await emit("answer.delta", {"text": "hello "})
                await emit("answer.delta", {"text": "world answer"})
            return fake_result

        with patch("api.v1.run_research", new=fake_run):
            from main import app

            client = TestClient(app)
            resp = client.post(
                "/v1/answer",
                json={"query": "test", "stream": True},
            )
            assert resp.status_code == 200
            events = [line[7:] for line in resp.text.splitlines() if line.startswith("event: ")]
            assert events[0] == "init"
            assert "planning" in events
            assert "source" in events
            assert "answer.delta" in events
            assert "citation" in events
            assert events[-1] == "done"

    def test_answer_json_unchanged_without_stream(self):
        from fastapi.testclient import TestClient

        fake_result = {
            "answer": "ans",
            "confidence": 0.9,
            "sources": [],
            "citations": [],
            "verification": {"claims_total": 2, "claims_verified": 2},
            "timings": {},
        }
        with patch("api.v1.run_research", new=AsyncMock(return_value=fake_result)):
            from main import app

            client = TestClient(app)
            resp = client.post("/v1/answer", json={"query": "q", "mode": "research"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["answer"] == "ans"
            # coverage = verified claims / total claims; confidence stays separate.
            assert body["coverage"] == 1.0
            assert body["confidence"] == 0.9
            assert body["verified"] is True

    def test_answer_coverage_zero_without_claims(self):
        from fastapi.testclient import TestClient

        fake_result = {
            "answer": "ans",
            "confidence": 0.5,
            "sources": [],
            "citations": [],
            "verification": {"claims_total": 0, "claims_verified": 0},
            "timings": {},
        }
        with patch("api.v1.run_research", new=AsyncMock(return_value=fake_result)):
            from main import app

            client = TestClient(app)
            resp = client.post("/v1/answer", json={"query": "q", "mode": "research"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["coverage"] == 0.0
            assert body["confidence"] == 0.5
