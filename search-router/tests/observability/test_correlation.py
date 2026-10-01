"""OPS-1: search_id correlation — ``Search-Id`` header → ``search_id_var`` → JSON logs."""

import asyncio
import json
import logging

from starlette.requests import Request
from starlette.responses import Response


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
        }
    )


def _capture_vars(request: Request) -> dict:
    """Run the real ``correlation_context`` middleware and capture the vars
    as seen inside request handling."""
    import main as app_module
    from observability.logging import request_id_var, search_id_var

    captured: dict[str, str] = {}

    async def call_next(_req: Request) -> Response:
        captured["request_id"] = request_id_var.get()
        captured["search_id"] = search_id_var.get()
        return Response(status_code=200)

    response = asyncio.run(app_module.correlation_context(request, call_next))
    captured["_status"] = str(response.status_code)
    captured["_x_request_id"] = response.headers.get("X-Request-ID", "")
    return captured


def test_search_id_header_propagates():
    from observability.logging import request_id_var, search_id_var

    captured = _capture_vars(
        _request([(b"search-id", b"srch_test_42"), (b"x-request-id", b"req_abc")])
    )
    assert captured["search_id"] == "srch_test_42"
    assert captured["request_id"] == "req_abc"
    assert captured["_x_request_id"] == "req_abc"
    # contextvars are reset once the request finishes
    assert search_id_var.get() == "-"
    assert request_id_var.get() == "-"


def test_search_id_falls_back_to_request_id():
    captured = _capture_vars(_request([(b"x-request-id", b"req_fallback")]))
    assert captured["search_id"] == "req_fallback"


def test_search_id_invalid_header_falls_back():
    captured = _capture_vars(
        _request([(b"search-id", b"bad id with spaces"), (b"x-request-id", b"req_x")])
    )
    assert captured["search_id"] == "req_x"


def test_json_log_line_carries_search_id():
    """End-to-end: the ``request.completed`` log line, formatted while the
    request context is active, must carry the Search-Id value."""
    import main as app_module
    from fastapi.testclient import TestClient
    from observability.logging import JsonFormatter

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _Collect()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client = TestClient(app_module.app)
        resp = client.get("/", headers={"Search-Id": "srch_e2e_9"})
        assert resp.status_code == 200
    finally:
        root.removeHandler(handler)

    payloads = [json.loads(line) for line in lines]
    completed = [p for p in payloads if p.get("event") == "request.completed"]
    assert completed, "correlation middleware emitted no request.completed record"
    assert any(p["search_id"] == "srch_e2e_9" for p in completed)
