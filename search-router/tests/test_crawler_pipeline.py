"""Tests for crawler/pipeline.py — the crawl orchestrator.

Everything below the pipeline is faked: a recording ``FakeFrontier`` for
enqueue/complete/fail, a stateful ``PipePool`` emulating the documents /
document_snapshots / crawl_frontier semantics of the module-level SQL, and
stub robots/limiter/fetcher/object_store. No DB, MinIO, or network needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

from canonical.content import content_fingerprint
from crawler import pipeline as pl
from crawler.fetcher import FetchResult
from crawler.pipeline import CrawlOutcome, CrawlPipeline, extract_links
from storage.object_store import CollisionError
from workers.freshness_worker import RecrawlTask

HTML = (
    "<html><head><title>Bao chinh phu</title></head><body>"
    "<p>Thủ tướng chủ trì phiên họp. Nội dung dài đủ để không bị coi là trang JS.</p>"
    '<a href="/tin-tuc/bai-1">Bai 1</a>'
    '<a href="https://chinhphu.vn/tin-tuc/bai-2">Bai 2</a>'
    '<a href="https://sub.chinhphu.vn/deep">Subdomain</a>'
    '<a href="https://other-site.vn/x">External</a>'
    '<a href="javascript:void(0)">JS</a>'
    '<a href="mailto:a@b.vn">Mail</a>'
    '<a href="#frag">Frag</a>'
    "</body></html>"
).encode()


def _html(*hrefs: str, body: bytes = b"<p>Content day du de vuot nguong JS.</p>") -> bytes:
    links = b"".join(f'<a href="{h}">x</a>'.encode() for h in hrefs)
    return b"<html><head><title>t</title></head><body>" + body + links + b"</body></html>"


# ─── Fakes ───────────────────────────────────────────────────────────────


class FakeFrontier:
    def __init__(self, complete_ok=True):
        self.completions: list[dict] = []
        self.failures: list[tuple] = []
        self.enqueued: list = []
        self.complete_ok = complete_ok

    async def complete(self, url, next_crawl_at=None, **kw):
        self.completions.append({"url": url, "next_crawl_at": next_crawl_at, **kw})
        return self.complete_ok

    async def fail(self, url, claim_token=None):
        self.failures.append((url, claim_token))
        return True

    async def enqueue(self, task):
        self.enqueued.append(task)
        return "db"


class FakeStore:
    def __init__(self, fail=False):
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.fail = fail

    async def put_raw(
        self, data, storage_key, content_type="application/octet-stream", *, if_none_match=False
    ):
        self.put_keys.append(storage_key)
        if self.fail:
            return False
        if if_none_match and storage_key in self.objects:
            raise CollisionError(storage_key)
        self.objects[storage_key] = data
        return True


class CollideOnceStore(FakeStore):
    """First put reports a 412-style collision; later puts succeed."""

    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def put_raw(
        self, data, storage_key, content_type="application/octet-stream", *, if_none_match=False
    ):
        self.attempts += 1
        self.put_keys.append(storage_key)
        if self.attempts == 1:
            raise CollisionError(storage_key)
        if if_none_match and storage_key in self.objects:
            raise CollisionError(storage_key)
        self.objects[storage_key] = data
        return True


class FakeRobots:
    """Tri-state verdicts per host: "allowed"|"disallowed"|"unavailable".

    ``allowed=False`` maps to an explicit ``disallowed`` verdict. Extra
    per-host verdicts can be seeded via ``verdicts``.
    """

    def __init__(self, allowed=True, delay=None, verdicts=None):
        self._verdict = "allowed" if allowed else "disallowed"
        self._verdicts = verdicts or {}
        self._delay = delay
        self.checked: list[str] = []

    def _v(self, url):
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return self._verdicts.get(host, self._verdict)

    async def check(self, url):
        self.checked.append(url)
        return self._v(url)

    async def allowed(self, url):
        return await self.check(url) == "allowed"

    async def crawl_delay(self, url):
        return self._delay


class FakeLimiter:
    def __init__(self):
        self.waits: list[tuple] = []

    async def wait(self, domain, crawl_delay=None):
        self.waits.append((domain, crawl_delay))


class FakeFetcher:
    def __init__(self, result: FetchResult | Exception):
        self.result = result
        self.calls: list[dict] = []

    async def fetch(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class HopFetcher:
    """Fetch stub that walks a redirect chain honoring ``policy_check``.

    Mirrors ``guarded_get``: the callback runs before *every* hop's
    request (origin included); a refusal aborts the chain — the refused
    URL is never "requested" — and surfaces via ``FetchResult.error``.
    """

    def __init__(self, hops: list[str], result: FetchResult | None = None):
        self.hops = hops
        self.result = result
        self.requested: list[str] = []

    async def fetch(self, url, policy_check=None, **kw):
        targets = [url, *self.hops]
        for target in targets:
            if policy_check is not None:
                refusal = await policy_check(target)
                if refusal is not None:
                    return FetchResult(ok=False, error=refusal, final_url=target)
            self.requested.append(target)
        return self.result or _ok_result(targets[-1])


class PipePool:
    """Stateful emulation of the pipeline's Postgres writes.

    Frontier rows default ``claim_token`` to the ``_task`` default so the
    claim guards see a held claim; set an explicit token to simulate a
    reclaimed row. ``fail_sqls`` injects SQL errors (H3 paths), and
    ``acquire()``/``transaction()`` are no-op context managers matching
    asyncpg's call shape.
    """

    def __init__(self, frontier_rows=None, documents=None, fail_sqls=()):
        self.frontier = {}
        for r in frontier_rows or []:
            row = dict(r)
            row.setdefault("claim_token", "tok-1")
            self.frontier[row["url"]] = row
        self.documents = {d["canonical_url"]: dict(d) for d in (documents or [])}
        self.fail_sqls = set(fail_sqls)
        self.snapshots: list[tuple] = []
        self._snapshot_seq = 0
        self.sources: set[str] = set()
        self.discovered: list[tuple] = []
        self.robots_blocked: list[str] = []
        self.calls: list[tuple] = []

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False

        return _Acquire()

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if sql in self.fail_sqls:
            raise RuntimeError(f"injected failure: {sql[:40]}")
        if sql == pl._FRONTIER_ROW_SQL:
            row = self.frontier.get(args[0])
            return [row] if row else []
        if sql == pl._DOC_SELECT_SQL:
            row = self.documents.get(args[0])
            return [row] if row else []
        if sql in (pl._CLAIM_CHECK_SQL, pl._CLAIM_LOCK_SQL):
            row = self.frontier.get(args[0])
            return [{"claim_token": row["claim_token"]}] if row else []
        if sql == pl._SNAPSHOT_SQL:
            self.snapshots.append(args)
            self._snapshot_seq += 1
            return [{"snapshot_id": self._snapshot_seq}]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql in self.fail_sqls:
            raise RuntimeError(f"injected failure: {sql[:40]}")
        if sql == pl._ROBOTS_BLOCK_SQL:
            row = self.frontier.get(args[0])
            if row is None or row["claim_token"] != args[1]:
                return "UPDATE 0"
            row["robots_allowed"] = False
            self.robots_blocked.append(args[0])
            return "UPDATE 1"
        if sql == pl._ENSURE_SOURCE_SQL:
            self.sources.add(args[0])
            return "INSERT 0 1"
        if sql == pl._DOC_UPSERT_SQL:
            doc_id, canonical, domain, title, chash, lang = args[:6]
            self.documents[canonical] = {
                "doc_id": doc_id,
                "canonical_url": canonical,
                "domain": domain,
                "title": title,
                "content_hash": chash,
                "language": lang,
                "status": "active",
            }
            return "INSERT 0 1"
        if sql == pl._DOC_SET_SNAPSHOT_SQL:
            for doc in self.documents.values():
                if doc["doc_id"] == args[0]:
                    doc["current_snapshot_id"] = args[1]
            return "UPDATE 1"
        if sql == pl._DOC_EXTRACTION_SQL:
            for doc in self.documents.values():
                if doc["doc_id"] == args[0]:
                    doc.update(
                        {
                            "main_text": args[1],
                            "extraction_status": args[11],
                            "extraction_method": args[9],
                            "word_count": args[7],
                            "quality_score": args[8],
                        }
                    )
            return "UPDATE 1"
        if sql == pl._DOC_INDEX_STATUS_SQL:
            for doc in self.documents.values():
                if doc["doc_id"] == args[0]:
                    doc["indexing_status"] = args[1]
                    doc["embedding_status"] = args[2]
            return "UPDATE 1"
        if sql == pl._DOC_STATUS_SQL:
            self.documents.setdefault(args[0], {})["status"] = args[1]
            return "UPDATE 1"
        if sql == pl._DISCOVER_SQL:
            self.discovered.append(args)
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql}")


class RaisingFailFrontier(FakeFrontier):
    async def fail(self, url, claim_token=None):
        raise RuntimeError("frontier down")


def _task(url: str, **kw) -> RecrawlTask:
    kw.setdefault("claim_token", "tok-1")
    return RecrawlTask(url=url, priority=0.5, scheduled_at=0.0, **kw)


def _ok_result(url: str, content: bytes = HTML, **kw) -> FetchResult:
    return FetchResult(
        ok=True,
        status=200,
        headers={"etag": '"e1"', "last-modified": "Mon, 21 Sep 2026 00:00:00 GMT"},
        content=content,
        mime="text/html",
        final_url=url,
        via="http",
        **kw,
    )


def _pipeline(
    *,
    fetch_result: FetchResult | Exception = None,
    robots_allowed: bool = True,
    robots_verdicts=None,
    store: FakeStore | None = None,
    pool: PipePool | None = None,
    frontier: FakeFrontier | None = None,
    allowed_domains=None,
):
    frontier = frontier or FakeFrontier()
    store = store or FakeStore()
    robots = FakeRobots(allowed=robots_allowed, verdicts=robots_verdicts)
    limiter = FakeLimiter()
    fetcher = FakeFetcher(fetch_result if fetch_result is not None else _ok_result("https://x.vn"))
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=robots,
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains=allowed_domains or {"chinhphu.vn"},
    )
    return pipe, frontier, store, robots, limiter, fetcher


def _run(coro):
    return asyncio.run(coro)


# ─── robots gate ─────────────────────────────────────────────────────────


def test_robots_disallowed_skips_fetch():
    pipe, frontier, store, robots, limiter, fetcher = _pipeline(robots_allowed=False)
    url = "https://chinhphu.vn/admin"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe._pool = pool

    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "skipped_robots"
    assert fetcher.calls == []
    assert store.objects == {}
    assert pool.robots_blocked == [url]
    # completed as done — no recrawl scheduled.
    assert frontier.completions[0]["next_crawl_at"] is None
    assert frontier.completions[0]["claim_token"] == "tok-1"


# ─── politeness ──────────────────────────────────────────────────────────


def test_limiter_waited_with_domain_and_delay():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    # Politeness is driven by the per-hop policy callback (G3) — use a
    # fetcher that honors it so the origin's wait is exercised.
    fetcher = HopFetcher([], result=_ok_result(url))
    limiter = FakeLimiter()
    robots = FakeRobots(delay=3.0)
    pipe = CrawlPipeline(
        frontier=FakeFrontier(),
        object_store=FakeStore(),
        robots=robots,
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn"},
    )
    _run(pipe.process_one(_task(url)))
    assert limiter.waits == [("chinhphu.vn", 3.0)]


# ─── 304 / conditional ───────────────────────────────────────────────────


def test_conditional_headers_from_frontier_row():
    url = "https://chinhphu.vn/"
    pool = PipePool(
        frontier_rows=[
            {"url": url, "depth": 0, "etag": '"old"', "last_modified": "Mon, 01 Sep 2026"}
        ]
    )
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    fetch_kwargs = pipe._fetcher.calls[0]
    assert fetch_kwargs["etag"] == '"old"'
    assert fetch_kwargs["last_modified"] == "Mon, 01 Sep 2026"


def test_304_not_modified():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(ok=True, status=304, not_modified=True, content=b"", final_url=url)
    pipe, frontier, store, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "not_modified"
    assert store.objects == {}
    comp = frontier.completions[0]
    assert comp["next_crawl_at"] is not None  # recrawl stays scheduled


# ─── success path ────────────────────────────────────────────────────────


def test_fetch_success_snapshots_and_upserts():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, store, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))

    assert outcome.outcome == "changed"  # first fetch → no previous hash
    assert outcome.doc_id.startswith("doc_")

    # MinIO raw snapshot written under the content-hash/uuid key layout.
    assert len(store.objects) == 1
    key = next(iter(store.objects))
    domain = "chinhphu.vn"
    norm_hash = content_fingerprint(HTML.decode())
    assert key.startswith(f"snapshots/{domain}/{norm_hash[:16]}/")
    assert store.objects[key] == HTML

    # documents upsert + snapshot row.
    canonical = "https://chinhphu.vn"
    assert canonical in pool.documents
    assert pool.documents[canonical]["content_hash"] == content_fingerprint(HTML.decode())
    assert pool.documents[canonical]["status"] == "active"
    assert pool.documents[canonical]["title"] == "Bao chinh phu"
    assert len(pool.snapshots) == 1
    snap = pool.snapshots[0]
    assert snap[1] == key  # storage_key
    assert snap[2] == hashlib.sha256(HTML).hexdigest()  # raw bytes hash
    assert snap[3] == 200  # http_status

    # source ensured + frontier completed with recrawl + validators stored.
    assert "chinhphu.vn" in pool.sources
    comp = frontier.completions[0]
    assert comp["next_crawl_at"] is not None
    assert comp["etag"] == '"e1"'
    assert comp["last_modified"] == "Mon, 21 Sep 2026 00:00:00 GMT"
    assert comp["claim_token"] == "tok-1"


def test_doc_metadata_carries_seed_lane():
    # P4 corpus: seed-domain docs get their vertical in metadata.
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    upserts = [args for sql, args in pool.calls if sql == pl._DOC_UPSERT_SQL]
    assert len(upserts) == 1
    meta = json.loads(upserts[0][6])
    assert meta["source_lane"] == "government"


def test_doc_metadata_lane_none_for_unseeded_domain():
    url = "https://unseeded.example/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(
        fetch_result=_ok_result(url),
        pool=pool,
        allowed_domains={"unseeded.example"},
    )
    _run(pipe.process_one(_task(url)))
    upserts = [args for sql, args in pool.calls if sql == pl._DOC_UPSERT_SQL]
    meta = json.loads(upserts[0][6])
    assert meta["source_lane"] is None


def test_unchanged_when_hash_matches():
    url = "https://chinhphu.vn/"
    canonical = "https://chinhphu.vn"
    known_hash = content_fingerprint(HTML.decode())
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_x", "content_hash": known_hash}],
    )
    pipe, frontier, store, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "unchanged"
    assert outcome.changed is False
    # Immutable history: snapshot still recorded even without a change.
    assert len(store.objects) == 1
    assert len(pool.snapshots) == 1


def test_language_detected_vi():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    assert pool.documents["https://chinhphu.vn"]["language"] == "vi"


# ─── link discovery ──────────────────────────────────────────────────────


def test_link_discovery_scoped_and_enqueued():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))

    discovered_urls = [d[0] for d in pool.discovered]
    # in-scope: same domain + subdomain; out: other-site.vn, javascript:, mailto:, #
    assert "https://chinhphu.vn/tin-tuc/bai-1" in discovered_urls
    assert "https://chinhphu.vn/tin-tuc/bai-2" in discovered_urls
    assert "https://sub.chinhphu.vn/deep" in discovered_urls
    assert not any("other-site.vn" in u for u in discovered_urls)
    assert not any(u.startswith("javascript:") or u.startswith("mailto:") for u in discovered_urls)
    # depth + priority bookkeeping — _DISCOVER_SQL params:
    # (url, canonical_url, domain, priority, discovered_from, depth).
    for row in pool.discovered:
        assert row[4] == url  # discovered_from
        assert row[5] == 1  # depth = parent 0 + 1
        assert 0 < row[3] < 1.0  # priority decays below seed 1.0
    assert outcome.links_found == len(discovered_urls)


def test_no_discovery_at_max_depth():
    url = "https://chinhphu.vn/deep/page"
    pool = PipePool(frontier_rows=[{"url": url, "depth": pl.MAX_DEPTH}])
    pipe, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.links_found == 0
    assert pool.discovered == []


def test_link_cap_50():
    hrefs = [f"https://chinhphu.vn/p{i}" for i in range(80)]
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(fetch_result=_ok_result(url, content=_html(*hrefs)), pool=pool)
    _run(pipe.process_one(_task(url)))
    assert len(pool.discovered) <= 50


def test_no_discovery_for_non_html():
    url = "https://chinhphu.vn/doc.pdf"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(
        ok=True, status=200, headers={}, content=b"%PDF-1.4", mime="application/pdf", final_url=url
    )
    pipe, *_ = _pipeline(fetch_result=result, pool=pool)
    _run(pipe.process_one(_task(url)))
    assert pool.discovered == []


# ─── failure paths ───────────────────────────────────────────────────────


def test_fetch_5xx_fails_with_backoff():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(ok=False, status=500, content=b"", final_url=url, error="HTTP 500")
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    assert frontier.completions == []


def test_410_marks_gone_no_recrawl():
    url = "https://chinhphu.vn/old"
    canonical = "https://chinhphu.vn/old"
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_9", "status": "active"}],
    )
    result = FetchResult(ok=False, status=410, content=b"", final_url=url)
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "gone"
    assert pool.documents[canonical]["status"] == "gone"
    assert frontier.completions[0]["next_crawl_at"] is None  # done
    assert frontier.failures == []


def test_403_blocked_slow_recrawl():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(ok=False, status=403, content=b"", final_url=url)
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    before = time.time()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "blocked"
    nca = frontier.completions[0]["next_crawl_at"]
    assert nca >= before + 29 * 86400  # ~30 days


def test_fetcher_exception_isolated():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, *_ = _pipeline(fetch_result=RuntimeError("kaboom"), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert "kaboom" in (outcome.error or "")
    assert frontier.failures == [(url, "tok-1")]


def test_batch_error_isolation():
    # One URL exploding must not sink the rest of the batch.
    good = "https://chinhphu.vn/good"
    bad = "https://chinhphu.vn/bad"
    pool = PipePool(frontier_rows=[{"url": good, "depth": 0}, {"url": bad, "depth": 0}])
    frontier = FakeFrontier()
    store = FakeStore()

    class _PickFetcher:
        async def fetch(self, url, **kw):
            if "bad" in url:
                raise RuntimeError("boom")
            return _ok_result(url)

    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=FakeRobots(),
        limiter=FakeLimiter(),
        fetcher=_PickFetcher(),
        pool=pool,
        allowed_domains={"chinhphu.vn"},
    )
    outcomes = _run(pipe.process_batch([_task(good), _task(bad)]))
    by_url = {o.url: o.outcome for o in outcomes}
    assert by_url[good] == "changed"
    assert by_url[bad] == "error"


# ─── change_rate EMA ─────────────────────────────────────────────────────


def test_change_rate_ema_decay_on_unchanged():
    url = "https://chinhphu.vn/"
    canonical = "https://chinhphu.vn"
    known_hash = content_fingerprint(HTML.decode())
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0, "change_rate": 1.0}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_x", "content_hash": known_hash}],
    )
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    new_rate = frontier.completions[0]["change_rate"]
    assert new_rate is not None and new_rate < 1.0


def test_recrawl_interval_within_bounds():
    assert pl.recrawl_interval(0.0) == pl.MAX_RECRAWL_S
    assert pl.recrawl_interval(1.0) == pl.MIN_RECRAWL_S
    mid = pl.recrawl_interval(0.5)
    assert pl.MIN_RECRAWL_S < mid < pl.MAX_RECRAWL_S


# ─── extract_links unit ──────────────────────────────────────────────────


def test_extract_links_absolutizes_and_filters():
    html = _html(
        "/rel/path",
        "https://chinhphu.vn/abs",
        "//chinhphu.vn/protocol-rel",
        "javascript:x()",
        "mailto:a@b.vn",
        "#top",
        "tel:+84123",
        "",
    ).decode()
    links = extract_links(html, "https://chinhphu.vn/dir/page")
    assert "https://chinhphu.vn/rel/path" in links
    assert "https://chinhphu.vn/abs" in links
    assert not any(lnk.startswith(("javascript:", "mailto:", "tel:", "#")) for lnk in links)
    # fragments stripped, dedup applied
    assert len(links) == len(set(links))


def test_extract_links_strips_fragments():
    html = _html("https://chinhphu.vn/a#sec", "https://chinhphu.vn/a").decode()
    links = extract_links(html, "https://chinhphu.vn/")
    assert links.count("https://chinhphu.vn/a") == 1


def test_extract_links_respects_base_href():
    html = (
        '<html><head><base href="https://chinhphu.vn/section/"></head>'
        '<body><a href="p1">x</a></body></html>'
    )
    links = extract_links(html, "https://chinhphu.vn/other")
    assert "https://chinhphu.vn/section/p1" in links


def test_outcome_defaults():
    o = CrawlOutcome(url="u", outcome="changed")
    assert o.status == 0 and o.error is None and o.links_found == 0


# ─── H2: robots unavailable ≠ disallowed ─────────────────────────────────


def test_robots_unavailable_reschedules_not_done():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, store, robots, limiter, fetcher = _pipeline(
        robots_verdicts={"chinhphu.vn": "unavailable"}, pool=pool
    )
    before = time.time()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "robots_unavailable"
    assert fetcher.calls == []  # URL never fetched
    assert store.objects == {}
    assert pool.robots_blocked == []  # not an explicit disallow
    comp = frontier.completions[0]
    # Requeued ~15 min out — still 'queued', not 'done'.
    assert before + pl.ROBOTS_UNAVAILABLE_RETRY_S - 5 <= comp["next_crawl_at"]
    assert comp["next_crawl_at"] <= before + pl.ROBOTS_UNAVAILABLE_RETRY_S + 5
    assert frontier.failures == []


def test_robots_unavailable_twice_still_queued():
    # Repeated robots 5xx must not permanently park the URL.
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, *_ = _pipeline(robots_verdicts={"chinhphu.vn": "unavailable"}, pool=pool)
    for _ in range(2):
        outcome = _run(pipe.process_one(_task(url)))
        assert outcome.outcome == "robots_unavailable"
    assert len(frontier.completions) == 2
    assert all(c["next_crawl_at"] is not None for c in frontier.completions)


def test_robots_explicit_disallow_is_done():
    url = "https://chinhphu.vn/admin"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, *_ = _pipeline(robots_verdicts={"chinhphu.vn": "disallowed"}, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "skipped_robots"
    assert frontier.completions[0]["next_crawl_at"] is None  # done forever
    assert pool.robots_blocked == [url]


# ─── H3: persistence failures fail the task, never fake success ──────────


def test_minio_failure_fails_task_no_validators():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, frontier, store, *_ = _pipeline(
        fetch_result=_ok_result(url), store=FakeStore(fail=True), pool=pool
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert outcome.error == "snapshot_store_failed"
    assert frontier.failures == [(url, "tok-1")]
    # No completion → no etag/last_modified stored, no document/snapshot row.
    assert frontier.completions == []
    assert pool.documents == {}
    assert pool.snapshots == []


def test_doc_upsert_failure_fails_task():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}], fail_sqls={pl._DOC_UPSERT_SQL})
    pipe, frontier, store, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    assert frontier.completions == []  # validators not saved after failure


def test_snapshot_insert_failure_fails_task():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}], fail_sqls={pl._SNAPSHOT_SQL})
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    assert frontier.completions == []


def test_complete_return_false_means_lost_claim():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    frontier = FakeFrontier(complete_ok=False)
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool, frontier=frontier)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "lost_claim"  # no crash, claim loss reported


# ─── H4: claim token guards every mutation ───────────────────────────────


def test_stale_claim_does_not_mutate():
    # A reclaimed row (claim_token rotated by the newer pop) must not be
    # overwritten by a stale worker still holding the old token.
    url = "https://chinhphu.vn/"
    canonical = "https://chinhphu.vn"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0, "claim_token": "tok-new"}])
    store = FakeStore()

    # The newer claim writes its document first.
    fresh_pipe, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool, store=store)
    fresh = _run(fresh_pipe.process_one(_task(url, claim_token="tok-new")))
    assert fresh.outcome == "changed"
    new_doc = dict(pool.documents[canonical])
    snapshots_after = len(pool.snapshots)
    objects_after = len(store.objects)

    # The stale claim arrives late — nothing may be overwritten.
    stale_pipe, frontier, *_ = _pipeline(
        fetch_result=_ok_result(url, content=_html()), pool=pool, store=store
    )
    outcome = _run(stale_pipe.process_one(_task(url, claim_token="tok-old")))
    assert outcome.outcome == "lost_claim"
    assert pool.documents[canonical] == new_doc
    assert len(pool.snapshots) == snapshots_after
    assert len(store.objects) == objects_after  # stale claim never reached MinIO
    assert frontier.completions == []
    assert frontier.failures == []


def test_robots_block_respects_claim():
    url = "https://chinhphu.vn/admin"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0, "claim_token": "tok-new"}])
    pipe, frontier, *_ = _pipeline(robots_allowed=False, pool=pool)
    outcome = _run(pipe.process_one(_task(url, claim_token="tok-old")))
    assert outcome.outcome == "lost_claim"
    assert pool.robots_blocked == []  # UPDATE 0 — flag not written


# ─── M8: oversize is its own outcome ──────────────────────────────────────


def test_oversize_no_snapshot_no_validators():
    url = "https://chinhphu.vn/big"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(
        ok=True,
        status=200,
        headers={"etag": '"x"'},
        content=b"partial",
        mime="text/html",
        final_url=url,
        oversize=True,
    )
    pipe, frontier, store, *_ = _pipeline(fetch_result=result, pool=pool)
    before = time.time()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "oversize"
    assert store.objects == {}
    assert pool.documents == {}
    assert pool.snapshots == []
    comp = frontier.completions[0]
    assert comp["next_crawl_at"] >= before + 29 * 86400  # parked ~30 d
    assert "etag" not in comp or comp.get("etag") is None


# ─── G4: js_required — JS shell with the renderer lane disabled ──────────


def test_js_required_parks_no_snapshot():
    # A JS shell with CRAWLER_FIRECRAWL_FALLBACK off is not a document:
    # parked ~30 d with an explicit outcome — never failed, snapshotted,
    # or indexed.
    url = "https://chinhphu.vn/spa"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = FetchResult(
        ok=True,
        status=200,
        headers={},
        content=b"<html><div id=root></div></html>",
        mime="text/html",
        final_url=url,
        js_required=True,
    )
    pipe, frontier, store, *_ = _pipeline(fetch_result=result, pool=pool)
    before = time.time()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "js_required"
    assert outcome.status == 200
    assert "CRAWLER_FIRECRAWL_FALLBACK" in (outcome.error or "")
    assert store.objects == {}
    assert pool.documents == {}
    assert pool.snapshots == []
    assert frontier.failures == []
    comp = frontier.completions[0]
    assert comp["next_crawl_at"] >= before + 29 * 86400  # parked ~30 d
    assert comp["claim_token"] == "tok-1"


def test_thin_static_html_still_discovers_links():
    # R2 regression guard: a thin static page (<200 visible chars) that
    # carries real anchors is NOT js_required — it persists like any
    # document and its links reach the frontier.
    url = "https://chinhphu.vn/nav"
    thin = (
        b"<html><body><nav>"
        b'<a href="/tin-tuc/1">B1</a><a href="/tin-tuc/2">B2</a>'
        b'<a href="/tin-tuc/3">B3</a></nav></body></html>'
    )
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = _ok_result(url, content=thin)
    pipe, frontier, store, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    assert outcome.links_found == 3
    assert len(pool.discovered) == 3


# ─── M9: cross-host redirect checks destination rules ────────────────────


def test_redirect_to_other_host_checks_its_robots_and_politeness():
    url = "https://chinhphu.vn/r"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = _ok_result("https://tuoitre.vn/article")
    pipe, frontier, store, robots, limiter, *_ = _pipeline(
        fetch_result=result,
        robots_verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "allowed"},
        pool=pool,
        allowed_domains={"chinhphu.vn", "tuoitre.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    # robots consulted for BOTH origins.
    assert "https://tuoitre.vn/article" in robots.checked
    # politeness stamped on the final host too.
    waited = {d for d, _ in limiter.waits}
    assert "tuoitre.vn" in waited
    # documents.domain follows the final canonical host.
    assert pool.documents["https://tuoitre.vn/article"]["domain"] == "tuoitre.vn"


def test_redirect_to_disallowed_host_skips_store():
    url = "https://chinhphu.vn/r"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    result = _ok_result("https://tuoitre.vn/article")
    pipe, frontier, store, *_ = _pipeline(
        fetch_result=result,
        robots_verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "disallowed"},
        pool=pool,
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "skipped_robots"
    assert store.objects == {}
    assert pool.documents == {}
    assert pool.robots_blocked == []  # the rule belongs to the OTHER origin


# ─── M10: snapshot keys never collide ────────────────────────────────────


def test_snapshot_keys_unique_per_fetch():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, _, store, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    _run(pipe.process_one(_task(url)))
    assert len(store.objects) == 2
    assert len(set(store.objects)) == 2


# ─── notes: frontier.fail must never sink a batch ─────────────────────────


def test_fail_exception_isolated():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(fetch_result=RuntimeError("boom"), pool=pool)
    pipe._frontier = RaisingFailFrontier()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"  # returned, not raised


# ─── F2: status updates live inside the claim transaction ────────────────


def test_doc_status_sql_failure_fails_task():
    # A failed status UPDATE must propagate — never a silent "success".
    url = "https://chinhphu.vn/old"
    canonical = "https://chinhphu.vn/old"
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_9", "status": "active"}],
        fail_sqls={pl._DOC_STATUS_SQL},
    )
    result = FetchResult(ok=False, status=410, content=b"", final_url=url)
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    assert frontier.completions == []
    assert pool.documents[canonical]["status"] == "active"  # not "gone"


def test_stale_claim_doc_status_not_written():
    # A reclaimed row (tok-new) must not take a stale worker's gone mark.
    url = "https://chinhphu.vn/old"
    canonical = "https://chinhphu.vn/old"
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0, "claim_token": "tok-new"}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_9", "status": "active"}],
    )
    result = FetchResult(ok=False, status=410, content=b"", final_url=url)
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url, claim_token="tok-old")))
    assert outcome.outcome == "lost_claim"
    assert pool.documents[canonical]["status"] == "active"
    assert frontier.completions == []
    # The FOR UPDATE check ran inside the tx and the UPDATE never fired.
    sqls = [sql for sql, _args in pool.calls]
    assert pl._CLAIM_LOCK_SQL in sqls
    assert pl._DOC_STATUS_SQL not in sqls


def test_blocked_status_under_claim_tx():
    # Same guard on the 401/403 path: claim lost → no 'blocked' write.
    url = "https://chinhphu.vn/"
    canonical = "https://chinhphu.vn"
    pool = PipePool(
        frontier_rows=[{"url": url, "depth": 0, "claim_token": "tok-new"}],
        documents=[{"canonical_url": canonical, "doc_id": "doc_9", "status": "active"}],
    )
    result = FetchResult(ok=False, status=403, content=b"", final_url=url)
    pipe, frontier, *_ = _pipeline(fetch_result=result, pool=pool)
    outcome = _run(pipe.process_one(_task(url, claim_token="tok-old")))
    assert outcome.outcome == "lost_claim"
    assert pool.documents[canonical]["status"] == "active"
    assert frontier.completions == []


def test_discover_aborts_when_claim_lost():
    # A mid-flight reclaim must zero the discovery batch — the FOR UPDATE
    # inside the discover tx sees the rotated token before any insert.
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0, "claim_token": "tok-new"}])
    pipe, *_ = _pipeline(pool=pool)
    count = _run(pipe._discover(pool, HTML.decode(), url, url, 0, token="tok-old"))
    assert count == 0
    assert pool.discovered == []


def test_discover_claim_lock_precedes_inserts():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(pool=pool)
    count = _run(pipe._discover(pool, HTML.decode(), url, url, 0, token="tok-1"))
    assert count > 0
    sqls = [sql for sql, _args in pool.calls]
    assert sqls.index(pl._CLAIM_LOCK_SQL) < sqls.index(pl._DISCOVER_SQL)


# ─── G2: discovery deadlock — sorted lock order + bounded retry ───────────


class _DeadlockError(Exception):
    """asyncpg DeadlockDetectedError stand-in — SQLSTATE 40P01."""

    sqlstate = "40P01"


class DeadlockOncePool(PipePool):
    """First _DISCOVER_SQL per source aborts with a 40P01 deadlock."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.deadlocked: set[str] = set()

    async def execute(self, sql, *args):
        if sql == pl._DISCOVER_SQL and args[4] not in self.deadlocked:
            self.deadlocked.add(args[4])
            raise _DeadlockError("deadlock detected")
        return await super().execute(sql, *args)


def test_discover_deadlock_retries_and_both_complete():
    # A discovers B while B discovers A — one tx is aborted by Postgres,
    # retries, and both discoveries land instead of failing the task.
    url_a = "https://chinhphu.vn/a"
    url_b = "https://chinhphu.vn/b"
    pool = DeadlockOncePool(frontier_rows=[{"url": url_a, "depth": 0}, {"url": url_b, "depth": 0}])
    pipe, *_ = _pipeline(pool=pool)
    html_a = _html(url_b).decode()
    html_b = _html(url_a).decode()

    async def both():
        return await asyncio.gather(
            pipe._discover(pool, html_a, url_a, url_a, 0, token="tok-1"),
            pipe._discover(pool, html_b, url_b, url_b, 0, token="tok-1"),
        )

    assert _run(both()) == [1, 1]
    assert sorted(d[0] for d in pool.discovered) == [url_a, url_b]


def test_discover_deadlock_exhausting_retries_fails_task():
    # Bounded: a still-deadlocked tx propagates after the retries → the
    # task fails (not silently dropped, not retried forever).
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    attempts = 0

    async def deadlocked_execute(sql, *args):
        nonlocal attempts
        if sql == pl._DISCOVER_SQL:
            attempts += 1
            raise _DeadlockError("deadlock detected")
        return await PipePool.execute(pool, sql, *args)

    pool.execute = deadlocked_execute
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    assert attempts == 3  # initial + 2 retries — bounded


def test_discover_non_deadlock_sql_error_not_retried():
    # 40P01 is the ONLY retryable sqlstate — other SQL failures propagate
    # on the first attempt.
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}], fail_sqls={pl._DISCOVER_SQL})
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert frontier.failures == [(url, "tok-1")]
    attempts = sum(1 for sql, _ in pool.calls if sql == pl._DISCOVER_SQL)
    assert attempts == 1


def test_discover_inserts_in_url_order():
    # Lock ordering: upserts run sorted by URL regardless of link order
    # in the document — two txs touching the same rows take the same
    # locks in the same order.
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, *_ = _pipeline(pool=pool)
    html = _html(
        "https://chinhphu.vn/z-last",
        "https://chinhphu.vn/a-first",
        "https://chinhphu.vn/m-mid",
    ).decode()
    count = _run(pipe._discover(pool, html, url, url, 0, token="tok-1"))
    assert count == 3
    urls = [d[0] for d in pool.discovered]
    assert urls == sorted(urls)


# ─── F3: per-hop robots + politeness inside the redirect chain ───────────


def test_redirect_hop_policy_blocks_before_request():
    # A → B where B's robots disallow: B must never be requested (the old
    # code downloaded B's body, then checked).
    url = "https://chinhphu.vn/r"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    fetcher = HopFetcher(["https://tuoitre.vn/x"])
    frontier = FakeFrontier()
    store = FakeStore()
    robots = FakeRobots(verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "disallowed"})
    limiter = FakeLimiter()
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=robots,
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn", "tuoitre.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "skipped_robots"
    assert fetcher.requested == ["https://chinhphu.vn/r"]  # B never fetched
    assert store.objects == {}
    assert pool.documents == {}
    assert frontier.completions[0]["next_crawl_at"] is None  # done
    # B's host never got a politeness wait either — it was never touched.
    assert not any(d == "tuoitre.vn" for d, _ in limiter.waits)


def test_redirect_hop_robots_unavailable_reschedules():
    url = "https://chinhphu.vn/r"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    fetcher = HopFetcher(["https://tuoitre.vn/x"])
    frontier = FakeFrontier()
    robots = FakeRobots(verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "unavailable"})
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=FakeStore(),
        robots=robots,
        limiter=FakeLimiter(),
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn", "tuoitre.vn"},
    )
    before = time.time()
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "robots_unavailable"
    assert fetcher.requested == ["https://chinhphu.vn/r"]
    comp = frontier.completions[0]
    assert before + pl.ROBOTS_UNAVAILABLE_RETRY_S - 5 <= comp["next_crawl_at"]


def test_redirect_hop_allowed_fetches_and_waits():
    url = "https://chinhphu.vn/r"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    fetcher = HopFetcher(["https://tuoitre.vn/x"])
    frontier = FakeFrontier()
    store = FakeStore()
    robots = FakeRobots(verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "allowed"})
    limiter = FakeLimiter()
    pipe = CrawlPipeline(
        frontier=frontier,
        object_store=store,
        robots=robots,
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn", "tuoitre.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    assert fetcher.requested == ["https://chinhphu.vn/r", "https://tuoitre.vn/x"]
    # Destination politeness ran exactly once — per-hop, not again after.
    assert [d for d, _ in limiter.waits].count("tuoitre.vn") == 1
    assert pool.documents["https://tuoitre.vn/x"]["domain"] == "tuoitre.vn"


def test_same_host_redirect_chain_waits_every_hop():
    # G3: A/1 → A/2 → A/3 — same-host hops must each pay the politeness
    # wait; revisiting a host may not ride the earlier reservation.
    url = "https://chinhphu.vn/1"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    fetcher = HopFetcher(["https://chinhphu.vn/2", "https://chinhphu.vn/3"])
    limiter = FakeLimiter()
    pipe = CrawlPipeline(
        frontier=FakeFrontier(),
        object_store=FakeStore(),
        robots=FakeRobots(),
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    waits = [d for d, _ in limiter.waits]
    assert waits.count("chinhphu.vn") == 3  # one wait per request hop


def test_cross_host_roundtrip_waits_host_again():
    # G3: A → B → A — the second visit to A is a fresh reservation, not
    # a free ride on the first hop's wait.
    url = "https://chinhphu.vn/1"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    fetcher = HopFetcher(
        ["https://tuoitre.vn/x", "https://chinhphu.vn/back"],
        result=_ok_result("https://chinhphu.vn/back"),
    )
    limiter = FakeLimiter()
    pipe = CrawlPipeline(
        frontier=FakeFrontier(),
        object_store=FakeStore(),
        robots=FakeRobots(verdicts={"chinhphu.vn": "allowed", "tuoitre.vn": "allowed"}),
        limiter=limiter,
        fetcher=fetcher,
        pool=pool,
        allowed_domains={"chinhphu.vn", "tuoitre.vn"},
    )
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    waits = [d for d, _ in limiter.waits]
    assert waits.count("chinhphu.vn") == 2  # hop 1 and hop 3
    assert waits.count("tuoitre.vn") == 1  # hop 2


# ─── F4: snapshot keys — full UUID + conditional create ──────────────────


def test_snapshot_key_uses_full_uuid():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    pipe, _, store, *_ = _pipeline(fetch_result=_ok_result(url), pool=pool)
    _run(pipe.process_one(_task(url)))
    key = next(iter(store.objects))
    leaf = key.rsplit("/", 1)[-1].removesuffix(".bin")
    assert len(leaf) == 32  # full uuid4 hex — not the old 8-char stub


def test_snapshot_collision_retries_fresh_key():
    url = "https://chinhphu.vn/"
    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    store = CollideOnceStore()
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), store=store, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "changed"
    assert store.attempts == 2
    assert len(set(store.put_keys)) == 2  # retry used a different key
    assert len(store.objects) == 1


def test_snapshot_double_collision_fails_task():
    url = "https://chinhphu.vn/"

    class AlwaysCollide(FakeStore):
        async def put_raw(
            self, data, storage_key, content_type="application/octet-stream", *, if_none_match=False
        ):
            raise CollisionError(storage_key)

    pool = PipePool(frontier_rows=[{"url": url, "depth": 0}])
    store = AlwaysCollide()
    pipe, frontier, *_ = _pipeline(fetch_result=_ok_result(url), store=store, pool=pool)
    outcome = _run(pipe.process_one(_task(url)))
    assert outcome.outcome == "error"
    assert outcome.error == "snapshot_store_failed"
    assert frontier.failures == [(url, "tok-1")]
