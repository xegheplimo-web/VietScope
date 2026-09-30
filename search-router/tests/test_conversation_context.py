"""P4 — conversation context: session state, follow-up resolution, compaction.

Hermetic: ``FakeRedis`` stands in for the Redis client (including
WATCH/MULTI pipelines via ``FakePipeline``) and the inference gateway is
mocked — no live Redis, no live LLM. Follows the ``test_storage.py``
fake-Redis pattern.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import api.v1 as api_v1
import core.conversation as conv
import pipeline.semantic_cache as sc
import pytest
from config import settings
from redis.exceptions import WatchError

# ─── Fakes ───────────────────────────────────────────────────────────────────


class FakePipeline:
    """WATCH/MULTI stand-in — version-checked EXEC, atomic apply.

    ``watch`` records the key's version; ``execute`` raises ``WatchError``
    when a concurrent write bumped it — the same optimistic-transaction
    semantics the real client provides.
    """

    def __init__(self, client: FakeRedis):
        self._client = client
        self._watched: dict[str, int] = {}
        self._queued: list[tuple] = []
        self._multi = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def watch(self, key):
        self._watched[key] = self._client._versions.get(key, 0)

    async def get(self, name):
        return await self._client.get(name)

    def multi(self):
        self._multi = True

    def set(self, name, value, ex=None, **kwargs):
        if self._multi:
            self._queued.append((name, value, ex))
        else:
            self._client._apply_set(name, value, ex)
        return True

    async def execute(self):
        for key, ver in self._watched.items():
            if self._client._versions.get(key, 0) != ver:
                self._queued.clear()
                raise WatchError(f"Watched key {key} has been modified")
        # Version check + write apply are atomic — no awaits in between.
        for name, value, ex in self._queued:
            self._client._apply_set(name, value, ex)
        n = len(self._queued)
        self._queued.clear()
        return [True] * n


class FakeRedis:
    """Minimal in-memory impl of the redis commands the manager uses.

    ``ops`` logs every command for zero-touch assertions; ``_versions``
    backs optimistic WATCH/MULTI concurrency. ``get`` snapshots then
    yields once so two racing coroutines deterministically interleave
    between read and write.
    """

    def __init__(self):
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._versions: dict[str, int] = {}
        self.ops: list[str] = []

    async def ping(self):
        return True

    def _prune(self, name):
        exp = self._expiry.get(name)
        if exp is not None and time.time() > exp:
            self._data.pop(name, None)
            self._expiry.pop(name, None)

    def _apply_set(self, name, value, ex=None):
        self._data[name] = value
        self._versions[name] = self._versions.get(name, 0) + 1
        if ex is not None:
            self._expiry[name] = time.time() + ex
        else:
            self._expiry.pop(name, None)

    async def get(self, name):
        self.ops.append("get")
        self._prune(name)
        value = self._data.get(name)
        await asyncio.sleep(0)  # yield AFTER snapshot — race reproduction
        return value

    async def set(self, name, value, ex=None, **kwargs):
        self.ops.append("set")
        self._apply_set(name, value, ex)
        return True

    async def delete(self, *names):
        self.ops.append("delete")
        removed = 0
        for name in names:
            if self._data.pop(name, None) is not None:
                removed += 1
                self._versions[name] = self._versions.get(name, 0) + 1
            self._expiry.pop(name, None)
        return removed

    def pipeline(self, transaction: bool = True):
        return FakePipeline(self)


class FakeGateway:
    """InferenceGateway stand-in — canned ``complete_json`` + call log."""

    def __init__(self, payload=None, *, delay: float = 0.0):
        self.payload = payload
        self.delay = delay
        self.calls: list[dict] = []

    async def complete_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload

    @property
    def last_user_message(self) -> str:
        if not self.calls:
            return ""
        return self.calls[-1]["messages"][-1]["content"]


def _redis_returning(client):
    async def _get():
        return client

    return _get


def _redis_returning_slow(client):
    """get_redis that yields once — lets concurrent coroutines interleave."""

    async def _get():
        await asyncio.sleep(0)
        return client

    return _get


def _key(sid: str, owner: str | None = None) -> str:
    return conv.ConversationContextManager.key(sid, owner)


def _redis_box(box: dict):
    """get_redis reading ``box['client']`` — flip mid-test to fake an outage."""

    async def _get():
        return box["client"]

    return _get


def _state(**kw) -> conv.ConversationState:
    base = {
        "last_query": "Ai là CEO của công ty ABC?",
        "last_answer_summary": "CEO của công ty ABC là Nguyễn Văn A.",
        "resolved_entities": ["Nguyễn Văn A", "công ty ABC"],
        "turns": [
            {"role": "user", "text": "Ai là CEO của công ty ABC?"},
            {"role": "assistant", "text": "CEO của công ty ABC là Nguyễn Văn A."},
        ],
    }
    base.update(kw)
    return conv.ConversationState(**base)


# ─── session_id validation ───────────────────────────────────────────────────


class TestSessionIdValidation:
    def test_valid_ids_accepted(self):
        for sid in ("sess-1", "abc_DEF-123", "A" * 64):
            req = api_v1.SearchRequest(query="q", session_id=sid)
            assert req.session_id == sid

    def test_bad_chars_rejected(self):
        from pydantic import ValidationError

        for sid in ("bad:id", "a b", "../x", "convctx:*", "{json}", "a" * 65, ""):
            with pytest.raises(ValidationError):
                api_v1.SearchRequest(query="q", session_id=sid)

    def test_api_rejects_bad_session_id(self):
        from fastapi.testclient import TestClient
        from main import app

        resp = TestClient(app).post("/v1/answer", json={"query": "q", "session_id": "bad:id"})
        assert resp.status_code == 422


# ─── follow-up gate ──────────────────────────────────────────────────────────


class TestFollowupGate:
    @pytest.mark.parametrize(
        "query",
        [
            "Ông ấy sinh năm bao nhiêu?",
            "Bà ấy quê ở đâu?",
            "Nó có rẻ không?",
            "chỗ đó còn mở không",
            "gần nhất là khi nào",
            "Thế giá bao nhiêu?",
            "he resigned when",
            "that place near me",
            "is it open now",
        ],
    )
    def test_anaphora_marks_followup(self, query):
        assert conv.looks_like_followup(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "What is the current market capitalization of Vinamilk on HOSE",
            "Vinamilk market capitalization latest annual report figures",
        ],
    )
    def test_long_standalone_skipped(self, query):
        assert conv.looks_like_followup(query) is False

    def test_word_boundary_no_false_hit(self):
        # "nói" must not trip on "nó"; "nay" must not trip on "này".
        assert conv._has_anaphora("Ông nói điều này hôm nay") is True  # has "này"
        assert conv._has_anaphora("giá vàng hôm nay bao nhiêu một chỉ") is False

    @pytest.mark.parametrize(
        "query",
        [
            "Ong ay sinh nam bao nhieu?",
            "Ba ay que o dau?",
            "Cho do con mo khong",
            "gan nhat la khi nao",
            "ong ta la ai",
        ],
    )
    def test_unaccented_markers_hit(self, query):
        # Accent-folded matching: unaccented Vietnamese still marks.
        assert conv.looks_like_followup(query) is True

    def test_folded_no_english_collisions(self):
        # Folded markers colliding with English words ("the"/"no"/"do")
        # must not mark plain English queries.
        assert conv._has_anaphora("the results look good so far") is False
        assert conv._has_anaphora("do re mi fa so la ti") is False
        assert conv._has_anaphora("do not show me that") is True  # real EN "that"
        assert (
            conv.looks_like_followup(
                "What is the current market capitalization of Vinamilk on HOSE"
            )
            is False
        )


# ─── resolver ────────────────────────────────────────────────────────────────


class TestResolver:
    def test_llm_resolution(self):
        gw = FakeGateway(
            {
                "standalone_query": "CEO công ty ABC Nguyễn Văn A sinh năm bao nhiêu",
                "entities": ["Nguyễn Văn A", "công ty ABC"],
                "locations": [],
                "constraints": [],
            }
        )
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw))
        assert out is not None
        assert out.source == "llm"
        assert "ABC" in out.standalone_query
        assert out.entities == ["Nguyễn Văn A", "công ty ABC"]
        # The LLM saw the stored context, not just the raw query.
        assert "Ai là CEO của công ty ABC" in gw.last_user_message

    def test_llm_none_falls_back_to_heuristic(self):
        gw = FakeGateway(None)
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw))
        assert out is not None
        assert out.source == "heuristic"
        assert "CEO" in out.standalone_query and "ABC" in out.standalone_query
        assert "Ông ấy sinh năm bao nhiêu" in out.standalone_query

    def test_timeout_falls_back(self):
        gw = FakeGateway({"standalone_query": "x"}, delay=0.5)
        out = asyncio.run(
            conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw, timeout=0.01)
        )
        assert out is not None
        assert out.source == "heuristic"

    def test_no_anaphora_no_combine(self):
        # Short query without anaphora → heuristic must not mangle it.
        gw = FakeGateway(None)
        out = asyncio.run(conv.resolve_followup("giá bao nhiêu", _state(), gateway=gw))
        assert out is None

    def test_invalid_json_falls_back(self):
        gw = FakeGateway({"unexpected": "shape"})
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw))
        # FakeGateway bypasses complete_json validation — _llm_resolve still
        # rejects a missing standalone_query, then heuristic takes over.
        assert out is not None and out.source == "heuristic"

    def test_nonstring_standalone_falls_back(self):
        # Resolver schema violation — never str()-coerce a dict into the
        # pipeline query; the heuristic takes over.
        gw = FakeGateway({"standalone_query": {"bad": "type"}})
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw))
        assert out is not None and out.source == "heuristic"
        assert "bad" not in out.standalone_query
        assert "Ông ấy sinh năm bao nhiêu?" in out.standalone_query

    def test_numeric_standalone_falls_back(self):
        gw = FakeGateway({"standalone_query": 42})
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", _state(), gateway=gw))
        assert out is not None and out.source == "heuristic"
        assert "42" not in out.standalone_query

    def test_bad_aux_fields_ignored(self):
        # Non-list[str] aux fields are dropped wholesale — never coerced.
        gw = FakeGateway(
            {
                "standalone_query": "standalone query",
                "entities": "not-a-list",
                "locations": ["ok", 5],
                "constraints": [{"bad": True}],
            }
        )
        out = asyncio.run(conv.resolve_followup("Ông ấy?", _state(), gateway=gw))
        assert out is not None and out.source == "llm"
        assert out.standalone_query == "standalone query"
        assert out.entities == [] and out.locations == [] and out.constraints == []


# ─── manager (Redis + fallback + compaction) ─────────────────────────────────


class TestManager:
    def test_save_load_roundtrip(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        state = _state()
        asyncio.run(mgr.save("s1", state))
        loaded = asyncio.run(mgr.load("s1"))
        assert loaded is not None
        assert loaded.last_query == state.last_query
        assert loaded.updated_at
        assert _key("s1") in fake._data

    def test_missing_returns_none(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        assert asyncio.run(mgr.load("nope")) is None

    def test_ttl_expiry(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state()))
        fake._expiry[_key("s1")] = time.time() - 1
        mgr._memory.pop(_key("s1"), None)  # force the Redis read path
        assert asyncio.run(mgr.load("s1")) is None

    def test_memory_fallback_ttl_expiry(self, monkeypatch):
        monkeypatch.setattr(conv, "get_redis", _redis_returning(None))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state()))
        assert asyncio.run(mgr.load("s1")) is not None
        exp, blob = mgr._memory[_key("s1")]
        mgr._memory[_key("s1")] = (time.time() - 1, blob)
        assert asyncio.run(mgr.load("s1")) is None

    def test_redis_down_memory_fallback(self, monkeypatch):
        monkeypatch.setattr(conv, "get_redis", _redis_returning(None))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state()))
        loaded = asyncio.run(mgr.load("s1"))
        assert loaded is not None and loaded.last_query

    def test_corrupt_blob_returns_none(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        fake._data[_key("s1")] = "{not json"
        mgr = conv.ConversationContextManager()
        assert asyncio.run(mgr.load("s1")) is None

    def test_compaction_bounds(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager(max_turns=2, max_sources=1)
        state = _state(
            last_answer_summary="x" * 800,
            resolved_entities=[f"e{i}" for i in range(20)],
            recent_sources=[{"url": f"https://{i}.example", "title": str(i)} for i in range(3)],
            turns=[{"role": "user", "text": f"t{i} " + "y" * 600} for i in range(5)],
        )
        asyncio.run(mgr.save("s1", state))
        loaded = asyncio.run(mgr.load("s1"))
        assert len(loaded.turns) == 2  # oldest dropped
        assert loaded.turns[0]["text"].startswith("t3")
        assert all(len(t["text"]) <= 500 for t in loaded.turns)
        assert len(loaded.last_answer_summary) == 500
        assert len(loaded.resolved_entities) == 10
        assert len(loaded.recent_sources) == 1

    def test_record_turn_updates_state(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        asyncio.run(
            mgr.record_turn(
                "s1",
                user_text="Ai là CEO của công ty ABC?",
                resolved_query="Ai là CEO của công ty ABC?",
                answer="CEO là Nguyễn Văn A.",
                sources=[{"url": "https://a.example", "title": "A", "score": 0.9}],
                resolution=conv.ResolutionResult(standalone_query="q", entities=["Nguyễn Văn A"]),
            )
        )
        loaded = asyncio.run(mgr.load("s1"))
        assert loaded.last_query == "Ai là CEO của công ty ABC?"
        assert loaded.last_answer_summary == "CEO là Nguyễn Văn A."
        assert loaded.resolved_entities == ["Nguyễn Văn A"]
        assert loaded.recent_sources == [{"url": "https://a.example", "title": "A"}]
        roles = [t["role"] for t in loaded.turns]
        assert roles == ["user", "assistant"]


# ─── owner isolation (F2) ────────────────────────────────────────────────────


class TestOwnerIsolation:
    def test_key_scoped_by_owner_hash(self):
        a = _key("s1", "alice")
        b = _key("s1", "bob")
        assert a != b
        assert a.startswith("convctx:") and a.endswith(":s1")
        # The principal is hashed — raw key material never lands in keys.
        assert "alice" not in a and "bob" not in b
        assert _key("s1") == _key("s1", "anonymous") == _key("s1", None)

    def test_owners_isolated_same_session_id(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state(last_query="alice's q"), owner="alice"))
        alice = asyncio.run(mgr.load("s1", owner="alice"))
        assert alice is not None and alice.last_query == "alice's q"
        # Another owner reads the same session_id as NOT FOUND.
        assert asyncio.run(mgr.load("s1", owner="bob")) is None
        asyncio.run(mgr.record_turn("s1", owner="bob", user_text="bob's turn"))
        alice = asyncio.run(mgr.load("s1", owner="alice"))
        assert "bob's turn" not in [t["text"] for t in alice.turns]
        bob = asyncio.run(mgr.load("s1", owner="bob"))
        assert "bob's turn" in [t["text"] for t in bob.turns]
        assert fake._data[_key("s1", "alice")] != fake._data[_key("s1", "bob")]

    def test_owners_isolated_in_memory_fallback(self, monkeypatch):
        monkeypatch.setattr(conv, "get_redis", _redis_returning(None))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state(), owner="alice"))
        assert asyncio.run(mgr.load("s1", owner="alice")) is not None
        assert asyncio.run(mgr.load("s1", owner="bob")) is None
        assert _key("s1", "alice") in mgr._memory
        assert _key("s1", "bob") not in mgr._memory

    def test_conversation_owner_from_request(self):
        class _Ctx:
            key_id = "key-abc-123"

        req = SimpleNamespace(state=SimpleNamespace(api_key=_Ctx()))
        assert api_v1._conversation_owner(req) == "key-abc-123"
        anon = SimpleNamespace(state=SimpleNamespace(api_key=None))
        assert api_v1._conversation_owner(anon) == "anonymous"
        bare = SimpleNamespace(state=SimpleNamespace())
        assert api_v1._conversation_owner(bare) == "anonymous"


# ─── concurrent record_turn (F3) ─────────────────────────────────────────────


class TestConcurrentRecordTurn:
    def test_redis_path_both_turns_survive(self, monkeypatch):
        """Two racing recorders merge — no last-writer-wins turn loss."""
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()

        async def run():
            await asyncio.gather(
                mgr.record_turn("s1", user_text="câu hỏi một"),
                mgr.record_turn("s1", user_text="câu hỏi hai"),
            )

        asyncio.run(run())
        texts = [t["text"] for t in json.loads(fake._data[_key("s1")])["turns"]]
        assert "câu hỏi một" in texts and "câu hỏi hai" in texts

    def test_memory_fallback_both_turns_survive(self, monkeypatch):
        """Same guarantee when the fallback dict is the store — the lock
        spans the whole read-modify-write."""
        monkeypatch.setattr(conv, "get_redis", _redis_returning_slow(None))
        mgr = conv.ConversationContextManager()

        async def run():
            await asyncio.gather(
                mgr.record_turn("s1", user_text="lượt một"),
                mgr.record_turn("s1", user_text="lượt hai"),
            )

        asyncio.run(run())
        _, blob = mgr._memory[_key("s1")]
        texts = [t["text"] for t in json.loads(blob)["turns"]]
        assert "lượt một" in texts and "lượt hai" in texts


# ─── Redis recovery reconciliation (F4) ──────────────────────────────────────


class TestRecoverySync:
    def test_turns_during_outage_survive_recovery(self, monkeypatch):
        fake = FakeRedis()
        box = {"client": fake}
        monkeypatch.setattr(conv, "get_redis", _redis_box(box))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state()))  # Q1 lands in Redis + mirror

        box["client"] = None  # ── outage ──
        asyncio.run(
            mgr.record_turn("s1", user_text="Ông ấy sinh năm bao nhiêu?", answer="Sinh năm 1970.")
        )
        assert _key("s1") in mgr._pending_sync

        box["client"] = fake  # ── recovery — next op reconciles ──
        loaded = asyncio.run(mgr.load("s1"))
        texts = [t["text"] for t in loaded.turns]
        assert "Ai là CEO của công ty ABC?" in texts  # Q1 kept
        assert "Ông ấy sinh năm bao nhiêu?" in texts  # Q2 preserved
        stored = json.loads(fake._data[_key("s1")])
        assert "Ông ấy sinh năm bao nhiêu?" in [t["text"] for t in stored["turns"]]
        assert mgr._pending_sync == {}

    def test_session_created_during_outage_syncs(self, monkeypatch):
        fake = FakeRedis()
        box = {"client": None}  # start mid-outage
        monkeypatch.setattr(conv, "get_redis", _redis_box(box))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("new", _state()))
        assert _key("new") not in fake._data

        box["client"] = fake  # recovery
        loaded = asyncio.run(mgr.load("new"))
        assert loaded is not None and loaded.last_query
        assert _key("new") in fake._data
        assert mgr._pending_sync == {}

    def test_newer_remote_discards_local_pending(self, monkeypatch):
        fake = FakeRedis()
        box = {"client": fake}
        monkeypatch.setattr(conv, "get_redis", _redis_box(box))
        mgr = conv.ConversationContextManager()
        key = _key("s1")
        newer = _state(last_query="remote wins")
        newer.updated_at = "2999-01-01T00:00:00Z"
        fake._data[key] = json.dumps(newer.to_dict(), ensure_ascii=False)
        stale = _state(last_query="stale local")
        stale.updated_at = "2000-01-01T00:00:00Z"
        stale_blob = json.dumps(stale.to_dict(), ensure_ascii=False)
        mgr._pending_sync[key] = (time.time() + 3600, stale_blob)
        mgr._memory[key] = (time.time() + 3600, stale_blob)

        loaded = asyncio.run(mgr.load("s1"))
        assert loaded.last_query == "remote wins"
        assert mgr._pending_sync == {}
        # Remote untouched; the stale mirror was refreshed to match.
        assert json.loads(fake._data[key])["last_query"] == "remote wins"
        _, mirror = mgr._memory[key]
        assert json.loads(mirror)["last_query"] == "remote wins"

    def test_flush_atomic_concurrent_remote_write_survives(self, monkeypatch):
        """A remote write landing between the flush's read and write is
        re-compared inside the same WATCH/MULTI — never clobbered."""
        fake = FakeRedis()
        box = {"client": fake}
        monkeypatch.setattr(conv, "get_redis", _redis_box(box))
        mgr = conv.ConversationContextManager()
        key = _key("s1")
        asyncio.run(mgr.save("s1", _state()))

        box["client"] = None  # outage — the turn goes pending
        asyncio.run(mgr.record_turn("s1", user_text="offline turn"))
        assert key in mgr._pending_sync
        box["client"] = fake  # recovery

        newer = _state(last_query="remote wins")
        newer.updated_at = "2999-01-01T00:00:00Z"
        newer_blob = json.dumps(newer.to_dict(), ensure_ascii=False)

        async def interleave():
            # Land a strictly-newer remote write inside the flush's
            # read→write window (the yield FakeRedis.get opens).
            while "get" not in fake.ops:
                await asyncio.sleep(0)
            await fake.set(key, newer_blob)

        async def run():
            await asyncio.gather(mgr.load("s1"), interleave())

        asyncio.run(run())
        assert json.loads(fake._data[key])["last_query"] == "remote wins"
        assert key not in mgr._pending_sync
        _, mirror = mgr._memory[key]
        assert json.loads(mirror)["last_query"] == "remote wins"

    def test_failed_flush_keeps_pending_and_merges(self, monkeypatch):
        """A mid-flush SET failure retains the pending entry; the current
        op merges into fallback state instead of writing through — the
        next healthy op flushes every turn."""

        class FlapRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self.fail_sets = 0

            def _apply_set(self, name, value, ex=None):
                if self.fail_sets:
                    self.fail_sets -= 1
                    raise ConnectionError("redis flapped mid-flush")
                super()._apply_set(name, value, ex)

        fake = FlapRedis()
        box = {"client": fake}
        monkeypatch.setattr(conv, "get_redis", _redis_box(box))
        mgr = conv.ConversationContextManager()
        key = _key("s1")
        asyncio.run(mgr.save("s1", _state()))

        box["client"] = None  # outage
        asyncio.run(mgr.record_turn("s1", user_text="offline turn"))
        assert key in mgr._pending_sync

        box["client"] = fake  # "recovery" — the flush SET flaps
        fake.fail_sets = 1
        asyncio.run(mgr.record_turn("s1", user_text="second turn"))

        # Nothing was written through or discarded: the pending entry
        # now carries both turns.
        assert key in mgr._pending_sync
        _, pending_blob = mgr._pending_sync[key]
        pending_turns = [t["text"] for t in json.loads(pending_blob)["turns"]]
        assert "offline turn" in pending_turns
        assert "second turn" in pending_turns
        remote_turns = [t["text"] for t in json.loads(fake._data[key])["turns"]]
        assert "offline turn" not in remote_turns
        assert "second turn" not in remote_turns

        # The next op reconciles — every turn reaches Redis.
        asyncio.run(mgr.record_turn("s1", user_text="third turn"))
        assert mgr._pending_sync == {}
        remote_turns = [t["text"] for t in json.loads(fake._data[key])["turns"]]
        assert "offline turn" in remote_turns
        assert "second turn" in remote_turns
        assert "third turn" in remote_turns

    def test_expired_pending_dropped_not_resurrected(self, monkeypatch):
        """A pending entry whose TTL lapsed during the outage is dropped
        on the next op — never flushed back with a fresh TTL."""
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        key = _key("s1")
        stale = json.dumps(_state(last_query="old context").to_dict(), ensure_ascii=False)
        mgr._pending_sync[key] = (time.time() - 1, stale)
        mgr._memory[key] = (time.time() - 1, stale)

        assert asyncio.run(mgr.load("s1")) is None
        assert key not in fake._data  # no resurrection SET landed
        assert key not in mgr._pending_sync
        assert key not in mgr._memory

    def test_flush_failure_still_prunes_expired_pending(self, monkeypatch):
        """A transport failure reconciling key A must not skip the expiry
        check for key B — expired entries are swept before any reconcile
        runs, so a failed flush can never leave dead context behind for
        ``_read`` to resurrect."""
        key_a = _key("live")
        key_b = _key("expired")

        class FlapRedis(FakeRedis):
            async def get(self, name):
                if name == key_a:
                    raise ConnectionError("redis flapped mid-flush")
                return await super().get(name)

        fake = FlapRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        live = json.dumps(_state(last_query="still fresh").to_dict(), ensure_ascii=False)
        stale = json.dumps(_state(last_query="old context").to_dict(), ensure_ascii=False)
        # Insertion order puts the live key first — the buggy loop broke
        # on A's reconcile before ever reaching B's expiry check.
        mgr._pending_sync[key_a] = (time.time() + 3600, live)
        mgr._pending_sync[key_b] = (time.time() - 1, stale)
        mgr._memory[key_a] = (time.time() + 3600, live)
        mgr._memory[key_b] = (time.time() - 1, stale)

        assert asyncio.run(mgr.load("expired")) is None
        assert key_b not in mgr._pending_sync  # pruned despite A's failure
        assert key_b not in mgr._memory
        assert key_a in mgr._pending_sync  # live pending survives the flap


# ─── state bounds (F7) ───────────────────────────────────────────────────────


class TestStateBounds:
    def test_config_clamped(self):
        assert conv.ConversationContextManager(max_turns=0)._max_turns == 1
        assert conv.ConversationContextManager(max_turns=99)._max_turns == 20
        assert conv.ConversationContextManager(max_sources=-3)._max_sources == 0
        assert conv.ConversationContextManager(max_sources=99)._max_sources == 10

    def test_zero_max_turns_keeps_latest_turn(self, monkeypatch):
        # Regression: ``[-0:]`` used to keep EVERYTHING — clamped to 1.
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager(max_turns=0)
        state = _state(turns=[{"role": "user", "text": f"t{i}"} for i in range(5)])
        asyncio.run(mgr.save("s1", state))
        loaded = asyncio.run(mgr.load("s1"))
        assert [t["text"] for t in loaded.turns] == ["t4"]

    def test_oversized_decoded_state_sanitized(self, monkeypatch):
        # A blob written by an older/buggy version (or tampered) is
        # re-bounded on decode — counts AND per-string lengths.
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        fake._data[_key("s1")] = json.dumps(
            {
                "last_query": "q" * 900,
                "last_answer_summary": "a" * 900,
                "resolved_entities": ["e" * 300],
                "recent_sources": [{"url": "https://" + "u" * 3000, "title": "t" * 500}],
                "turns": [{"role": "user", "text": "x" * 900}] * 30,
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
        loaded = asyncio.run(mgr.load("s1"))
        assert len(loaded.last_query) <= 500
        assert len(loaded.last_answer_summary) <= 500
        assert len(loaded.resolved_entities[0]) <= 200
        assert len(loaded.recent_sources[0]["url"]) <= 2048
        assert len(loaded.recent_sources[0]["title"]) <= 300
        assert len(loaded.turns) <= 20
        assert all(len(t["text"]) <= 500 for t in loaded.turns)

    def test_giant_entity_truncated_on_write(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state(resolved_entities=["e" * 500])))
        loaded = asyncio.run(mgr.load("s1"))
        assert len(loaded.resolved_entities[0]) <= 200

    def test_blob_size_cap_sheds_sources_first(self, monkeypatch):
        monkeypatch.setattr(conv, "_MAX_STATE_BYTES", 2048)
        state = _state(
            recent_sources=[
                {"url": f"https://{i}.example/" + "u" * 100, "title": "t" * 200} for i in range(10)
            ],
            turns=[{"role": "user", "text": "x" * 400} for _ in range(10)],
            resolved_entities=["e" * 150] * 8,
        )
        blob = conv._bounded_blob(state)
        assert len(blob.encode("utf-8")) <= 2048
        assert state.recent_sources == []  # sources shed first

    def test_decode_enforces_aggregate_byte_cap(self):
        """Per-field caps alone can still overshoot the serialized
        ceiling — multibyte chars at legal counts. Decode must shed like
        the write path (sources → oldest turns → entities), not trust
        the per-field limits to compose under the cap."""
        data = {
            "last_query": "q",
            "last_answer_summary": "s",
            "resolved_entities": [f"{i}-" + "𠀋" * 198 for i in range(10)],
            "resolved_locations": [f"{i}-" + "𠀋" * 198 for i in range(5)],
            "important_constraints": [f"{i}-" + "𠀋" * 198 for i in range(10)],
            "recent_sources": [
                {"url": f"https://{i}.example/" + "𠀋" * 2000, "title": "𠀋" * 300}
                for i in range(10)
            ],
            "turns": [{"role": "user", "text": "𠀋" * 500} for _ in range(15)],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        raw = json.dumps(data, ensure_ascii=False)
        assert len(raw.encode("utf-8")) > conv._MAX_STATE_BYTES  # repro is oversized
        state = conv.ConversationState.from_dict(json.loads(raw))
        out = json.dumps(state.to_dict(), ensure_ascii=False).encode("utf-8")
        assert len(out) <= conv._MAX_STATE_BYTES
        assert len(state.recent_sources) < 10  # sources shed first
        assert len(state.turns) == 15  # turns survive — sources sufficed


# ─── heuristic truncation (F6) ───────────────────────────────────────────────


class TestHeuristicTruncation:
    def test_long_anchor_truncated_not_question(self):
        # 500-char previous query + follow-up → full current question kept.
        state = _state(last_query="x" * 500)
        gw = FakeGateway(None)  # LLM down → deterministic heuristic
        out = asyncio.run(conv.resolve_followup("Ông ấy sinh năm bao nhiêu?", state, gateway=gw))
        assert out is not None and out.source == "heuristic"
        assert out.standalone_query.endswith("Ông ấy sinh năm bao nhiêu?")
        assert len(out.standalone_query) <= 2000

    def test_anchor_cut_to_fit_big_query(self):
        q = "Ông ấy " + "t" * 1900
        out = conv._heuristic_resolve(q, _state(last_query="y" * 500))
        assert out is not None
        assert out.standalone_query.endswith(q)
        assert len(out.standalone_query) <= 2000

    def test_no_room_for_anchor_passes_through(self):
        # A max-length query leaves no room — unresolved, never a fake
        # "resolution" that drops the question.
        q = "Ông ấy " + "t" * 2000
        assert conv._heuristic_resolve(q, _state()) is None


# ─── history merge ───────────────────────────────────────────────────────────


class TestMerge:
    def test_history_only_builds_state(self):
        state = conv._merge_history(
            None,
            [["human", "Ai là CEO của công ty ABC?"], ["ai", "Nguyễn Văn A."]],
        )
        assert state is not None
        assert state.last_query == "Ai là CEO của công ty ABC?"
        assert [t["role"] for t in state.turns] == ["user", "assistant"]

    def test_stored_wins_scalars_history_supplies_turns(self):
        stored = _state()
        merged = conv._merge_history(
            stored, [["human", "khác hẳn câu đã lưu"], ["human", "một câu nữa"]]
        )
        assert merged.last_query == stored.last_query  # stored wins
        texts = [t["text"] for t in merged.turns]
        assert texts[:2] == ["khác hẳn câu đã lưu", "một câu nữa"]
        assert texts[-1] == stored.turns[-1]["text"]

    def test_dict_turns_normalized(self):
        state = conv._merge_history(
            None, [{"role": "human", "content": "câu hỏi"}, {"role": "bot", "message": "trả lời"}]
        )
        assert [t["role"] for t in state.turns] == ["user", "assistant"]

    def test_no_state_no_history(self):
        assert conv._merge_history(None, None) is None
        assert conv._merge_history(None, []) is None


# ─── API integration (TestClient + stubbed pipeline) ─────────────────────────


_FAKE_RESULT = {
    "answer": "Nguyễn Văn A là CEO của công ty ABC.",
    "confidence": 0.8,
    "sources": [{"url": "https://a.example", "title": "A", "score": 0.9}],
    "citations": [],
    "search": {"generated_queries": ["q"], "raw_results": 1, "pages_read": 1},
    "verification": {"claims_total": 1, "claims_verified": 1},
    "timings": {"total": 0.1},
}


def _stub_run(captured: dict, result=None):
    async def fake_run(context, **kw):
        captured["query"] = context.query
        return result if result is not None else dict(_FAKE_RESULT)

    return fake_run


def _wire(monkeypatch, *, redis_client=None, gateway=None):
    """Patch Redis, gateway and the pipeline; returns (captured, FakeGateway)."""
    fake_redis = redis_client if redis_client is not None else FakeRedis()
    gw = gateway or FakeGateway(
        {
            "standalone_query": "CEO công ty ABC Nguyễn Văn A sinh năm bao nhiêu",
            "entities": ["Nguyễn Văn A", "công ty ABC"],
            "locations": [],
            "constraints": [],
        }
    )
    monkeypatch.setattr(conv, "get_redis", _redis_returning(fake_redis))
    monkeypatch.setattr(conv, "get_inference_gateway", lambda: gw)
    captured: dict = {}
    fake = _stub_run(captured)
    # /v1/answer uses the module-level import in api.v1; _research_search
    # re-imports run_research locally — patch both names.
    monkeypatch.setattr(api_v1, "run_research", fake)
    import agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_research", fake)

    # Answer cache: fresh instance per test — the shared singleton would
    # otherwise serve repeats across parametrized cases (intended behavior
    # in production, but it must not leak between tests).
    async def _no_embed(_q):
        return None

    monkeypatch.setattr(sc, "semantic_cache", sc.SemanticCache(embedder=_no_embed))
    return fake_redis, gw, captured


def _stub_orchestrator(monkeypatch):
    """Keep ``/v1/search`` hermetic — regex analyze, no provider lanes."""
    from unittest.mock import MagicMock

    from core.query_understanding import QueryProfile

    orch = MagicMock()
    orch.query_understanding.analyze.side_effect = lambda q: QueryProfile(
        language="vi" if "à" in q or "ế" in q else "en"
    )
    orch.registry.get.return_value = None
    monkeypatch.setattr(api_v1, "_get_orchestrator", lambda: orch)
    return orch


# The acceptance pair, accented and accent-free — the unaccented variant
# must resolve identically (F5: diacritic-insensitive marker matching).
_ACCEPTANCE_PAIRS = [
    ("Ai là CEO của công ty ABC?", "Ông ấy sinh năm bao nhiêu?"),
    ("Ai la CEO cua cong ty ABC?", "Ong ay sinh nam bao nhieu?"),
]


class TestAnswerEndpoint:
    @pytest.mark.parametrize("q1,q2", _ACCEPTANCE_PAIRS)
    def test_acceptance_followup_resolves_against_stored_state(self, monkeypatch, q1, q2):
        """Q1 persists under the session; Q2's anaphora resolves before the pipeline."""
        fake_redis, gw, captured = _wire(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        r1 = client.post("/v1/answer", json={"query": q1, "session_id": "sess-a"})
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["session_id"] == "sess-a"
        assert body1["followup_resolved"] is False
        assert captured["query"] == q1  # standalone question passed through

        stored = json.loads(fake_redis._data[_key("sess-a")])
        assert stored["last_query"] == q1
        assert stored["last_answer_summary"]

        r2 = client.post(
            "/v1/answer",
            json={"query": q2, "session_id": "sess-a"},
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["session_id"] == "sess-a"
        assert body2["followup_resolved"] is True
        resolved = body2["resolved_query"]
        assert "ABC" in resolved and "CEO" in resolved
        # The pipeline received the standalone query, not the raw anaphora.
        assert captured["query"] == resolved
        assert captured["query"] != q2
        # The resolver was invoked with the Q1 state.
        assert q1 in gw.last_user_message

    def test_no_session_zero_diff(self, monkeypatch):
        _wire(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        resp = TestClient(app).post("/v1/answer", json={"query": "q", "mode": "research"})
        assert resp.status_code == 200
        body = resp.json()
        assert "session_id" not in body
        assert "resolved_query" not in body
        assert "followup_resolved" not in body

    def test_no_context_zero_machinery(self, monkeypatch):
        """Neither session_id nor history → no manager access, no Redis touch."""
        _wire(monkeypatch)
        calls = {"manager": 0, "redis": 0}

        def _mgr_spy():
            calls["manager"] += 1
            return conv.ConversationContextManager()

        async def _redis_spy():
            calls["redis"] += 1
            return None

        monkeypatch.setattr(conv, "get_conversation_manager", _mgr_spy)
        monkeypatch.setattr(api_v1, "get_conversation_manager", _mgr_spy)
        monkeypatch.setattr(conv, "get_redis", _redis_spy)
        from fastapi.testclient import TestClient
        from main import app

        resp = TestClient(app).post("/v1/answer", json={"query": "q", "mode": "research"})
        assert resp.status_code == 200
        assert calls == {"manager": 0, "redis": 0}
        body = resp.json()
        assert "session_id" not in body and "followup_resolved" not in body

    def test_history_stateless_resolution(self, monkeypatch):
        """Follow-up + explicit history → resolved, fields present (no session_id)."""
        _, gw, captured = _wire(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        resp = TestClient(app).post(
            "/v1/answer",
            json={
                "query": "Ông ấy sinh năm bao nhiêu?",
                "history": [
                    ["human", "Ai là CEO của công ty ABC?"],
                    ["ai", "Nguyễn Văn A."],
                ],
            },
        )
        body = resp.json()
        assert body["followup_resolved"] is True
        assert "ABC" in body["resolved_query"]
        assert "session_id" not in body
        assert captured["query"] == body["resolved_query"]
        assert len(gw.calls) == 1  # the resolver ran — the query is a follow-up

    def test_history_nonfollowup_zero_diff(self, monkeypatch):
        """history + standalone question → no LLM call, NO conversation
        fields — byte-identical shape to a context-free request."""
        _, gw, captured = _wire(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        q = "What is the current market capitalization of Vinamilk on HOSE"
        resp = TestClient(app).post(
            "/v1/answer",
            json={
                "query": q,
                "history": [["human", "Ai là CEO?"], ["ai", "Nguyễn Văn A."]],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "session_id" not in body
        assert "followup_resolved" not in body
        assert "resolved_query" not in body
        assert gw.calls == []  # resolver never invoked for a non-follow-up
        assert captured["query"] == q  # raw query reached the pipeline

    def test_redis_down_request_still_succeeds(self, monkeypatch):
        _wire(monkeypatch, redis_client=None)
        monkeypatch.setattr(conv, "get_redis", _redis_returning(None))
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        r1 = client.post(
            "/v1/answer", json={"query": "Một câu hỏi độc lập dài", "session_id": "sess-mem"}
        )
        assert r1.status_code == 200
        assert r1.json()["session_id"] == "sess-mem"
        # In-memory fallback kept the turn — the follow-up resolves.
        r2 = client.post(
            "/v1/answer",
            json={"query": "Ông ấy sinh năm bao nhiêu?", "session_id": "sess-mem"},
        )
        body = r2.json()
        assert body["followup_resolved"] is True
        assert "resolved_query" in body

    def test_disabled_feature_passes_through(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(settings, "conversation_context_enabled", False)
        from fastapi.testclient import TestClient
        from main import app

        resp = TestClient(app).post(
            "/v1/answer",
            json={"query": "Ông ấy sinh năm bao nhiêu?", "session_id": "sess-off"},
        )
        body = resp.json()
        assert body["followup_resolved"] is False
        assert "resolved_query" not in body

    def test_disabled_zero_store_calls(self, monkeypatch):
        """Feature flag off → no resolution AND no persistence on either lane."""
        fake_redis, _, _ = _wire(monkeypatch)
        _stub_orchestrator(monkeypatch)
        monkeypatch.setattr(settings, "conversation_context_enabled", False)
        mgr = conv.ConversationContextManager()
        record_calls = []
        orig_record = mgr.record_turn

        async def _counted(*a, **kw):
            record_calls.append(1)
            return await orig_record(*a, **kw)

        monkeypatch.setattr(mgr, "record_turn", _counted)
        monkeypatch.setattr(conv, "get_conversation_manager", lambda: mgr)
        monkeypatch.setattr(api_v1, "get_conversation_manager", lambda: mgr)
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        r1 = client.post(
            "/v1/answer",
            json={"query": "Ông ấy sinh năm bao nhiêu?", "session_id": "sess-off"},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/v1/search",
            json={"query": "Ông ấy?", "mode": "fast", "session_id": "sess-off"},
        )
        assert r2.status_code == 200
        assert record_calls == []  # record_turn never invoked
        assert fake_redis.ops == []  # no Redis command issued
        assert mgr._memory == {} and mgr._pending_sync == {}


class TestSearchEndpoint:
    @pytest.mark.parametrize("q1,q2", _ACCEPTANCE_PAIRS)
    def test_search_mode_resolves_followup(self, monkeypatch, q1, q2):
        _, _, captured = _wire(monkeypatch)
        _stub_orchestrator(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        client.post(
            "/v1/search",
            json={"query": q1, "mode": "fast", "session_id": "sess-s"},
        )
        resp = client.post(
            "/v1/search",
            json={
                "query": q2,
                "mode": "fast",
                "session_id": "sess-s",
            },
        )
        body = resp.json()
        assert body["session_id"] == "sess-s"
        assert body["followup_resolved"] is True
        assert "ABC" in body["resolved_query"]
        assert captured["query"] == body["resolved_query"]

    def test_search_raw_lane_untouched(self, monkeypatch):
        _wire(monkeypatch)
        _stub_orchestrator(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        # No ``mode`` → raw lane; session_id must not break it.
        resp = TestClient(app).post("/v1/search", json={"query": "q", "session_id": "sess-raw"})
        assert resp.status_code == 200


class TestStreamEndpoint:
    @pytest.mark.parametrize("q1,q2", _ACCEPTANCE_PAIRS)
    def test_sse_init_and_done_carry_conversation_fields(self, monkeypatch, q1, q2):
        fake_redis, _, captured = _wire(monkeypatch)
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        client.post("/v1/answer", json={"query": q1, "session_id": "sess-sse"})
        resp = client.post(
            "/v1/answer",
            json={
                "query": q2,
                "session_id": "sess-sse",
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events: dict[str, dict] = {}
        current = None
        for line in resp.text.splitlines():
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: ") and current:
                events.setdefault(current, json.loads(line[6:]))
        init = events["init"]
        assert init["session_id"] == "sess-sse"
        assert init["followup_resolved"] is True
        assert "ABC" in init["resolved_query"]
        done = events["done"]
        assert done["session_id"] == "sess-sse"
        assert done["followup_resolved"] is True
        assert captured["query"] == init["resolved_query"]
        # The turn persisted even though the answer streamed.
        stored = json.loads(fake_redis._data[_key("sess-sse")])
        assert stored["turns"][-1]["role"] == "assistant"


class TestRequestResolution:
    def test_disabled_returns_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "conversation_context_enabled", False)
        out = asyncio.run(
            conv.resolve_request_query("Ông ấy?", session_id="x", history=[["human", "h"]])
        )
        assert out.query == "Ông ấy?" and out.context_active is False

    def test_no_context_returns_raw(self):
        out = asyncio.run(conv.resolve_request_query("Ông ấy sinh năm bao nhiêu?"))
        assert out.query == "Ông ấy sinh năm bao nhiêu?"
        assert out.context_active is False and out.resolved is False

    def test_state_no_followup_marks_context_only(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(conv, "get_redis", _redis_returning(fake))
        mgr = conv.ConversationContextManager()
        asyncio.run(mgr.save("s1", _state()))
        out = asyncio.run(
            conv.resolve_request_query(
                "What is the current market capitalization of Vinamilk on HOSE",
                session_id="s1",
                manager=mgr,
            )
        )
        assert out.context_active is True
        assert out.resolved is False
        assert out.query.startswith("What is")
