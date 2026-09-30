"""BGE Reranker Service — BAAI/bge-reranker-v2-m3 via sentence-transformers.

Provides cross-encoder relevance scoring for search results. The model is
lazy-loaded on first use and gracefully degrades when unavailable.
"""

from __future__ import annotations

import logging
import math

from config import settings

logger = logging.getLogger(__name__)


class BgeRerankerService:
    """Lazy-loaded BGE reranker (bge-reranker-v2-m3).

    Loads the model on first use and caches it for the process lifetime.
    When the model or its dependencies are unavailable, ``available()``
    returns False and all scoring calls return None, allowing callers to
    fall back to deterministic heuristics.
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        max_length: int = 512,
    ) -> None:
        self.model_name = model_name or "BAAI/bge-reranker-v2-m3"
        self.device = device or settings.reranker_device or None
        self.batch_size = batch_size or settings.reranker_batch_size
        self.max_length = max_length
        self._model = None
        self._load_failed = False
        import threading

        self._load_lock = threading.Lock()

    def _detect_device(self) -> str | None:
        """Auto-detect best available device (cuda > mps > cpu)."""
        if self.device:
            return self.device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _load(self) -> bool:
        """Load the cross-encoder model. Returns True on success."""
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        # ThreadingHTTPServer can race concurrent /score calls into multiple
        # model loads. Serialize so only the first thread loads.
        with self._load_lock:
            if self._model is not None or self._load_failed:
                return self._model is not None
            return self._load_impl()

    def _load_impl(self) -> bool:
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:
            logger.warning("sentence-transformers unavailable: %s", exc)
            self._load_failed = True
            return False
        try:
            device = self._detect_device()
            kwargs = {"device": device} if device else {}
            self._model = CrossEncoder(
                self.model_name,
                max_length=self.max_length,
                **kwargs,
            )
            logger.info("BgeRerankerService loaded %s on %s", self.model_name, device)
            return True
        except Exception as exc:
            logger.warning("BgeRerankerService load failed (%s): %s", self.model_name, exc)
            self._load_failed = True
            return False

    def available(self) -> bool:
        """Check if the reranker model is loaded and ready."""
        return self._load()

    @property
    def loaded(self) -> bool:
        """Non-blocking readiness check — never triggers a model load."""
        return self._model is not None and not self._load_failed

    def score(self, query: str, documents: list[str]) -> list[float] | None:
        """Score (query, document) pairs.

        Returns sigmoid-normalized scores in [0, 1], or None when the
        model is unavailable or prediction fails.
        """
        if not documents or not self._load():
            return None
        pairs = [(query, doc) for doc in documents]
        try:
            raw = self._model.predict(
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:
            logger.warning("BgeRerankerService predict failed: %s", exc)
            return None
        scores: list[float] = []
        for value in list(raw):
            s = float(value)
            # Apply sigmoid normalization for logits from bge-reranker-v2-m3
            if s < 0.0 or s > 1.0:
                s = 1.0 / (1.0 + math.exp(-s))
            scores.append(max(0.0, min(1.0, s)))
        return scores


# Singleton instance
_DEFAULT_SERVICE: BgeRerankerService | None = None


def get_bge_reranker() -> BgeRerankerService:
    """Return the process-wide default BgeRerankerService singleton."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = BgeRerankerService()
    return _DEFAULT_SERVICE


# ── Standalone HTTP service entry point ──────────────────────────────────────


def _run_service() -> None:
    """Run as a standalone HTTP service (for Docker container).

    Endpoints:
        GET  /health  → {"status": "ok", "model": str, "available": bool}
        POST /score   → {"query": str, "documents": [...]} → {"scores": [...]}
    """
    import contextlib
    import json
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    service = get_bge_reranker()
    port = int(os.getenv("PORT", "8889"))
    model_name = service.model_name

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                # Non-blocking: report load state without triggering a load.
                available = service.loaded
                body = json.dumps(
                    {
                        "status": "ok",
                        "model": model_name,
                        "available": available,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    # Client disconnected during model load — not fatal
                    self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/score":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    data = json.loads(raw) if raw else {}
                    query = data.get("query", "")
                    documents = data.get("documents", [])
                    scores = service.score(query, documents)
                    body = json.dumps({"scores": scores}).encode()
                    self.send_response(200)
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                    self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # Suppress request logging

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 — must be reachable from compose network
    print(f"BgeRerankerService HTTP on :{port} (model={model_name})")
    server.serve_forever()


if __name__ == "__main__":
    _run_service()
