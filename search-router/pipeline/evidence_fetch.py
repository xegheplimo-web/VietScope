"""Evidence fetch — structured sources → reranked passages with provenance.

Server-side implementation behind ``POST /v1/evidence`` and the MCP
``fetch_evidence`` tool. Each source URL is read through the tiered
reader (``pipeline.reader``) — the SSRF-guarded path where every redirect
hop is vetted by ``crawler.netguard`` — then chunked and reranked by
``pipeline.passage_reranker`` (BGE cross-encoder when available, the
keyword/BM25/phrase heuristic otherwise).

Search-side provenance survives end to end: ``source_id``,
``canonical_url``, ``published_at``, ``search_provider`` and ``domain``
are echoed back on every evidence item, so a citation maps
claim → passage_id → source_id → URL without re-deriving identity.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from canonical.url import canonical_url
from config import settings
from models import ScrapeResult
from security.ssrf import SSRFError, check_url

from pipeline.passage_reranker import chunk_and_rerank
from pipeline.reader import read_batch
from pipeline.reranker import _domain_from_url

# Freshness classes → (use_cache, max_age_s) for the reader's page cache.
# "realtime" bypasses the cache entirely so fresh-content queries
# ("giá vàng hôm nay", "tỷ giá hiện tại") never serve a stale snapshot;
# "static" keeps the historical never-expire behavior.
_FRESHNESS_POLICY: dict[str, tuple[bool, float | None]] = {
    "realtime": (False, 0.0),
    "high": (True, 300.0),
    "normal": (True, 3600.0),
    "static": (True, None),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_sources(sources: list[Any]) -> list[dict]:
    """Uniform dict per source — structured input or a bare URL string."""
    out: list[dict] = []
    for i, raw in enumerate(sources):
        s = {"url": raw} if isinstance(raw, str) else dict(raw)
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        s["url"] = url
        s.setdefault("source_id", f"src_{i:03d}")
        out.append(s)
    return out


def _dedup_key(source: dict) -> str:
    """Canonical identity for dedup — caller-supplied canonical_url wins."""
    return (source.get("canonical_url") or canonical_url(source["url"]) or source["url"]).lower()


def _error_item(source: dict, error: str, *, content_provider: str) -> dict:
    """Citation-contract item for a source that produced no passages."""
    url = source["url"]
    return {
        "source_id": source["source_id"],
        "passage_id": f"{source['source_id']}:p0",
        "url": url,
        "canonical_url": source.get("canonical_url") or canonical_url(url),
        "title": source.get("title") or "",
        "domain": source.get("domain") or _domain_from_url(url),
        "published_at": source.get("published_at"),
        "retrieved_at": _now_iso(),
        "text": "",
        "quote": "",
        "score": 0.0,
        "search_provider": source.get("search_provider") or "",
        "content_provider": content_provider,
        "error": error,
    }


def _passage_item(chunk: dict, source: dict, scraped: ScrapeResult | None) -> dict:
    """Citation-contract item for one reranked passage."""
    src_url = chunk.get("source_url") or ""
    md = scraped.metadata if scraped is not None else {}
    text = chunk.get("chunk_text") or ""
    source_id = source.get("source_id") or ""
    return {
        "source_id": source_id,
        "passage_id": f"{source_id}:p{chunk.get('chunk_index', 0):03d}",
        "url": source.get("url") or src_url,
        "canonical_url": source.get("canonical_url") or canonical_url(src_url),
        "title": source.get("title") or chunk.get("title") or "",
        "domain": source.get("domain") or _domain_from_url(src_url),
        "published_at": source.get("published_at") or md.get("published_at"),
        "retrieved_at": _now_iso(),
        "text": text[:1000],
        "quote": text[:200],
        "score": round(float(chunk.get("score") or 0.0), 4),
        "search_provider": source.get("search_provider") or "",
        "content_provider": md.get("cached_tier") or md.get("reader_tier") or "",
    }


async def build_evidence(
    sources: list[Any],
    query: str,
    *,
    max_passages: int = 5,
    max_per_source: int = 3,
    freshness: str = "normal",
    read_urls: Any | None = None,
    reranker: Any | None = None,
) -> dict:
    """Read sources and return reranked passages in citation form.

    ``sources`` mixes structured dicts (the ``search`` contract:
    ``source_id``/``url``/``canonical_url``/``title``/``domain``/
    ``published_at``/``search_provider``/``score``) and bare URL strings.
    Duplicates collapse on canonical identity; blocked or unreadable URLs
    yield error items rather than failing the batch. ``freshness`` picks
    the reader-cache policy (realtime|high|normal|static). ``read_urls`` /
    ``reranker`` are injectable for tests.
    """
    start = time.monotonic()
    normalized = _normalize_sources(sources)

    # Dedup on canonical identity, keep first occurrence.
    kept: list[dict] = []
    seen: set[str] = set()
    for s in normalized:
        key = _dedup_key(s)
        if key in seen:
            continue
        seen.add(key)
        kept.append(s)

    # Cheap shape-level SSRF check per URL (scheme/host/literal IP);
    # the reader's NetGuard re-validates every resolved address per hop.
    to_read: list[dict] = []
    errors: list[dict] = []
    for s in kept:
        try:
            check_url(s["url"], resolve=False)
        except SSRFError as exc:
            errors.append(
                _error_item(
                    s, f"URL blocked by SSRF policy: {exc}", content_provider="ssrf_blocked"
                )
            )
            continue
        to_read.append(s)

    # Bounded-parallel read through the tiered, netguarded reader.
    use_cache, max_age_s = _FRESHNESS_POLICY.get(freshness, _FRESHNESS_POLICY["normal"])
    reader = read_urls or read_batch
    urls = [s["url"] for s in to_read]
    read_results = (
        await reader(
            urls,
            timeout=settings.scrape_timeout,
            max_concurrent=5,
            use_cache=use_cache,
            max_age_s=max_age_s,
        )
        if urls
        else []
    )

    scraped: list[ScrapeResult] = []
    by_url: dict[str, tuple[dict, ScrapeResult]] = {}
    for s, rr in zip(to_read, read_results, strict=True):
        if not getattr(rr, "success", False) or not getattr(rr, "text", ""):
            errors.append(
                _error_item(
                    s,
                    getattr(rr, "error", "") or "read failed",
                    content_provider=getattr(rr, "tier", "") or "reader",
                )
            )
            continue
        sr = rr.to_scrape_result()
        scraped.append(sr)
        by_url[sr.url] = (s, sr)

    # Chunk + rerank with the shared passage pipeline (BGE service when
    # available, heuristic fallback) — scoring is CPU-bound, keep it off
    # the loop.
    if reranker is None:
        try:
            from agent.reranker import get_default_reranker

            reranker = get_default_reranker()
        except Exception:  # noqa: BLE001 — heuristic fallback still applies
            reranker = None
    try:
        chunks = await asyncio.to_thread(
            chunk_and_rerank, query, scraped, top_n=0, reranker=reranker
        )
    except Exception:  # noqa: BLE001 — never fail the batch on scoring
        chunks = []

    evidence: list[dict] = []
    per_source: dict[str, int] = {}
    for chunk in chunks:
        src_url = chunk.get("source_url") or ""
        if per_source.get(src_url, 0) >= max_per_source:
            continue
        per_source[src_url] = per_source.get(src_url, 0) + 1
        s, sr = by_url.get(src_url, ({}, None))
        evidence.append(_passage_item(chunk, s, sr))
        if len(evidence) >= max_passages:
            break

    # max_passages bounds passages only — per-source error items always
    # survive, or an SSRF-blocked/unreadable source could silently vanish.
    items = evidence[:max_passages] + errors
    return {
        "query": query,
        "evidence": items,
        "count": len(items),
        "elapsed_seconds": round(time.monotonic() - start, 2),
    }
