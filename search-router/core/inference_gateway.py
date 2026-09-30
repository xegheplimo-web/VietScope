"""Canonical inference gateway — the ONLY module that talks to the LLM.

All LLM traffic (chat completions, JSON completions, streaming, embeddings)
goes through ``InferenceGateway``. Other modules must never call
``/chat/completions`` or build provider HTTP requests themselves.

Design notes:

- OpenAI-compatible: works with OpenAI, Groq, Ollama, or a LiteLLM proxy.
  Point ``LLM_BASE_URL`` at a LiteLLM deployment later — zero code changes.
- Model roles (``planner``/``synthesizer``/``verifier``/``extractor``) map to
  ``LLM_*_MODEL`` env vars and fall back to ``LLM_MODEL``.
- Per-role backends (A9): ``LLM_<ROLE>_BASE_URL``/``_API_KEY`` route a role
  to its own endpoint (e.g. extractor → LocalAI, synthesizer → OpenRouter).
  Each distinct base_url gets its own HTTP client and circuit breaker, so
  one endpoint's outage never trips another's. ``LLM_<ROLE>_FALLBACK=default``
  retries a failed call once on the global backend (with ``LLM_MODEL``);
  unset = return ``None`` and let callers take their deterministic fallback.
- Resilience: per-call timeout, bounded retries on transient failures
  (transport errors, 429, 5xx), and a circuit breaker that fails fast while
  the provider is down. A failed call is followed by a cheap ``GET /models``
  liveness probe — when the endpoint is unreachable the breaker trips on the
  first failure instead of burning ``threshold`` full timeouts; permanent
  statuses (401/403/404) trip it instantly; timed-out calls are never
  retried (the timeout already spent the budget).
- Error normalization: public methods never raise — they return ``None``
  (or end the stream) so callers keep their deterministic fallbacks.
- ``metrics`` exposes call/failure/retry counters for observability.
"""

import asyncio
import contextlib
import json
import logging
import re
import statistics
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from enum import StrEnum

import httpx
from config import settings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Auth failures — never transient inside a request; the breaker trips on the
# first one instead of burning the full threshold. 404 is deliberately absent:
# a misconfigured *role* model must not trip the global breaker for the rest.
_PERMANENT_STATUS = frozenset({401, 403})
_PROBE_TIMEOUT_S = 5.0
# Bounded per-role latency samples for p50/p95 in ``metrics`` — a query
# snapshot, not a histogram; 256 recent calls is enough for dashboards.
_LATENCY_SAMPLES = 256


def _error_kind(exc: BaseException | None = None, status: int | None = None) -> str:
    """Classify a failure for diagnostics — the ``:18434`` 401 blocker showed
    'LLM unavailable' was not actionable; kinds make the failure legible."""
    if status is not None:
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "model_missing"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "server_error"
        return "invalid_request"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "network"
    return "error"


class _Lane:
    """Per-endpoint state: own HTTP client + breaker + last error kind.

    Roles sharing one base_url share the lane — an OpenRouter outage must
    not trip LocalAI's breaker, and vice versa."""

    __slots__ = ("client", "breaker", "last_error_kind")

    def __init__(self, threshold: int, cooldown: float) -> None:
        self.client: httpx.AsyncClient | None = None
        self.breaker = _CircuitBreaker(threshold, cooldown)
        self.last_error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """Resolved target for one call — role overrides merged over globals."""

    base_url: str
    api_key: str
    model: str
    override: bool  # role pinned a non-default base_url
    fallback_default: bool  # on failure, retry once on the default backend


class ModelRole(StrEnum):
    """LLM model roles — route work to different models via LLM_*_MODEL envs."""

    PLANNER = "planner"
    SYNTHESIZER = "synthesizer"
    VERIFIER = "verifier"
    EXTRACTOR = "extractor"
    DEFAULT = "default"


class _CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after cooldown."""

    def __init__(self, threshold: int = 3, cooldown: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        """True when closed, or when cooldown elapsed (half-open probe)."""
        if self._opened_at is None:
            return True
        return (time.monotonic() - self._opened_at) >= self.cooldown

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> bool:
        """Record a failure; returns True when the breaker just opened."""
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()
            return self._failures == self.threshold
        return False

    def trip(self) -> bool:
        """Force the breaker open; returns False when already open."""
        if self._opened_at is not None:
            return False
        self._failures = max(self._failures, self.threshold)
        self._opened_at = time.monotonic()
        return True


class InferenceGateway:
    """Single OpenAI-compatible LLM gateway — see module docstring."""

    def __init__(
        self,
        *,
        max_attempts: int = 2,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 60.0,
    ) -> None:
        # ``None`` means "read from settings at call time" — keeps a shared
        # instance honest when tests or runtime reconfigure settings.
        self._api_key: str | None = None
        self._base_url: str | None = None
        self._model: str | None = None
        self._timeout = httpx.Timeout(
            connect=10.0, read=float(settings.llm_timeout), write=30.0, pool=10.0
        )
        self._max_attempts = max(1, max_attempts)
        self._breaker = _CircuitBreaker(breaker_threshold, breaker_cooldown)
        self._http: httpx.AsyncClient | None = None
        # A9: lanes for role-override endpoints (keyed by base_url). The
        # default backend keeps using ``self._http`` / ``self._breaker``
        # directly — no extra lane entry, nothing changes for single-endpoint
        # deployments.
        self._lanes: dict[str, _Lane] = {}
        self._lane_threshold = breaker_threshold
        self._lane_cooldown = breaker_cooldown
        self._default_lane_kind: str | None = None
        self._role_stats: dict[str, dict] = {}
        self._metrics: dict = {
            "calls": 0,
            "failures": 0,
            "retries": 0,
            "circuit_rejects": 0,
            "circuit_opens": 0,
            "by_role": {},
            "last_error": None,
            "last_error_kind": None,
            "endpoint_probes": 0,
            "fallbacks": 0,
        }

    # -- configuration (settings-backed, overridable for tests/DI) ------

    @property
    def api_key(self) -> str:
        return self._api_key if self._api_key is not None else settings.llm_api_key

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key = value

    @property
    def base_url(self) -> str:
        return self._base_url if self._base_url is not None else settings.llm_base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value

    @property
    def model(self) -> str:
        return self._model if self._model is not None else settings.llm_model

    @model.setter
    def model(self, value: str) -> None:
        self._model = value

    @property
    def embedding_model(self) -> str:
        return settings.embedding_model

    @property
    def metrics(self) -> dict:
        m = dict(self._metrics)
        m["by_role"] = dict(self._metrics["by_role"])
        m["circuit_open"] = self._breaker.open
        m["backends"] = {
            self.base_url: {
                "open": self._breaker.open,
                "last_error_kind": self._default_lane_kind,
            }
        }
        m["backends"].update(
            {
                url: {"open": lane.breaker.open, "last_error_kind": lane.last_error_kind}
                for url, lane in self._lanes.items()
            }
        )
        m["roles"] = {
            name: self._role_metrics_view(name, s) for name, s in self._role_stats.items()
        }
        return m

    def _role_metrics_view(self, name: str, s: dict) -> dict:
        lat = sorted(s["latencies"])
        p50 = round(statistics.median(lat), 1) if lat else None
        p95 = round(statistics.quantiles(lat, n=20)[-1], 1) if len(lat) >= 20 else None
        ep = self.endpoint_for(name)
        lane = self._lanes.get(ep.base_url)
        lane_open = self._breaker.open if lane is None else lane.breaker.open
        kind = lane.last_error_kind if lane is not None else self._default_lane_kind
        if lane_open:
            state = "down"
        elif kind in ("auth", "model_missing"):
            state = kind
        elif s["last_error_kind"]:
            state = "degraded"
        else:
            state = "healthy"
        return {
            "backend": ep.base_url,
            "model": ep.model,
            "override": ep.override,
            "state": state,
            "calls": s["calls"],
            "failures": s["failures"],
            "circuit_rejects": s["circuit_rejects"],
            "fallbacks": s["fallbacks"],
            "input_tokens": s["input_tokens"],
            "output_tokens": s["output_tokens"],
            "p50_ms": p50,
            "p95_ms": p95,
            "last_error_kind": s["last_error_kind"],
        }

    def model_for(self, role: ModelRole | str) -> str:
        """Resolve a role to a concrete model — role env override, else default."""
        role = ModelRole(role)
        override = {
            ModelRole.PLANNER: settings.llm_planner_model,
            ModelRole.SYNTHESIZER: settings.llm_synth_model,
            ModelRole.VERIFIER: settings.llm_verify_model,
            ModelRole.EXTRACTOR: settings.llm_extract_model,
        }.get(role, "")
        return override or self.model

    def endpoint_for(self, role: ModelRole | str) -> _Endpoint:
        """Resolve a role to its backend endpoint (A9).

        ``LLM_<ROLE>_BASE_URL``/``_API_KEY`` override the global endpoint;
        unset means the default backend. ``LLM_<ROLE>_FALLBACK=default``
        allows one retry of a failed call against the default backend.
        """
        role = ModelRole(role)
        url_o, key_o, fb = {
            ModelRole.PLANNER: (
                settings.llm_planner_base_url,
                settings.llm_planner_api_key,
                settings.llm_planner_fallback,
            ),
            ModelRole.SYNTHESIZER: (
                settings.llm_synth_base_url,
                settings.llm_synth_api_key,
                settings.llm_synth_fallback,
            ),
            ModelRole.VERIFIER: (
                settings.llm_verify_base_url,
                settings.llm_verify_api_key,
                settings.llm_verify_fallback,
            ),
            ModelRole.EXTRACTOR: (
                settings.llm_extract_base_url,
                settings.llm_extract_api_key,
                settings.llm_extract_fallback,
            ),
        }.get(role, ("", "", ""))
        default_url = self.base_url.rstrip("/")
        override = bool(url_o) and url_o.rstrip("/") != default_url
        base_url = (url_o or self.base_url).rstrip("/")
        return _Endpoint(
            base_url=base_url,
            api_key=key_o or self.api_key,
            model=self.model_for(role),
            override=override,
            fallback_default=override and fb.strip().lower() == "default",
        )

    # -- public API -----------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        *,
        role: ModelRole | str = ModelRole.DEFAULT,
        temperature: float = 0.7,
        max_tokens: int = 512,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Chat completion → assistant content, or ``None`` on any failure."""
        role = ModelRole(role)
        t0 = time.monotonic()
        body = {
            "model": self.model_for(role),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = await self._send(
            "chat/completions",
            body,
            timeout=timeout,
            json_mode_fallback=json_mode,
            role=role,
        )
        if resp is None:
            return None
        try:
            data = resp.json()
            self._observe(role, t0, data)
            choices = data.get("choices") or []
            if not choices:
                logger.warning("Inference response did not contain choices")
                self._role_stat(role)["last_error_kind"] = "invalid_response"
                return None
            content = (choices[0].get("message") or {}).get("content")
            return content if isinstance(content, str) else None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Bad inference response: %s", exc)
            self._observe(role, t0, None)
            self._role_stat(role)["last_error_kind"] = "invalid_response"
            return None

    async def complete_json(
        self,
        messages: list[dict],
        *,
        role: ModelRole | str = ModelRole.DEFAULT,
        schema_hint: str | None = None,
        required_keys: Iterable[str] | None = None,
        expect: type | tuple[type, ...] = dict,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout: float | None = None,
    ) -> dict | list | None:
        """JSON completion → parsed object/array, or ``None`` on any failure.

        Sends ``response_format=json_object``; on HTTP 400 retries once
        without it (providers that reject JSON mode). The parsed value must
        satisfy ``isinstance(parsed, expect)`` and, for objects, contain
        non-null ``required_keys``. Tolerates markdown JSON fences.
        """
        role = ModelRole(role)
        t0 = time.monotonic()
        extra = []
        if schema_hint:
            extra.append(
                {
                    "role": "system",
                    "content": f"Return ONLY valid JSON matching this schema: {schema_hint}",
                }
            )
        body = {
            "model": self.model_for(role),
            "messages": list(messages) + extra,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        resp = await self._send(
            "chat/completions",
            body,
            timeout=timeout,
            json_mode_fallback=True,
            role=role,
        )
        if resp is None:
            return None
        try:
            data = resp.json()
            self._observe(role, t0, data)
            choices = data.get("choices") or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
        except (ValueError, TypeError, KeyError, AttributeError):
            self._observe(role, t0, None)
            self._role_stat(role)["last_error_kind"] = "invalid_response"
            return None
        if not isinstance(content, str):
            self._role_stat(role)["last_error_kind"] = "invalid_response"
            return None
        parsed = _safe_parse_json(content)
        if parsed is None or not isinstance(parsed, expect):
            self._role_stat(role)["last_error_kind"] = "invalid_response"
            return None
        if required_keys and isinstance(parsed, dict):
            missing = [k for k in required_keys if parsed.get(k) is None]
            if missing:
                logger.warning("JSON completion missing required keys: %s", missing)
                return None
        return parsed

    async def stream(
        self,
        messages: list[dict],
        *,
        role: ModelRole | str = ModelRole.DEFAULT,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas from a streaming chat completion.

        Real async iterator — yields tokens as they arrive over SSE.
        On failure the stream simply ends (logged), matching the
        ``None``-fallback contract of the other methods. Streaming calls
        are not retried (partial output cannot be replayed safely), but
        failures still feed the circuit breaker. When the role's endpoint
        sets ``LLM_<ROLE>_FALLBACK=default``, a leg that fails *before the
        first token* retries once on the default backend — once a delta has
        been yielded the stream can no longer be replayed.
        """
        role = ModelRole(role)
        ep = self.endpoint_for(role)
        if not ep.api_key:
            return
        legs = [ep]
        if ep.fallback_default:
            legs.append(self.endpoint_for(ModelRole.DEFAULT))
        body = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        for i, leg in enumerate(legs):
            if i:
                self._metrics["fallbacks"] += 1
                self._role_stat(role)["fallbacks"] += 1
                logger.warning("stream: role %s falling back to default backend", role)
            breaker = self._breaker_for(leg.base_url)
            if not breaker.allow():
                self._metrics["circuit_rejects"] += 1
                self._role_stat(role)["circuit_rejects"] += 1
                continue
            client = await self._client_for(leg.base_url)
            url = f"{leg.base_url}/chat/completions"
            self._count_call(role)
            yielded = False
            try:
                async with client.stream(
                    "POST",
                    url,
                    json={**body, "model": leg.model},
                    headers=self._headers_for(leg.api_key),
                    timeout=timeout if timeout is not None else self._timeout,
                ) as resp:
                    if resp.status_code >= 400:
                        self._record_failure(
                            f"http_{resp.status_code}",
                            leg.base_url,
                            role,
                            kind=_error_kind(status=resp.status_code),
                        )
                        if resp.status_code in _PERMANENT_STATUS:
                            self._trip_breaker(f"permanent http_{resp.status_code}", leg.base_url)
                        continue  # no tokens yet — the next leg may still serve
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            yielded = True
                            yield delta
                breaker.record_success()
                self._record_success(leg.base_url, role)
                return
            except httpx.TransportError as exc:
                self._record_failure(
                    f"{type(exc).__name__}: {exc}",
                    leg.base_url,
                    role,
                    kind=_error_kind(exc),
                )
                if not await self._endpoint_alive(client, leg):
                    self._trip_breaker("endpoint unreachable", leg.base_url)
                if yielded:
                    return  # partial output — cannot replay on the next leg
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                self._record_failure(
                    f"{type(exc).__name__}: {exc}",
                    leg.base_url,
                    role,
                    kind=_error_kind(exc),
                )
                if yielded:
                    return

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embedding vectors via OpenAI-compatible ``/embeddings``."""
        if not self.api_key or not texts:
            return None
        body = {"model": self.embedding_model, "input": list(texts)}
        resp = await self._send("embeddings", body)
        if resp is None:
            return None
        try:
            data = resp.json()
            raw = data.get("data") or []
            if not raw:
                logger.warning("Embeddings response did not contain data")
                return None
            vectors = []
            for item in raw:
                vec = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(vec, list):
                    return None
                vectors.append([float(v) for v in vec if isinstance(v, (int, float))])
            return vectors if vectors and len(vectors) == len(texts) else None
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Embedding request failed: %s", exc)
            return None

    async def extract(self, text: str, schema: dict | str) -> dict | None:
        """Structured extraction via the ``extractor`` role."""
        result = await self.complete_json(
            [
                {"role": "system", "content": "Extract structured data as JSON only."},
                {"role": "user", "content": f"Schema: {schema}\n\nText:\n{text}"},
            ],
            role=ModelRole.EXTRACTOR,
            schema_hint=str(schema),
            expect=dict,
        )
        return result if isinstance(result, dict) else None

    async def aclose(self) -> None:
        """Close all HTTP clients — default backend + override lanes."""
        if self._http is not None:
            with contextlib.suppress(Exception):  # best-effort cleanup
                await self._http.aclose()
            self._http = None
        for lane in self._lanes.values():
            if lane.client is not None:
                with contextlib.suppress(Exception):
                    await lane.client.aclose()
                lane.client = None

    # -- internals ------------------------------------------------------

    def _headers_for(self, api_key: str) -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _headers(self) -> dict:
        return self._headers_for(self.api_key)

    async def _client(self) -> httpx.AsyncClient:
        """One shared client per gateway — keep-alive across LLM calls."""
        if self._http is None or getattr(self._http, "is_closed", True):
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def _client_for(self, base_url: str) -> httpx.AsyncClient:
        """Client for a resolved endpoint — default URL keeps the shared
        ``self._http``; override endpoints get (and keep) their own."""
        if base_url == self.base_url.rstrip("/"):
            return await self._client()
        lane = self._lanes.get(base_url)
        if lane is None:
            lane = self._lanes[base_url] = _Lane(self._lane_threshold, self._lane_cooldown)
        if lane.client is None or lane.client.is_closed:
            lane.client = httpx.AsyncClient(timeout=self._timeout)
        return lane.client

    def _breaker_for(self, base_url: str) -> _CircuitBreaker:
        if base_url == self.base_url.rstrip("/"):
            return self._breaker
        lane = self._lanes.get(base_url)
        if lane is None:
            lane = self._lanes[base_url] = _Lane(self._lane_threshold, self._lane_cooldown)
        return lane.breaker

    def _role_stat(self, role: ModelRole) -> dict:
        return self._role_stats.setdefault(
            role.value,
            {
                "calls": 0,
                "failures": 0,
                "circuit_rejects": 0,
                "fallbacks": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "latencies": deque(maxlen=_LATENCY_SAMPLES),
                "last_error_kind": None,
            },
        )

    def _observe(self, role: ModelRole, t0: float, data: dict | None) -> None:
        """Record per-role latency + token usage from a completed response."""
        s = self._role_stat(role)
        s["latencies"].append(round((time.monotonic() - t0) * 1000, 1))
        usage = (data or {}).get("usage") or {}
        s["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        s["output_tokens"] += int(usage.get("completion_tokens") or 0)

    def _count_call(self, role: ModelRole) -> None:
        self._metrics["calls"] += 1
        key = role.value
        self._metrics["by_role"][key] = self._metrics["by_role"].get(key, 0) + 1
        self._role_stat(role)["calls"] += 1

    def _trip_breaker(self, reason: str, base_url: str | None = None) -> None:
        if self._breaker_for(base_url or self.base_url.rstrip("/")).trip():
            self._metrics["circuit_opens"] += 1
        logger.warning("Inference circuit breaker tripped (%s)", reason)

    async def _endpoint_alive(self, client: httpx.AsyncClient, ep: _Endpoint) -> bool:
        """Cheap liveness probe — distinguishes a dead gateway (hangs or
        refuses every request) from one slow completion. Any HTTP status
        counts as alive; only transport failure means dead."""
        self._metrics["endpoint_probes"] += 1
        try:
            resp = await client.get(
                f"{ep.base_url}/models",
                headers=self._headers_for(ep.api_key),
                timeout=_PROBE_TIMEOUT_S,
            )
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    def _record_success(self, base_url: str | None, role: ModelRole) -> None:
        """Success clears current-error state; cumulative counters stay."""
        url = base_url or self.base_url.rstrip("/")
        if url == self.base_url.rstrip("/"):
            self._default_lane_kind = None
        elif url in self._lanes:
            self._lanes[url].last_error_kind = None
        self._role_stat(role)["last_error_kind"] = None

    def _record_failure(
        self,
        error: str,
        base_url: str | None = None,
        role: ModelRole = ModelRole.DEFAULT,
        *,
        kind: str | None = None,
    ) -> None:
        self._metrics["failures"] += 1
        self._metrics["last_error"] = error
        self._metrics["last_error_kind"] = kind
        url = base_url or self.base_url.rstrip("/")
        if url == self.base_url.rstrip("/"):
            self._default_lane_kind = kind
        elif url in self._lanes:
            self._lanes[url].last_error_kind = kind
        s = self._role_stat(role)
        s["failures"] += 1
        s["last_error_kind"] = kind
        if self._breaker_for(url).record_failure():
            self._metrics["circuit_opens"] += 1
            logger.warning("Inference circuit breaker opened (%s)", error)
        else:
            logger.warning("Inference call failed: %s", error)

    async def probe(self, role: ModelRole | str = ModelRole.DEFAULT) -> dict:
        """Diagnose a role's endpoint without touching the circuit breaker.

        GET /models then a 1-token chat completion — classifies the failure
        as ``unreachable`` | ``auth_failed`` | ``model_missing`` |
        ``server_error`` | ``timeout`` | ``healthy`` so a dead/misconfigured
        backend (the ``:18434`` 401 class of bug) is legible, not just
        "LLM unavailable".
        """
        role = ModelRole(role)
        ep = self.endpoint_for(role)
        out = {"role": role.value, "backend": ep.base_url, "model": ep.model}
        if not ep.api_key:
            out["state"] = "no_api_key"
            return out
        client = await self._client_for(ep.base_url)
        self._metrics["endpoint_probes"] += 1
        try:
            r = await client.get(
                f"{ep.base_url}/models",
                headers=self._headers_for(ep.api_key),
                timeout=_PROBE_TIMEOUT_S,
            )
        except httpx.TimeoutException as exc:
            return out | {"state": "timeout", "detail": str(exc)[:200]}
        except httpx.TransportError as exc:
            return out | {"state": "unreachable", "detail": str(exc)[:200]}
        if r.status_code in (401, 403):
            return out | {"state": "auth_failed", "http_status": r.status_code}
        if r.status_code >= 500:
            return out | {"state": "server_error", "http_status": r.status_code}
        try:
            r2 = await client.post(
                f"{ep.base_url}/chat/completions",
                json={
                    "model": ep.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                headers=self._headers_for(ep.api_key),
                timeout=15.0,
            )
        except httpx.TimeoutException as exc:
            return out | {"state": "timeout", "detail": str(exc)[:200]}
        except httpx.TransportError as exc:
            return out | {"state": "unreachable", "detail": str(exc)[:200]}
        state = {
            401: "auth_failed",
            403: "auth_failed",
            404: "model_missing",
        }.get(r2.status_code)
        if state is None:
            state = (
                "healthy"
                if r2.status_code < 400
                else ("server_error" if r2.status_code >= 500 else "invalid_request")
            )
        return out | {"state": state, "http_status": r2.status_code}

    async def _send(
        self,
        path: str,
        body: dict,
        *,
        timeout: float | httpx.Timeout | None = None,
        json_mode_fallback: bool = False,
        role: ModelRole = ModelRole.DEFAULT,
    ) -> httpx.Response | None:
        """POST ``path`` with retry + circuit breaker. Never raises."""
        ep = self.endpoint_for(role)
        if not ep.api_key:
            return None
        legs = [ep]
        if ep.fallback_default:
            legs.append(self.endpoint_for(ModelRole.DEFAULT))
        for i, leg in enumerate(legs):
            if i:
                self._metrics["fallbacks"] += 1
                self._role_stat(role)["fallbacks"] += 1
                logger.warning("role %s: falling back to default backend", role)
            resp = await self._send_leg(
                path,
                body,
                ep=leg,
                timeout=timeout,
                json_mode_fallback=json_mode_fallback,
                role=role,
            )
            if resp is not None:
                return resp
        return None

    async def _send_leg(
        self,
        path: str,
        body: dict,
        *,
        ep: _Endpoint,
        timeout: float | httpx.Timeout | None = None,
        json_mode_fallback: bool = False,
        role: ModelRole = ModelRole.DEFAULT,
    ) -> httpx.Response | None:
        """One backend leg of a ``_send`` — retries + breaker scoped to ``ep``."""
        url = f"{ep.base_url}/{path}"
        breaker = self._breaker_for(ep.base_url)
        client = await self._client_for(ep.base_url)
        effective_timeout = timeout if timeout is not None else self._timeout
        for attempt in range(self._max_attempts):
            if not breaker.allow():
                self._metrics["circuit_rejects"] += 1
                self._role_stat(role)["circuit_rejects"] += 1
                logger.warning("Inference call rejected — circuit open (%s)", path)
                return None
            self._count_call(role)
            try:
                resp = await client.post(
                    url,
                    json={**body, "model": ep.model},
                    headers=self._headers_for(ep.api_key),
                    timeout=effective_timeout,
                )
            except httpx.TimeoutException as exc:
                self._record_failure(
                    f"{type(exc).__name__}: {exc}",
                    ep.base_url,
                    role,
                    kind=_error_kind(exc),
                )
                # If the endpoint can't answer a cheap probe either, the
                # gateway is down: trip now instead of failing N more times.
                if not await self._endpoint_alive(client, ep):
                    self._trip_breaker("endpoint unreachable", ep.base_url)
                    return None
                if isinstance(exc, httpx.ReadTimeout):
                    # A read timeout already spent its full budget — no retry.
                    return None
                # Connect/write/pool timeouts didn't burn the read budget —
                # worth retrying like any other transient transport error.
                if attempt + 1 < self._max_attempts:
                    self._metrics["retries"] += 1
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return None
            except httpx.TransportError as exc:
                self._record_failure(
                    f"{type(exc).__name__}: {exc}",
                    ep.base_url,
                    role,
                    kind=_error_kind(exc),
                )
                if not await self._endpoint_alive(client, ep):
                    self._trip_breaker("endpoint unreachable", ep.base_url)
                    return None
                if attempt + 1 < self._max_attempts:
                    self._metrics["retries"] += 1
                    await asyncio.sleep(0.5 * (attempt + 1))
                continue
            except Exception as exc:  # noqa: BLE001 — normalize to None
                self._record_failure(f"{type(exc).__name__}: {exc}", ep.base_url, role)
                return None

            if (
                json_mode_fallback
                and resp.status_code == 400
                and body.pop("response_format", None) is not None
            ):
                # Capability negotiation, not a provider failure — retry
                # without JSON mode (does not count against the breaker).
                logger.warning("JSON mode rejected (400) — retrying without response_format")
                continue
            if resp.status_code in _RETRYABLE_STATUS:
                self._record_failure(
                    f"http_{resp.status_code}",
                    ep.base_url,
                    role,
                    kind=_error_kind(status=resp.status_code),
                )
                if attempt + 1 < self._max_attempts:
                    self._metrics["retries"] += 1
                    await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code in _PERMANENT_STATUS:
                self._record_failure(
                    f"http_{resp.status_code}",
                    ep.base_url,
                    role,
                    kind=_error_kind(status=resp.status_code),
                )
                self._trip_breaker(f"permanent http_{resp.status_code}", ep.base_url)
                return None
            if resp.status_code >= 400:
                self._record_failure(
                    f"http_{resp.status_code}",
                    ep.base_url,
                    role,
                    kind=_error_kind(status=resp.status_code),
                )
                return None
            breaker.record_success()
            self._record_success(ep.base_url, role)
            return resp
        return None


_gateway: InferenceGateway | None = None


def get_inference_gateway() -> InferenceGateway:
    """Process-wide gateway — one connection pool, one circuit breaker."""
    global _gateway
    if _gateway is None:
        _gateway = InferenceGateway()
    return _gateway


def _safe_parse_json(raw: str) -> dict | list | None:
    """Parse a JSON string, tolerating markdown fences and stray whitespace."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # Strip markdown fences if present: ```json ... ```
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, (dict, list)) else None
    except json.JSONDecodeError:
        # Last resort: find a lone `{...}` or `[...]` and parse it.
        for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
            match = re.search(pattern, raw)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except json.JSONDecodeError:
                    continue
        logger.warning("Failed to parse JSON from completion response")
        return None
