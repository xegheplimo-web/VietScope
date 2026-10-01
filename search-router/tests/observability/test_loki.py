"""OPS-1: LokiLogHandler — Loki push-API payload shape + fail-open transport."""

import json
import logging
import logging.handlers

import pytest


def _record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("test", level, __file__, 1, msg, None, None)


def test_build_payload_shape():
    from observability.logging import JsonFormatter, LokiLogHandler

    handler = LokiLogHandler(
        "http://loki:3100",
        labels={"service": "search-router", "env": "test", "tier": "api"},
    )
    handler.setFormatter(JsonFormatter())
    payload = handler.build_payload(_record())

    (stream,) = payload["streams"]
    assert stream["stream"] == {"service": "search-router", "env": "test", "tier": "api"}
    (ts, line) = stream["values"][0]
    assert ts.isdigit() and len(ts) > 15  # nanosecond epoch
    body = json.loads(line)
    assert body["message"] == "hello"
    assert body["service"] == "search-router"
    assert "request_id" in body and "search_id" in body


def test_build_payload_reuses_preformatted_message():
    """QueueHandler.prepare() bakes the JSON line into ``record.message`` in
    the emitting thread — the handler must ship it verbatim."""
    from observability.logging import LokiLogHandler

    handler = LokiLogHandler("http://loki:3100", labels={})
    record = _record()
    record.message = '{"pre":"formatted"}'
    payload = handler.build_payload(record)
    assert payload["streams"][0]["values"][0][1] == '{"pre":"formatted"}'


def test_url_parsing_and_push_path():
    from observability.logging import LokiLogHandler

    handler = LokiLogHandler("http://loki.example:3100/base", labels={})
    assert handler._host == "loki.example"
    assert handler._port == 3100
    assert handler._path == "/base/loki/api/v1/push"


def test_emit_posts_payload():
    from observability.logging import JsonFormatter, LokiLogHandler

    handler = LokiLogHandler("http://loki:3100", labels={"service": "x"})
    handler.setFormatter(JsonFormatter())
    sent: list[bytes] = []
    handler._post = sent.append  # type: ignore[method-assign]
    handler.emit(_record())
    (body,) = sent
    line = json.loads(body)["streams"][0]["values"][0][1]
    assert json.loads(line)["message"] == "hello"


def test_emit_never_raises(monkeypatch):
    from observability.logging import LokiLogHandler

    monkeypatch.setattr(logging, "raiseExceptions", False)
    handler = LokiLogHandler("http://127.0.0.1:1", labels={})
    handler.emit(_record())  # connection refused — must be swallowed


def test_configure_json_logging_adds_loki_fanout(monkeypatch):
    import observability.logging as obs

    monkeypatch.setenv("JSON_LOGS", "true")
    monkeypatch.setenv("JSON_LOGS_LOKI", "true")
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    try:
        obs.configure_json_logging()
        queue_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.QueueHandler)]
        assert queue_handlers
        assert queue_handlers[0].queue.maxsize > 0  # bounded — no unbounded RAM
        assert obs._LOKI_LISTENER is not None
    finally:
        if obs._LOKI_LISTENER is not None:
            obs._LOKI_LISTENER.stop()
            obs._LOKI_LISTENER = None
        root.handlers = old_handlers


def test_configure_json_logging_loki_off_by_default(monkeypatch):
    import observability.logging as obs

    monkeypatch.setenv("JSON_LOGS", "true")
    monkeypatch.delenv("JSON_LOGS_LOKI", raising=False)
    monkeypatch.delenv("LOKI_URL", raising=False)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    try:
        obs.configure_json_logging()
        assert not any(isinstance(h, logging.handlers.QueueHandler) for h in root.handlers)
    finally:
        root.handlers = old_handlers


def test_post_counts_non_2xx(monkeypatch):
    """A 429/5xx from Loki must be counted — not silently treated as sent."""
    import http.client

    from observability.logging import LokiLogHandler

    import observability.prometheus as prom

    class _Resp:
        status = 429

        def read(self) -> None:
            return None

    class _Conn:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def request(self, *args, **kwargs) -> None:
            pass

        def getresponse(self):
            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", _Conn)
    before = prom.LOKI_PUSH_ERRORS._value.get()
    LokiLogHandler("http://loki:3100", labels={})._post(b"{}")
    assert prom.LOKI_PUSH_ERRORS._value.get() == before + 1


def test_post_counts_transport_error(monkeypatch):
    import http.client

    from observability.logging import LokiLogHandler

    import observability.prometheus as prom

    class _Conn:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def request(self, *args, **kwargs) -> None:
            raise OSError("connection refused")

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", _Conn)
    handler = LokiLogHandler("http://loki:3100", labels={})
    before = prom.LOKI_PUSH_ERRORS._value.get()
    with pytest.raises(OSError):
        handler._post(b"{}")
    assert prom.LOKI_PUSH_ERRORS._value.get() == before + 1


def test_queue_handler_drops_and_counts_when_full():
    import queue as _queue

    from observability.logging import _LokiQueueHandler

    import observability.prometheus as prom

    q: _queue.Queue = _queue.Queue(maxsize=1)
    handler = _LokiQueueHandler(q)
    before = prom.LOKI_DROPPED_LOGS._value.get()
    handler.enqueue(_record())
    handler.enqueue(_record())  # full → drop, count, never raise
    assert q.qsize() == 1
    assert prom.LOKI_DROPPED_LOGS._value.get() == before + 1
