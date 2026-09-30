"""Semantic embedding reranker — optional OpenAI-compatible /embeddings endpoint.

Provides ``EmbeddingReranker`` for embedding-based relevance scoring. When no
embedding endpoint is configured (empty model / key / base url) the reranker
is disabled and callers fall back to the existing keyword-only path, so there
is zero behavior change for existing deployments.
"""

from __future__ import annotations

import logging
import math

import httpx
from config import settings

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


class EmbeddingReranker:
    """OpenAI-compatible embedding reranker (deterministic, no external LLM)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.embedding_base_url).rstrip(
            "/"
        )
        self.api_key = api_key if api_key is not None else settings.embedding_api_key
        self.model = model if model is not None else settings.embedding_model
        self.timeout = timeout

    def available(self) -> bool:
        """True when an embedding endpoint is fully configured."""
        return bool(self.api_key and self.base_url and self.model)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a list of texts via POST ``{base_url}/embeddings``.

        Returns a list of vectors aligned with ``texts``, or ``None`` on any
        error (never raises — logs a warning and lets callers fall back).
        """
        if not self.available():
            logger.warning("EmbeddingReranker.embed called but not configured")
            return None
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            data.sort(key=lambda item: item.get("index", 0))
            vectors = [
                item["embedding"] for item in data if isinstance(item.get("embedding"), list)
            ]
            if len(vectors) != len(texts):
                logger.warning(
                    "Embedding endpoint returned %d vectors for %d inputs",
                    len(vectors),
                    len(texts),
                )
                return None
            return vectors
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            logger.warning("Embedding request failed: %s", exc)
            return None

    def _score(self, query: str, docs: list[str]) -> list[float] | None:
        """Cosine similarity of each doc against the query (None on failure)."""
        if not docs:
            return []
        vectors = self.embed([query] + list(docs))
        if vectors is None or len(vectors) != len(docs) + 1:
            return None
        q_vec = vectors[0]
        return [_cosine(q_vec, doc_vec) for doc_vec in vectors[1:]]

    def rerank(self, query: str, docs: list[str], top_n: int) -> list[int]:
        """Return indices of ``docs`` sorted by semantic similarity, top_n first.

        On embed failure returns ``range(len(docs))`` (identity), so callers
        degrade gracefully to keyword-only ranking.
        """
        if not docs:
            return []
        scores = self._score(query, docs)
        if scores is None:
            return list(range(len(docs)))
        ordered = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return ordered[: max(0, top_n)] if top_n else ordered
