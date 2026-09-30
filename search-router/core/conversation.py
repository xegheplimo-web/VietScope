"""P4 — Conversation context: per-session state + follow-up resolution.

``ConversationContextManager`` keeps one compact JSON blob per session in
Redis under ``convctx:{owner_hash}:{session_id}`` (TTL-refreshed on write)
and degrades to a bounded in-process map when Redis is unreachable — the
same degrade-by-design contract as ``pipeline/cache.py``. ``owner_hash``
is a truncated sha256 of the authenticated principal (never raw key
material), so a session can only be read or overwritten by the API key
that owns it. Turns recorded in the fallback during an outage are tracked
in ``_pending_sync`` and reconciled back to Redis on the next successful
contact.

The follow-up resolver lives in this module by design: the resolver and
the state schema it consumes evolve together. It rewrites anaphoric
follow-ups ("Ông ấy sinh năm bao nhiêu?") into standalone queries via the
P0 inference gateway (strict JSON, planner role), bounded by
``conversation_context_resolution_timeout``. Every entry point is
fail-open — a down Redis, a missing LLM, a timeout, or invalid JSON falls
back to a deterministic heuristic (or plain pass-through); nothing here
ever raises into the request path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from config import settings
from redis.exceptions import WatchError
from storage.redis_client import get_redis, mark_redis_unavailable

from core.inference_gateway import ModelRole, get_inference_gateway

logger = logging.getLogger(__name__)

_KEY_PREFIX = "convctx"
_ANON_OWNER = "anonymous"

# Stored-state bounds — the blob stays compact on purpose (no raw pages,
# no full transcripts). ``max_turns``/``max_sources`` are configurable and
# clamped to the hard ceilings; the rest are schema constants.
_MAX_SUMMARY_CHARS = 500
_MAX_TURN_CHARS = 500
_MAX_FIELD_CHARS = 200  # entity / location / constraint strings
_MAX_SOURCE_URL_CHARS = 2048
_MAX_SOURCE_TITLE_CHARS = 300
_MAX_ENTITIES = 10
_MAX_LOCATIONS = 5
_MAX_CONSTRAINTS = 10
_HARD_MAX_TURNS = 20  # config clamp ceiling + decode bound
_HARD_MAX_SOURCES = 10  # config clamp ceiling + decode bound
_MAX_STATE_BYTES = 64 * 1024  # serialized blob ceiling — sheds lists, never raises
_MAX_STANDALONE_CHARS = 2000  # SearchRequest.query limit — resolved query must fit
_RECENT_TURNS_FOR_PROMPT = 4
_MEMORY_MAX_SESSIONS = 1000
_RMW_RETRIES = 3  # optimistic-transaction attempts on contended record_turn

# Gate for the resolver: very short queries are always candidates;
# longer ones need an anaphora/deixis marker.
_SHORT_QUERY_WORDS = 4


def _strip_diacritics(text: str) -> str:
    """Fold Vietnamese to lowercase ASCII — NFD, drop combining marks, đ→d.

    ``đ``/``Đ`` have no NFD decomposition, so they are replaced after
    lowering. Used to match anaphora markers against unaccented input.
    """
    decomposed = unicodedata.normalize("NFD", text or "")
    folded = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return folded.lower().replace("đ", "d")


# Anaphora / deixis markers (VI + EN) — word-boundary matched so "nói"
# does not trip on "nó" and "nay" does not trip on "này".
_ANAPHORA_VI = (
    "ông ấy",
    "bà ấy",
    "anh ấy",
    "chị ấy",
    "cô ấy",
    "chú ấy",
    "em ấy",
    "ông ta",
    "bà ta",
    "anh ta",
    "người đó",
    "công ty đó",
    "nước đó",
    "chỗ đó",
    "nơi đó",
    "ở đó",
    "gần nhất",
    "mới nhất",
    "vừa rồi",
    "lúc nãy",
    "rồi sao",
    "sao rồi",
    "thế nào",
    "ra sao",
    "hắn",
    "họ",
    "nó",
    "đó",
    "ấy",
    "kia",
    "này",
    "thế",
    "vậy",
    "thì",
    "còn",
    "tiếp",
)
_ANAPHORA_EN = (
    "that place",
    "the same",
    "the former",
    "the latter",
    "he",
    "she",
    "it",
    "they",
    "them",
    "him",
    "his",
    "her",
    "its",
    "their",
    "that",
    "this",
    "these",
    "those",
    "there",
)
_ANAPHORA_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _ANAPHORA_VI + _ANAPHORA_EN) + r")\b",
    re.IGNORECASE,
)

# Folded single-word markers that collide with plain English words are
# excluded from accent-insensitive matching — "the" (thế), "no" (nó),
# "do" (đó) would mark nearly every English query as a follow-up.
# Multi-word markers stay distinctive when folded.
_FOLDED_SKIP = {"the", "no", "do", "con", "nay", "ho"}
_ANAPHORA_FOLDED_RE = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(m)
        for m in (_strip_diacritics(mk) for mk in _ANAPHORA_VI)
        if m not in _FOLDED_SKIP
    )
    + "|"
    + "|".join(re.escape(m) for m in _ANAPHORA_EN)
    + r")\b",
    re.IGNORECASE,
)

_ROLE_ALIASES = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "bot": "assistant",
    "system": "system",
}

_RESOLVER_SYSTEM_PROMPT = (
    "You rewrite follow-up questions into standalone search queries using "
    "the provided conversation state. Resolve pronouns and deixis "
    "(he/she/it/ông ấy/bà ấy/nó/chỗ đó...) against the last query, answer "
    "summary, and entities. Keep the query's language — Vietnamese input "
    "produces a Vietnamese standalone query, English produces English. Do "
    "NOT answer the question. Return ONLY a JSON object: "
    '{"standalone_query": "...", "entities": ["..."], '
    '"locations": ["..."], "constraints": ["..."]}. '
    "entities/locations/constraints may be empty arrays."
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _owner_hash(owner: str | None) -> str:
    """Short sha256 of the owner principal — raw key material never lands
    in a Redis key."""
    return hashlib.sha256((owner or _ANON_OWNER).encode("utf-8")).hexdigest()[:16]


def _dedupe_strs(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _str_list(value: Any, cap: int) -> list[str]:
    """Strict ``list[str]`` extraction — a non-list or mixed-type value is
    ignored wholesale rather than ``str()``-coerced."""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return []
    return _dedupe_strs([v.strip()[:_MAX_FIELD_CHARS] for v in value])[:cap]


def _state_ts(raw: Any) -> datetime | None:
    """Parse ``updated_at`` out of a serialized blob (None if unreadable)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        ts = datetime.fromisoformat(str(data.get("updated_at") or "").replace("Z", "+00:00"))
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001 — unparseable = no timestamp
        return None


def _local_newer(local_blob: str, remote_raw: str | None) -> bool:
    """Recovery reconciliation: the pending blob overwrites Redis unless
    the remote is STRICTLY newer. Pending entries exist only because they
    were recorded after the last successful write, so a timestamp tie
    (coarse clocks can make two writes share a tick) still goes local."""
    if remote_raw is None:
        return True
    remote_ts = _state_ts(remote_raw)
    if remote_ts is None:
        return True  # corrupt remote — the pending blob owns the key
    local_ts = _state_ts(local_blob)
    if local_ts is None:
        return False
    try:
        return local_ts >= remote_ts
    except TypeError:  # naive-vs-aware compare — prefer the known-good blob
        return True


@dataclass
class ConversationState:
    """Bounded per-session context blob stored under ``convctx:{owner}:{id}``."""

    last_query: str = ""
    last_answer_summary: str = ""
    resolved_entities: list[str] = field(default_factory=list)
    resolved_locations: list[str] = field(default_factory=list)
    important_constraints: list[str] = field(default_factory=list)
    recent_sources: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "last_query": self.last_query,
            "last_answer_summary": self.last_answer_summary,
            "resolved_entities": list(self.resolved_entities),
            "resolved_locations": list(self.resolved_locations),
            "important_constraints": list(self.important_constraints),
            "recent_sources": list(self.recent_sources),
            "turns": list(self.turns),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ConversationState:
        """Tolerant decode — unknown keys ignored, bad types coerced to
        empty, and every field re-bounded on the way in (including the
        aggregate serialized cap). Blobs written by an older/buggy
        version (or tampered with) must not resurrect oversized state."""
        if not isinstance(data, dict):
            return cls()
        turns = [
            {
                "role": str(t.get("role") or "user")[:32],
                "text": str(t.get("text") or "")[:_MAX_TURN_CHARS],
            }
            for t in (data.get("turns") or [])
            if isinstance(t, dict)
        ][-_HARD_MAX_TURNS:]
        sources = [
            {
                "url": str(s.get("url") or "")[:_MAX_SOURCE_URL_CHARS],
                "title": str(s.get("title") or "")[:_MAX_SOURCE_TITLE_CHARS],
            }
            for s in (data.get("recent_sources") or [])
            if isinstance(s, dict) and s.get("url")
        ][:_HARD_MAX_SOURCES]
        state = cls(
            last_query=str(data.get("last_query") or "")[:_MAX_TURN_CHARS],
            last_answer_summary=str(data.get("last_answer_summary") or "")[:_MAX_SUMMARY_CHARS],
            resolved_entities=_str_list(data.get("resolved_entities"), _MAX_ENTITIES),
            resolved_locations=_str_list(data.get("resolved_locations"), _MAX_LOCATIONS),
            important_constraints=_str_list(data.get("important_constraints"), _MAX_CONSTRAINTS),
            recent_sources=sources,
            turns=turns,
            updated_at=str(data.get("updated_at") or "")[:64],
        )
        # Per-field caps can still overshoot the serialized ceiling on
        # multibyte input — enforce the same aggregate bound as writes.
        _bounded_blob(state)
        return state

    def compacted(self, *, max_turns: int, max_sources: int) -> ConversationState:
        """Enforce bounds on write: drop oldest turns, truncate strings/lists."""
        self.last_query = str(self.last_query or "")[:_MAX_TURN_CHARS]
        self.last_answer_summary = str(self.last_answer_summary or "")[:_MAX_SUMMARY_CHARS]
        self.resolved_entities = [
            str(e or "")[:_MAX_FIELD_CHARS] for e in self.resolved_entities[:_MAX_ENTITIES]
        ]
        self.resolved_locations = [
            str(loc or "")[:_MAX_FIELD_CHARS] for loc in self.resolved_locations[:_MAX_LOCATIONS]
        ]
        self.important_constraints = [
            str(c or "")[:_MAX_FIELD_CHARS] for c in self.important_constraints[:_MAX_CONSTRAINTS]
        ]
        self.recent_sources = [
            {
                "url": str(s.get("url") or "")[:_MAX_SOURCE_URL_CHARS],
                "title": str(s.get("title") or "")[:_MAX_SOURCE_TITLE_CHARS],
            }
            for s in self.recent_sources[:max_sources]
            if isinstance(s, dict)
        ]
        self.turns = [
            {
                "role": str(t.get("role") or "user")[:32],
                "text": str(t.get("text") or "")[:_MAX_TURN_CHARS],
            }
            for t in self.turns
            if isinstance(t, dict)
        ][-max_turns:]
        return self


@dataclass
class ResolutionResult:
    """Resolver output: standalone query + optional state updates."""

    standalone_query: str
    entities: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | heuristic


@dataclass
class RequestResolution:
    """What the API layer needs: the effective query + context bookkeeping."""

    query: str  # effective (possibly rewritten) query for the pipeline
    resolved: bool = False  # a follow-up resolution produced this query
    context_active: bool = False  # session/history context engaged
    state: ConversationState | None = None
    resolution: ResolutionResult | None = None


def _bounded_blob(state: ConversationState) -> str:
    """Serialize under ``_MAX_STATE_BYTES`` — sheds sources first, then
    oldest turns, then entity/location/constraint lists; never raises."""
    while True:
        blob = json.dumps(state.to_dict(), ensure_ascii=False)
        if len(blob.encode("utf-8")) <= _MAX_STATE_BYTES:
            return blob
        if state.recent_sources:
            state.recent_sources.pop()
        elif state.turns:
            state.turns.pop(0)  # oldest first
        elif state.resolved_entities:
            state.resolved_entities.pop()
        elif state.resolved_locations:
            state.resolved_locations.pop()
        elif state.important_constraints:
            state.important_constraints.pop()
        else:
            return blob  # scalars are individually capped — cannot shrink further


def _has_anaphora(query: str) -> bool:
    """Marker hit on the raw query OR its accent-folded form.

    Unaccented Vietnamese ("Ong ay sinh nam bao nhieu?") must match
    markers like "ông ấy"; folded forms that collide with English words
    ("the", "no", "do"...) are excluded so plain English stays unmarked.
    """
    if not query:
        return False
    return bool(_ANAPHORA_RE.search(query)) or bool(
        _ANAPHORA_FOLDED_RE.search(_strip_diacritics(query))
    )


def looks_like_followup(query: str) -> bool:
    """Cheap gate — resolution only runs on plausible follow-ups.

    An anaphora/deixis marker qualifies at any length; without one the
    query must be very short (``<= _SHORT_QUERY_WORDS`` words). Long
    standalone-looking queries are skipped to protect latency.
    """
    if _has_anaphora(query):
        return True
    return len((query or "").split()) <= _SHORT_QUERY_WORDS


def _normalize_role(role: str) -> str:
    return _ROLE_ALIASES.get(role.strip().lower(), role.strip().lower() or "user")


def _turns_from_history(history: list | None) -> list[dict]:
    """Normalize client ``history`` ([role, text] pairs or dicts) into turns."""
    turns: list[dict] = []
    for item in history or []:
        role = text = None
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            role, text = item[0], item[1]
        elif isinstance(item, dict):
            role = item.get("role") or item.get("speaker") or item.get("from")
            text = item.get("text") or item.get("content") or item.get("message")
        if role is None or text is None:
            continue
        turns.append({"role": _normalize_role(str(role)), "text": str(text)[:_MAX_TURN_CHARS]})
    return turns


def _dedupe_turns(turns: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for t in turns:
        key = (t.get("role") or "", t.get("text") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _merge_history(
    state: ConversationState | None, history: list | None
) -> ConversationState | None:
    """Merge client ``history`` into stored session state.

    Precedence: the stored state wins on scalar fields (``last_query``,
    ``last_answer_summary``) and already-resolved lists; ``history``
    contributes older ``turns`` (stored turns stay last — they are the
    most recent record of the session). With no stored state, history
    alone builds a stateless context whose ``last_query`` is the last
    user turn.
    """
    hist_turns = _turns_from_history(history)
    if state is None:
        if not hist_turns:
            return None
        state = ConversationState(turns=hist_turns)
        for t in reversed(hist_turns):
            if t["role"] == "user":
                state.last_query = t["text"]
                break
        return state
    if hist_turns:
        state.turns = _dedupe_turns(hist_turns + state.turns)
    return state


class ConversationContextManager:
    """Redis-backed session store with in-memory fallback (never raises).

    Keys are ``convctx:{owner_hash}:{session_id}`` — the owner binds a
    session to the authenticated principal so one API key can never read
    or overwrite another's conversation state (the in-memory fallback is
    keyed by the same composite). Writes mirror to the bounded fallback
    map; while Redis is down they also land in ``_pending_sync`` and are
    reconciled on the next successful Redis op.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = None,
        max_turns: int | None = None,
        max_sources: int | None = None,
    ) -> None:
        self._ttl = (
            ttl_seconds if ttl_seconds is not None else settings.conversation_context_ttl_seconds
        )
        turns = max_turns if max_turns is not None else settings.conversation_context_max_turns
        sources = (
            max_sources if max_sources is not None else settings.conversation_context_max_sources
        )
        # Clamp config: 0 turns is invalid (``[-0:]`` keeps everything),
        # 0 sources is legal (state simply stores no sources).
        self._max_turns = min(_HARD_MAX_TURNS, max(1, int(turns)))
        self._max_sources = min(_HARD_MAX_SOURCES, max(0, int(sources)))
        self._memory: OrderedDict[str, tuple[float, str]] = OrderedDict()
        # Pending entries carry their own expiry (write time + TTL) so an
        # outage longer than the TTL cannot resurrect dead context.
        self._pending_sync: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def key(session_id: str, owner: str | None = None) -> str:
        return f"{_KEY_PREFIX}:{_owner_hash(owner)}:{session_id}"

    async def load(self, session_id: str, *, owner: str | None = None) -> ConversationState | None:
        key = self.key(session_id, owner)
        raw = await self._read(key)
        return self._decode_state(raw, key)

    async def save(
        self, session_id: str, state: ConversationState, *, owner: str | None = None
    ) -> None:
        try:
            await self._write(self.key(session_id, owner), self._serialize_state(state))
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning("conversation save failed for %s", session_id, exc_info=True)

    async def record_turn(
        self,
        session_id: str,
        *,
        owner: str | None = None,
        user_text: str = "",
        resolved_query: str = "",
        answer: str = "",
        sources: list | None = None,
        resolution: ResolutionResult | None = None,
    ) -> None:
        """Fold one completed exchange into the session state (fail-open).

        The Redis path is an optimistic WATCH/MULTI transaction with
        bounded retries, and the fallback path holds ``_lock`` across the
        whole read-modify-write — concurrent recorders on one session
        merge instead of last-writer-wins losing a turn.
        """
        key = self.key(session_id, owner)
        try:
            client = None
            try:
                client = await get_redis()
            except Exception:  # noqa: BLE001 — drop to memory below
                mark_redis_unavailable(client)
                client = None
            last_raw: str | None = None
            if client is not None:
                done, last_raw = await self._record_turn_redis(
                    client,
                    key,
                    user_text=user_text,
                    resolved_query=resolved_query,
                    answer=answer,
                    sources=sources,
                    resolution=resolution,
                )
                if done:
                    return
            # Fallback: the lock spans the WHOLE read-modify-write.
            async with self._lock:
                state = (
                    self._decode_state(self._memory_get(key))
                    or self._decode_state(last_raw)
                    or ConversationState()
                )
                self._apply_turn(
                    state,
                    user_text=user_text,
                    resolved_query=resolved_query,
                    answer=answer,
                    sources=sources,
                    resolution=resolution,
                )
                blob = self._serialize_state(state)
                self._memory_set(key, blob)
                self._pending_sync[key] = (time.time() + self._ttl, blob)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning("record_turn failed for %s", session_id, exc_info=True)

    async def _record_turn_redis(self, client, key: str, **turn) -> tuple[bool, str | None]:
        """WATCH/MULTI read-modify-write with bounded retries.

        Returns ``(handled, last_raw)`` — ``handled`` False drops to the
        in-memory fallback; ``last_raw`` is the last state read inside the
        transaction (fallback base when Redis dies mid-write).
        """
        try:
            unreconciled = await self._flush_pending(client)
        except Exception:  # noqa: BLE001 — treat as still-down
            mark_redis_unavailable(client)
            return False, None
        if key in unreconciled:
            # Pending turns never reached Redis — writing through would
            # clobber them. Merge this turn into the fallback instead;
            # the next successful op retries the flush.
            return False, None
        last_raw: str | None = None
        for _ in range(_RMW_RETRIES):
            try:
                async with client.pipeline() as pipe:
                    await pipe.watch(key)
                    last_raw = await pipe.get(key)
                    state = self._decode_state(last_raw) or ConversationState()
                    self._apply_turn(state, **turn)
                    blob = self._serialize_state(state)
                    pipe.multi()
                    pipe.set(key, blob, ex=self._ttl)
                    await pipe.execute()
            except WatchError:
                continue  # a concurrent write landed — re-read and re-apply
            except Exception:  # noqa: BLE001 — Redis died mid-transaction
                mark_redis_unavailable(client)
                return False, last_raw
            # Mirror successful writes so a later Redis flap keeps context.
            async with self._lock:
                self._memory_set(key, blob)
            # Drop the pending entry only if it holds the blob we just
            # committed — a different blob is a concurrent fallback write
            # that still needs its own reconcile.
            pending_entry = self._pending_sync.get(key)
            if pending_entry is not None and pending_entry[1] == blob:
                self._pending_sync.pop(key, None)
            return True, last_raw
        # Contention outlasted the retry bound — dropping the turn is
        # safer than a blind last-writer-wins overwrite.
        logger.warning("record_turn for %s dropped after %d retries", key, _RMW_RETRIES)
        return True, last_raw

    def _apply_turn(
        self,
        state: ConversationState,
        *,
        user_text: str,
        resolved_query: str,
        answer: str,
        sources: list | None,
        resolution: ResolutionResult | None,
    ) -> None:
        """Mutate ``state`` with one exchange — shared by both write paths."""
        # The standalone (resolved) query anchors the next turn — it
        # already carries the resolved entities the user omitted.
        state.last_query = (resolved_query or user_text or "")[:_MAX_TURN_CHARS]
        summary = (answer or "").strip()[:_MAX_SUMMARY_CHARS]
        if summary:
            state.last_answer_summary = summary
        if resolution is not None:
            state.resolved_entities = _dedupe_strs(resolution.entities + state.resolved_entities)[
                :_MAX_ENTITIES
            ]
            state.resolved_locations = _dedupe_strs(
                resolution.locations + state.resolved_locations
            )[:_MAX_LOCATIONS]
            state.important_constraints = _dedupe_strs(
                resolution.constraints + state.important_constraints
            )[:_MAX_CONSTRAINTS]
        recent = [
            {"url": str(url), "title": str(title or "")}
            for url, title in (_source_brief(s) for s in sources or [])
            if url
        ]
        if recent:
            state.recent_sources = recent[: self._max_sources]
        state.turns.append({"role": "user", "text": (user_text or "")[:_MAX_TURN_CHARS]})
        if summary:
            state.turns.append({"role": "assistant", "text": summary})

    def _serialize_state(self, state: ConversationState) -> str:
        state.updated_at = _now_iso()
        state.compacted(max_turns=self._max_turns, max_sources=self._max_sources)
        return _bounded_blob(state)

    @staticmethod
    def _decode_state(raw: str | None, key: str = "") -> ConversationState | None:
        if raw is None:
            return None
        try:
            return ConversationState.from_dict(json.loads(raw))
        except Exception:  # noqa: BLE001 — corrupt blob = absent state
            logger.warning("conversation state for %s is corrupt — ignoring", key or "?")
            return None

    async def _flush_pending(self, client) -> set[str]:
        """Reconcile fallback writes back to Redis after an outage.

        Entries whose own TTL lapsed during the outage are swept FIRST —
        independently of reconciliation — so a transport failure on one
        key can never skip another key's expiry check. Each surviving key
        is then reconciled atomically (WATCH/MULTI — same pattern as
        ``_record_turn_redis``) so a remote write landing mid-flush is
        re-compared, never clobbered; a live entry that flushes gets a
        full fresh TTL, matching the write path. Aborts at the first
        transport failure — remaining entries retry on the next Redis
        operation.

        Returns the keys still pending afterwards — callers must not
        write through to Redis for those keys this round.
        """
        now = time.time()
        for key, entry in list(self._pending_sync.items()):
            expires_at, _ = entry
            if now > expires_at and self._pending_sync.get(key) == entry:
                self._pending_sync.pop(key, None)
                async with self._lock:
                    mem = self._memory.get(key)
                    if mem is not None and now > mem[0]:
                        self._memory.pop(key, None)
        for key, entry in list(self._pending_sync.items()):
            _, blob = entry
            try:
                resolved = await self._reconcile_key(client, key, blob)
            except Exception:  # noqa: BLE001 — Redis flapped again mid-flush
                break  # everything left stays pending for the next op
            if resolved and self._pending_sync.get(key) == entry:
                self._pending_sync.pop(key, None)
        return set(self._pending_sync)

    async def _reconcile_key(self, client, key: str, blob: str) -> bool:
        """WATCH/MULTI reconcile for one pending key.

        Returns True once the key is settled — the pending blob written
        (a pending blob wins unless the remote is STRICTLY newer), or a
        strictly-newer remote kept and mirrored locally. False means
        contention outlasted ``_RMW_RETRIES`` — the entry stays pending.
        Transport errors propagate so the caller aborts the flush.
        """
        for _ in range(_RMW_RETRIES):
            remote: str | None = None
            local_wins = False
            try:
                async with client.pipeline() as pipe:
                    await pipe.watch(key)
                    remote = await pipe.get(key)
                    local_wins = _local_newer(blob, remote)
                    if local_wins:
                        pipe.multi()
                        pipe.set(key, blob, ex=self._ttl)
                        await pipe.execute()
            except WatchError:
                continue  # a remote write landed mid-flush — re-decide
            if remote is not None and not local_wins:
                async with self._lock:
                    self._memory_set(key, remote)
            return True
        return False

    async def _read(self, key: str) -> str | None:
        client = None
        try:
            client = await get_redis()
            if client is not None:
                unreconciled = await self._flush_pending(client)
                if key in unreconciled:
                    entry = self._pending_sync[key]
                    if time.time() <= entry[0]:
                        # Unflushed pending state is fresher than the remote.
                        return entry[1]
                    # Lapsed mid-flush — treat as absent, read remote.
                    self._pending_sync.pop(key, None)
                return await client.get(key)
        except Exception:  # noqa: BLE001 — drop to memory below
            mark_redis_unavailable(client)
        async with self._lock:
            return self._memory_get(key)

    async def _write(self, key: str, blob: str) -> None:
        client = None
        wrote_redis = False
        try:
            client = await get_redis()
            if client is not None:
                unreconciled = await self._flush_pending(client)
                if key not in unreconciled:
                    await client.set(key, blob, ex=self._ttl)
                    pending_entry = self._pending_sync.get(key)
                    if pending_entry is None or pending_entry[1] == blob:
                        self._pending_sync.pop(key, None)
                    wrote_redis = True
        except Exception:  # noqa: BLE001 — memory write still proceeds
            mark_redis_unavailable(client)
        # Mirror the cache pattern: memory is always written so a Redis
        # flap mid-session does not lose context; when the Redis write
        # did not land the blob also stays pending for the next flush.
        async with self._lock:
            self._memory_set(key, blob)
            if not wrote_redis:
                self._pending_sync[key] = (time.time() + self._ttl, blob)

    def _memory_get(self, key: str) -> str | None:
        """Read the fallback mirror — caller must hold ``_lock``."""
        entry = self._memory.get(key)
        if entry is None:
            return None
        expires_at, blob = entry
        if time.time() > expires_at:
            del self._memory[key]
            self._pending_sync.pop(key, None)
            return None
        self._memory.move_to_end(key)
        return blob

    def _memory_set(self, key: str, blob: str) -> None:
        """Write the fallback mirror — caller must hold ``_lock``."""
        self._memory[key] = (time.time() + self._ttl, blob)
        self._memory.move_to_end(key)
        while len(self._memory) > _MEMORY_MAX_SESSIONS:
            evicted, _ = self._memory.popitem(last=False)
            self._pending_sync.pop(evicted, None)


def _source_brief(source: Any) -> tuple[str | None, str | None]:
    if isinstance(source, dict):
        return source.get("url"), source.get("title")
    return getattr(source, "url", None), getattr(source, "title", "")


async def _llm_resolve(gateway, query: str, state: ConversationState) -> ResolutionResult | None:
    """LLM rewrite via the P0 gateway → ResolutionResult, ``None`` on failure."""
    state_blob = {
        "last_query": state.last_query,
        "answer_summary": state.last_answer_summary,
        "entities": state.resolved_entities,
        "locations": state.resolved_locations,
        "constraints": state.important_constraints,
        "recent_turns": state.turns[-_RECENT_TURNS_FOR_PROMPT:],
    }
    messages = [
        {"role": "system", "content": _RESOLVER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Conversation state (JSON):\n"
                + json.dumps(state_blob, ensure_ascii=False)
                + f"\n\nNew user query: {query}"
            ),
        },
    ]
    parsed = await gateway.complete_json(
        messages,
        role=ModelRole.PLANNER,
        required_keys=["standalone_query"],
        expect=dict,
        max_tokens=300,
        temperature=0.1,
    )
    if not isinstance(parsed, dict):
        return None
    # Strict schema: standalone_query must be a bounded string — never
    # str()-coerce a dict/list/number into the pipeline query.
    standalone_raw = parsed.get("standalone_query")
    if not isinstance(standalone_raw, str):
        return None
    standalone = standalone_raw.strip()
    if not standalone or len(standalone) > _MAX_STANDALONE_CHARS:
        return None
    # Auxiliary fields must be list[str]; a bad field is ignored (the
    # merge keeps prior state) rather than coerced.
    return ResolutionResult(
        standalone_query=standalone,
        entities=_str_list(parsed.get("entities"), _MAX_ENTITIES),
        locations=_str_list(parsed.get("locations"), _MAX_LOCATIONS),
        constraints=_str_list(parsed.get("constraints"), _MAX_CONSTRAINTS),
        source="llm",
    )


def _heuristic_resolve(query: str, state: ConversationState) -> ResolutionResult | None:
    """Deterministic fallback — crude combine with ``last_query``.

    Only fires when the query carries an anaphora marker; without one the
    query passes through untouched rather than being mangled. Space is
    reserved for the CURRENT question — the anchor (previous query) is
    truncated to fit; when no anchor fits, the query passes through
    unresolved instead of producing a fake rewrite.
    """
    anchor = (state.last_query or "").strip().rstrip("?.!…")
    current = (query or "").strip()
    if not anchor or not _has_anaphora(current):
        return None
    anchor = anchor[: max(0, _MAX_STANDALONE_CHARS - len(current) - 3)]
    if not anchor:
        return None
    return ResolutionResult(
        standalone_query=f"{anchor} — {current}",
        source="heuristic",
    )


async def resolve_followup(
    query: str,
    state: ConversationState,
    *,
    gateway=None,
    timeout: float | None = None,
) -> ResolutionResult | None:
    """Rewrite ``query`` against ``state`` — LLM first, heuristic fallback.

    Bounded by ``conversation_context_resolution_timeout`` (default 2s);
    any failure lands on the deterministic heuristic, never raises.
    """
    bound = settings.conversation_context_resolution_timeout if timeout is None else timeout
    try:
        result = await asyncio.wait_for(
            _llm_resolve(gateway or get_inference_gateway(), query, state),
            timeout=bound,
        )
    except Exception:  # noqa: BLE001 — timeout/transport/parse → fallback
        result = None
    if result is not None:
        return result
    return _heuristic_resolve(query, state)


async def resolve_request_query(
    query: str,
    *,
    session_id: str | None = None,
    history: list | None = None,
    owner: str | None = None,
    manager: ConversationContextManager | None = None,
    gateway=None,
    timeout: float | None = None,
) -> RequestResolution:
    """Resolve ``query`` against conversation context (fail-open).

    ``session_id`` loads the stored session state (scoped to ``owner`` —
    another principal's session reads as absent); ``history`` supplies
    stateless context; both → merged per :func:`_merge_history` (stored
    state wins on conflicting fields). Resolution runs only when context
    exists AND the query looks like a follow-up. With neither
    ``session_id`` nor ``history`` no conversation machinery runs at all.
    """
    out = RequestResolution(query=query)
    if not settings.conversation_context_enabled:
        return out
    if session_id is None and not history:
        return out  # zero machinery — no manager access, no store touch
    try:
        state: ConversationState | None = None
        if session_id:
            state = await (manager or get_conversation_manager()).load(session_id, owner=owner)
        state = _merge_history(state, history)
        if state is None:
            return out
        out.context_active = True
        out.state = state
        if not looks_like_followup(query):
            return out
        resolution = await resolve_followup(query, state, gateway=gateway, timeout=timeout)
        if resolution is None or not resolution.standalone_query:
            return out
        out.resolved = True
        out.resolution = resolution
        out.query = resolution.standalone_query
        return out
    except Exception:  # noqa: BLE001 — never break the request path
        logger.warning("conversation resolution failed — passing query through", exc_info=True)
        return RequestResolution(query=query)


_default_manager: ConversationContextManager | None = None


def get_conversation_manager() -> ConversationContextManager:
    """Process-wide manager — mirrors ``get_inference_gateway``."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ConversationContextManager()
    return _default_manager
