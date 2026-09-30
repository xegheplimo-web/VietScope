"""OpenTelemetry trace per-stage for Search-Hub.

Each stage (L0-L17) emits a span with latency, status, and metadata.
When OTEL is not configured, traces are logged locally for debugging.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
import os

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


@dataclass
class StageTrace:
    """Single stage trace record."""

    stage: str
    start_ms: float
    end_ms: float = 0.0
    status: str = "ok"  # ok | error | degraded | skipped
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end_ms - self.start_ms) * 1000


_PROVIDER: TracerProvider | None = None if _OTEL_AVAILABLE else None
_INSTRUMENTED = False


def setup_telemetry(service_name: str = "search-router") -> None:
    """Install one process-wide OTLP provider when explicitly enabled."""
    global _PROVIDER
    if not _OTEL_AVAILABLE or os.getenv("OTEL_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return
    if _PROVIDER is not None:
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    try:
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
            "service.version": os.getenv("APP_VERSION", "3.0.0"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "dev"),
            "service.instance.id": os.getenv("HOSTNAME", "search-router"),
        })
        _PROVIDER = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        _PROVIDER.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(_PROVIDER)
    except Exception:  # telemetry must never stop the router
        logger.exception("OpenTelemetry setup failed")
        _PROVIDER = None


def instrument_app(app: Any) -> None:
    """Install framework/client instrumentation after the app exists."""
    global _INSTRUMENTED
    if _INSTRUMENTED or not _OTEL_AVAILABLE or os.getenv("OTEL_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        RedisInstrumentor().instrument()
        _INSTRUMENTED = True
    except Exception:
        logger.exception("OpenTelemetry instrumentation failed")


class SearchTracer:
    """Per-request tracer — collects stage spans and exports to OTEL or log."""

    def __init__(self, request_id: str, enabled: bool = True):
        self.request_id = request_id
        self.enabled = enabled
        self.stages: list[StageTrace] = []
        self._active: dict[str, StageTrace] = {}
        self._otel_tracer = None

        if _OTEL_AVAILABLE and enabled:
            try:
                setup_telemetry()
                self._otel_tracer = trace.get_tracer("search-hub")
            except Exception:
                self._otel_tracer = None

    @contextmanager
    def span(self, stage: str, **metadata):
        """Context manager for a stage span."""
        if not self.enabled:
            yield
            return

        trace_record = StageTrace(
            stage=stage,
            start_ms=time.monotonic(),
            metadata=metadata,
        )
        self._active[stage] = trace_record

        otel_span = None
        if self._otel_tracer:
            otel_span = self._otel_tracer.start_span(stage)
            for k, v in metadata.items():
                otel_span.set_attribute(k, v)

        try:
            yield trace_record
            trace_record.status = "ok"
        except Exception as exc:
            trace_record.status = "error"
            trace_record.metadata["error"] = str(exc)
            if otel_span:
                otel_span.set_attribute("error", True)
                otel_span.set_attribute("error.message", str(exc))
            raise
        finally:
            trace_record.end_ms = time.monotonic()
            self.stages.append(trace_record)
            self._active.pop(stage, None)

            if otel_span:
                otel_span.end()

    def summary(self) -> dict[str, Any]:
        """Return trace summary for logging/debugging."""
        total_ms = sum(s.duration_ms for s in self.stages)
        return {
            "request_id": self.request_id,
            "total_ms": round(total_ms, 1),
            "stages": [
                {
                    "stage": s.stage,
                    "duration_ms": round(s.duration_ms, 1),
                    "status": s.status,
                    **s.metadata,
                }
                for s in self.stages
            ],
        }

    def log_summary(self) -> None:
        """Log trace summary at INFO level."""
        if self.enabled:
            logger.info("trace: %s", json.dumps(self.summary(), ensure_ascii=False))
