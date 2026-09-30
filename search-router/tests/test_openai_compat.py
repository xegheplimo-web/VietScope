"""P1 — OpenAI-compatible surface: /v1/models + /v1/chat/completions.

The adapter is a translation layer only; run_research / resolve_request_query
are stubbed so tests are hermetic (same pattern as test_conversation_context).
"""

import asyncio
import json
from types import SimpleNamespace

import api.openai_compat as compat
import pytest
from security.apikeys import required_scope

_FAKE_RESULT = {
    "answer": "Giá vàng hôm nay khoảng 90 triệu/lượng. [1]",
    "confidence": 0.82,
    "sources": [
        {"url": "https://vnexpress.net/gia-vang", "title": "Giá vàng hôm nay", "score": 0.9},
        {"url": "https://doji.vn/gia-vang", "title": "Bảng giá vàng", "score": 0.8},
    ],
    "citations": [{"claim": "90tr/lượng", "url": "https://vnexpress.net/gia-vang"}],
    "search": {"generated_queries": ["gia vang hom nay"], "raw_results": 5},
    "verification": {"claims_total": 1, "claims_verified": 1},
    "timings": {"total": 0.1},
}


def _stub_pipeline(monkeypatch, result=None, emit_deltas=()):
    """Patch run_research + resolver; returns dict capturing call args."""
    captured: dict = {}

    async def fake_run(context, **kw):
        captured["query"] = context.query
        emit = kw.get("emit")
        if emit is not None:
            for text in emit_deltas:
                await emit("answer.delta", {"text": text})
        return dict(result if result is not None else _FAKE_RESULT)

    async def fake_resolve(query, *, history=None, owner=None, **kw):
        captured["history"] = history
        captured["owner"] = owner
        return SimpleNamespace(query=query, resolved=False)

    monkeypatch.setattr(compat, "run_research", fake_run)
    monkeypatch.setattr(compat, "resolve_request_query", fake_resolve)
    return captured


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


def _sse_chunks(body: str) -> list[dict]:
    """Parse a streaming body into decoded data: payloads (excluding [DONE])."""
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            out.append(json.loads(payload))
    return out


# ─── /v1/models ──────────────────────────────────────────────────────────────


class TestModels:
    def test_list_shape(self, client):
        r = client.get("/v1/models")
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert "duyai-search" in ids
        assert all(m["object"] == "model" for m in body["data"])

    def test_scope_map(self):
        # models needs a product scope (chat:use) — not public, not admin.
        assert required_scope("/v1/models", "GET") == "chat:use"
        assert required_scope("/v1/chat/completions", "POST") == "chat:use"
        # Neighbors unchanged.
        assert required_scope("/v1/search", "POST") == "search:read"
        assert required_scope("/v1/health", "GET") is None
        assert required_scope("/v1/providers/health", "GET") == "admin:debug"


# ─── non-stream chat/completions ────────────────────────────────────────────


class TestChatCompletions:
    def test_completion_shape(self, monkeypatch, client):
        _stub_pipeline(monkeypatch)
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "Giá vàng hôm nay?"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "duyai-search"
        assert body["id"].startswith("chatcmpl-")
        choice = body["choices"][0]
        assert choice["index"] == 0
        assert choice["finish_reason"] == "stop"
        msg = choice["message"]
        assert msg["role"] == "assistant"
        assert "Giá vàng hôm nay" in msg["content"]
        # Citations must be human-readable inside content for any client.
        assert "**Sources:**" in msg["content"]
        assert "vnexpress.net/gia-vang" in msg["content"]
        assert "usage" in body
        # Structured metadata for DuyAI-aware clients, ignored by stock ones.
        assert body["search_hub"]["confidence"] == 0.82

    def test_multi_turn_last_user_wins(self, monkeypatch, client):
        captured = _stub_pipeline(monkeypatch)
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [
                    {"role": "system", "content": "Bạn là trợ lý."},
                    {"role": "user", "content": "Giá vàng hôm nay?"},
                    {"role": "assistant", "content": "Khoảng 90 triệu."},
                    {"role": "user", "content": "Còn giá bạc?"},
                ],
            },
        )
        assert r.status_code == 200
        assert captured["query"] == "Còn giá bạc?"
        # All prior turns — system included — flow to the resolver as
        # conversation context. The system message is a context turn only;
        # the pipeline has no system-prompt channel for it to inject into.
        assert captured["history"] == [
            {"role": "system", "content": "Bạn là trợ lý."},
            {"role": "user", "content": "Giá vàng hôm nay?"},
            {"role": "assistant", "content": "Khoảng 90 triệu."},
        ]

    def test_invalid_model(self, client):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["code"] == "model_not_found"
        assert err["type"] == "invalid_request_error"
        assert err["param"] == "model"

    def test_empty_messages(self, client):
        r = client.post("/v1/chat/completions", json={"model": "duyai-search", "messages": []})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_no_user_message(self, client):
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "system", "content": "only system"}],
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "messages"

    def test_content_parts_flattened(self, monkeypatch, client):
        captured = _stub_pipeline(monkeypatch)
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Giá "},
                            {"type": "text", "text": "vàng hôm nay?"},
                        ],
                    }
                ],
            },
        )
        assert r.status_code == 200
        assert captured["query"] == "Giá vàng hôm nay?"

    def test_max_tokens_truncates(self, monkeypatch, client):
        _stub_pipeline(monkeypatch, result={**_FAKE_RESULT, "answer": "x" * 500})
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "max_tokens": 10,  # ~40 chars budget
            },
        )
        body = r.json()
        assert body["choices"][0]["finish_reason"] == "length"
        assert len(body["choices"][0]["message"]["content"]) < 500

    def test_max_completion_tokens_alias(self, monkeypatch, client):
        _stub_pipeline(monkeypatch, result={**_FAKE_RESULT, "answer": "x" * 500})
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "max_completion_tokens": 10,
            },
        )
        assert r.json()["choices"][0]["finish_reason"] == "length"

    def test_stop_sequence_truncates(self, monkeypatch, client):
        _stub_pipeline(
            monkeypatch,
            result={**_FAKE_RESULT, "answer": "phần một STOP phần hai"},
        )
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stop": "STOP",
            },
        )
        msg = r.json()["choices"][0]["message"]["content"]
        assert msg.startswith("phần một ")
        assert "phần hai" not in msg
        # The sources footer is appended after stop-truncation — citations
        # must survive a stop string that fired mid-answer.
        assert "**Sources:**" in msg

    def test_ignored_params_accepted(self, monkeypatch, client):
        # Documented-ignored params must not 400 — Open WebUI sends them all.
        _stub_pipeline(monkeypatch)
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "temperature": 0.3,
                "top_p": 0.9,
                "seed": 42,
                "presence_penalty": 0.1,
                "frequency_penalty": 0.2,
                "user": "alice",
                "stream_options": {"include_usage": True},
                # Open WebUI's default builtin-tools capability sends a
                # non-empty array on every request — tolerated, ignored.
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
                "tool_choice": "auto",
            },
        )
        assert r.status_code == 200

    def test_rejects_forced_tool_choice(self, client):
        # tools present is fine (ignored); a FORCED tool call is not — the
        # pipeline cannot emit tool_calls, so answering would pretend.
        for tc in ("required", {"type": "function", "function": {"name": "f"}}):
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "duyai-search",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
                    "tool_choice": tc,
                },
            )
            assert r.status_code == 400
            err = r.json()["error"]
            assert err["code"] == "unsupported_parameter"
            assert err["param"] == "tool_choice"

    def test_rejects_n_gt_1(self, client):
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "n": 2,
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unsupported_parameter"

    def test_rejects_json_response_format(self, client):
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "response_format": {"type": "json_object"},
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "response_format"

    def test_malformed_body_openai_shape(self, client):
        # Pydantic 422 must surface as an OpenAI error on this endpoint.
        r = client.post(
            "/v1/chat/completions",
            json={"model": "duyai-search", "messages": "not-an-array"},
        )
        assert r.status_code == 400
        body = r.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"
        assert "detail" not in body

    def test_auth_error_openai_shape(self, monkeypatch, client):
        # With auth on and no key, the compat surface must still answer in
        # OpenAI shape — not FastAPI's {"detail": ...}.
        monkeypatch.setattr(compat.settings, "api_auth_enabled", True)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "duyai-search", "messages": [{"role": "user", "content": "q"}]},
        )
        assert r.status_code == 401
        body = r.json()
        assert "error" in body
        assert body["error"]["type"] == "authentication_error"
        assert "detail" not in body
        assert r.headers.get("www-authenticate") == "Bearer"

    def test_native_surface_keeps_fastapi_shape(self, client):
        # The scoped validation handler must not leak onto native /v1.
        r = client.post("/v1/search", json={"query": 123})
        assert r.status_code == 422
        assert "detail" in r.json()

    def test_upstream_error_openai_shape(self, monkeypatch, client):
        async def boom(context, **kw):
            raise RuntimeError("synthesis blew up")

        monkeypatch.setattr(compat, "run_research", boom)
        monkeypatch.setattr(
            compat,
            "resolve_request_query",
            lambda query, **kw: SimpleNamespace(query=query, resolved=False),
        )
        r = client.post(
            "/v1/chat/completions",
            json={"model": "duyai-search", "messages": [{"role": "user", "content": "q"}]},
        )
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "server_error"

    def test_timeout_openai_shape(self, monkeypatch, client):
        async def slow(context, **kw):
            await asyncio.sleep(5)
            return _FAKE_RESULT

        monkeypatch.setattr(compat, "run_research", slow)
        monkeypatch.setattr(
            compat,
            "resolve_request_query",
            lambda query, **kw: SimpleNamespace(query=query, resolved=False),
        )
        monkeypatch.setattr(compat.settings, "openai_timeout_s", 0.01)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "duyai-search", "messages": [{"role": "user", "content": "q"}]},
        )
        assert r.status_code == 504
        assert r.json()["error"]["type"] == "server_error"


# ─── streaming ───────────────────────────────────────────────────────────────


class TestStreaming:
    def test_stream_chunks(self, monkeypatch, client):
        _stub_pipeline(monkeypatch, emit_deltas=["Giá vàng ", "hôm nay "])
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = "".join(r.iter_text())
        chunks = _sse_chunks(body)
        assert body.rstrip().endswith("data: [DONE]")
        # First chunk opens the assistant message.
        assert chunks[0]["object"] == "chat.completion.chunk"
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        # Content deltas carry the synthesis tokens.
        deltas = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[1:])
        assert "Giá vàng hôm nay" in deltas
        assert "**Sources:**" in deltas
        # Final chunk terminates with finish_reason.
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert chunks[-1]["choices"][0]["delta"] == {}

    def test_stream_without_deltas_still_sends_answer(self, monkeypatch, client):
        _stub_pipeline(monkeypatch, emit_deltas=[])
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
            },
        ) as r:
            body = "".join(r.iter_text())
        deltas = "".join(c["choices"][0]["delta"].get("content", "") for c in _sse_chunks(body)[1:])
        assert "Giá vàng hôm nay" in deltas
        assert "**Sources:**" in deltas

    def test_stream_error_shape(self, monkeypatch, client):
        async def boom(context, **kw):
            raise RuntimeError("pipeline failed")

        monkeypatch.setattr(compat, "run_research", boom)
        monkeypatch.setattr(
            compat,
            "resolve_request_query",
            lambda query, **kw: SimpleNamespace(query=query, resolved=False),
        )
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
            },
        ) as r:
            body = "".join(r.iter_text())
        chunks = _sse_chunks(body)
        assert any("error" in c for c in chunks)
        assert body.rstrip().endswith("data: [DONE]")

    def test_stream_max_tokens_parity(self, monkeypatch, client):
        # stream=true must honor the same content budget as non-stream.
        _stub_pipeline(monkeypatch, emit_deltas=["x" * 300])
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
                "max_tokens": 10,  # ~40 chars
            },
        ) as r:
            body = "".join(r.iter_text())
        chunks = _sse_chunks(body)
        assert body.rstrip().endswith("data: [DONE]")
        assert chunks[-1]["choices"][0]["finish_reason"] == "length"
        deltas = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[1:])
        assert len(deltas) <= 40

    def test_stream_stop_sequence(self, monkeypatch, client):
        _stub_pipeline(monkeypatch, emit_deltas=["phần một STOP phần hai"])
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
                "stop": ["STOP"],
            },
        ) as r:
            body = "".join(r.iter_text())
        chunks = _sse_chunks(body)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        deltas = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[1:])
        assert "phần hai" not in deltas

    def test_stream_outer_timeout(self, monkeypatch, client):
        async def slow(context, **kw):
            await asyncio.sleep(5)
            return _FAKE_RESULT

        monkeypatch.setattr(compat, "run_research", slow)
        monkeypatch.setattr(
            compat,
            "resolve_request_query",
            lambda query, **kw: SimpleNamespace(query=query, resolved=False),
        )
        monkeypatch.setattr(compat.settings, "openai_timeout_s", 0.05)
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "q"}],
                "stream": True,
            },
        ) as r:
            body = "".join(r.iter_text())
        chunks = _sse_chunks(body)
        assert any("error" in c and "timeout" in c["error"]["message"] for c in chunks)
        assert body.rstrip().endswith("data: [DONE]")
