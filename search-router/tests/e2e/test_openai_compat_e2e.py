"""P1.2 — Live compatibility gate for the OpenAI surface (E2E, real stack).

Unlike ``tests/test_openai_compat.py`` (hermetic; ``run_research`` stubbed)
this module drives the REAL pipeline over HTTP against the running compose
stack: a ``dsa_test_`` key with ``chat:use`` scope is created via
``manage_keys.py`` (same helpers as ``test_stack_e2e.py``), ``GET /v1/models``
and ``POST /v1/chat/completions`` are exercised non-stream + stream, and the
SSE stream must terminate with ``data: [DONE]``.

Auth-dependent assertions adapt to the stack's ``API_AUTH_ENABLED`` state:
a bogus-format key always returns 401 when auth is armed (OpenAI error
shape asserted); when auth is off the request passes through, and the
negative-scope check is skipped.

Skipped unless ``E2E=1`` — see ``scripts/e2e_run.sh``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("E2E") != "1",
        reason="E2E stack tests — set E2E=1 (or run scripts/e2e_run.sh)",
    ),
]

httpx = pytest.importorskip("httpx", reason="e2e requires httpx")

BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8888").rstrip("/")
TIMEOUT = 300.0  # research pipeline; degraded stacks can be slow


# ─── Key management (mirrors test_stack_e2e.py) ──────────────────────────────


def _pg_dsn() -> str:
    import manage_keys

    manage_keys._load_dotenv()
    return os.getenv("E2E_DSN") or manage_keys._resolve_dsn(None)


async def _create_key(*, scopes: list[str]) -> dict:
    asyncpg = pytest.importorskip("asyncpg", reason="e2e key mgmt needs asyncpg")
    import manage_keys

    conn = await asyncpg.connect(dsn=_pg_dsn(), timeout=10)
    try:
        return await manage_keys.create_key(
            conn,
            tenant_id="e2e",
            name="pytest-openai-compat",
            scopes=scopes,
            rpm=600,
            quota=-1,
            kind="test",
        )
    finally:
        await conn.close()


async def _revoke_key(key_id: str) -> None:
    asyncpg = pytest.importorskip("asyncpg", reason="e2e key mgmt needs asyncpg")
    import manage_keys

    conn = await asyncpg.connect(dsn=_pg_dsn(), timeout=10)
    try:
        await manage_keys.revoke_key(conn, key_id=key_id)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def chat_key() -> dict:
    """Session-scoped ``dsa_test_`` key holding only ``chat:use``."""
    created = asyncio.run(_create_key(scopes=["chat:use"]))
    yield created
    asyncio.run(_revoke_key(created["key_id"]))


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c


def _auth(key: dict) -> dict:
    return {"Authorization": f"Bearer {key['full_key']}"}


def _is_auth_on(client) -> bool:
    """Probe auth state: a bogus-format key 401s only when auth is armed."""
    r = client.get("/v1/models", headers={"Authorization": "Bearer bogus"})
    if r.status_code == 401:
        body = r.json()
        # OpenAI error shape on the compat surface — checked regardless of
        # whether we take the auth-on branch below.
        assert "error" in body, body
        assert body["error"]["type"] == "authentication_error"
        assert "detail" not in body
        return True
    assert r.status_code == 200
    return False


def _sse_frames(body: str) -> tuple[list[dict], bool]:
    """Parse SSE body → (data payloads, saw_done)."""
    payloads, done = [], False
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw == "[DONE]":
                done = True
            else:
                payloads.append(json.loads(raw))
    return payloads, done


# ─── live gate ───────────────────────────────────────────────────────────────


class TestModelsLive:
    def test_models_list(self, client, chat_key):
        r = client.get("/v1/models", headers=_auth(chat_key))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["object"] == "list"
        assert any(m["id"] == "duyai-search" for m in body["data"])

    def test_models_auth_shape(self, client, chat_key):
        if not _is_auth_on(client):
            pytest.skip("stack runs API_AUTH_ENABLED=false — no auth gate")
        # chat:use key works; a key WITHOUT the scope is rejected in
        # OpenAI shape.
        created = asyncio.run(_create_key(scopes=["search:read"]))
        try:
            r = client.get("/v1/models", headers=_auth(created))
            assert r.status_code == 403
            assert "error" in r.json()
            assert "detail" not in r.json()
        finally:
            asyncio.run(_revoke_key(created["key_id"]))


class TestChatCompletionsLive:
    def test_non_stream_real_pipeline(self, client, chat_key):
        r = client.post(
            "/v1/chat/completions",
            headers=_auth(chat_key),
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "Thủ đô của Việt Nam là gì?"}],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        choice = body["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert choice["message"]["content"].strip()
        assert choice["finish_reason"] in ("stop", "length")
        # DuyAI extension block is always present; citations are present
        # (possibly empty on a degraded stack — the contract is the key,
        # not external source availability).
        assert "search_hub" in body
        assert isinstance(body["search_hub"].get("citations"), list)
        sources = body["search_hub"].get("citations") or []
        if sources:
            assert "**Sources:**" in choice["message"]["content"]

    def test_stream_real_pipeline(self, client, chat_key):
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=_auth(chat_key),
            json={
                "model": "duyai-search",
                "messages": [{"role": "user", "content": "Thủ đô của Việt Nam là gì?"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200, r.read().decode()
            assert r.headers["content-type"].startswith("text/event-stream")
            body = "".join(r.iter_text())
        frames, done = _sse_frames(body)
        assert done, "stream must terminate with data: [DONE]"
        chunks = [f for f in frames if f.get("object") == "chat.completion.chunk"]
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert any(c["choices"][0].get("finish_reason") for c in chunks)
        errors = [f for f in frames if "error" in f]
        assert not errors, errors

    def test_invalid_model_live(self, client, chat_key):
        r = client.post(
            "/v1/chat/completions",
            headers=_auth(chat_key),
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["code"] == "model_not_found"
        assert err["type"] == "invalid_request_error"


# ─── official SDK smoke ──────────────────────────────────────────────────────


class TestOpenAISDK:
    """Official `openai` Python client against the live surface.

    Skips when the SDK isn't installed (it is a client-side dep, not a
    router dep) — the httpx tests above always cover the wire contract.
    """

    def test_sdk_stream_and_non_stream(self, client, chat_key):
        openai = pytest.importorskip("openai", reason="pip install openai for SDK smoke")
        sdk = openai.OpenAI(
            base_url=f"{BASE_URL}/v1",
            api_key=chat_key["full_key"],
            timeout=TIMEOUT,
            max_retries=0,
        )
        models = sdk.models.list()
        assert any(m.id == "duyai-search" for m in models.data)

        resp = sdk.chat.completions.create(
            model="duyai-search",
            messages=[{"role": "user", "content": "Thủ đô của Việt Nam là gì?"}],
        )
        assert resp.choices[0].message.content.strip()
        assert resp.choices[0].finish_reason in ("stop", "length")

        chunks = list(
            sdk.chat.completions.create(
                model="duyai-search",
                messages=[{"role": "user", "content": "Thủ đô của Việt Nam là gì?"}],
                stream=True,
            )
        )
        assert chunks[0].choices[0].delta.role == "assistant"
        text = "".join(c.choices[0].delta.content or "" for c in chunks)
        assert text.strip()
        assert chunks[-1].choices[0].finish_reason in ("stop", "length")
