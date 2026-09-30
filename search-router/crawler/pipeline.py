"""Crawl pipeline — robots → politeness → fetch → snapshot → documents → discovery.

``CrawlPipeline.process_one`` takes a claimed ``RecrawlTask`` from the
frontier and drives it end to end:

1. robots.txt gate — an *explicit* disallow marks ``robots_allowed=false``
   and completes (done); a *transient* unavailable robots.txt reschedules
   the URL a short time out instead of losing it.
2. per-domain politeness via the per-hop policy callback — robots
   ``Crawl-delay`` honored (capped) on EVERY request, so same-host
   redirect chains and A→B→A roundtrips pay a fresh wait (G3).
3. conditional GET (validators come from the frontier row). A redirect
   landing on a different host must pass that host's robots and
   politeness before the body is accepted.
4. snapshot raw bytes into MinIO, upsert ``documents``, insert a
   ``document_snapshots`` history row — even when unchanged — inside one
   transaction, and only while the claim token is still held; a lost
   claim stops all document mutation (``lost_claim`` outcome). ETag /
   Last-Modified validators are stored *after* the document is durable —
   never before, or a lost write would 304 forever.
5. change detection via normalized content fingerprint + change-rate EMA.
6. same-scope link discovery (``depth < MAX_DEPTH``) back into the frontier.
7. recrawl scheduling: volatile pages resurface within an hour, stable
   pages drift toward a week. Oversize bodies (> fetcher cap) and JS
   shells with the renderer lane disabled are not documents — parked
   ~30 days as ``oversize``/``js_required``.

Every per-URL error is caught and routed to ``frontier.fail`` — one bad
URL can never sink a batch. Persistence failures (MinIO, Postgres) are
*not* swallowed: they fail the task so validators and documents can
never diverge. Without a Postgres pool the pipeline degrades: fetches
and MinIO snapshots still run; document rows and discovery skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html as html_mod
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from canonical.content import content_fingerprint
from canonical.url import canonical_url
from extraction.models import (
    REEXTRACT_STATUSES,
    RETRYABLE_STATUSES,
    STATUS_SUCCESS,
    ExtractionResult,
    status_rank,
)
from storage.object_store import CollisionError
from workers.freshness_worker import RecrawlTask
from workers.indexing_worker import IndexingTask

from crawler.robots import VERDICT_DISALLOWED, VERDICT_UNAVAILABLE
from crawler.seeds import SEED_DOMAINS, registrable_domain, seed_lane

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
MIN_RECRAWL_S = 3600.0  # 1 h — volatile pages
MAX_RECRAWL_S = 7 * 86400.0  # 7 d — stable pages
BLOCKED_RECRAWL_S = 30 * 86400.0  # 30 d — 401/403 cool-off
ROBOTS_UNAVAILABLE_RETRY_S = 15 * 60.0  # robots fetch failure → retry soon
OVERSIZE_RECRAWL_S = BLOCKED_RECRAWL_S  # >cap bodies re-checked monthly
JS_REQUIRED_RECRAWL_S = BLOCKED_RECRAWL_S  # JS shells re-checked monthly (G4)
CHANGE_RATE_ALPHA = 0.3  # EMA weight of the newest observation
MAX_LINKS_PER_PAGE = 50

# Discovery deadlock handling (G2): Postgres aborts one side of a
# reciprocal A↔B lock with SQLSTATE 40P01 — retry the whole transaction
# a bounded number of times with small backoff; other SQL errors
# propagate (the task fails rather than losing the batch silently).
_DEADLOCK_SQLSTATE = "40P01"
_DISCOVER_MAX_ATTEMPTS = 3  # 1 try + 2 retries
_DEADLOCK_BACKOFF_S = 0.1  # scaled by attempt number

# Postgres statements — module constants so tests can dispatch on them.

_FRONTIER_ROW_SQL = """
SELECT canonical_url, depth, etag, last_modified, change_rate
FROM crawl_frontier WHERE url = $1
"""

_ROBOTS_BLOCK_SQL = """
UPDATE crawl_frontier SET robots_allowed = FALSE
WHERE url = $1 AND claim_token IS NOT DISTINCT FROM $2
"""

# Claim-token guards (H4): every document/frontier mutation checks the
# claim is still held — a lease-expired + reclaimed URL must not let a
# stale worker overwrite the new claim's document. _CLAIM_LOCK_SQL runs
# inside the document transaction and serializes against reclaim pops.
_CLAIM_CHECK_SQL = "SELECT claim_token FROM crawl_frontier WHERE url = $1"

_CLAIM_LOCK_SQL = "SELECT claim_token FROM crawl_frontier WHERE url = $1 FOR UPDATE"

_ENSURE_SOURCE_SQL = """
INSERT INTO sources (domain, last_seen) VALUES ($1, now())
ON CONFLICT (domain) DO UPDATE SET last_seen = now()
"""

_DOC_SELECT_SQL = (
    "SELECT doc_id, content_hash, extraction_status FROM documents WHERE canonical_url = $1"
)

_DOC_UPSERT_SQL = """
INSERT INTO documents
    (doc_id, canonical_url, domain, title, content_hash, status, language, metadata, last_seen)
VALUES ($1, $2, $3, $4, $5, 'active', $6, $7::jsonb, now())
ON CONFLICT (canonical_url) DO UPDATE SET
    title = COALESCE(EXCLUDED.title, documents.title),
    content_hash = EXCLUDED.content_hash,
    status = 'active',
    language = COALESCE(EXCLUDED.language, documents.language),
    last_seen = now(),
    metadata = documents.metadata || EXCLUDED.metadata
"""

_SNAPSHOT_SQL = """
INSERT INTO document_snapshots
    (doc_id, storage_key, content_hash, http_status, mime, headers)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
RETURNING snapshot_id
"""

_DOC_SET_SNAPSHOT_SQL = "UPDATE documents SET current_snapshot_id = $2 WHERE doc_id = $1"

# Extraction write: real columns for the search/filter surface, provenance
# + raw extractor payload merged into metadata JSONB (005_extraction).
_DOC_EXTRACTION_SQL = """
UPDATE documents SET
    main_text = $2,
    description = $3,
    author = $4,
    published_at = $5,
    site_name = $6,
    language = COALESCE($7, language),
    word_count = $8,
    quality_score = $9,
    extraction_method = $10,
    extraction_version = $11,
    extraction_status = $12,
    updated_at = now(),
    metadata = documents.metadata || $13::jsonb
WHERE doc_id = $1
"""

_DOC_INDEX_STATUS_SQL = """
UPDATE documents SET
    indexing_status = $2,
    embedding_status = $3,
    updated_at = now()
WHERE doc_id = $1
"""

_DOC_STATUS_SQL = "UPDATE documents SET status = $2, last_seen = now() WHERE canonical_url = $1"

# Discovered links keep their existing status on conflict (a 'done' or
# in-flight row is never resurrected); only the best priority and the
# shallowest known depth merge in.
_DISCOVER_SQL = """
INSERT INTO crawl_frontier
    (url, canonical_url, domain, status, priority, scheduled_at, next_crawl_at,
     discovered_from, depth)
VALUES ($1, $2, $3, 'queued', $4, now(), now(), $5, $6)
ON CONFLICT (url) DO UPDATE SET
    priority = GREATEST(crawl_frontier.priority, EXCLUDED.priority),
    depth = LEAST(crawl_frontier.depth, EXCLUDED.depth)
"""


def recrawl_interval(change_rate: float) -> float:
    """Map change-rate (0..1) to a recrawl delay: volatile → 1 h, stable → 7 d."""
    cr = min(max(change_rate or 0.0, 0.0), 1.0)
    return MAX_RECRAWL_S - cr * (MAX_RECRAWL_S - MIN_RECRAWL_S)


def _ema(old: float | None, observed: float, alpha: float = CHANGE_RATE_ALPHA) -> float:
    if old is None:
        return observed
    return (1 - alpha) * old + alpha * observed


# ─── Link extraction ─────────────────────────────────────────────────────


class _LinkParser(HTMLParser):
    """Collect <a href> targets plus the document's <base href>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)
        elif tag == "base" and self.base is None:
            self.base = dict(attrs).get("href") or self.base


def extract_links(html_text: str, base_url: str) -> list[str]:
    """Absolute http(s) link targets in document order, deduped, fragment-free.

    ``javascript:``/``mailto:``/``tel:``/empty hrefs are dropped by the
    scheme filter; ``#frag`` collapses onto the page itself and is skipped.
    """
    parser = _LinkParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 — HTMLParser is forgiving; keep what we got
        pass

    effective_base = urljoin(base_url, parser.base) if parser.base else base_url
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        href = href.strip()
        if not href:
            continue
        absolute = urljoin(effective_base, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if clean == base_url or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


# ─── Outcome ─────────────────────────────────────────────────────────────


class _ClaimLost(Exception):
    """Frontier row no longer carries our claim token — stop mutating."""


@dataclass
class CrawlOutcome:
    """Result of processing one frontier task."""

    url: str
    outcome: str  # changed|unchanged|not_modified|skipped_robots|robots_unavailable|politeness|oversize|js_required|gone|blocked|lost_claim|error
    status: int = 0
    doc_id: str = ""
    storage_key: str = ""
    changed: bool = False
    links_found: int = 0
    extraction_status: str | None = None
    indexed: bool | None = None
    elapsed_ms: float = 0.0
    error: str | None = None


# ─── HTML metadata helpers ───────────────────────────────────────────────

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LANG_RE = re.compile(r"<html[^>]*\slang\s*=\s*[\"']?([a-zA-Z-]+)", re.IGNORECASE)
_CHARSET_RE = re.compile(r"charset=([a-zA-Z0-9_-]+)")
_VI_CHARS = re.compile(
    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)


def _decode(content: bytes, headers: dict[str, str]) -> str:
    charset = "utf-8"
    match = _CHARSET_RE.search(headers.get("content-type", ""))
    if match:
        charset = match.group(1)
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _extract_title(text: str) -> str:
    match = _TITLE_RE.search(text)
    if not match:
        return ""
    return " ".join(html_mod.unescape(match.group(1)).split())


def _detect_language(text: str) -> str | None:
    match = _LANG_RE.search(text)
    if match:
        return match.group(1).lower().split("-", 1)[0]
    if len(_VI_CHARS.findall(text[:8000])) >= 5:
        return "vi"
    return None


# ─── Pipeline ────────────────────────────────────────────────────────────


class CrawlPipeline:
    """Orchestrates one claimed frontier task through the full crawl."""

    def __init__(
        self,
        *,
        frontier,
        object_store,
        robots,
        limiter,
        fetcher,
        pool=None,
        allowed_domains: set[str] | frozenset[str] | None = None,
        extraction=None,
        indexer=None,
    ) -> None:
        self._frontier = frontier
        self._object_store = object_store
        self._robots = robots
        self._limiter = limiter
        self._fetcher = fetcher
        self._pool = pool
        self._allowed = set(allowed_domains) if allowed_domains else set(SEED_DOMAINS)
        # Phase 3: extraction → indexing. ``extraction`` is an
        # ExtractionService (None = store snapshots only); ``indexer``
        # is an async callable IndexingTask → report dict (None = don't
        # push extracted text into OpenSearch/Qdrant).
        self._extraction = extraction
        self._indexer = indexer

    @property
    def frontier(self):
        """The FreshnessWorker queue this pipeline drains."""
        return self._frontier

    async def process_batch(self, tasks: list[RecrawlTask]) -> list[CrawlOutcome]:
        """Process a popped batch; per-task errors are isolated inside."""
        return await asyncio.gather(*(self.process_one(t) for t in tasks))

    async def process_one(self, task: RecrawlTask) -> CrawlOutcome:
        started = time.monotonic()
        try:
            return await self._process(task, started)
        except Exception as exc:  # noqa: BLE001 — never sink a batch
            logger.warning("crawl failed for %s: %r", task.url, exc)
            try:
                await self._frontier.fail(task.url, claim_token=task.claim_token or None)
            except Exception as fail_exc:  # noqa: BLE001
                logger.debug("frontier.fail raised for %s: %r", task.url, fail_exc)
            return CrawlOutcome(
                url=task.url,
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=_ms(started),
            )

    async def _pool_or_none(self):
        if self._pool is not None:
            return self._pool
        from storage import pg_client

        return await pg_client.get_pool()

    async def _frontier_row(self, pool, url: str) -> dict:
        if pool is None:
            return {}
        try:
            rows = await pool.fetch(_FRONTIER_ROW_SQL, url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("frontier row read failed for %s: %r", url, exc)
            return {}
        return dict(rows[0]) if rows else {}

    async def _process(self, task: RecrawlTask, started: float) -> CrawlOutcome:
        url = task.url
        token = task.claim_token or None
        pool = await self._pool_or_none()
        row = await self._frontier_row(pool, url)
        depth = int(row.get("depth") or 0)
        old_change_rate = row.get("change_rate")
        origin_host = urlsplit(url).netloc.lower()

        def _lost() -> CrawlOutcome:
            return CrawlOutcome(url=url, outcome="lost_claim", elapsed_ms=_ms(started))

        # 1 ── robots gate: explicit disallow parks the URL as done; a
        # transient unavailable robots.txt reschedules instead (the URL
        # must not be lost to a 429/5xx/network blip — RFC 9309 §2.3.1.4).
        verdict = await self._robots.check(url)
        if verdict == VERDICT_DISALLOWED:
            if not await self._mark_robots_blocked(pool, url, token):
                return _lost()
            if not await self._frontier.complete(url, claim_token=token):
                return _lost()
            return CrawlOutcome(url=url, outcome="skipped_robots", elapsed_ms=_ms(started))
        if verdict == VERDICT_UNAVAILABLE:
            ok = await self._frontier.complete(
                url,
                next_crawl_at=time.time() + ROBOTS_UNAVAILABLE_RETRY_S,
                claim_token=token,
            )
            if not ok:
                return _lost()
            return CrawlOutcome(url=url, outcome="robots_unavailable", elapsed_ms=_ms(started))

        # 2 ── per-hop egress policy (F3): robots + politeness must hold
        # BEFORE each request — origin hop included — and not merely
        # after the body has already landed. Politeness runs for EVERY
        # hop (G3): a same-host redirect chain or an A→B→A roundtrip pays
        # a fresh wait instead of riding the earlier reservation, and a
        # refused hop never gets waited on at all. The fetcher aborts the
        # chain on a refusal and reports the reason via FetchResult.error.
        politeness_done: set[str] = set()

        async def _hop_policy(target: str) -> str | None:
            verdict = await self._robots.check(target)
            if verdict == VERDICT_DISALLOWED:
                return "skipped_robots"
            if verdict == VERDICT_UNAVAILABLE:
                return "robots_unavailable"
            host = urlsplit(target).netloc.lower()
            if host:
                politeness_done.add(host)
                await self._limiter.wait(host, await self._robots.crawl_delay(target))
            return None

        # 3 ── fetch (conditional on stored validators) ───────────────────
        res = await self._fetcher.fetch(
            url,
            etag=row.get("etag"),
            last_modified=row.get("last_modified"),
            policy_check=_hop_policy,
        )

        # 4 ── 304: content stands, only the recrawl clock moves ─────────
        if res.not_modified:
            cr = _ema(old_change_rate, 0.0)
            ok = await self._frontier.complete(
                url,
                next_crawl_at=time.time() + recrawl_interval(cr),
                change_rate=cr,
                claim_token=token,
            )
            if not ok:
                return _lost()
            return CrawlOutcome(
                url=url, outcome="not_modified", status=304, elapsed_ms=_ms(started)
            )

        canonical = canonical_url(res.final_url or url) or canonical_url(url) or url

        # 5 ── HTTP-level outcomes ────────────────────────────────────────
        if not res.ok:
            # A hop-level policy refusal (F3) lands here via error — the
            # destination was never contacted.
            if res.error == "skipped_robots":
                ok = await self._frontier.complete(url, claim_token=token)
                if not ok:
                    return _lost()
                return CrawlOutcome(url=url, outcome="skipped_robots", elapsed_ms=_ms(started))
            if res.error in ("robots_unavailable", "politeness"):
                ok = await self._frontier.complete(
                    url,
                    next_crawl_at=time.time() + ROBOTS_UNAVAILABLE_RETRY_S,
                    claim_token=token,
                )
                if not ok:
                    return _lost()
                return CrawlOutcome(url=url, outcome=res.error, elapsed_ms=_ms(started))
            if res.status == 410:
                if not await self._set_doc_status(pool, canonical, "gone", url=url, token=token):
                    return _lost()
                ok = await self._frontier.complete(url, claim_token=token)
                if not ok:
                    return _lost()
                return CrawlOutcome(url=url, outcome="gone", status=410, elapsed_ms=_ms(started))
            if res.status in (401, 403):
                if not await self._set_doc_status(pool, canonical, "blocked", url=url, token=token):
                    return _lost()
                ok = await self._frontier.complete(
                    url,
                    next_crawl_at=time.time() + BLOCKED_RECRAWL_S,
                    claim_token=token,
                )
                if not ok:
                    return _lost()
                return CrawlOutcome(
                    url=url,
                    outcome="blocked",
                    status=res.status,
                    elapsed_ms=_ms(started),
                )
            await self._frontier.fail(url, claim_token=token)
            return CrawlOutcome(
                url=url,
                outcome="error",
                status=res.status,
                error=res.error or f"HTTP {res.status}",
                elapsed_ms=_ms(started),
            )

        # 6 ── js_required: fetched fine but it's a JS shell and the
        # renderer fallback is disabled (G4) — park the URL ~30 d with a
        # note; no snapshot, no validators. The stub body is not a
        # document and must never reach the store or the index.
        if res.js_required:
            ok = await self._frontier.complete(
                url,
                next_crawl_at=time.time() + JS_REQUIRED_RECRAWL_S,
                claim_token=token,
            )
            if not ok:
                return _lost()
            return CrawlOutcome(
                url=url,
                outcome="js_required",
                status=res.status,
                error="renderer fallback disabled (CRAWLER_FIRECRAWL_FALLBACK=false)",
                elapsed_ms=_ms(started),
            )

        # 7 ── oversize: a capped body is not a document — no snapshot, no
        # validators; park it instead of silently storing a prefix (M8).
        if res.oversize:
            ok = await self._frontier.complete(
                url,
                next_crawl_at=time.time() + OVERSIZE_RECRAWL_S,
                claim_token=token,
            )
            if not ok:
                return _lost()
            return CrawlOutcome(
                url=url,
                outcome="oversize",
                status=res.status,
                error=f"body exceeds fetch cap ({res.via})",
                elapsed_ms=_ms(started),
            )

        # 8 ── cross-host redirect backstop: per-hop policy (F3) already
        # vets every hop before contact, but a fetcher that does not honor
        # the callback (or a renderer-side landing) still gets its final
        # origin's robots + politeness enforced before the body is kept (M9).
        domain = urlsplit(canonical).netloc.lower() or origin_host
        final_host = urlsplit(res.final_url or url).netloc.lower()
        if final_host and final_host != origin_host:
            fverdict = await self._robots.check(res.final_url)
            if fverdict == VERDICT_DISALLOWED:
                ok = await self._frontier.complete(url, claim_token=token)
                if not ok:
                    return _lost()
                return CrawlOutcome(url=url, outcome="skipped_robots", elapsed_ms=_ms(started))
            if fverdict == VERDICT_UNAVAILABLE:
                ok = await self._frontier.complete(
                    url,
                    next_crawl_at=time.time() + ROBOTS_UNAVAILABLE_RETRY_S,
                    claim_token=token,
                )
                if not ok:
                    return _lost()
                return CrawlOutcome(url=url, outcome="robots_unavailable", elapsed_ms=_ms(started))
            if final_host not in politeness_done:
                politeness_done.add(final_host)
                await self._limiter.wait(final_host, await self._robots.crawl_delay(res.final_url))

        # 9 ── snapshot + document bookkeeping. The claim is verified
        # before any durable write; document+snapshot then commit in one
        # transaction that re-checks the claim under FOR UPDATE (H3/H4).
        if not await self._claim_held(pool, url, token):
            return _lost()

        doc_id = "doc_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]
        text = _decode(res.content, res.headers) if res.content else ""
        is_html = res.mime == "text/html"
        norm_hash = (
            content_fingerprint(text)
            if (is_html or res.mime.startswith("text/") or res.mime == "text/markdown")
            else hashlib.sha256(res.content).hexdigest()
        )
        raw_hash = hashlib.sha256(res.content).hexdigest()
        # content-hash dir + full-uuid leaf: 122 random bits per snapshot
        # make key collision practically impossible (F4/M10). The put is
        # conditional (If-None-Match: *) — a 412 means another fetch owns
        # the key; mint a fresh one and retry once instead of overwriting.
        stored = False
        storage_key = ""
        for _attempt in range(2):
            storage_key = f"snapshots/{domain}/{norm_hash[:16]}/{uuid.uuid4().hex}.bin"
            try:
                stored = await self._object_store.put_raw(
                    res.content,
                    storage_key,
                    res.mime or "application/octet-stream",
                    if_none_match=True,
                )
            except CollisionError:
                stored = False
                continue
            break
        if not stored:
            # Nothing durable yet → fail the task; validators stay unset so
            # the next attempt re-downloads instead of 304-ing a ghost.
            await self._frontier.fail(url, claim_token=token)
            return CrawlOutcome(
                url=url,
                outcome="error",
                status=res.status,
                error="snapshot_store_failed",
                elapsed_ms=_ms(started),
            )

        changed = True
        needs_extraction = True
        snapshot_id = None
        if pool is not None:
            old_doc = await self._select_doc(pool, canonical)
            changed = old_doc is None or old_doc.get("content_hash") != norm_hash
            # Extract+index only when worth it: new/changed content, a doc
            # that predates extraction (NULL status), or a transient
            # 'error' worth one retry. An unchanged page with a
            # deterministic status (empty/low_*) is left alone — the
            # fingerprint already proved nothing moved.
            needs_extraction = (
                changed
                or old_doc is None
                or old_doc.get("extraction_status") in REEXTRACT_STATUSES | {None}
            )
            try:
                snapshot_id = await self._write_document(
                    pool,
                    url=url,
                    token=token,
                    doc_id=doc_id,
                    canonical=canonical,
                    domain=domain,
                    title=_extract_title(text),
                    norm_hash=norm_hash,
                    language=_detect_language(text),
                    res=res,
                    storage_key=storage_key,
                    raw_hash=raw_hash,
                )
            except _ClaimLost:
                return _lost()
            # SQL errors propagate → process_one fails the task (H3).

        # ── extraction → index (Phase 3). The raw snapshot is durable at
        # this point, so an extraction failure never loses the page — it
        # is re-attempted on a later recrawl. Unchanged docs skip the
        # whole stage (no re-extract, no re-embed, no reindex).
        extraction_status = None
        indexed = None
        if needs_extraction and self._extraction is not None:
            extraction_status, indexed = await self._extract_and_index(
                pool,
                url=url,
                token=token,
                res=res,
                text=text,
                doc_id=doc_id,
                canonical=canonical,
                domain=domain,
                snapshot_id=snapshot_id,
            )

        # 10 ── link discovery (DB-bound: dedup + depth live in the row) ──
        links_found = 0
        if is_html and depth < MAX_DEPTH and pool is not None:
            links_found = await self._discover(
                pool, text, res.final_url or url, url, depth, token=token
            )

        # 11 ── recrawl scheduling: validators saved only after the
        # document + snapshot are durable (H3).
        cr = _ema(old_change_rate, 1.0 if changed else 0.0)
        ok = await self._frontier.complete(
            url,
            next_crawl_at=time.time() + recrawl_interval(cr),
            etag=res.headers.get("etag"),
            last_modified=res.headers.get("last-modified"),
            change_rate=cr,
            claim_token=token,
        )
        if not ok:
            return _lost()
        return CrawlOutcome(
            url=url,
            outcome="changed" if changed else "unchanged",
            status=res.status,
            doc_id=doc_id,
            storage_key=storage_key,
            changed=changed,
            links_found=links_found,
            extraction_status=extraction_status,
            indexed=indexed,
            elapsed_ms=_ms(started),
        )

    # ── Postgres helpers ─────────────────────────────────────────────────

    @staticmethod
    def _row_claim(row) -> str | None:
        try:
            return row["claim_token"]
        except Exception:  # noqa: BLE001 — Record/dict without the column
            return None

    async def _claim_held(self, pool, url: str, token: str | None) -> bool:
        """True while the frontier row still carries our claim token.

        ``token=None`` (memory-frontier tasks) skips the check — there is
        no DB claim to lose. A missing row counts as lost.
        """
        if pool is None or token is None:
            return True
        try:
            rows = await pool.fetch(_CLAIM_CHECK_SQL, url)
        except Exception as exc:  # noqa: BLE001 — can't verify → don't mutate
            logger.debug("claim check failed for %s: %r", url, exc)
            return False
        return bool(rows) and self._row_claim(rows[0]) == token

    async def _mark_robots_blocked(self, pool, url: str, token: str | None) -> bool:
        """Flag the frontier row robots-blocked, guarded by the claim."""
        if pool is None:
            return True
        result = await pool.execute(_ROBOTS_BLOCK_SQL, url, token)
        return result.split()[-1] != "0"

    async def _select_doc(self, pool, canonical: str) -> dict | None:
        try:
            rows = await pool.fetch(_DOC_SELECT_SQL, canonical)
        except Exception as exc:  # noqa: BLE001
            logger.debug("documents read failed for %s: %r", canonical, exc)
            return None
        return dict(rows[0]) if rows else None

    async def _set_doc_status(
        self, pool, canonical: str, status: str, *, url: str, token: str | None
    ) -> bool:
        """Update document status only while the claim is held (F2).

        The claim check (``FOR UPDATE``) and the document UPDATE share
        one transaction: a stale worker whose frontier row was reclaimed
        gets a mismatch under the row lock and the status write never
        commits — it cannot mark the new claim's document gone/blocked.
        SQL errors propagate so the caller fails the task instead of
        pretending the status landed. Returns False only on claim loss.
        """
        if pool is None:
            return True
        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn, conn.transaction():
                return await self._doc_status_tx(conn, canonical, status, url=url, token=token)
        return await self._doc_status_tx(pool, canonical, status, url=url, token=token)

    async def _doc_status_tx(
        self, db, canonical: str, status: str, *, url: str, token: str | None
    ) -> bool:
        if token is not None:
            rows = await db.fetch(_CLAIM_LOCK_SQL, url)
            if not rows or self._row_claim(rows[0]) != token:
                return False  # nothing written — the tx commits empty
        await db.execute(_DOC_STATUS_SQL, canonical, status)
        return True

    async def _write_document(self, pool, *, url, token, **kw) -> int | None:
        """Persist source+document+snapshot atomically (H3c).

        Returns the new snapshot's id (extraction provenance links to it).
        Raises ``_ClaimLost`` when the frontier row changed hands, and lets
        SQL errors propagate — the caller fails the task rather than
        reporting success on a half-written document.
        """
        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn, conn.transaction():
                return await self._doc_tx(conn, url=url, token=token, **kw)
        return await self._doc_tx(pool, url=url, token=token, **kw)

    async def _doc_tx(
        self,
        db,
        *,
        url,
        token,
        doc_id,
        canonical,
        domain,
        title,
        norm_hash,
        language,
        res,
        storage_key,
        raw_hash,
    ) -> int | None:
        if token is not None:
            rows = await db.fetch(_CLAIM_LOCK_SQL, url)
            if not rows or self._row_claim(rows[0]) != token:
                raise _ClaimLost(url)
        metadata = json.dumps(
            {
                "via": res.via,
                "http_status": res.status,
                "mime": res.mime,
                "final_url": res.final_url,
                # Corpus vertical (P5-VN): seed-domain lane flows into the
                # own-index metadata so ranking/freshness treat a vbpl.vn
                # doc as legal and a cafef.vn doc as market.
                "source_lane": seed_lane(domain),
            }
        )
        await db.execute(_ENSURE_SOURCE_SQL, domain)
        await db.execute(
            _DOC_UPSERT_SQL,
            doc_id,
            canonical,
            domain,
            title or None,
            norm_hash,
            language,
            metadata,
        )
        snap_rows = await db.fetch(
            _SNAPSHOT_SQL,
            doc_id,
            storage_key,
            raw_hash,
            res.status,
            res.mime or None,
            json.dumps(res.headers),
        )
        snapshot_id = snap_rows[0]["snapshot_id"] if snap_rows else None
        if snapshot_id is not None:
            await db.execute(_DOC_SET_SNAPSHOT_SQL, doc_id, snapshot_id)
        return snapshot_id

    # ── Extraction → indexing (Phase 3) ────────────────────────────────
    #
    # Runs strictly after the raw snapshot is durable: an extraction or
    # indexing failure can never lose the page — the stored
    # extraction_status drives retry on a later recrawl. The pipeline
    # only orchestrates; extraction quality/dispatch lives in
    # ExtractionService, indexing mechanics in IndexingWorker.

    _RENDER_RETRY_MIMES = frozenset({"text/html", "application/xhtml+xml"})

    async def _extract_and_index(
        self,
        pool,
        *,
        url,
        token,
        res,
        text,
        doc_id,
        canonical,
        domain,
        snapshot_id,
    ) -> tuple[str | None, bool | None]:
        """Extract → persist → index for one fetched body.

        Returns (extraction_status, indexed). Indexing failures do NOT
        fail the crawl task — the document is durable, the status says
        'failed', and only that stage is retried.
        """
        result = await self._extraction.extract(
            url=res.final_url or url,
            mime=res.mime,
            content=text,
            snapshot_id=snapshot_id,
        )

        # Render ladder: static HTML that yielded nothing indexable gets
        # one Firecrawl render (markdown), then a re-extract. The rendered
        # body is persisted as its OWN document_snapshot first — extraction
        # provenance must point at the capture it actually read, never at
        # the static snapshot that produced nothing. Opt-in only — render()
        # returns None unless CRAWLER_FIRECRAWL_FALLBACK is on.
        if result.status in RETRYABLE_STATUSES and res.mime in self._RENDER_RETRY_MIMES:
            rendered = await self._try_render(res.final_url or url)
            if rendered is not None:
                try:
                    rendered_snap_id = await self._persist_rendered_snapshot(
                        pool,
                        url=url,
                        token=token,
                        doc_id=doc_id,
                        domain=domain,
                        res=rendered,
                    )
                except _ClaimLost:
                    return result.status, None
                retry = await self._extraction.extract(
                    url=rendered.final_url or url,
                    mime=rendered.mime,
                    content=_decode(rendered.content, rendered.headers),
                    snapshot_id=rendered_snap_id,
                )
                adopted = status_rank(retry.status) > status_rank(result.status)
                fallback = {
                    "fetch_method": "firecrawl_render",
                    "from_snapshot_id": snapshot_id,
                    "rendered_snapshot_id": rendered_snap_id,
                    "adopted": adopted,
                }
                if adopted:
                    retry.provenance["render_fallback"] = fallback
                    result = retry
                    if rendered_snap_id is not None:
                        try:
                            await self._set_current_snapshot(
                                pool,
                                url=url,
                                token=token,
                                doc_id=doc_id,
                                snapshot_id=rendered_snap_id,
                            )
                        except _ClaimLost:
                            return result.status, None
                else:
                    result.provenance["render_fallback"] = fallback

        if pool is not None:
            try:
                await self._write_extraction(
                    pool,
                    url=url,
                    token=token,
                    doc_id=doc_id,
                    snapshot_id=snapshot_id,
                    result=result,
                )
            except _ClaimLost:
                return result.status, None

        indexed = await self._index_extracted(canonical, doc_id, domain, result)
        if pool is not None and indexed is not None:
            with contextlib.suppress(_ClaimLost):
                await self._write_index_status(
                    pool,
                    url=url,
                    token=token,
                    doc_id=doc_id,
                    indexing_status=indexed.get("indexing_status", "failed"),
                    embedding_status=indexed.get("embedding_status", "failed"),
                )
        return result.status, (indexed.get("indexed") if indexed else None)

    async def _try_render(self, url):
        """One rendered re-fetch via the opt-in Firecrawl lane."""
        render = getattr(self._fetcher, "render", None)
        if render is None:
            return None
        try:
            rendered = await render(url)
        except Exception as exc:  # noqa: BLE001 — render failure keeps first result
            logger.debug("render retry failed for %s: %r", url, exc)
            return None
        if rendered is None or not rendered.ok or rendered.oversize:
            return None
        return rendered

    async def _persist_rendered_snapshot(
        self, pool, *, url, token, doc_id, domain, res
    ) -> int | None:
        """Store a rendered body as its own ``document_snapshots`` row.

        The render lane returns markdown — a real capture of what the
        renderer saw, held to the same durability contract as the
        original fetch (MinIO blob + snapshot row under the claim lock).
        Returns the new ``snapshot_id``; ``None`` means the body could
        not be made durable, so extraction provenance must not claim a
        snapshot row that does not exist.
        """
        if pool is None:
            return None
        raw_hash = hashlib.sha256(res.content).hexdigest()
        norm_hash = (
            content_fingerprint(_decode(res.content, res.headers))
            if res.mime.startswith("text/")
            else raw_hash
        )
        storage_key = f"snapshots/{domain}/{norm_hash[:16]}/{uuid.uuid4().hex}.bin"
        try:
            stored = await self._object_store.put_raw(
                res.content,
                storage_key,
                res.mime or "application/octet-stream",
                if_none_match=True,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, original stands
            logger.debug("rendered snapshot store failed for %s: %r", url, exc)
            return None
        if not stored:
            logger.debug("rendered snapshot store returned false for %s", url)
            return None

        async def _tx(db) -> int | None:
            if token is not None:
                rows = await db.fetch(_CLAIM_LOCK_SQL, url)
                if not rows or self._row_claim(rows[0]) != token:
                    raise _ClaimLost(url)
            snap_rows = await db.fetch(
                _SNAPSHOT_SQL,
                doc_id,
                storage_key,
                raw_hash,
                res.status,
                res.mime or None,
                json.dumps(res.headers),
            )
            return snap_rows[0]["snapshot_id"] if snap_rows else None

        try:
            if hasattr(pool, "acquire"):
                async with pool.acquire() as conn, conn.transaction():
                    return await _tx(conn)
            return await _tx(pool)
        except _ClaimLost:
            raise
        except Exception as exc:  # noqa: BLE001 — original snapshot stays truth
            logger.debug("rendered snapshot row failed for %s: %r", url, exc)
            return None

    async def _set_current_snapshot(self, pool, *, url, token, doc_id, snapshot_id) -> None:
        """Repoint ``documents.current_snapshot_id`` under the claim guard."""

        async def _tx(db) -> None:
            if token is not None:
                rows = await db.fetch(_CLAIM_LOCK_SQL, url)
                if not rows or self._row_claim(rows[0]) != token:
                    raise _ClaimLost(url)
            await db.execute(_DOC_SET_SNAPSHOT_SQL, doc_id, snapshot_id)

        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn, conn.transaction():
                await _tx(conn)
            return
        await _tx(pool)

    async def _index_extracted(
        self, canonical: str, doc_id: str, domain: str, result: ExtractionResult
    ) -> dict | None:
        """Push a successful extraction into the index. None = skipped."""
        if result.status != STATUS_SUCCESS or result.document is None:
            return None
        if self._indexer is None:
            return {"indexed": False, "indexing_status": "skipped", "embedding_status": "skipped"}
        doc = result.document
        task = IndexingTask(
            doc_id=doc_id,
            url=canonical,
            title=doc.title or "",
            text=doc.text,
            domain=domain,
            source_type="crawler",
            published_at=doc.published_at.isoformat() if doc.published_at else None,
            description=doc.description,
            site_name=doc.site_name,
            author=doc.author,
            language=doc.language,
        )
        try:
            return await self._indexer(task)
        except Exception as exc:  # noqa: BLE001 — index loss ≠ crawl loss
            logger.warning("indexing call failed for %s: %r", canonical, exc)
            return {
                "indexed": False,
                "indexing_status": "failed",
                "embedding_status": "failed",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }

    async def _write_extraction(self, pool, *, url, token, doc_id, snapshot_id, result) -> None:
        """Persist extraction columns + provenance, guarded by the claim."""
        doc = result.document
        metadata = json.dumps(
            {
                "provenance": result.provenance,
                "extraction": doc.metadata if doc else {},
            }
        )

        async def _tx(db) -> None:
            if token is not None:
                rows = await db.fetch(_CLAIM_LOCK_SQL, url)
                if not rows or self._row_claim(rows[0]) != token:
                    raise _ClaimLost(url)
            await db.execute(
                _DOC_EXTRACTION_SQL,
                doc_id,
                doc.text if doc else None,
                doc.description if doc else None,
                doc.author if doc else None,
                doc.published_at if doc else None,
                doc.site_name if doc else None,
                doc.language if doc else None,
                doc.word_count if doc else None,
                doc.quality_score if doc else None,
                doc.extraction_method if doc else None,
                doc.extraction_version if doc else None,
                result.status,
                metadata,
            )

        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn, conn.transaction():
                await _tx(conn)
            return
        await _tx(pool)

    async def _write_index_status(
        self, pool, *, url, token, doc_id, indexing_status, embedding_status
    ) -> None:
        """Persist the index/embedding outcome, guarded by the claim."""

        async def _tx(db) -> None:
            if token is not None:
                rows = await db.fetch(_CLAIM_LOCK_SQL, url)
                if not rows or self._row_claim(rows[0]) != token:
                    raise _ClaimLost(url)
            await db.execute(_DOC_INDEX_STATUS_SQL, doc_id, indexing_status, embedding_status)

        if hasattr(pool, "acquire"):
            async with pool.acquire() as conn, conn.transaction():
                await _tx(conn)
            return
        await _tx(pool)

    async def _discover(
        self,
        pool,
        text: str,
        base_url: str,
        source_url: str,
        depth: int,
        *,
        token: str | None = None,
    ) -> int:
        """Enqueue in-scope links at ``depth + 1``; returns count enqueued.

        The whole batch is one transaction holding the claim row
        ``FOR UPDATE`` (F2): the claim cannot change hands between the
        check and the last insert, and a mid-batch reclaim aborts with
        ``_ClaimLost`` → 0 instead of leaving a half-written frontier.
        A row-level SQL failure aborts the transaction and propagates —
        discovery is part of the task's durable writes.

        Deadlock handling (G2): reciprocal discovery (A links B while B
        links A) makes Postgres abort one tx with SQLSTATE 40P01. Two
        mitigations — links are upserted in sorted-URL order so
        concurrent txs take the same row locks in the same order, and a
        deadlocked tx retries a bounded ``_DISCOVER_MAX_ATTEMPTS`` times
        with small backoff instead of failing the task.
        """
        new_depth = depth + 1
        priority = max(0.05, 1.0 - 0.25 * new_depth)
        links: list[tuple[str, str]] = []
        for link in extract_links(text, base_url):
            if len(links) >= MAX_LINKS_PER_PAGE:
                break
            canonical = canonical_url(link)
            if not canonical:
                continue
            host = urlsplit(canonical).netloc
            if registrable_domain(host) not in self._allowed:
                continue
            links.append((canonical, host))
        if not links:
            return 0
        links.sort()  # (canonical, host) — consistent row-lock order (G2)
        for attempt in range(_DISCOVER_MAX_ATTEMPTS):
            try:
                if hasattr(pool, "acquire"):
                    async with pool.acquire() as conn, conn.transaction():
                        return await self._discover_tx(
                            conn,
                            links,
                            source_url=source_url,
                            new_depth=new_depth,
                            priority=priority,
                            token=token,
                        )
                return await self._discover_tx(
                    pool,
                    links,
                    source_url=source_url,
                    new_depth=new_depth,
                    priority=priority,
                    token=token,
                )
            except _ClaimLost:
                return 0
            except Exception as exc:  # noqa: BLE001 — only 40P01 retries
                if (
                    getattr(exc, "sqlstate", None) != _DEADLOCK_SQLSTATE
                    or attempt + 1 >= _DISCOVER_MAX_ATTEMPTS
                ):
                    raise
                await asyncio.sleep(_DEADLOCK_BACKOFF_S * (attempt + 1))

    async def _discover_tx(
        self, db, links: list[tuple[str, str]], *, source_url, new_depth, priority, token
    ) -> int:
        if token is not None:
            rows = await db.fetch(_CLAIM_LOCK_SQL, source_url)
            if not rows or self._row_claim(rows[0]) != token:
                raise _ClaimLost(source_url)
        for canonical, host in links:
            await db.execute(
                _DISCOVER_SQL, canonical, canonical, host, priority, source_url, new_depth
            )
        return len(links)


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0
