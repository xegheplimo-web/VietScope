"""Phase 3 E2E — discover → crawl → snapshot → extract → index → /v1/search.

Skipped unless ``E2E=1``. Needs the live local stack (hub-postgres :5433,
MinIO :9000, OpenSearch :9200, Qdrant :6333, embedding :8892, router :8888)
plus a ``dsa_test_`` key — same env conventions as ``test_stack_e2e.py`` /
``scripts/e2e_run.sh``.

The crawl itself runs in-process (same pattern as ``test_crawler_live``):
the pipeline is constructed here with ``NetGuard(allow_private=True)`` —
the *only* place the SSRF escape hatch is legitimate — so the worker can
reach a fixture page served on 127.0.0.1. Everything downstream (MinIO
snapshot, documents row, OpenSearch, Qdrant) is the real production path.

Definition of done (the user-facing contract):
  seed → crawl → raw snapshot in MinIO → documents.main_text +
  extraction_status='success' → web_documents/web_passages + Qdrant
  points → ``POST /v1/search`` (mode=fast) returns the fixture URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("E2E") != "1",
        reason="E2E stack tests — set E2E=1 (or run scripts/e2e_run.sh)",
    ),
]

httpx = pytest.importorskip("httpx", reason="e2e requires httpx")

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[2]


def _bootstrap_env() -> None:
    """Resolve hub-postgres + MinIO env BEFORE ``config`` is imported.

    Mirrors ``scripts/crawl_seed.py`` — repo-root ``.env`` provides
    HUB_*/MINIO_*; localhost fallbacks match the compose stack. Runs only
    under E2E=1: pytest imports this module during plain-suite collection
    too, and mutating os.environ here would leak a live pg_client pool
    into unit tests that rely on the degraded no-Postgres path.
    """
    if str(_SEARCH_ROUTER_DIR) not in sys.path:
        sys.path.insert(0, str(_SEARCH_ROUTER_DIR))
    import manage_keys

    manage_keys._load_dotenv()
    # An empty HUB_DATABASE_URL= line in .env (the template default) defeats
    # setdefault — treat empty as unset, same as _resolve_dsn does.
    if not os.getenv("HUB_DATABASE_URL"):
        os.environ["HUB_DATABASE_URL"] = (
            os.getenv("E2E_DSN") or manage_keys._resolve_dsn(None)
        )
    os.environ.setdefault("MINIO_ENDPOINT", f"http://127.0.0.1:{os.getenv('MINIO_PORT', '9000')}")
    os.environ.setdefault("MINIO_ACCESS_KEY", os.getenv("MINIO_ROOT_USER", "minioadmin"))
    os.environ.setdefault("MINIO_SECRET_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"))
    os.environ.setdefault("MINIO_BUCKET_RAW", "sh-raw-snapshots")
    os.environ.setdefault("OPENSEARCH_ENABLED", "true")
    os.environ.setdefault("QDRANT_ENABLED", "true")
    os.environ.setdefault("INDEXING_ENABLED", "true")


if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8888").rstrip("/")
# Unique per run — guarantees the only search hit is our fixture doc.
TOKEN = f"qxzkb{uuid.uuid4().hex[:10]}"

FIXTURE_HTML = f"""<!DOCTYPE html>
<html lang="vi"><head><title>{TOKEN} Fixture Search-Hub</title>
<meta name="description" content="Fixture e2e cho extraction pipeline.">
</head><body><article>
<h1>{TOKEN} Fixture title</h1>
<p>Day la trang fixture cho bai test crawl-to-search cua Search-Hub.
Token duy nhat {TOKEN} dam bao ket qua tim kiem chi co the den tu
document vua duoc crawl va index.</p>
<p>Noi dung bo sung de vuot nguong quality gate: extraction pipeline can
main text du dai de danh gia success. {TOKEN} xuat hien them mot lan.</p>
</article></body></html>"""

ROBOTS_TXT = "User-agent: *\nAllow: /\n"


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server API
        if self.path == "/robots.txt":
            body, mime = ROBOTS_TXT.encode(), "text/plain"
        elif self.path.startswith("/page"):
            body, mime = FIXTURE_HTML.encode(), "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("ETag", '"fixture-1"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logs
        pass


@pytest.fixture(scope="module")
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/page"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def api_key() -> dict:
    """Reuse the suite's key conventions: E2E_API_KEY wins, else create +
    revoke a ``dsa_test_`` wildcard key via manage_keys."""
    _bootstrap_env()
    env_key = os.getenv("E2E_API_KEY")
    if env_key:
        yield {"full_key": env_key, "key_id": os.getenv("E2E_KEY_ID", "")}
        return

    asyncpg = pytest.importorskip("asyncpg", reason="e2e key mgmt needs asyncpg")
    import manage_keys

    created = {}

    async def _create() -> None:
        conn = await asyncpg.connect(dsn=os.environ["HUB_DATABASE_URL"], timeout=10)
        try:
            created.update(
                await manage_keys.create_key(
                    conn,
                    tenant_id="e2e",
                    name="pytest-crawl2search",
                    scopes=["*"],
                    rpm=600,
                    quota=-1,
                    kind="test",
                )
            )
        finally:
            await conn.close()

    asyncio.run(_create())
    yield created

    async def _revoke() -> None:
        conn = await asyncpg.connect(dsn=os.environ["HUB_DATABASE_URL"], timeout=10)
        try:
            await manage_keys.revoke_key(conn, key_id=created["key_id"])
        finally:
            await conn.close()

    asyncio.run(_revoke())


async def _pg(sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(dsn=os.environ["HUB_DATABASE_URL"], timeout=10)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


class TestCrawlToSearch:
    def test_crawl_extract_index_search(self, fixture_server, api_key):
        asyncio.run(self._main(fixture_server, api_key["full_key"]))

    async def _main(self, url: str, bearer: str) -> None:
        from crawler.fetcher import Fetcher
        from crawler.netguard import NetGuard
        from crawler.pipeline import CrawlPipeline
        from crawler.politeness import DomainRateLimiter
        from crawler.robots import RobotsCache
        from extraction.service import ExtractionService
        from storage import pg_client
        from storage.object_store import get_object_store
        from workers.freshness_worker import FreshnessWorker, RecrawlTask
        from workers.indexing_worker import get_indexing_worker

        pool = await pg_client.get_pool()
        if pool is None:
            pytest.skip("hub-postgres unreachable — cannot drive the frontier")

        # SSRF escape hatch ONLY for the fixture lane — production fetchers
        # keep the default guard. This is the documented test/internal use.
        fixture_netguard = NetGuard(allow_private=True)
        fetcher = Fetcher(netguard=fixture_netguard)
        frontier = FreshnessWorker()
        pipe = CrawlPipeline(
            frontier=frontier,
            object_store=get_object_store(),
            robots=RobotsCache(netguard=fixture_netguard),
            limiter=DomainRateLimiter(),
            fetcher=fetcher,
            pool=pool,
            extraction=ExtractionService(),
            indexer=get_indexing_worker().process_one,
        )

        # Astronomical priority — the live frontier already holds real
        # seeds; pop_batch orders by priority DESC and the fixture must win.
        # inf, not 1e18: priority is REAL (float4), and stale rows left by
        # earlier e2e runs already occupy float4(1e18) — a finite bump can
        # still tie on scheduled_at and lose.
        await frontier.enqueue(
            RecrawlTask(url=url, priority=float("inf"), scheduled_at=0.0, discovered_from="e2e")
        )
        batch = await frontier.pop_batch(1)
        assert batch and batch[0].url == url, (
            f"frontier popped {batch[0].url if batch else None}, not the fixture"
        )
        outcome = await pipe.process_one(batch[0])

        # Cleanup lives in ``finally`` — a failed assertion must not leave
        # fixture rows/points in the real stores (each stage guards on what
        # actually got initialized, so it is safe at any exit point).
        doc = None
        osc = None
        qc = None
        try:
            # ── Stage assertions: crawl + snapshot ──────────────────────
            assert outcome.outcome in ("changed", "unchanged"), outcome.error
            assert outcome.storage_key, "no MinIO storage_key — snapshot missing"
            blob = await get_object_store().get_raw(outcome.storage_key)
            assert blob and TOKEN.encode() in blob, "raw snapshot unreadable from MinIO"

            # ── documents row: extraction columns ───────────────────────
            # canonical_url() preserves the fixture's http scheme — match
            # on doc_id, which is derived from the canonical form and is
            # always exact.
            rows = await _pg(
                "SELECT doc_id, canonical_url, main_text, extraction_status,"
                " extraction_method, indexing_status, embedding_status,"
                " current_snapshot_id"
                " FROM documents WHERE doc_id = $1",
                outcome.doc_id,
            )
            assert rows, "documents row missing"
            doc = rows[0]
            canonical = doc["canonical_url"]
            assert doc["extraction_status"] == "success", doc["extraction_status"]
            assert doc["indexing_status"] == "success", doc["indexing_status"]
            assert doc["main_text"] and TOKEN in doc["main_text"]
            assert doc["extraction_method"] in ("trafilatura", "raw_passthrough")
            assert doc["current_snapshot_id"] is not None

            # ── OpenSearch: document + passages indexed ─────────────────
            from opensearch.client import OpenSearchClient

            osc = OpenSearchClient()
            os_doc = await asyncio.to_thread(
                lambda: osc._get_client().get(index="web_documents", id=doc["doc_id"], _source=True)
            )
            assert os_doc["found"], "web_documents hit missing"
            hits = osc.search_bm25("web_passages", TOKEN, top_k=5)
            assert any(doc["doc_id"] in (h.get("doc_id") or "") for h in hits), (
                "no web_passages hit for the unique token"
            )

            # ── Qdrant: passage points when the embedding lane is up ────
            # embedding_status comes straight from the document row — CI
            # runs without the BGE-M3 service, where the honest status is
            # 'failed' and the BM25-only lane still proves the path.
            if doc["embedding_status"] == "success":
                from qdrant.client import QdrantClient

                qc = QdrantClient()
                point_ids = await qc.get_all_ids("web_passages_v1")
                assert any(pid.startswith(f"{doc['doc_id']}#p_") for pid in point_ids), (
                    "no Qdrant points for the fixture doc"
                )

            # ── /v1/search: the unique phrase must surface the URL ──────
            deadline = time.monotonic() + 900  # LLM-down degraded path is slow
            last_body = ""
            while True:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                    resp = await client.post(
                        f"{BASE_URL}/v1/search",
                        headers={"Authorization": f"Bearer {bearer}"},
                        json={"query": TOKEN, "mode": "fast"},
                    )
                assert resp.status_code == 200, resp.text[:400]
                last_body = resp.text
                if canonical in last_body:
                    break
                if time.monotonic() > deadline:
                    pytest.fail(f"/v1/search never returned {canonical}\n{last_body[:1200]}")
                await asyncio.sleep(3)
        finally:
            with contextlib.suppress(Exception):
                if osc is not None and doc is not None:
                    await osc.delete_by_query("web_passages", {"term": {"doc_id": doc["doc_id"]}})
                    await osc.delete_by_query("web_documents", {"ids": {"values": [doc["doc_id"]]}})
            with contextlib.suppress(Exception):
                if qc is not None and doc is not None:
                    await qc.delete_points(
                        "web_passages_v1",
                        must=[{"key": "doc_id", "match": {"value": doc["doc_id"]}}],
                    )
            with contextlib.suppress(Exception):
                if doc is not None:
                    await _pg("DELETE FROM documents WHERE doc_id = $1", doc["doc_id"])
            with contextlib.suppress(Exception):
                await _pg("DELETE FROM crawl_frontier WHERE url = $1", url)
