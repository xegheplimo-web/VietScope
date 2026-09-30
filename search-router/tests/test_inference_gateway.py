"""Tests for the unified inference gateway (P0).

Covers the DoD contract: model-role routing, circuit breaker, retries,
``complete_json`` schema validation, the real async-iterator ``stream``,
metrics, and the never-raises fallback contract.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import config
import httpx
import pytest
from core.inference_gateway import (
    InferenceGateway,
    ModelRole,
    _safe_parse_json,
    get_inference_gateway,
)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.setattr(config.settings, "llm_planner_model", "")
    monkeypatch.setattr(config.settings, "llm_synth_model", "")
    monkeypatch.setattr(config.settings, "llm_verify_model", "")
    monkeypatch.setattr(config.settings, "llm_extract_model", "")
    for role in ("planner", "synth", "verify", "extract"):
        monkeypatch.setattr(config.settings, f"llm_{role}_base_url", "")
        monkeypatch.setattr(config.settings, f"llm_{role}_api_key", "")
        monkeypatch.setattr(config.settings, f"llm_{role}_fallback", "")


def _fake_client(responses, *, probe_status=200):
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.post = AsyncMock(side_effect=responses)
    instance.get = AsyncMock(return_value=_mock_response(status=probe_status))
    return instance


def _mock_response(status=200, content=None):
    resp = AsyncMock()
    resp.status_code = status
    resp.json = Mock(return_value=content)
    return resp


def _ok(content="hello"):
    return _mock_response(content={"choices": [{"message": {"content": content}}]})


def _transport_error():
    return httpx.ConnectError(
        "boom", request=httpx.Request("POST", "http://llm.local/v1/chat/completions")
    )


def _timeout_error():
    return httpx.ReadTimeout(
        "boom", request=httpx.Request("POST", "http://llm.local/v1/chat/completions")
    )


def _dead_client(responses):
    """Client whose liveness probe also fails — the endpoint is unreachable."""
    instance = _fake_client(responses)
    instance.get = AsyncMock(
        side_effect=httpx.ConnectError(
            "dead", request=httpx.Request("GET", "http://llm.local/v1/models")
        )
    )
    return instance


# ─── model roles ─────────────────────────────────────────────────────────────


def test_model_role_routing(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_planner_model", "planner-model")
    monkeypatch.setattr(config.settings, "llm_verify_model", "verify-model")

    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([_ok(), _ok()])):
            gw = InferenceGateway()
            gw.api_key = "k"
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.VERIFIER)
            return gw

    gw = asyncio.run(_run())
    posts = gw._http.post.call_args_list
    assert posts[0].kwargs["json"]["model"] == "planner-model"
    assert posts[1].kwargs["json"]["model"] == "verify-model"


def test_model_role_falls_back_to_default():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([_ok()])):
            gw = InferenceGateway()
            gw.api_key = "k"
            gw.model = "base-model"
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            return gw

    gw = asyncio.run(_run())
    assert gw._http.post.call_args_list[0].kwargs["json"]["model"] == "base-model"


# ─── circuit breaker ─────────────────────────────────────────────────────────


def test_circuit_breaker_opens_and_fails_fast():
    err = _transport_error()

    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([err, err, err])):
            gw = InferenceGateway(max_attempts=1, breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            for _ in range(3):
                await gw.complete([{"role": "user", "content": "x"}])
            # Breaker now open — next call must not hit the network.
            result = await gw.complete([{"role": "user", "content": "x"}])
            return gw, result

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._breaker.open
    assert gw._http.post.call_count == 3  # 4th call rejected without HTTP
    assert gw.metrics["circuit_rejects"] == 1
    assert gw.metrics["circuit_opens"] == 1


def test_dead_endpoint_trips_on_first_timeout():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_dead_client([_timeout_error()])):
            gw = InferenceGateway(breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._breaker.open  # one dead call + one dead probe — no threshold wait
    assert gw._http.post.call_count == 1  # timeout is not retried
    assert gw.metrics["endpoint_probes"] == 1
    assert gw.metrics["circuit_rejects"] == 1


def test_dead_endpoint_trips_on_first_transport_error():
    err = _transport_error()

    async def _run():
        with patch("httpx.AsyncClient", return_value=_dead_client([err])):
            gw = InferenceGateway(breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            return gw

    gw = asyncio.run(_run())
    assert gw._breaker.open
    assert gw._http.post.call_count == 1


def test_timeout_no_retry_when_endpoint_alive():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([_timeout_error()])):
            gw = InferenceGateway(breaker_threshold=3)
            gw.api_key = "k"
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._http.post.call_count == 1  # timed-out calls are never retried
    assert not gw._breaker.open  # alive probe → single transient failure
    assert gw.metrics["failures"] == 1


def test_permanent_status_trips_breaker_immediately():
    async def _run():
        with patch(
            "httpx.AsyncClient",
            return_value=_fake_client([_mock_response(status=401)]),
        ):
            gw = InferenceGateway(breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._breaker.open  # auth failures are permanent — trip at once
    assert gw._http.post.call_count == 1
    assert gw.metrics["circuit_rejects"] == 1


def test_404_counts_as_failure_not_instant_trip():
    """One misconfigured role model must not open the global breaker."""

    async def _run():
        with patch(
            "httpx.AsyncClient",
            return_value=_fake_client([_mock_response(status=404)] * 4, probe_status=200),
        ):
            gw = InferenceGateway(max_attempts=1, breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            assert not gw._breaker.open  # 404 is per-role — normal failure path
            await gw.complete([{"role": "user", "content": "x"}])
            assert not gw._breaker.open
            await gw.complete([{"role": "user", "content": "x"}])
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._breaker.open  # opens only at the normal threshold
    assert gw._http.post.call_count == 3
    assert gw.metrics["circuit_rejects"] == 1


def test_connect_timeout_retries_when_endpoint_alive():
    """Connect/write/pool timeouts never spent the read budget — retry them."""

    def _connect_timeout():
        return httpx.ConnectTimeout(
            "slow", request=httpx.Request("POST", "http://llm.local/v1/chat/completions")
        )

    async def _run():
        with patch(
            "httpx.AsyncClient",
            return_value=_fake_client([_connect_timeout(), _ok()]),
        ):
            gw = InferenceGateway(max_attempts=2, breaker_threshold=3)
            gw.api_key = "k"
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is not None
    assert gw._http.post.call_count == 2  # retried after the connect timeout
    assert not gw._breaker.open
    assert gw.metrics["retries"] == 1


def test_connect_timeout_trips_when_endpoint_dead():
    def _connect_timeout():
        return httpx.ConnectTimeout(
            "slow", request=httpx.Request("POST", "http://llm.local/v1/chat/completions")
        )

    async def _run():
        with patch("httpx.AsyncClient", return_value=_dead_client([_connect_timeout()])):
            gw = InferenceGateway(breaker_threshold=3, breaker_cooldown=9999)
            gw.api_key = "k"
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            return gw

    gw = asyncio.run(_run())
    assert gw._breaker.open  # dead probe → same immediate trip as read timeouts
    assert gw._http.post.call_count == 1


def test_circuit_breaker_half_open_recovers():
    err = _transport_error()

    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([err, err, _ok()])):
            gw = InferenceGateway(max_attempts=1, breaker_threshold=2, breaker_cooldown=0)
            gw.api_key = "k"
            await gw.complete([{"role": "user", "content": "x"}])
            await gw.complete([{"role": "user", "content": "x"}])
            assert gw._breaker.open
            # cooldown=0 → half-open probe allowed immediately
            result = await gw.complete([{"role": "user", "content": "x"}])
            return gw, result

    gw, result = asyncio.run(_run())
    assert result == "hello"
    assert not gw._breaker.open  # success reset the breaker


# ─── retries ─────────────────────────────────────────────────────────────────


def test_retry_on_retryable_status():
    async def _run():
        with patch(
            "httpx.AsyncClient",
            return_value=_fake_client([_mock_response(status=503), _ok("done")]),
        ):
            gw = InferenceGateway()
            gw.api_key = "k"
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result == "done"
    assert gw.metrics["retries"] == 1
    assert gw._http.post.call_count == 2


def test_no_retry_on_client_error():
    async def _run():
        with patch(
            "httpx.AsyncClient",
            return_value=_fake_client([_mock_response(status=401)]),
        ):
            gw = InferenceGateway()
            gw.api_key = "k"
            return gw, await gw.complete([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result is None
    assert gw._http.post.call_count == 1  # 4xx is not retried


# ─── complete_json ───────────────────────────────────────────────────────────


def test_complete_json_required_keys():
    async def _run():
        missing = _mock_response(content={"choices": [{"message": {"content": '{"a": 1}'}}]})
        present = _mock_response(
            content={"choices": [{"message": {"content": '{"a": 1, "b": 2}'}}]}
        )
        with patch("httpx.AsyncClient", return_value=_fake_client([missing, present])):
            gw = InferenceGateway()
            gw.api_key = "k"
            bad = await gw.complete_json(
                [{"role": "user", "content": "x"}], required_keys=["a", "b"]
            )
            good = await gw.complete_json(
                [{"role": "user", "content": "x"}], required_keys=["a", "b"]
            )
            return bad, good

    bad, good = asyncio.run(_run())
    assert bad is None
    assert good == {"a": 1, "b": 2}


def test_complete_json_expect_list():
    async def _run():
        resp = _mock_response(content={"choices": [{"message": {"content": '["Q1", "Q2"]'}}]})
        with patch("httpx.AsyncClient", return_value=_fake_client([resp, resp])):
            gw = InferenceGateway()
            gw.api_key = "k"
            as_list = await gw.complete_json([{"role": "user", "content": "x"}], expect=list)
            as_dict = await gw.complete_json([{"role": "user", "content": "x"}], expect=dict)
            return as_list, as_dict

    as_list, as_dict = asyncio.run(_run())
    assert as_list == ["Q1", "Q2"]
    assert as_dict is None  # isinstance guard rejects the array


def test_complete_json_400_strips_response_format():
    async def _run():
        bad = _mock_response(status=400, content={"error": "json mode unsupported"})
        good = _mock_response(content={"choices": [{"message": {"content": '{"ok": true}'}}]})
        with patch("httpx.AsyncClient", return_value=_fake_client([bad, good])):
            gw = InferenceGateway()
            gw.api_key = "k"
            return gw, await gw.complete_json([{"role": "user", "content": "x"}])

    gw, result = asyncio.run(_run())
    assert result == {"ok": True}
    second_body = gw._http.post.call_args_list[1].kwargs["json"]
    assert "response_format" not in second_body


# ─── stream ──────────────────────────────────────────────────────────────────


class _FakeStreamCM:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def aiter_lines(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


def _sse_lines(*texts):
    lines = ["data: " + json.dumps({"choices": [{"delta": {"content": t}}]}) for t in texts]
    lines.append("data: [DONE]")
    return lines


def test_stream_yields_deltas():
    async def _run():
        client = MagicMock()
        client.is_closed = False
        client.stream = Mock(return_value=_FakeStreamCM(_sse_lines("Hello", " world", "!")))
        with patch("httpx.AsyncClient", return_value=client):
            gw = InferenceGateway()
            gw.api_key = "k"
            chunks = [c async for c in gw.stream([{"role": "user", "content": "hi"}])]
            return gw, chunks

    gw, chunks = asyncio.run(_run())
    assert chunks == ["Hello", " world", "!"]
    body = gw._http.stream.call_args.kwargs["json"]
    assert body["stream"] is True


def test_stream_ends_on_http_error():
    async def _run():
        client = MagicMock()
        client.is_closed = False
        client.stream = Mock(return_value=_FakeStreamCM([], status=500))
        with patch("httpx.AsyncClient", return_value=client):
            gw = InferenceGateway()
            gw.api_key = "k"
            return gw, [c async for c in gw.stream([{"role": "user", "content": "hi"}])]

    gw, chunks = asyncio.run(_run())
    assert chunks == []
    assert gw.metrics["failures"] == 1


def test_stream_no_key_yields_nothing():
    async def _run():
        gw = InferenceGateway()
        return [c async for c in gw.stream([{"role": "user", "content": "hi"}])]

    assert asyncio.run(_run()) == []


# ─── fallback contract + metrics ─────────────────────────────────────────────


def test_no_api_key_returns_none_without_http():
    async def _run():
        with patch("httpx.AsyncClient") as ctor:
            gw = InferenceGateway()
            assert await gw.complete([{"role": "user", "content": "x"}]) is None
            assert await gw.complete_json([{"role": "user", "content": "x"}]) is None
            assert await gw.embed(["x"]) is None
            return ctor

    ctor = asyncio.run(_run())
    ctor.assert_not_called()  # fail fast — no client even created


def test_metrics_track_calls_and_roles():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([_ok(), _ok()])):
            gw = InferenceGateway()
            gw.api_key = "k"
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.SYNTHESIZER)
            return gw

    gw = asyncio.run(_run())
    m = gw.metrics
    assert m["calls"] == 2
    assert m["by_role"] == {"planner": 1, "synthesizer": 1}
    assert m["failures"] == 0
    assert m["circuit_open"] is False


def test_shared_singleton():
    import core.inference_gateway as mod

    mod._gateway = None
    a = get_inference_gateway()
    b = get_inference_gateway()
    assert a is b


def test_safe_parse_json_arrays():
    assert _safe_parse_json('["a", "b"]') == ["a", "b"]
    assert _safe_parse_json('```json\n["x"]\n```') == ["x"]
    assert _safe_parse_json('{"a": 1}') == {"a": 1}
    assert _safe_parse_json("plain text") is None


# ─── A9: per-role backend routing ────────────────────────────────────────────


def _routing_client(post_map=None, *, probe_status=200, probe_error_for=None):
    """Fake client routing by request URL — ``post_map`` maps a URL substring
    to a response or exception; ``probe_error_for`` fails GET /models for a
    URL substring (dead endpoint)."""

    def _pick(url):
        for needle, outcome in (post_map or {}).items():
            if needle in url:
                return outcome
        return _ok()

    async def _post(url, **kwargs):
        outcome = _pick(url)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _get(url, **kwargs):
        if probe_error_for and probe_error_for in url:
            raise httpx.ConnectError("dead", request=httpx.Request("GET", url))
        return _mock_response(status=probe_status)

    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.post = AsyncMock(side_effect=_post)
    instance.get = AsyncMock(side_effect=_get)

    # client.stream(...) returns an async-context-manager wrapper
    def _stream(method, url, **kwargs):
        outcome = _pick(url)
        if isinstance(outcome, BaseException):
            raise outcome
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=outcome)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    instance.stream = Mock(side_effect=_stream)
    return instance


def test_role_backend_override_routes_to_own_endpoint(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_extract_base_url", "http://localai:8080/v1")
    monkeypatch.setattr(config.settings, "llm_extract_api_key", "local")
    monkeypatch.setattr(config.settings, "llm_extract_model", "qwen-small")

    async def _run():
        with patch("httpx.AsyncClient", return_value=_routing_client()):
            gw = InferenceGateway()
            gw.api_key = "k"
            out = await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.EXTRACTOR)
            return gw, out

    gw, out = asyncio.run(_run())
    assert out == "hello"
    lane = gw._lanes["http://localai:8080/v1"]
    call = lane.client.post.call_args_list[0]
    assert call.args[0] == "http://localai:8080/v1/chat/completions"
    assert call.kwargs["headers"]["Authorization"] == "Bearer local"
    assert call.kwargs["json"]["model"] == "qwen-small"
    assert gw.metrics["backends"]["http://localai:8080/v1"]["open"] is False


def test_default_backend_unaffected_by_override_outage(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_extract_base_url", "http://localai:8080/v1")
    monkeypatch.setattr(config.settings, "llm_extract_api_key", "local")

    async def _run():
        client = _routing_client({"localai": _transport_error()}, probe_error_for="localai")
        with patch("httpx.AsyncClient", return_value=client):
            gw = InferenceGateway()
            gw.api_key = "k"
            dead = await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.EXTRACTOR)
            alive = await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            return gw, dead, alive

    gw, dead, alive = asyncio.run(_run())
    assert dead is None
    assert alive == "hello"
    # override lane tripped; default breaker stayed closed
    assert gw._lanes["http://localai:8080/v1"].breaker.open
    assert not gw._breaker.open
    assert gw.metrics["backends"]["http://localai:8080/v1"]["open"] is True


def test_role_fallback_retries_on_default_backend(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_synth_base_url", "http://localai:8080/v1")
    monkeypatch.setattr(config.settings, "llm_synth_api_key", "local")
    monkeypatch.setattr(config.settings, "llm_synth_model", "qwen-big")
    monkeypatch.setattr(config.settings, "llm_synth_fallback", "default")
    monkeypatch.setattr(config.settings, "llm_model", "gpt-4o-mini")

    async def _run():
        client = _routing_client({"localai": _transport_error()}, probe_error_for="localai")
        with patch("httpx.AsyncClient", return_value=client):
            gw = InferenceGateway()
            gw.api_key = "k"
            out = await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.SYNTHESIZER)
            return gw, out

    gw, out = asyncio.run(_run())
    assert out == "hello"
    assert gw.metrics["fallbacks"] == 1
    assert gw.metrics["roles"]["synthesizer"]["fallbacks"] == 1
    # fallback leg used the global default model, not the override model
    default_posts = [c for c in gw._http.post.call_args_list if "localai" not in c.args[0]]
    assert default_posts[-1].kwargs["json"]["model"] == "gpt-4o-mini"


def test_probe_classifies_endpoint(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_extract_base_url", "http://localai:8080/v1")
    monkeypatch.setattr(config.settings, "llm_extract_api_key", "local")
    monkeypatch.setattr(config.settings, "llm_extract_model", "qwen-small")

    async def _probe_with(client):
        # fresh gateway per scenario — a lane caches its client, so reusing
        # one gateway would keep the first (dead) mock for every probe
        with patch("httpx.AsyncClient", return_value=client):
            gw = InferenceGateway()
            gw.api_key = "k"
            return gw, await gw.probe(ModelRole.EXTRACTOR)

    async def _run():
        gw, dead = await _probe_with(_routing_client({}, probe_error_for="localai"))
        _, auth = await _probe_with(_routing_client({}, probe_status=401))
        _, missing = await _probe_with(_routing_client({"chat/completions": _mock_response(404)}))
        _, ok = await _probe_with(_routing_client())
        return gw, dead, auth, missing, ok

    gw, dead, auth, missing, ok = asyncio.run(_run())
    assert dead["state"] == "unreachable"
    assert auth["state"] == "auth_failed"
    assert missing["state"] == "model_missing"
    assert ok["state"] == "healthy"
    assert ok["backend"] == "http://localai:8080/v1"
    # probes never trip a breaker
    assert not gw._breaker.open


def test_role_metrics_expose_backend_and_latency():
    async def _run():
        with patch("httpx.AsyncClient", return_value=_fake_client([_ok()])):
            gw = InferenceGateway()
            gw.api_key = "k"
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            return gw

    gw = asyncio.run(_run())
    role = gw.metrics["roles"]["planner"]
    assert role["calls"] == 1
    assert role["backend"] == config.settings.llm_base_url.rstrip("/")
    assert role["model"] == config.settings.llm_model
    assert role["override"] is False
    assert role["state"] == "healthy"
    assert role["p50_ms"] is not None


def test_success_clears_role_error_state():
    """A transient failure must not pin a role 'degraded' for the process
    lifetime: success clears current-error state while the cumulative
    failure counter keeps its history."""

    async def _run():
        state = {"fail": True}
        instance = _fake_client([_ok()])

        async def _post(url, **kwargs):
            return _mock_response(500) if state["fail"] else _ok()

        instance.post = AsyncMock(side_effect=_post)
        with patch("httpx.AsyncClient", return_value=instance):
            gw = InferenceGateway(max_attempts=1)
            gw.api_key = "k"
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            degraded = gw.metrics["roles"]["planner"]["state"]
            state["fail"] = False
            await gw.complete([{"role": "user", "content": "x"}], role=ModelRole.PLANNER)
            return gw, degraded

    gw, degraded = asyncio.run(_run())
    role = gw.metrics["roles"]["planner"]
    assert degraded == "degraded"
    assert role["state"] == "healthy"
    assert role["last_error_kind"] is None
    assert role["failures"] == 1  # cumulative history preserved
