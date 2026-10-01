"""Structured, redacted JSON logging with trace/request correlation."""

from __future__ import annotations

import http.client
import json
import logging
import logging.handlers
import os
import queue
import re
import time
from contextvars import ContextVar
from urllib.parse import urlsplit

from opentelemetry import trace

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def safe_request_id(value: str | None) -> str:
    return value if value and _REQUEST_ID_RE.fullmatch(value) else ""


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
search_id_var: ContextVar[str] = ContextVar("search_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "service": os.getenv("OTEL_SERVICE_NAME", "search-router"),
            "event": getattr(record, "event", record.name),
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "search_id": search_id_var.get(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else "-",
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else "-",
        }
        for key in ("method", "route", "status_code", "duration_ms", "outcome"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class LokiLogHandler(logging.Handler):
    """Best-effort push of JSON log lines to Loki's ``/loki/api/v1/push``.

    Designed to sit behind a ``QueueListener`` so the HTTP POST never runs on
    the request path. ``emit`` never raises — Loki is optional infrastructure.
    """

    def __init__(self, url: str, *, labels: dict[str, str], timeout: float = 2.0) -> None:
        super().__init__()
        parts = urlsplit(url.rstrip("/"))
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname or "localhost"
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._path = f"{parts.path or ''}/loki/api/v1/push"
        self.labels = dict(labels)
        self.timeout = timeout

    def build_payload(self, record: logging.LogRecord) -> dict:
        # ``record.message`` is the pre-rendered line when the record arrived
        # via QueueHandler (``prepare`` formats in the emitting thread, where
        # request/search-id contextvars are still set). Format on demand when
        # the handler is used directly.
        line = getattr(record, "message", "") or self.format(record)
        return {
            "streams": [
                {
                    "stream": dict(self.labels),
                    "values": [[str(int(record.created * 1e9)), line]],
                }
            ]
        }

    def _post(self, body: bytes) -> None:
        conn_cls = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_cls(self._host, self._port, timeout=self.timeout)
        try:
            conn.request(
                "POST", self._path, body=body, headers={"Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            resp.read()  # drain so the connection closes cleanly
        finally:
            conn.close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._post(json.dumps(self.build_payload(record)).encode("utf-8"))
        except Exception:
            self.handleError(record)


_LOKI_LISTENER: logging.handlers.QueueListener | None = None


def configure_json_logging() -> None:
    if os.getenv("JSON_LOGS", "true").lower() not in {"1", "true", "yes"}:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    # Optional Loki fan-out (opt-in: JSON_LOGS_LOKI=true + LOKI_URL). The
    # QueueHandler renders the JSON line in the emitting thread — contextvars
    # intact — and the listener thread only performs the HTTP POST.
    global _LOKI_LISTENER
    loki_url = os.getenv("LOKI_URL", "").strip()
    if os.getenv("JSON_LOGS_LOKI", "").lower() in {"1", "true", "yes"} and loki_url:
        if _LOKI_LISTENER is not None:
            _LOKI_LISTENER.stop()
        loki = LokiLogHandler(
            loki_url,
            labels={
                "service": os.getenv("OTEL_SERVICE_NAME", "search-router"),
                "env": os.getenv("DEPLOYMENT_ENVIRONMENT", "dev"),
                "tier": "api",
            },
        )
        loki.setFormatter(JsonFormatter())
        loki_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        queue_handler = logging.handlers.QueueHandler(loki_queue)
        queue_handler.setFormatter(JsonFormatter())
        root.addHandler(queue_handler)
        _LOKI_LISTENER = logging.handlers.QueueListener(loki_queue, loki)
        _LOKI_LISTENER.start()
