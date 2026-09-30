"""Structured, redacted JSON logging with trace/request correlation."""
from __future__ import annotations
import json, logging, os, re, time
from contextvars import ContextVar

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

def safe_request_id(value: str | None) -> str:
    return value if value and _REQUEST_ID_RE.fullmatch(value) else ""

from opentelemetry import trace

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

def configure_json_logging() -> None:
    if os.getenv("JSON_LOGS", "true").lower() not in {"1", "true", "yes"}:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
