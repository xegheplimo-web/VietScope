"""Tests for Phase 4: semantic cache, sufficiency loop, indexing worker, domain profiles."""

import asyncio
import fnmatch
import time

import pipeline.semantic_cache as sc
from pipeline.semantic_cache import TTL_BY_FRESHNESS, CacheHit, SemanticCache
from pipeline.sufficiency_loop import SufficiencyLoop, SufficiencyResult
from workers.domain_profiles import DomainProfileStore
from workers.indexing_worker import IndexingWorker

# ─── SemanticCache ──────────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal redis stand-in for the commands SemanticCache uses."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lists: dict[str, list[str]] = {}

    def _prune(self, name):
        exp = self._expiry.get(name)
        if exp is not None and time.time() > exp:
            self._data.pop(name, None)
            self._expiry.pop(name, None)

    async def get(self, name):
        self._prune(name)
        return self._data.get(name)

    async def set(self, name, value, ex=None, **kw):
        self._data[name] = value
        if ex is not None:
            self._expiry[name] = time.time() + ex
        return True

    async def delete(self, *names):
        n = 0
        for name in names:
            n += int(self._data.pop(name, None) is not None)
            n += int(self._lists.pop(name, None) is not None)
            self._expiry.pop(name, None)
        return n

    async def scan_iter(self, match="*"):
        for name in list(self._data) + list(self._lists):
            if fnmatch.fnmatch(name, match):
                yield name

    async def rpush(self, name, value):
        self._lists.setdefault(name, []).append(value)
        return len(self._lists[name])

    async def ltrim(self, name, start, end):
        lst = self._lists.get(name, [])
        self._lists[name] = lst[start : end + 1 if end != -1 else len(lst)]
        return True

    async def lrange(self, name, start, end):
        lst = self._lists.get(name, [])
        return lst[start : end + 1 if end != -1 else len(lst)]


def _memory_cache(**kw) -> SemanticCache:
    """SemanticCache forced onto the in-memory backend (no Redis probe)."""
    cache = SemanticCache(**kw)
    cache._redis_enabled = False
    return cache


def _patch_redis(monkeypatch, client):
    async def _get():
        return client

    monkeypatch.setattr(sc, "get_redis", _get)


class TestSemanticCache:
    def test_exact_set_and_get_memory(self):
        cache = _memory_cache()
        asyncio.run(cache.set("test query", {"answer": "a"}, freshness_class="medium"))
        hit = asyncio.run(cache.get("test query", freshness_class="medium"))
        assert isinstance(hit, CacheHit)
        assert hit.layer == "exact"
        assert hit.value["answer"] == "a"

    def test_normalization_matches(self):
        cache = _memory_cache()
        asyncio.run(cache.set("  Test   Query ", {"a": 1}))
        hit = asyncio.run(cache.get("test query", freshness_class="medium"))
        assert hit is not None and hit.value["a"] == 1

    def test_cache_expiry_memory(self):
        cache = _memory_cache()
        asyncio.run(cache.set("q", {"a": 1}, freshness_class="realtime"))
        key = "q:" + cache._make_key("q", "balanced", "en", "")
        entry = cache._local[key]
        entry.created_at = 0  # force expiry
        assert asyncio.run(cache.get("q", freshness_class="realtime")) is None

    def test_key_suffix_partitions(self):
        """Different response-shape suffixes must not share entries."""
        cache = _memory_cache()
        asyncio.run(cache.set("q", {"a": 1}, key_suffix="ep:search"))
        assert asyncio.run(cache.get("q", key_suffix="ep:answer")) is None
        assert asyncio.run(cache.get("q", key_suffix="ep:search")) is not None

    def test_evidence_and_page_layers(self):
        cache = _memory_cache()
        asyncio.run(cache.set_evidence("ev1", {"sources": []}))
        asyncio.run(cache.set_page("https://ex.com/p", {"title": "T"}))
        assert asyncio.run(cache.get_evidence("ev1"))["sources"] == []
        assert asyncio.run(cache.get_page("https://ex.com/p"))["title"] == "T"
        assert asyncio.run(cache.get_page("https://ex.com/other")) is None

    def test_semantic_hit_via_embedder_memory(self):
        async def emb(q):
            return {"alpha": [1.0, 0.0], "alpha variant": [0.99, 0.1]}[q]

        cache = _memory_cache(embedder=emb, sim_threshold=0.9)
        asyncio.run(cache.set("alpha", {"a": 1}, freshness_class="static"))
        hit = asyncio.run(cache.get("alpha variant", freshness_class="static"))
        assert hit is not None and hit.layer == "semantic" and hit.value["a"] == 1

    def test_semantic_below_threshold_misses(self):
        async def emb(q):
            return {"a": [1.0, 0.0], "b": [0.0, 1.0]}[q]

        cache = _memory_cache(embedder=emb, sim_threshold=0.94)
        asyncio.run(cache.set("a", {"v": 1}, freshness_class="static"))
        assert asyncio.run(cache.get("b", freshness_class="static")) is None

    def test_semantic_skipped_for_fresh_classes(self):
        calls = []

        async def emb(q):
            calls.append(q)
            return [1.0]

        cache = _memory_cache(embedder=emb)
        asyncio.run(cache.set("q", {"a": 1}, freshness_class="medium"))
        assert calls == []  # medium is not indexed semantically
        asyncio.run(cache.get("other", freshness_class="realtime"))
        assert calls == []  # realtime never triggers embedding

    def test_embedder_down_falls_back_to_exact(self):
        async def emb(q):
            return None

        cache = _memory_cache(embedder=emb)
        asyncio.run(cache.set("q", {"a": 1}, freshness_class="static"))
        assert asyncio.run(cache.get("similar", freshness_class="static")) is None
        assert asyncio.run(cache.get("q", freshness_class="static")) is not None

    def test_redis_backend_roundtrip(self, monkeypatch):
        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        cache = SemanticCache()
        asyncio.run(cache.set("q", {"a": 1}, freshness_class="medium"))
        assert asyncio.run(cache.get("q", freshness_class="medium")).value["a"] == 1
        # Value lives in Redis under the semcache namespace.
        assert any(k.startswith("searchhub:semcache:q:") for k in fake._data)

    def test_redis_semantic_index(self, monkeypatch):
        async def emb(q):
            return {"x": [1.0, 0.0], "y": [0.98, 0.2]}[q]

        fake = _FakeRedis()
        _patch_redis(monkeypatch, fake)
        cache = SemanticCache(embedder=emb, sim_threshold=0.9)
        asyncio.run(cache.set("x", {"v": 7}, freshness_class="slow"))
        hit = asyncio.run(cache.get("y", freshness_class="slow"))
        assert hit is not None and hit.layer == "semantic" and hit.value["v"] == 7

    def test_invalidate_all_and_prefix(self):
        cache = _memory_cache()
        asyncio.run(cache.set("a", {"v": 1}))
        asyncio.run(cache.set_evidence("ev9", {"b": 2}))
        removed = asyncio.run(cache.invalidate("ev:"))
        assert removed == 1
        assert asyncio.run(cache.get("a")) is not None
        assert asyncio.run(cache.invalidate("*")) >= 1
        assert asyncio.run(cache.get("a")) is None

    def test_ttl_by_freshness(self):
        assert TTL_BY_FRESHNESS["realtime"] == 60
        assert TTL_BY_FRESHNESS["high"] == 900
        assert TTL_BY_FRESHNESS["medium"] == 21600
        assert TTL_BY_FRESHNESS["slow"] == 259200
        assert TTL_BY_FRESHNESS["static"] == 2592000


# ─── API wiring ─────────────────────────────────────────────────────────────


_FAKE_RESULT = {
    "answer": "cached answer",
    "confidence": 0.8,
    "sources": [{"url": "https://a.example", "title": "A", "score": 0.9}],
    "citations": [],
    "search": {"generated_queries": ["q"], "raw_results": 1, "pages_read": 1},
    "verification": {"claims_total": 1, "claims_verified": 1},
    "timings": {"total": 0.1},
}


class TestAnswerCacheWiring:
    def test_repeat_query_serves_from_cache(self, monkeypatch):
        """Second identical /v1/search?mode request must not re-run the pipeline."""
        from unittest.mock import MagicMock

        import agent.orchestrator as orch
        import api.v1 as api_v1
        from core.query_understanding import QueryProfile

        fake_redis = _FakeRedis()
        _patch_redis(monkeypatch, fake_redis)
        monkeypatch.setattr(sc, "semantic_cache", SemanticCache(embedder=lambda q: None))

        calls = {"n": 0}

        async def fake_run(context, **kw):
            calls["n"] += 1
            return dict(_FAKE_RESULT)

        monkeypatch.setattr(api_v1, "run_research", fake_run)
        monkeypatch.setattr(orch, "run_research", fake_run)

        orch_mock = MagicMock()
        orch_mock.query_understanding.analyze.side_effect = lambda q: QueryProfile(language="en")
        orch_mock.registry.get.return_value = None
        monkeypatch.setattr(api_v1, "_get_orchestrator", lambda: orch_mock)

        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        payload = {"query": "test cache query", "mode": "normal"}
        r1 = client.post("/v1/search", json=payload)
        assert r1.status_code == 200
        assert calls["n"] == 1
        assert "cache" not in r1.json()

        r2 = client.post("/v1/search", json=payload)
        assert r2.status_code == 200
        assert calls["n"] == 1  # pipeline not re-run
        body2 = r2.json()
        assert body2["cache"] == {"hit": True, "layer": "exact"}
        assert body2["answer"] == "cached answer"

    def test_citations_flag_partitions_cache(self, monkeypatch):
        """cit=0 and cit=1 requests must not share entries (response shape differs)."""
        from unittest.mock import MagicMock

        import agent.orchestrator as orch
        import api.v1 as api_v1
        from core.query_understanding import QueryProfile

        _patch_redis(monkeypatch, _FakeRedis())
        monkeypatch.setattr(sc, "semantic_cache", SemanticCache(embedder=lambda q: None))

        calls = {"n": 0}

        async def fake_run(context, **kw):
            calls["n"] += 1
            return dict(_FAKE_RESULT)

        monkeypatch.setattr(api_v1, "run_research", fake_run)
        monkeypatch.setattr(orch, "run_research", fake_run)

        orch_mock = MagicMock()
        orch_mock.query_understanding.analyze.side_effect = lambda q: QueryProfile(language="en")
        orch_mock.registry.get.return_value = None
        monkeypatch.setattr(api_v1, "_get_orchestrator", lambda: orch_mock)

        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        q = {"query": "partition check", "mode": "normal"}
        client.post("/v1/search", json={**q, "citations": False})
        client.post("/v1/search", json={**q, "citations": True})
        assert calls["n"] == 2


# ─── SufficiencyLoop ────────────────────────────────────────────────────────


class TestSufficiencyLoop:
    def setup_method(self):
        self.loop = SufficiencyLoop(max_rounds=3, coverage_threshold=0.8, min_domains=3)

    def test_sufficient_evidence(self):
        evidence = [
            {"text": "Python 3.14 released in 2026", "domain": "python.org"},
            {"text": "New features include type hints", "domain": "docs.python.org"},
            {"text": "Performance improvements", "domain": "blog.python.org"},
        ]
        result = self.loop.evaluate("Python 3.14 features", evidence, ["Python 3.14", "features"])
        assert isinstance(result, SufficiencyResult)
        assert result.sufficient

    def test_insufficient_evidence(self):
        evidence = [
            {"text": "Python 3.14 released", "domain": "python.org"},
        ]
        result = self.loop.evaluate(
            "Python 3.14 features", evidence, ["Python 3.14", "features", "performance"]
        )
        assert not result.sufficient
        assert result.need_more_search
        assert len(result.proposed_queries) > 0

    def test_coverage_ratio(self):
        evidence = [
            {"text": "Python 3.14 released", "domain": "python.org"},
            {"text": "New features", "domain": "docs.python.org"},
        ]
        result = self.loop.evaluate(
            "Python 3.14", evidence, ["Python 3.14", "features", "performance"]
        )
        assert result.coverage_ratio < 1.0
        assert "performance" in result.missing_facts


# ─── IndexingWorker ─────────────────────────────────────────────────────────


class TestIndexingWorker:
    def setup_method(self):
        self.worker = IndexingWorker()

    def test_chunk_text(self):
        text = "This is a test. " * 100
        chunks = self.worker._chunk_text(text, chunk_size=100, overlap=20)
        assert len(chunks) > 0
        assert chunks[0]["char_start"] == 0
        assert chunks[0]["char_end"] == 100

    def test_compute_simhash(self):
        text = "This is test content for simhash"
        simhash = self.worker._compute_simhash(text)
        assert len(simhash) == 16
        assert simhash != ""


# ─── DomainProfileStore ────────────────────────────────────────────────────


class TestDomainProfileStore:
    def setup_method(self):
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        self.store = DomainProfileStore(storage_path=f"{self._tmpdir}/profiles.json")

    def test_get_or_create_profile(self):
        profile = self.store.get_profile("example.com")
        assert profile.domain == "example.com"
        assert profile.base_authority == 0.5

    def test_update_citation_feedback(self):
        self.store.update_citation_feedback("example.com", 0.9)
        profile = self.store.get_profile("example.com")
        assert profile.observed_citation_precision > 0.5
        assert profile.total_served == 1

    def test_get_effective_authority(self):
        profile = self.store.get_profile("example.com")
        profile.authority_by_intent["pricing_lookup"] = 0.9
        authority = profile.get_effective_authority("pricing_lookup")
        assert authority > 0.5
