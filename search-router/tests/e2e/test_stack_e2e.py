"""P11-T5 — Full-stack E2E against the live local Search-Hub deployment.

Runs REAL requests — no mocks — against:

* search-router   ``E2E_BASE_URL`` (default http://127.0.0.1:8888)
* MCP server      ``E2E_MCP_URL``  (default http://127.0.0.1:8901/mcp)
* hub-postgres    ``E2E_DSN``      (default: manage_keys.py resolution)

Every test carries ``@pytest.mark.e2e`` and the whole module is skipped
unless ``E2E=1`` — CI/unit runs must not need the Docker stack. The
canonical runner is ``scripts/e2e_run.sh`` (creates + revokes a test key).

Degraded mode: when the LLM gateway is unreachable the research pipeline
falls back to extractive/raw-results answers after several ``llm_timeout``
waits (~3 min each, so a single /v1/answer can take ~12 min). In that state
the contract under test is sources + coverage + query_id, not prose quality.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta

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
MCP_URL = os.getenv("E2E_MCP_URL", "http://127.0.0.1:8901/mcp")

# Captured at collection time — strictly before any test traffic — so the
# metering test can slice query_logs on "rows written during this run".
_TEST_START = datetime.now(UTC) - timedelta(seconds=10)

# Research-mode calls traverse plan/search/scrape/synthesize/verify; each LLM
# touchpoint can burn a full llm_timeout (180s) when the gateway is down, so
# worst case is ~13 min. Healthy path is ~1-2 min.
TIMEOUT_FAST = 60.0
TIMEOUT_SEARCH = 120.0
TIMEOUT_RESEARCH = 900.0
TIMEOUT_SSE = 900.0


# ─── Hub-postgres helpers (shared with manage_keys.py) ───────────────────────


def _pg_dsn() -> str:
    """Resolve the hub-postgres DSN exactly like manage_keys.py does."""
    import manage_keys

    manage_keys._load_dotenv()
    return os.getenv("E2E_DSN") or manage_keys._resolve_dsn(None)


async def _pg_fetch(sql: str, *args):
    asyncpg = pytest.importorskip("asyncpg", reason="e2e metering needs asyncpg")

    conn = await asyncpg.connect(dsn=_pg_dsn(), timeout=10)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _create_key(*, scopes: list[str], rpm: int, quota: int) -> dict:
    asyncpg = pytest.importorskip("asyncpg", reason="e2e key mgmt needs asyncpg")
    import manage_keys

    conn = await asyncpg.connect(dsn=_pg_dsn(), timeout=10)
    try:
        return await manage_keys.create_key(
            conn,
            tenant_id="e2e",
            name="pytest-e2e",
            scopes=scopes,
            rpm=rpm,
            quota=quota,
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


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def api_key() -> dict:
    """Session API key: env ``E2E_API_KEY`` wins, else create + revoke a
    wildcard ``dsa_test_`` key via manage_keys.py against hub-postgres."""
    env_key = os.getenv("E2E_API_KEY")
    if env_key:
        yield {"full_key": env_key, "key_id": os.getenv("E2E_KEY_ID", "")}
        return
    created = asyncio.run(_create_key(scopes=["*"], rpm=600, quota=-1))
    yield created
    asyncio.run(_revoke_key(created["key_id"]))


@pytest.fixture(scope="session")
def auth(api_key) -> dict:
    return {"Authorization": f"Bearer {api_key['full_key']}"}


@pytest.fixture(scope="session")
def client(api_key):
    # Constructing the client triggers the api_key fixture first, so a
    # completely dead stack fails fast on key creation rather than timeouts.
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_FAST) as c:
        yield c


# ─── SSE parsing ──────────────────────────────────────────────────────────────


def _iter_sse(lines):
    """Yield (event, data_dict) pairs from a ``text/event-stream`` line iter."""
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                raw = "\n".join(data)
                try:
                    yield event, json.loads(raw)
                except json.JSONDecodeError:
                    yield event, {"_raw": raw}
            event, data = "message", []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        raw = "\n".join(data)
        try:
            yield event, json.loads(raw)
        except json.JSONDecodeError:
            yield event, {"_raw": raw}


def _mcp_post(client: httpx.Client, payload: dict, session_id: str | None = None):
    """One JSON-RPC call to the streamable-http MCP endpoint.

    Returns (httpx.Response, decoded_json_or_None). Session id comes back on
    the ``Mcp-Session-Id`` response header of ``initialize``.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    resp = client.post(MCP_URL, json=payload, headers=headers, timeout=120.0)
    body = None
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for _ev, data in _iter_sse(resp.iter_lines()):
            if isinstance(data, dict) and data.get("jsonrpc"):
                body = data
    elif "json" in ctype:
        body = resp.json()
    return resp, body


# ─── Health & capabilities ───────────────────────────────────────────────────


class TestHealth:
    def test_health_public_shape(self, client):
        resp = client.get("/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert data["version"]

    def test_health_services_ok(self, client, auth):
        resp = client.get("/v1/health", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        # Detailed shape requires an admin-capable key (scopes=*).
        services = data.get("services")
        assert services, f"expected detailed health with services, got {data}"
        assert data["status"] == "ok", f"stack degraded: {services}"
        for name, state in services.items():
            assert state == "ok", f"service {name} is {state}"

    def test_capabilities(self, client):
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        modes = {m if isinstance(m, str) else m.get("value", m) for m in data["modes"]}
        assert modes & {"fast", "normal", "deep"}
        assert data["features"]["auth"]["enabled"] is True
        assert data["features"]["stream"] is True


# ─── Auth ─────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_no_key_401(self, client):
        resp = client.post("/v1/search", json={"query": "pytest"})
        assert resp.status_code == 401

    def test_bad_key_401(self, client):
        resp = client.post(
            "/v1/search",
            json={"query": "pytest"},
            headers={"Authorization": "Bearer dsa_live_0000000000000000000000000000000x"},
        )
        assert resp.status_code == 401

    def test_good_key_200(self, client, auth):
        resp = client.post(
            "/v1/search",
            json={"query": "pytest", "max_results": 3},
            headers=auth,
            timeout=TIMEOUT_SEARCH,
        )
        assert resp.status_code == 200


# ─── Search / hybrid / answer ────────────────────────────────────────────────


class TestSearch:
    def test_web_search_results(self, client, auth):
        resp = client.post(
            "/v1/search",
            json={"query": "python fastapi tutorial", "type": "web", "max_results": 5},
            headers=auth,
            timeout=TIMEOUT_SEARCH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert data["results"], "expected non-empty results"
        for r in data["results"]:
            assert r.get("url"), f"result missing url: {r}"
            assert r.get("title"), f"result missing title: {r}"

    def test_hybrid_mode_fast(self, client, auth):
        resp = client.post(
            "/v1/search",
            json={"query": "python fastapi tutorial", "mode": "fast", "max_results": 5},
            headers=auth,
            timeout=TIMEOUT_RESEARCH,
        )
        assert resp.status_code == 200
        data = resp.json()
        hybrid = (data.get("timings") or {}).get("hybrid")
        assert hybrid is not None, "timings.hybrid missing — hybrid lane did not run"
        for key in ("os_hits", "qdrant_hits", "fused", "degraded"):
            assert key in hybrid, f"timings.hybrid missing {key}: {hybrid}"


class TestAnswer:
    def test_answer_contract(self, client, auth):
        resp = client.post(
            "/v1/answer",
            json={"query": "what is pytest", "mode": "fast"},
            headers=auth,
            timeout=TIMEOUT_RESEARCH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert re.fullmatch(r"srch_[0-9a-f]{12}", data["query_id"])
        assert isinstance(data["sources"], list) and len(data["sources"]) > 0
        assert isinstance(data.get("coverage"), int | float)

        answer = (data.get("answer") or "").strip()
        if not answer:
            pytest.fail(
                "answer empty despite sources present — degraded LLM should "
                "still produce the extractive/raw fallback text"
            )

    def test_answer_sse_stream(self, client, auth):
        events: list[tuple[str, dict]] = []
        with client.stream(
            "POST",
            "/v1/answer",
            json={"query": "what is pytest", "mode": "fast", "stream": True},
            headers=auth,
            timeout=TIMEOUT_SSE,
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            for item in _iter_sse(resp.iter_lines()):
                events.append(item)

        names = [e for e, _ in events]
        assert names, "no SSE events received"
        assert names[0] == "init", f"first event must be init, got {names[0]}"
        assert names[-1] == "done", f"last event must be done, got {names[-1]}"

        done = events[-1][1]
        assert re.fullmatch(r"srch_[0-9a-f]{12}", done.get("query_id", ""))

        if "warning" in names:
            # Pipeline itself failed mid-stream — warning + done, no sources.
            pytest.fail(f"stream emitted warning (pipeline error): {events}")

        assert names.count("source") >= 1, f"no source events in {names}"
        assert names.count("answer.delta") >= 1, f"no answer deltas in {names}"


# ─── Read / news ──────────────────────────────────────────────────────────────


class TestReadAndNews:
    def test_read_url(self, client, auth):
        resp = client.post(
            "/v1/read",
            json={"url": "https://example.com"},
            headers=auth,
            timeout=TIMEOUT_SEARCH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("error") in (None, ""), f"read error: {data.get('error')}"
        passages = data.get("passages") or []
        assert passages, "expected non-empty passages"
        assert any((p.get("text") or "").strip() for p in passages)

    def test_news(self, client, auth):
        resp = client.post(
            "/v1/news",
            json={"query": "AI", "max_results": 5},
            headers=auth,
            timeout=TIMEOUT_SEARCH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("results"), list)
        assert data.get("count") == len(data["results"])


# ─── Rate limiting ────────────────────────────────────────────────────────────


class TestRateLimit:
    def test_rpm_one_key(self, client):
        """A key with rpm=1 accepts one request then rejects the next."""
        key = asyncio.run(_create_key(scopes=["search:read"], rpm=1, quota=-1))
        try:
            headers = {"Authorization": f"Bearer {key['full_key']}"}
            body = {"query": "rate limit probe", "max_results": 1}
            r1 = client.post("/v1/search", json=body, headers=headers, timeout=TIMEOUT_SEARCH)
            r2 = client.post("/v1/search", json=body, headers=headers, timeout=TIMEOUT_SEARCH)
            assert r1.status_code == 200, f"first call should pass, got {r1.status_code}"
            assert r2.status_code in (403, 429), (
                f"second call inside the window must be rejected, got {r2.status_code}"
            )
        finally:
            asyncio.run(_revoke_key(key["key_id"]))


# ─── Metering ────────────────────────────────────────────────────────────────


class TestMetering:
    def test_query_logs_written(self, api_key):
        """Every authed /v1 call lands in query_logs (fire-and-forget — poll)."""
        key_id = api_key["key_id"]
        assert key_id, "metering assertion needs a key_id (env key lacks one)"
        deadline = time.monotonic() + 45
        endpoints: set[str] = set()
        rows = []
        while time.monotonic() < deadline:
            rows = asyncio.run(
                _pg_fetch(
                    "SELECT endpoint, COUNT(*) AS n FROM query_logs "
                    "WHERE ts > $1 AND key_id = $2 GROUP BY endpoint",
                    _TEST_START,
                    key_id,
                )
            )
            endpoints = {r["endpoint"] for r in rows}
            if {"search", "answer"} <= endpoints:
                break
            time.sleep(2)
        assert {"search", "answer"} <= endpoints, (
            f"query_logs missing search/answer rows for key {key_id}: {rows}"
        )
        for r in rows:
            assert int(r["n"]) >= 1


# ─── MCP server (:8901) ──────────────────────────────────────────────────────


class TestMcp:
    def test_initialize_list_call(self, client):
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0.1"},
            },
        }
        resp, body = _mcp_post(client, init)
        assert resp.status_code == 200, f"initialize → {resp.status_code}: {resp.text[:300]}"
        session_id = resp.headers.get("mcp-session-id")
        assert session_id, "initialize must return Mcp-Session-Id header"
        assert body and body.get("result", {}).get("serverInfo"), f"bad init body: {body}"

        # Required notification handshake before further calls.
        resp, _ = _mcp_post(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
        assert resp.status_code in (200, 202, 204)

        resp, body = _mcp_post(
            client,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        assert resp.status_code == 200
        tools = {t["name"] for t in body["result"]["tools"]}
        # Default surface is exactly the 3-tool retrieval lane (P1.2):
        # answer is never registered; fetch/research only appear when the
        # stack runs with MCP_ENABLE_DEEP_RESEARCH=true (unit-tested).
        assert tools == {"search", "fetch_evidence", "code_search"}

        resp, body = _mcp_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"query": "pytest", "max_results": 3},
                },
            },
            session_id=session_id,
        )
        assert resp.status_code == 200
        result = body.get("result") or {}
        content = result.get("content") or []
        assert content, f"tools/call returned no content: {body}"
        text = content[0].get("text", "")
        payload = json.loads(text)
        assert payload.get("results"), "MCP search tool returned no results"
