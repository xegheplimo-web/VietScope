"""Embedding Service — BAAI/bge-m3 for semantic retrieval.

Provides dense vector embeddings for search result reranking and semantic
similarity. Supports both local sentence-transformers and OpenAI-compatible
API endpoints. Lazy-loaded, batch support, graceful degradation.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class BgeEmbeddingService:
    """Lazy-loaded BGE embedding model (bge-m3).

    Loads the model on first use and caches it for the process lifetime.
    Supports batch embedding for efficiency. When the model is unavailable,
    ``available()`` returns False and all embedding calls return None.
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int = 32,
        max_seq_length: int = 8192,
    ) -> None:
        self.model_name = model_name or "BAAI/bge-m3"
        self.device = device or None
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length
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
        """Load the embedding model. Returns True on success."""
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        # ThreadingHTTPServer can race concurrent /embed calls into multiple
        # model loads (each ~2 GB). Serialize so only the first thread loads.
        with self._load_lock:
            if self._model is not None or self._load_failed:
                return self._model is not None
            return self._load_impl()

    def _load_impl(self) -> bool:
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            logger.warning("sentence-transformers unavailable: %s", exc)
            self._load_failed = True
            return False
        try:
            device = self._detect_device()
            kwargs = {"device": device} if device else {}
            self._model = SentenceTransformer(
                self.model_name,
                **kwargs,
            )
            if self.max_seq_length:
                self._model.max_seq_length = self.max_seq_length
            logger.info("BgeEmbeddingService loaded %s on %s", self.model_name, device)
            return True
        except Exception as exc:
            logger.warning("BgeEmbeddingService load failed (%s): %s", self.model_name, exc)
            self._load_failed = True
            return False

    def available(self) -> bool:
        """Check if the embedding model is loaded and ready."""
        return self._load()

    @property
    def loaded(self) -> bool:
        """Non-blocking readiness check — never triggers a model load."""
        return self._model is not None and not self._load_failed

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a list of texts.

        Returns a list of dense vectors aligned with ``texts``, or None on
        failure (never raises — logs a warning and lets callers fall back).
        """
        if not texts or not self._load():
            return None
        try:
            vectors = self._model.encode(
                texts,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            # numpy arrays → native Python float lists (JSON-serializable)
            return vectors.tolist()
        except Exception as exc:
            logger.warning("BgeEmbeddingService embed failed: %s", exc)
            return None

    def embed_query(self, query: str) -> list[float] | None:
        """Embed a single query string."""
        vectors = self.embed([query])
        if vectors is None or not vectors:
            return None
        return vectors[0]

    def embed_documents(self, documents: list[str]) -> list[list[float]] | None:
        """Embed a list of document strings (batch)."""
        return self.embed(documents)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """Rerank documents by cosine similarity to query.

        Returns ``[(doc_index, score), ...]`` sorted by score descending.
        On embed failure returns ``[(i, 0.0) for i in range(len(documents))]``
        (identity), so callers degrade gracefully.
        """
        if not documents:
            return []
        q_vec = self.embed_query(query)
        if q_vec is None:
            return [(i, 0.0) for i in range(len(documents))]
        vectors = self.embed_documents(documents)
        if vectors is None:
            return [(i, 0.0) for i in range(len(documents))]
        scored = [(i, _cosine(q_vec, v)) for i, v in enumerate(vectors)]
        scored.sort(key=lambda x: x[1], reverse=True)
        if top_n is not None and top_n > 0:
            scored = scored[:top_n]
        return scored


# Singleton instance
_DEFAULT_EMBEDDING: BgeEmbeddingService | None = None


def get_bge_embedding() -> BgeEmbeddingService:
    """Return the process-wide default BgeEmbeddingService singleton."""
    global _DEFAULT_EMBEDDING
    if _DEFAULT_EMBEDDING is None:
        _DEFAULT_EMBEDDING = BgeEmbeddingService()
    return _DEFAULT_EMBEDDING


# ── Standalone HTTP service entry point ──────────────────────────────────────


def _run_service() -> None:
    """Run as a standalone HTTP service (for Docker container).

    Endpoints:
        GET  /health   → {"status": "ok", "model": str, "available": bool}
        POST /embed    → {"texts": [...]} → {"vectors": [[...], ...]}
    """
    import contextlib
    import json
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    service = get_bge_embedding()
    port = int(os.getenv("PORT", "8890"))
    model_name = service.model_name

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                # Non-blocking: report load state without triggering a load.
                # available() would block for minutes while the model loads.
                body = json.dumps(
                    {
                        "status": "ok",
                        "model": model_name,
                        "available": service.loaded,
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
            if self.path == "/embed":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    data = json.loads(raw) if raw else {}
                    texts = data.get("texts", [])
                    vectors = service.embed(texts)
                    body = json.dumps({"vectors": vectors}).encode()
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
    print(f"BgeEmbeddingService HTTP on :{port} (model={model_name})")
    server.serve_forever()


if __name__ == "__main__":
    _run_service()
