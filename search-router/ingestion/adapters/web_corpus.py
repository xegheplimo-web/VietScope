"""Web-corpus bridge adapter (P15/P15.1).

Reuses the existing crawl corpus — ``documents`` rows (+ their trafilatura
metadata) — as a place source. A page becomes a candidate when its
metadata carries place-ish schema.org/LocalBusiness fields (``name`` +
``telephone``/``address``/``geo``); pages without place signals are
skipped, not errored. external_id = doc_id, provider = ``web_corpus``.

P15.1: the adapter streams — it never materializes the corpus in memory.
Sources, in priority order:
  ``pool`` ctor arg / ``pool`` param → keyset-paged ``documents`` scan
  ``docs`` ctor arg / param        → any iterable of document dicts
    (CLI streams JSONL lazily; tests inject lists)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any

from ingestion.base import IngestionContext, RawPlaceRecord, SourceProbe

_PLACE_KEYS = ("telephone", "address", "geo", "latitude", "openingHours")

_PAGE_SQL = """
SELECT doc_id, canonical_url, title, metadata
FROM documents
WHERE doc_id > $1
ORDER BY doc_id
LIMIT $2
"""


def _doc_to_record(doc: dict[str, Any], fetched_at: datetime) -> RawPlaceRecord | None:
    meta = doc.get("metadata") or {}
    name = meta.get("name") or doc.get("title")
    if not name:
        return None
    has_signal = any(meta.get(k) for k in _PLACE_KEYS)
    if not has_signal:
        return None
    lat = _f(meta.get("latitude") or (meta.get("geo") or {}).get("latitude"))
    lon = _f(meta.get("longitude") or (meta.get("geo") or {}).get("longitude"))
    addr = meta.get("address")
    if isinstance(addr, dict):  # schema.org PostalAddress
        addr = (
            ", ".join(
                str(addr.get(k) or "")
                for k in ("streetAddress", "addressLocality", "addressRegion")
                if addr.get(k)
            )
            or None
        )
    return RawPlaceRecord(
        provider=WebCorpusAdapter.name,
        external_id=str(doc.get("doc_id") or doc.get("id") or ""),
        external_id_type="doc_id",
        source_url=doc.get("canonical_url") or doc.get("url"),
        raw_name=str(name),
        raw_address=str(addr) if addr else None,
        raw_phone=str(meta.get("telephone")) if meta.get("telephone") else None,
        raw_website=doc.get("canonical_url") or doc.get("url"),
        raw_category=meta.get("@type") or meta.get("category"),
        lat=lat,
        lon=lon,
        raw_payload={
            "doc_id": doc.get("doc_id"),
            "url": doc.get("canonical_url"),
            "title": doc.get("title"),
            "metadata": meta,
        },
        observed_at=fetched_at,
        fetched_at=fetched_at,
    )


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class WebCorpusAdapter:
    """Streams crawled documents → raw place candidates.

    ``pool`` mode pages the ``documents`` table by keyset (doc_id cursor)
    with ``page_size`` rows per fetch — resumable via
    ``checkpoint["after"] = <doc_id>``. ``docs`` mode accepts any iterable
    and checkpoints by count.
    """

    name = "web_corpus"
    adapter_version = "web-corpus-v2"

    def __init__(
        self,
        docs: Iterable[dict[str, Any]] | None = None,
        *,
        pool: Any = None,
        page_size: int = 1000,
    ):
        self._docs = docs
        self._pool = pool
        self._page_size = page_size
        self.source_dataset: dict[str, Any] = {}

    async def probe(self) -> SourceProbe:
        if self._pool is not None:
            return SourceProbe(ok=True, provider=self.name, detail="documents table stream")
        return SourceProbe(
            ok=True,
            provider=self.name,
            detail="document iterable injected" if self._docs is not None else "needs docs param",
        )

    async def _db_stream(self, context: IngestionContext) -> AsyncIterator[dict[str, Any]]:
        """Keyset-paged documents scan — cursor survives resume."""
        after = str(context.checkpoint.get("after") or "")
        while True:
            rows = await self._pool.fetch(_PAGE_SQL, after, self._page_size)
            if not rows:
                return
            for r in rows:
                after = r["doc_id"]
                context.checkpoint["after"] = after
                yield dict(r)

    async def ingest(self, context: IngestionContext) -> AsyncIterator[RawPlaceRecord]:
        pool = self._pool or context.param("pool")
        fetched = datetime.now(UTC)
        if pool is not None:
            self.source_dataset = {"source": "documents", "snapshot": "live"}
            async for doc in self._db_stream(context):
                rec = _doc_to_record(doc, fetched)
                if rec is not None:
                    yield rec
            return
        docs = self._docs or context.param("docs") or []
        self.source_dataset = {"source": "docs_iterable"}
        skip = int(context.checkpoint.get("docs_seen") or 0)
        for i, doc in enumerate(docs):
            if i < skip:
                continue
            context.checkpoint["docs_seen"] = i + 1
            rec = _doc_to_record(doc, fetched)
            if rec is not None:
                yield rec
