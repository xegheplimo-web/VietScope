"""P17 index synchronization — canonical places → OpenSearch ``places``.

Modes:

- ``rebuild()`` — full reprojection into a NEW concrete index, then an
  atomic alias swap. Readers never see a half-filled index; the old
  generation is dropped only after the swap succeeds.
- ``sync()`` — incremental upsert of rows changed since the durable
  (``updated_at``, ``place_id``) cursor in ``serving_index_state``.
  The cursor advances only after the batch is durably bulked, so a crash
  mid-run re-projects the same rows — upserts by ``place_id`` are
  idempotent.
- ``delete(place_id)`` — tombstone a single place (canonical deletion is
  not a resolver operation, so deletes arrive via ops/reconciliation).
- ``reconcile()`` — index↔canonical id-set diff that removes docs whose
  canonical row no longer exists.

Failures never touch the canonical database: the index is a disposable
read model — a failed sync just resumes from its checkpoint. Every
successful write path bumps the cache epoch so stale search/autocomplete
entries die at the next read.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from serving.places.cache import PlaceCache
from serving.places.document import PLACE_DOCUMENT_VERSION, PlaceDocumentV1
from serving.places.os_index import PlaceIndexUnavailable, PlaceOSIndex
from serving.places.projection import (
    ALIASES_SQL,
    PLACE_IDS_PAGE_SQL,
    SCAN_DELTA_SQL,
    SCAN_PAGE_SQL,
    project_rows,
)

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.4


class IndexState(Protocol):
    """Checkpoint ledger — PG-backed in production, dict-backed in tests."""

    async def load(self) -> dict[str, Any] | None: ...
    async def note_generation(self, generation: int, concrete: str) -> None: ...
    async def advance(
        self,
        *,
        cursor_updated_at: Any,
        cursor_place_id: int,
        docs_delta: int,
        concrete: str | None = None,
    ) -> None: ...
    async def note_error(self, error: str | None) -> None: ...
    async def set_docs_indexed(self, count: int) -> None: ...


class DictIndexState:
    """In-memory ledger — unit tests and the no-DB path."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "generation": 0,
            "concrete_index": None,
            "cursor_updated_at": None,
            "cursor_place_id": 0,
            "docs_indexed": 0,
            "last_error": None,
        }

    async def load(self) -> dict[str, Any]:
        return dict(self.state)

    async def note_generation(self, generation: int, concrete: str) -> None:
        self.state["generation"] = generation
        self.state["concrete_index"] = concrete

    async def advance(
        self, *, cursor_updated_at, cursor_place_id, docs_delta, concrete=None
    ) -> None:
        self.state["cursor_updated_at"] = cursor_updated_at
        self.state["cursor_place_id"] = cursor_place_id
        self.state["docs_indexed"] += docs_delta
        if concrete is not None:
            self.state["concrete_index"] = concrete

    async def note_error(self, error: str | None) -> None:
        self.state["last_error"] = error

    async def set_docs_indexed(self, count: int) -> None:
        self.state["docs_indexed"] = count
        self.state["last_error"] = None


_STATE_UPSERT = """
INSERT INTO serving_index_state (index_name, doc_version, last_run_at)
VALUES ($1, $2, now())
ON CONFLICT (index_name) DO NOTHING
"""

_STATE_LOAD = """
SELECT generation, concrete_index, cursor_updated_at, cursor_place_id,
       docs_indexed, last_error
FROM serving_index_state WHERE index_name = $1
"""

_STATE_ADVANCE = """
UPDATE serving_index_state
SET cursor_updated_at = $2, cursor_place_id = $3,
    docs_indexed = docs_indexed + $4,
    concrete_index = COALESCE($5, concrete_index),
    last_run_at = now(), last_error = NULL
WHERE index_name = $1
"""

_STATE_ERROR = """
UPDATE serving_index_state SET last_error = $2, last_run_at = now()
WHERE index_name = $1
"""


class PgIndexState:
    """``serving_index_state`` ledger (migration 012)."""

    def __init__(self, pool: Any, index_name: str) -> None:
        self._pool = pool
        self._name = index_name

    async def _ensure_row(self) -> None:
        await self._pool.execute(_STATE_UPSERT, self._name, PLACE_DOCUMENT_VERSION)

    async def load(self) -> dict[str, Any] | None:
        await self._ensure_row()
        r = await self._pool.fetchrow(_STATE_LOAD, self._name)
        return dict(r) if r else None

    async def note_generation(self, generation: int, concrete: str) -> None:
        await self._ensure_row()
        await self._pool.execute(
            "UPDATE serving_index_state SET generation = $2, concrete_index = $3,"
            " last_run_at = now() WHERE index_name = $1",
            self._name,
            generation,
            concrete,
        )

    async def advance(
        self, *, cursor_updated_at, cursor_place_id, docs_delta, concrete=None
    ) -> None:
        await self._ensure_row()
        await self._pool.execute(
            _STATE_ADVANCE,
            self._name,
            cursor_updated_at,
            int(cursor_place_id),
            int(docs_delta),
            concrete,
        )

    async def note_error(self, error: str | None) -> None:
        try:
            await self._ensure_row()
            await self._pool.execute(_STATE_ERROR, self._name, (error or "")[:500])
        except Exception:
            logger.warning("could not record index error", exc_info=True)

    async def set_docs_indexed(self, count: int) -> None:
        await self._ensure_row()
        await self._pool.execute(
            "UPDATE serving_index_state SET docs_indexed = $2,"
            " last_error = NULL WHERE index_name = $1",
            self._name,
            count,
        )


class PlaceIndexer:
    """Canonical → index synchronizer. PG pool is the canonical read side;
    the OpenSearch lane is the write side; the ledger survives crashes."""

    def __init__(
        self,
        pool: Any | None,
        *,
        os_index: PlaceOSIndex | None = None,
        cache: PlaceCache | None = None,
        state: IndexState | None = None,
    ) -> None:
        self._pool = pool
        self._os = os_index or PlaceOSIndex()
        self._cache = cache or PlaceCache()
        if state is not None:
            self._state = state
        elif pool is not None:
            self._state = PgIndexState(pool, self._os.alias)
        else:
            self._state = DictIndexState()

    # ── projection helpers ───────────────────────────────────────────────

    async def _aliases(self, place_ids: list[int]) -> dict[int, list[str]]:
        if not place_ids or self._pool is None:
            return {}
        rows = await self._pool.fetch(ALIASES_SQL, place_ids)
        out: dict[int, list[str]] = {}
        for r in rows:
            d = dict(r)
            pid, name = d.get("place_id"), d.get("raw_name")
            if pid is not None and name:
                out.setdefault(int(pid), []).append(name)
        return out

    async def _project_page(self, rows: list[dict[str, Any]]) -> list[PlaceDocumentV1]:
        aliases = await self._aliases([int(r["place_id"]) for r in rows])
        return project_rows(rows, aliases_by_place=aliases)

    async def _bulk_with_retry(self, docs, *, index: str | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return await self._os.upsert_docs(docs, index=index)
            except PlaceIndexUnavailable as exc:
                last = exc
                await asyncio.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        raise last or PlaceIndexUnavailable("bulk failed")

    # ── public API ───────────────────────────────────────────────────────

    async def ensure(self) -> str:
        return await self._os.ensure()

    async def rebuild(self, *, batch_size: int = 1000) -> dict[str, Any]:
        """Full reindex into a fresh concrete index + atomic alias swap."""
        if self._pool is None:
            return {"status": "unavailable", "reason": "postgres"}
        await self._os.ensure()
        state = await self._state.load() or {}
        generation = int(state.get("generation") or 0) + 1
        new_index = await self._os.create_generation(generation)
        await self._state.note_generation(generation, new_index)

        scanned = indexed = failed = 0
        cursor = 0
        last_ts: Any = None
        try:
            while True:
                rows = await self._pool.fetch(SCAN_PAGE_SQL, cursor, batch_size)
                if not rows:
                    break
                rows = [dict(r) for r in rows]
                docs = await self._project_page(rows)
                res = await self._bulk_with_retry(docs, index=new_index)
                scanned += len(rows)
                indexed += res["indexed"]
                failed += res["failed"]
                cursor = int(rows[-1]["place_id"])
                last_ts = rows[-1]["updated_at"]
                if len(rows) < batch_size:
                    break
            await self._os.refresh(index=new_index)
            old = await self._os.swap_alias(new_index)
            for name in old:
                if name != new_index:
                    await self._os.drop_index(name)
            await self._state.advance(
                cursor_updated_at=last_ts,
                cursor_place_id=cursor,
                docs_delta=0,
                concrete=new_index,
            )
            # docs_indexed is an absolute count after a rebuild, not a delta.
            await self._state.set_docs_indexed(indexed)
            await self._cache.invalidate_all()
            return {
                "status": "done",
                "mode": "rebuild",
                "generation": generation,
                "index": new_index,
                "scanned": scanned,
                "indexed": indexed,
                "failed": failed,
                "dropped": old,
            }
        except Exception as exc:
            await self._state.note_error(str(exc))
            raise

    async def migrate_price_level_mapping(self, *, generation: int | None = None) -> dict[str, Any]:
        """Guarded migration for the ``price_level`` keyword mapping fix.

        OpenSearch cannot change a field's mapping on an existing index — the
        ``keyword`` type in ``places.json`` only applies to newly created
        indices. ``ensure()`` returns early for existing aliases, so a live
        index built before the fix still has ``price_level`` as ``text``.

        This method builds a fresh generation (``places_v1_g<N>``), copies
        documents from the current concrete index via the ``_reindex`` API,
        and atomically swaps the read alias. The pre-rollover index is
        **preserved** (not deleted) as a rollback point; only the alias moves.
        """
        await self._os.ensure()
        state = await self._state.load() or {}
        current_gen = int(state.get("generation") or 0)
        target_gen = generation if generation is not None else current_gen + 1

        if current_gen >= target_gen:
            return {
                "status": "skipped",
                "reason": f"generation {current_gen} >= target {target_gen}",
                "generation": current_gen,
            }

        current_concrete = await self._os.current_concrete()
        if current_concrete is None:
            return {"status": "unavailable", "reason": "no existing concrete index"}

        new_index = await self._os.create_generation(target_gen)
        docs = await self._os.reindex(current_concrete, new_index)
        await self._os.refresh(index=new_index)

        # Atomic alias swap — old concrete preserved, not dropped.
        old = await self._os.swap_alias(new_index)
        await self._state.note_generation(target_gen, new_index)
        await self._state.set_docs_indexed(docs)
        await self._cache.invalidate_all()

        return {
            "status": "done",
            "mode": "reindex",
            "generation": target_gen,
            "index": new_index,
            "indexed": docs,
            "preserved": old,
        }

    async def sync(
        self, *, batch_size: int = 1000, max_batches: int | None = None
    ) -> dict[str, Any]:
        """Incremental upsert from the durable (updated_at, place_id) cursor."""
        if self._pool is None:
            return {"status": "unavailable", "reason": "postgres"}
        await self._os.ensure()
        state = await self._state.load() or {}
        cur_ts = state.get("cursor_updated_at") or datetime(1970, 1, 1, tzinfo=UTC)
        cur_id = int(state.get("cursor_place_id") or 0)

        scanned = indexed = failed = batches = 0
        touched: list[str] = []
        try:
            while True:
                rows = await self._pool.fetch(SCAN_DELTA_SQL, cur_ts, cur_id, batch_size)
                if not rows:
                    break
                rows = [dict(r) for r in rows]
                docs = await self._project_page(rows)
                res = await self._bulk_with_retry(docs)
                scanned += len(rows)
                indexed += res["indexed"]
                failed += res["failed"]
                batches += 1
                last = rows[-1]
                cur_ts = last["updated_at"]
                cur_id = int(last["place_id"])
                # Checkpoint AFTER the batch is durable — crash-safe.
                await self._state.advance(
                    cursor_updated_at=cur_ts, cursor_place_id=cur_id, docs_delta=res["indexed"]
                )
                touched.extend(d.place_id for d in docs)
                if len(rows) < batch_size or (max_batches and batches >= max_batches):
                    break
            await self._os.refresh()
            for pid in touched:
                await self._cache.invalidate_place(pid)
            return {
                "status": "done",
                "mode": "incremental",
                "scanned": scanned,
                "indexed": indexed,
                "failed": failed,
                "batches": batches,
                "cursor_place_id": cur_id,
            }
        except Exception as exc:
            await self._state.note_error(str(exc))
            raise

    async def delete(self, place_id: int | str) -> dict[str, Any]:
        """Tombstone one place out of the serving index + caches."""
        deleted = await self._os.delete(str(place_id))
        await self._cache.invalidate_place(str(place_id))
        return {"status": "done", "place_id": str(place_id), "deleted": bool(deleted)}

    async def reconcile(self, *, batch_size: int = 5000, apply: bool = True) -> dict[str, Any]:
        """Drop index docs whose canonical row no longer exists.

        Streaming merge over canonical ids (ordered keyset pages) against
        the index's full id set — bounded by the index side, which is the
        smaller set in practice.
        """
        if self._pool is None:
            return {"status": "unavailable", "reason": "postgres"}
        index_ids = await self._os.all_ids()
        canonical: set[str] = set()
        cursor = 0
        while True:
            rows = await self._pool.fetch(PLACE_IDS_PAGE_SQL, cursor, batch_size)
            if not rows:
                break
            for r in rows:
                pid = int(dict(r)["place_id"])
                canonical.add(str(pid))
                cursor = max(cursor, pid)
            if len(rows) < batch_size:
                break
        extra = sorted(index_ids - canonical, key=int)
        removed = 0
        if apply and extra:
            removed = await self._os.delete_ids(extra)
            await self._cache.invalidate_all()
        return {
            "status": "done",
            "index_docs": len(index_ids),
            "canonical_docs": len(canonical),
            "extra_ids": len(extra),
            "removed": removed,
            "applied": apply,
        }

    async def status(self) -> dict[str, Any]:
        state = await self._state.load()
        stats = await self._os.stats()
        return {"state": state, "index": stats}
