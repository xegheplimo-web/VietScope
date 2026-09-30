"""Ingestion runner (P15.1): stream → validate → DLQ → anchor → COPY → merge → observe.

Per run:
  ingestion_runs row tracks counters, checkpoint, adapter_version,
  source_dataset fingerprint and error_summary — with explicit status
  (done|failed|aborted) so orchestration can distinguish fatal exits;
  valid records flow through a TEXT-typed temp stage table via asyncpg
  COPY (1k–10k batches — never per-row upsert), then per batch:
    * every record appends to place_source_observations (append-only
      history — re-ingestion never destroys prior observations)
    * INSERT…SELECT…ON CONFLICT merges current state into
      place_source_records — idempotent on (provider, external_id),
      or (provider, identity_hash) for id-less records;
  new/changed/unchanged classified by observation_hash diff (opening
  hours and other mutable fields count); invalid records land in
  place_source_errors as always-valid JSON (never mid-string truncation)
  and never abort the run; oversized payloads overflow to MinIO keeping
  only a raw_payload_ref inline.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from storage import pg_client

from ingestion.base import IngestionContext, PlaceSourceAdapter, RawPlaceRecord
from ingestion.validate import canonical_website, normalize_phone, validate

logger = logging.getLogger(__name__)

RESOLVER_VERSION = "p14b-seed-geojson-v1"
BATCH_SIZE = 2000
RAW_INLINE_CAP = 64 * 1024  # bytes; larger payloads go to MinIO
DLQ_INLINE_CAP = 32 * 1024  # DLQ payloads never exceed this as JSON

# Columns that exist on place_source_records (stage carries these + a
# change_type column used only for the observation insert).
_RECORD_COLS = [
    "provider",
    "external_id",
    "external_id_type",
    "source_url",
    "raw_name",
    "raw_address",
    "raw_phone",
    "normalized_phone",
    "raw_website",
    "canonical_website",
    "raw_category",
    "raw_hours",
    "raw_status",
    "lat",
    "lon",
    "location",
    "raw_payload",
    "raw_payload_ref",
    "observed_at",
    "fetched_at",
    "ingestion_run_id",
    "admin_unit_id",
    "resolver_version",
    "resolution_confidence",
    "content_hash",
    "identity_hash",
    "observation_hash",
    "record_status",
]

_STAGE_COLS = [*_RECORD_COLS, "change_type"]

_CREATE_STAGE = f"""
CREATE TEMPORARY TABLE _p15_stage (
    {", ".join(f"{c} TEXT" for c in _STAGE_COLS)}
)
"""

_CAST = {
    "lat": "::float8",
    "lon": "::float8",
    "location": "::geometry",
    "raw_hours": "::jsonb",
    "raw_payload": "::jsonb",
    "observed_at": "::timestamptz",
    "fetched_at": "::timestamptz",
    "ingestion_run_id": "::bigint",
    "admin_unit_id": "::bigint",
    "resolution_confidence": "::real",
}

# Static column lists — no user input reaches these statements.
_MERGE_WITH_ID = f"""
INSERT INTO place_source_records ({", ".join(_RECORD_COLS)})
SELECT {", ".join(f"s.{c}{_CAST.get(c, '')}" for c in _RECORD_COLS)}
FROM _p15_stage s
WHERE s.external_id IS NOT NULL
ON CONFLICT (provider, external_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _RECORD_COLS if c not in ("provider", "external_id", "record_status"))},
    ingested_at = now()
"""

_MERGE_NO_ID = f"""
INSERT INTO place_source_records ({", ".join(_RECORD_COLS)})
SELECT {", ".join(f"s.{c}{_CAST.get(c, '')}" for c in _RECORD_COLS)}
FROM _p15_stage s
WHERE s.external_id IS NULL
ON CONFLICT (provider, identity_hash) WHERE external_id IS NULL
DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _RECORD_COLS if c not in ("provider", "external_id", "identity_hash", "record_status"))},
    ingested_at = now()
"""

# required: partial unique index backing the no-id merge
_MERGE_NO_ID_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_psr_identity_noid
    ON place_source_records (provider, identity_hash)
    WHERE external_id IS NULL
"""

_OBS_COLS = [
    "provider",
    "external_id",
    "external_id_type",
    "identity_hash",
    "observation_hash",
    "ingestion_run_id",
    "observed_at",
    "fetched_at",
    "change_type",
    "admin_unit_id",
    "raw_payload",
    "raw_payload_ref",
]

_OBS_INSERT = f"""
INSERT INTO place_source_observations ({", ".join(_OBS_COLS)})
SELECT {", ".join(f"s.{c}{_CAST.get(c, '')}" for c in _OBS_COLS)}
FROM _p15_stage s
"""

_DLQ_SQL = """
INSERT INTO place_source_errors
    (ingestion_run_id, provider, reason, detail, payload, retryable)
VALUES ($1, $2, $3, $4, $5::jsonb, $6)
"""


async def _anchor(
    admin_store: Any, lat: float | None, lon: float | None
) -> tuple[int | None, float | None]:
    """(admin_unit_id, confidence) via P14 point-in-polygon; None-safe."""
    if admin_store is None or lat is None or lon is None:
        return None, None
    try:
        units = await admin_store.units_containing(lat, lon)
    except Exception as exc:
        logger.debug("admin anchor failed (%.5f,%.5f): %r", lat, lon, exc)
        return None, None
    unit = units[0] if units else None
    if unit is None or unit.unit_id is None:
        return None, 0.0 if not units else None
    return unit.unit_id, 1.0


def _row_dict(
    rec: RawPlaceRecord,
    run_id: int,
    admin_unit_id: int | None,
    confidence: float | None,
    payload: str | None,
    payload_ref: str | None,
) -> dict[str, Any]:
    lat, lon = rec.lat, rec.lon
    obs_hash = rec.observation_hash()
    return {
        "provider": rec.provider,
        "external_id": rec.external_id,
        "external_id_type": rec.external_id_type,
        "source_url": rec.source_url,
        "raw_name": rec.raw_name,
        "raw_address": rec.raw_address,
        "raw_phone": rec.raw_phone,
        "normalized_phone": normalize_phone(rec.raw_phone or "") if rec.raw_phone else None,
        "raw_website": rec.raw_website,
        "canonical_website": canonical_website(rec.raw_website or "") if rec.raw_website else None,
        "raw_category": rec.raw_category,
        "raw_hours": json.dumps(rec.raw_hours) if rec.raw_hours else None,
        "raw_status": rec.raw_status,
        "lat": str(lat) if lat is not None else None,
        "lon": str(lon) if lon is not None else None,
        "location": f"SRID=4326;POINT({lon} {lat})"
        if lat is not None and lon is not None
        else None,
        "raw_payload": payload,
        "raw_payload_ref": payload_ref,
        "observed_at": rec.observed_at.isoformat() if rec.observed_at else None,
        "fetched_at": rec.fetched_at.isoformat() if rec.fetched_at else None,
        "ingestion_run_id": str(run_id),
        "admin_unit_id": str(admin_unit_id) if admin_unit_id is not None else None,
        "resolver_version": RESOLVER_VERSION
        if admin_unit_id is not None or confidence is not None
        else None,
        "resolution_confidence": str(confidence) if confidence is not None else None,
        # content_hash keeps observation semantics for pre-P15.1 readers
        "content_hash": obs_hash,
        "identity_hash": rec.identity_hash(),
        "observation_hash": obs_hash,
        "record_status": "valid",
        "change_type": None,
    }


async def _create_run(
    pool: Any,
    provider: str,
    parameters: dict[str, Any],
    adapter_version: str | None,
    resume_of: int | None,
) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO ingestion_runs
            (provider, parameters, source_version, adapter_version, resume_of)
        VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING run_id
        """,
        provider,
        json.dumps(parameters),
        adapter_version,
        adapter_version,
        resume_of,
    )
    return int(row["run_id"])


async def _load_run(pool: Any, run_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        """
        SELECT provider, parameters, checkpoint, cursor
        FROM ingestion_runs WHERE run_id = $1
        """,
        run_id,
    )
    return dict(row) if row else None


def _run_meta(adapter: PlaceSourceAdapter) -> tuple[str | None, dict[str, Any]]:
    return (
        getattr(adapter, "adapter_version", getattr(adapter, "source_version", None)),
        getattr(adapter, "source_dataset", {}) or {},
    )


async def _checkpoint_run(
    pool: Any, run_id: int, ctx: IngestionContext, counters: Counter[str], adapter: Any
) -> None:
    await pool.execute(
        """
        UPDATE ingestion_runs SET
            records_seen = $2, records_new = $3, records_changed = $4,
            records_unchanged = $5, records_invalid = $6, records_failed = $7,
            checkpoint = $8::jsonb, cursor = $9, source_dataset = $10::jsonb,
            completed_at = NULL
        WHERE run_id = $1
        """,
        run_id,
        counters["seen"],
        counters["new"],
        counters["changed"],
        counters["unchanged"],
        counters["invalid"],
        counters["failed"],
        json.dumps(ctx.checkpoint),
        ctx.cursor,
        json.dumps(_run_meta(adapter)[1]),
    )


async def _finish_run(
    pool: Any,
    run_id: int,
    status: str,
    counters: Counter[str],
    ctx: IngestionContext,
    adapter: Any,
    error_summary: dict[str, Any],
    committed_checkpoint: dict[str, Any] | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE ingestion_runs SET
            status = $2, completed_at = now(),
            records_seen = $3, records_new = $4, records_changed = $5,
            records_unchanged = $6, records_invalid = $7, records_failed = $8,
            checkpoint = $9::jsonb, cursor = $10, error_summary = $11::jsonb,
            source_dataset = $12::jsonb
        WHERE run_id = $1
        """,
        run_id,
        status,
        counters["seen"],
        counters["new"],
        counters["changed"],
        counters["unchanged"],
        counters["invalid"],
        counters["failed"],
        # persist the last COMMITTED position — the live checkpoint may
        # point at elements yielded but never merged (a resume from it
        # would silently skip them)
        json.dumps(ctx.checkpoint if committed_checkpoint is None else committed_checkpoint),
        ctx.cursor,
        json.dumps(error_summary),
        json.dumps(_run_meta(adapter)[1]),
    )


async def _overflow_payload(objects: Any, key: str, payload: str) -> str | None:
    """Best-effort MinIO overflow; returns the object key or None."""
    if objects is None or not getattr(objects, "configured", False):
        return None
    try:
        ok = await objects.put_raw(key, payload.encode("utf-8"))
    except Exception as exc:
        logger.warning("payload overflow put failed: %r", exc)
        return None
    return key if ok else None


async def _stash_payload(
    objects: Any, run_id: int, rec: RawPlaceRecord
) -> tuple[str | None, str | None]:
    """(inline_payload, ref) — overflow to MinIO past the inline cap."""
    try:
        payload = json.dumps(rec.raw_payload, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = json.dumps({"__unserializable__": str(rec.raw_payload)[:4000]})
    if len(payload) <= RAW_INLINE_CAP:
        return payload, None
    ref = await _overflow_payload(objects, f"p15/{run_id}/{rec.observation_hash()}.json", payload)
    return (None, ref) if ref else (payload, None)  # never truncate mid-JSON


async def _dlq_payload(objects: Any, run_id: int, rec: RawPlaceRecord) -> str:
    """Rejected-record payload that is ALWAYS valid JSON.

    Small payloads store verbatim; oversized ones overflow to MinIO or
    store a truncated *envelope* — never a mid-string cut that breaks the
    JSONB cast (a broken DLQ insert would kill the run it's reporting).
    """
    try:
        raw = json.dumps(rec.raw_payload, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = ""
    if raw and len(raw) <= DLQ_INLINE_CAP:
        return raw
    ref = None
    if raw:
        ref = await _overflow_payload(
            objects, f"p15/dlq/{run_id}/{rec.observation_hash()}.json", raw
        )
    envelope: dict[str, Any] = {
        "truncated": True,
        "size_bytes": len(raw),
        "preview": raw[: DLQ_INLINE_CAP - 1024] if raw else str(rec.raw_payload)[:4000],
    }
    if ref:
        envelope["raw_payload_ref"] = ref
    out = json.dumps(envelope, ensure_ascii=False)
    if len(out) > DLQ_INLINE_CAP:  # escapes expanded the preview past the cap
        keep = max(0, len(envelope["preview"]) - (len(out) - DLQ_INLINE_CAP) - 64)
        envelope["preview"] = envelope["preview"][:keep]
        out = json.dumps(envelope, ensure_ascii=False)
    return out


async def _merge_batch(
    pool: Any, run_id: int, rows: list[dict[str, Any]], counters: Counter[str]
) -> None:
    """COPY the batch, classify change_type, append observations, merge.

    Classification needs prior state — read it before COPY so each stage
    row carries its own change_type into the observation log.
    """
    if not rows:
        return
    async with pool.acquire() as conn, conn.transaction():
        provider = rows[0]["provider"]
        ids = [r["external_id"] for r in rows if r["external_id"]]
        ih = [r["identity_hash"] for r in rows if not r["external_id"]]
        known: dict[str, str] = {}
        known_no_id: dict[str, str] = {}
        if ids:
            for r in await conn.fetch(
                "SELECT external_id, observation_hash FROM place_source_records "
                "WHERE provider = $1 AND external_id = ANY($2::text[])",
                provider,
                ids,
            ):
                known[r["external_id"]] = r["observation_hash"]
        if ih:
            for r in await conn.fetch(
                "SELECT identity_hash, observation_hash FROM place_source_records "
                "WHERE provider = $1 AND external_id IS NULL AND identity_hash = ANY($2::text[])",
                provider,
                ih,
            ):
                known_no_id[r["identity_hash"]] = r["observation_hash"]
        for r in rows:
            prior = (
                known.get(r["external_id"])
                if r["external_id"]
                else known_no_id.get(r["identity_hash"])
            )
            if prior is None:
                r["change_type"] = "new"
            elif prior == r["observation_hash"]:
                r["change_type"] = "unchanged"
            else:
                r["change_type"] = "changed"
            counters[r["change_type"]] += 1

        await conn.execute(_CREATE_STAGE)
        tuples = [tuple(r[c] for c in _STAGE_COLS) for r in rows]
        await conn.copy_records_to_table("_p15_stage", columns=_STAGE_COLS, records=tuples)
        await conn.execute(_OBS_INSERT)
        await conn.execute(_MERGE_WITH_ID)
        await conn.execute(_MERGE_NO_ID)
        await conn.execute("DROP TABLE _p15_stage")


async def run_ingestion(
    adapter: PlaceSourceAdapter,
    *,
    parameters: dict[str, Any] | None = None,
    pool: Any | None = None,
    admin_store: Any | None = None,
    object_store: Any | None = None,
    batch_size: int = BATCH_SIZE,
    resume_of: int | None = None,
) -> dict[str, Any] | None:
    """Run one adapter into staging; None when Postgres is unavailable.

    ``resume_of`` loads a prior run's checkpoint/parameters so the adapter
    continues where that run stopped (new run row, ``resume_of`` lineage).
    The result dict carries ``status``: ``done``|``failed``|``aborted`` —
    callers (CLI, orchestration) must treat non-``done`` as failure.
    """
    if pool is None:
        pool = await pg_client.get_pool()
    if pool is None:
        logger.warning("ingestion skipped: hub-postgres unavailable")
        return None
    if admin_store is None:
        try:
            from storage.admin_store import PgAdminStore

            admin_store = PgAdminStore()
        except Exception:
            admin_store = None
    if object_store is None:
        try:
            from storage.object_store import get_object_store

            object_store = get_object_store()
        except Exception:
            object_store = None

    params = parameters or {}
    resume_checkpoint: dict[str, Any] = {}
    resume_cursor: str | None = None
    if resume_of is not None:
        prior = await _load_run(pool, resume_of)
        if prior is None:
            raise ValueError(f"resume run {resume_of} not found")
        if prior["provider"] != adapter.name:
            raise ValueError(
                f"resume provider mismatch: run {resume_of} is "
                f"{prior['provider']}, adapter is {adapter.name}"
            )
        params = {**(prior["parameters"] or {}), **params}
        resume_checkpoint = prior["checkpoint"] or {}
        resume_cursor = prior["cursor"]

    adapter_version, _dataset = _run_meta(adapter)
    run_id = await _create_run(pool, adapter.name, params, adapter_version, resume_of)
    ctx = IngestionContext(
        run_id=run_id,
        provider=adapter.name,
        parameters=params,
        checkpoint=resume_checkpoint,
        cursor=resume_cursor,
    )
    counters: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    batch: list[dict[str, Any]] = []
    # last checkpoint durable on disk — updated per committed batch
    saved_checkpoint: dict[str, Any] = dict(resume_checkpoint)

    await pool.execute(_MERGE_NO_ID_INDEX)

    status = "done"
    try:
        async for rec in adapter.ingest(ctx):
            counters["seen"] += 1
            try:
                reasons = validate(rec)
            except Exception as exc:  # validation itself must never crash a run
                reasons, detail = ["validation_error"], repr(exc)
            else:
                detail = ";".join(reasons)
            if reasons:
                counters["invalid"] += 1
                for reason in reasons:
                    errors[reason] += 1
                try:
                    await pool.execute(
                        _DLQ_SQL,
                        run_id,
                        rec.provider,
                        ";".join(reasons),
                        detail[:500],
                        await _dlq_payload(object_store, run_id, rec),
                        False,
                    )
                except Exception as exc:
                    counters["failed"] += 1
                    logger.warning("DLQ write failed: %r", exc)
                continue
            try:
                uid, conf = await _anchor(admin_store, rec.lat, rec.lon)
                payload, ref = await _stash_payload(object_store, run_id, rec)
                batch.append(_row_dict(rec, run_id, uid, conf, payload, ref))
            except Exception as exc:
                counters["failed"] += 1
                errors["row_build"] += 1
                logger.warning("row build failed: %r", exc)
            if len(batch) >= batch_size:
                await _merge_batch(pool, run_id, batch, counters)
                batch.clear()
                await _checkpoint_run(pool, run_id, ctx, counters, adapter)
                saved_checkpoint = dict(ctx.checkpoint)
        if batch:
            await _merge_batch(pool, run_id, batch, counters)
            saved_checkpoint = dict(ctx.checkpoint)
    except Exception as exc:
        status = "failed"
        counters["failed"] += 1
        logger.exception("ingestion run %d failed", run_id)
        error_summary = {**dict(errors), "fatal": repr(exc)[:500]}
    else:
        error_summary = dict(errors)
    await _finish_run(
        pool,
        run_id,
        status,
        counters,
        ctx,
        adapter,
        error_summary,
        committed_checkpoint=saved_checkpoint if status != "done" else None,
    )

    return {
        "run_id": run_id,
        "provider": adapter.name,
        "status": status,
        "resume_of": resume_of,
        "adapter_version": adapter_version,
        "source_dataset": _run_meta(adapter)[1],
        **{k: counters[k] for k in ("seen", "new", "changed", "unchanged", "invalid", "failed")},
        "errors": dict(errors),
    }
