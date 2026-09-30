"""OpenAI-compatible public surface (P1) — thin gateway over the existing core.

Exposes ``GET /v1/models`` + ``POST /v1/chat/completions`` so any
OpenAI-compatible client (Open WebUI, LangChain, the official SDK) can use
Search-Hub with only ``base_url`` + ``api_key``.  The adapter owns request
translation and wire format only — routing, retrieval, synthesis and
verification stay in ``run_research`` (the same engine ``/v1/answer`` runs),
so there is no duplicated orchestration and nothing to keep in sync.

Error policy: every failure on these two paths — validation, auth, upstream —
returns the OpenAI ``{"error": {message,type,param,code}}`` shape.  Auth
errors are raised by ``require_api_key`` as HTTPException and re-shaped by
``OpenAIHTTPException`` (handled app-wide in ``main.py``); body-validation
errors use the scoped ``openai_validation_handler`` so native /v1 routes keep
FastAPI's default 422 contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from agent.orchestrator import run_research
from config import settings
from core.conversation import resolve_request_query
from fastapi import APIRouter, Depends, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from research_models.research_state import ResearchContext
from security.apikeys import require_api_key

logger = logging.getLogger(__name__)

_SSE_KEEPALIVE_S = 20.0
# ~4 chars/token for the max_tokens/max_completion_tokens content budget.
_CHARS_PER_TOKEN = 4


class OpenAIHTTPException(Exception):
    """HTTP error carried in OpenAI ``{"error": ...}`` shape.

    Raised only on the OpenAI-compat surface; ``main.py`` registers the
    handler that serializes it, so native /v1 endpoints keep FastAPI's
    ``{"detail": ...}`` contract.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        err_type: str | None = None,
    ):
        self.status = status
        self.message = message
        self.code = code
        self.param = param
        self.err_type = err_type or _error_type(status)
        super().__init__(message)


def _error_type(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "invalid_request_error",
        429: "rate_limit_error",
    }.get(status, "server_error")


def _openai_error(
    status: int,
    message: str,
    *,
    code: str | None = None,
    param: str | None = None,
    err_type: str | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "message": message,
            "type": err_type or _error_type(status),
            "param": param,
            "code": code,
        }
    }
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(status_code=status, content=body, headers=headers)


async def openai_http_exception_handler(request: Request, exc: Exception):
    """Serialize OpenAIHTTPException → OpenAI error body (registered on app)."""
    assert isinstance(exc, OpenAIHTTPException)  # registered for this class only
    return _openai_error(
        exc.status, exc.message, code=exc.code, param=exc.param, err_type=exc.err_type
    )


_COMPAT_PATHS = ("/v1/models", "/v1/chat/completions")


async def openai_validation_handler(request: Request, exc: Exception):
    """Shape pydantic 422s as OpenAI errors on the compat surface only."""
    assert isinstance(exc, RequestValidationError)  # registered for this class only
    if any(request.url.path.startswith(p) for p in _COMPAT_PATHS):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "invalid request body")
        return _openai_error(400, f"{msg} ({loc})" if loc else msg, param=loc or None)
    return await request_validation_exception_handler(request, exc)


async def _require_api_key(request: Request):
    """`require_api_key` with OpenAI-shaped failures on this surface."""
    try:
        return await require_api_key(request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise OpenAIHTTPException(
            exc.status_code,
            str(detail.get("error") or detail.get("detail") or exc.detail),
            code=str(detail.get("error")) if detail.get("error") else None,
            err_type=_error_type(exc.status_code),
        ) from exc


router = APIRouter(prefix="/v1", tags=["openai"], dependencies=[Depends(_require_api_key)])


def _models_list() -> list[dict]:
    created = int(datetime.now(UTC).timestamp())
    return [
        {
            "id": name.strip(),
            "object": "model",
            "created": created,
            "owned_by": "search-hub",
        }
        for name in settings.openai_models.split(",")
        if name.strip()
    ]


class ChatMessage(BaseModel):
    role: str
    content: Any = ""  # str or OpenAI content-part list


class ChatCompletionRequest(BaseModel):
    """OpenAI ``/v1/chat/completions`` body.

    Honored: ``model``, ``messages``, ``stream``, ``max_tokens`` /
    ``max_completion_tokens`` (content budget, ~4 chars/token), ``stop``
    (post-hoc truncation).  Accepted-but-ignored: ``temperature``, ``top_p``,
    ``seed``, ``presence_penalty``, ``frequency_penalty``, ``user``,
    ``tools``, ``tool_choice`` — the pipeline has no knobs for them today
    (documented, not silently "supported").  ``tools`` in particular must be
    tolerated: Open WebUI's default builtin-tools capability sends a
    non-empty array on every request; rejecting it would break the
    flagship connect+chat flow.  Rejected with 400
    ``unsupported_parameter``: forced ``tool_choice`` (``"required"`` or a
    specific function — the client demands a tool_call we cannot produce),
    JSON-mode ``response_format``, ``n > 1``.
    Unknown extra fields are ignored (OpenAI clients send plenty).
    """

    model_config = ConfigDict(extra="ignore")

    model: str = Field(default="duyai-search")
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    n: int | None = Field(default=None, ge=1)
    tools: list | None = None
    tool_choice: Any = None
    response_format: dict | None = None
    stream_options: dict | None = None

    @property
    def token_cap(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    @property
    def stop_list(self) -> list[str]:
        if self.stop is None:
            return []
        return [self.stop] if isinstance(self.stop, str) else list(self.stop)


def _message_text(content: Any) -> str:
    """Flatten an OpenAI message ``content`` (str or part list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _last_user_and_history(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
    """Return (last user text, prior turns as role/content dicts).

    All prior turns — ``system`` included — flow into
    ``resolve_request_query`` as conversation context so follow-up
    resolution sees them.  The research pipeline itself has no
    system-prompt channel, so a system message can steer context
    resolution but can never inject instructions into retrieval/synthesis —
    that is the deliberate policy boundary.
    """
    idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user" and _message_text(messages[i].content).strip():
            idx = i
            break
    if idx < 0:
        return "", []
    history = [{"role": m.role, "content": _message_text(m.content)} for m in messages[:idx]]
    return _message_text(messages[idx].content).strip(), history


def _sources_footer(sources: list[dict]) -> str:
    """Human-readable source block appended inside assistant content."""
    if not sources:
        return ""
    lines = ["", "", "**Sources:**"]
    for i, s in enumerate(sources[:10], start=1):
        title = (s.get("title") or s.get("url") or "").strip() or "untitled"
        url = s.get("url") or ""
        lines.append(f"[{i}] {title} — {url}")
    return "\n".join(lines)


def _apply_stops(text: str, stops: list[str]) -> tuple[str, bool]:
    """Truncate at the first stop string. Returns (text, hit_stop)."""
    cut = len(text)
    for s in stops:
        pos = text.find(s)
        if pos != -1:
            cut = min(cut, pos)
    return text[:cut], cut < len(text)


def _usage_stub() -> dict:
    # The research engine reports timings, not token counts; emit zeros
    # rather than inventing numbers. OpenAI clients accept absent totals.
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _duyai_meta(result: dict, *, stream: bool) -> dict:
    """Optional structured metadata — extra top-level key, ignored by
    stock OpenAI clients, available to DuyAI-aware consumers."""
    meta = {
        "confidence": result.get("confidence", 0.0),
        "search_rounds": result.get("search_rounds"),
        "verification": result.get("verification", {}),
        "timings": result.get("timings", {}),
    }
    if not stream:
        meta["citations"] = result.get("citations") or []
    return meta


@router.get("/models")
async def list_models():
    return {"object": "list", "data": _models_list()}


def _validate(req: ChatCompletionRequest, known_models: set[str]) -> JSONResponse | None:
    if req.model not in known_models:
        return _openai_error(
            404,
            f"The model `{req.model}` does not exist",
            code="model_not_found",
            param="model",
        )
    if not req.messages:
        return _openai_error(400, "messages must be a non-empty array", param="messages")
    if req.n is not None and req.n != 1:
        return _openai_error(400, "n > 1 is not supported", code="unsupported_parameter", param="n")
    if isinstance(req.tool_choice, dict) or req.tool_choice == "required":
        return _openai_error(
            400,
            "forced tool_choice is not supported; the pipeline cannot emit tool_calls",
            code="unsupported_parameter",
            param="tool_choice",
        )
    if req.response_format and req.response_format.get("type") not in (None, "text"):
        return _openai_error(
            400,
            "response_format other than text is not supported",
            code="unsupported_parameter",
            param="response_format",
        )
    return None


@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    err = _validate(req, {m["id"] for m in _models_list()})
    if err is not None:
        return err

    query, history = _last_user_and_history(req.messages)
    if not query:
        return _openai_error(
            400,
            "messages must contain at least one user message with content",
            param="messages",
        )

    # Owner binds conversation state to the API key — same principal the
    # native endpoints use.
    ctx = getattr(request.state, "api_key", None)
    owner = getattr(ctx, "key_id", "") if ctx is not None else "anonymous"
    try:
        resolution = await resolve_request_query(query, history=history, owner=owner)
    except Exception:  # noqa: BLE001 — resolution is fail-open by contract
        resolution = None
    effective_query = resolution.query if resolution is not None else query

    context = ResearchContext(query=effective_query, mode="balanced")
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(datetime.now(UTC).timestamp())

    if req.stream:
        return _sse_stream(_completion_stream(context, completion_id, created, req))

    try:
        result = await asyncio.wait_for(run_research(context), timeout=settings.openai_timeout_s)
    except TimeoutError:
        return _openai_error(504, "upstream timeout", err_type="server_error")
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat/completions pipeline failed")
        return _openai_error(500, str(exc), err_type="server_error")

    sources = result.get("sources") or []
    answer = _apply_stops(result.get("answer") or "", req.stop_list)[0]
    content, finish = _truncate_tokens(answer, req.token_cap)
    content += _sources_footer(sources)
    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                    "logprobs": None,
                }
            ],
            "usage": _usage_stub(),
            "search_hub": _duyai_meta(result, stream=False),
        }
    )


def _truncate_tokens(text: str, cap: int | None) -> tuple[str, str]:
    if cap is None:
        return text, "stop"
    budget = cap * _CHARS_PER_TOKEN
    if len(text) <= budget:
        return text, "stop"
    return text[:budget], "length"


def _chunk(completion_id: str, created: int, model: str, delta: dict, finish=None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _completion_stream(
    context: ResearchContext, completion_id: str, created: int, req: ChatCompletionRequest
):
    """Translate run_research emit events → chat.completion.chunk SSE.

    Honors the same request contract as the non-stream path: an outer
    ``openai_timeout_s`` deadline, the ``max_tokens`` content budget
    (``finish_reason: "length"``), and ``stop`` sequences (post-hoc
    truncation, ``finish_reason: "stop"``).  Client disconnect cancels the
    pipeline via the ``finally`` task.cancel().
    """
    queue: asyncio.Queue = asyncio.Queue()
    model = req.model
    budget = req.token_cap * _CHARS_PER_TOKEN if req.token_cap else None
    emitted = 0
    full_text = ""
    deadline = time.monotonic() + settings.openai_timeout_s

    yield _chunk(completion_id, created, model, {"role": "assistant"})

    async def emit(event: str, data: dict) -> None:
        if event == "answer.delta":
            await queue.put(("delta", str((data or {}).get("text") or "")))

    async def _run() -> None:
        try:
            result = await run_research(context, emit=emit)
            await queue.put(("_done", result))
        except Exception as exc:  # noqa: BLE001 — stream must not die
            await queue.put(("_error", exc))

    def _emit_content(text: str):
        """Yield-safe: track the emitted text against the token budget."""
        nonlocal emitted, full_text
        full_text += text
        if budget is not None:
            remaining = budget - emitted
            text = text[:remaining]
        emitted += len(text)
        if text:
            return _chunk(completion_id, created, model, {"content": text})
        return None

    async def _finish(reason: str):
        yield _chunk(completion_id, created, model, {}, finish=reason)
        yield "data: [DONE]\n\n"

    task = asyncio.create_task(_run())
    try:
        while True:
            if time.monotonic() > deadline:
                err = json.dumps({"error": {"message": "upstream timeout", "type": "server_error"}})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return
            try:
                event, data = await asyncio.wait_for(
                    queue.get(),
                    timeout=min(_SSE_KEEPALIVE_S, max(deadline - time.monotonic(), 0.1)),
                )
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event == "delta":
                # stop sequences: truncate at the first hit, end as "stop"
                if req.stop_list:
                    new_text, hit = _apply_stops(full_text + data, req.stop_list)
                    data = new_text[len(full_text) :]
                    if hit:
                        piece = _emit_content(data)
                        if piece:
                            yield piece
                        async for tail in _finish("stop"):
                            yield tail
                        return
                piece = _emit_content(data)
                if piece:
                    yield piece
                if budget is not None and emitted >= budget:
                    async for tail in _finish("length"):
                        yield tail
                    return
                continue
            if event == "_error":
                err = json.dumps({"error": {"message": str(data), "type": "server_error"}})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return
            # _done
            result = data or {}
            sources = result.get("sources") or []
            if emitted == 0:
                piece = _emit_content(result.get("answer") or "")
                if piece:
                    yield piece
            piece = _emit_content(_sources_footer(sources))
            if piece:
                yield piece
            reason = "length" if (budget is not None and emitted >= budget) else "stop"
            async for tail in _finish(reason):
                yield tail
            return
    finally:
        task.cancel()  # client disconnect → cancel the pipeline run


def _sse_stream(events):
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
