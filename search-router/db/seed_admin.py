#!/usr/bin/env python3
"""Load the canonical VN administrative seed into hub-postgres (P14A).

Reads ``db/seeds/vn_admin_units.json`` (built by
``scripts/build_vn_admin_seed.py``) and upserts into
``administrative_units`` / ``administrative_aliases`` /
``administrative_relations`` (migration 006), including the GeoJSON
boundary geometry attached by ``--geojson`` (P14B).

Idempotent: rows are keyed by ``(code, valid_from-era)`` for units,
``(unit, normalized_alias, valid_from)`` for aliases and
``(from, to, relation_type)`` for relations — reruns update in place and
never delete historical units. Apply migrations first
(``python -m db.migrate``).

Usage:
    python -m db.seed_admin [--dsn postgresql://...] [--seed PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

_SR = Path(__file__).resolve().parents[1]
if str(_SR) not in sys.path:
    sys.path.insert(0, str(_SR))

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402
from storage.admin_store import SEED_PATH, load_seed  # noqa: E402

logger = logging.getLogger(__name__)

_EPOCH = "0001-01-01"


def _coalesce_era(valid_from: str | None) -> str:
    return valid_from or _EPOCH


async def seed_admin(conn, seed: dict, *, batch_size: int = 500) -> dict[str, int]:
    """Upsert the seed document on one asyncpg connection."""
    units = seed.get("units", [])
    aliases = seed.get("aliases", [])
    relations = seed.get("relations", [])

    # ── units ────────────────────────────────────────────────────────────
    # Existing (code, era) -> unit_id map makes reruns cheap and idempotent.
    existing = {
        (r["code"], str(r["vf"]))
        for r in await conn.fetch(
            "SELECT code, COALESCE(valid_from, DATE '0001-01-01') AS vf FROM administrative_units"
        )
    }
    key_by_code_era: dict[tuple[str, str], str] = {}
    to_insert = []
    for u in units:
        era = (u["code"], _coalesce_era(u.get("valid_from")))
        key_by_code_era[era] = u["key"]
        if era not in existing:
            to_insert.append(u)
    for i in range(0, len(to_insert), batch_size):
        batch = to_insert[i : i + batch_size]
        await conn.executemany(
            """
            INSERT INTO administrative_units (
                code, name, normalized_name, type, admin_level,
                valid_from, valid_to, status, source, source_updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6::date,$7::date,$8,$9,$10)
            """,
            [
                (
                    u["code"],
                    u["name"],
                    u.get("normalized_name") or None,
                    u["type"],
                    u["admin_level"],
                    u.get("valid_from"),
                    u.get("valid_to"),
                    u.get("status", "current"),
                    u.get("source"),
                    seed.get("generated_at"),
                )
                for u in batch
            ],
        )
    if to_insert:
        logger.info("inserted %d units (had %d)", len(to_insert), len(existing))

    # keep mutable fields fresh on reruns
    for i in range(0, len(units), batch_size):
        batch = units[i : i + batch_size]
        await conn.executemany(
            """
            UPDATE administrative_units SET
                name = $2, normalized_name = $3, type = $4,
                admin_level = $5, valid_to = $6::date, status = $7,
                source = $8, source_updated_at = $9
            WHERE code = $1
              AND COALESCE(valid_from, DATE '0001-01-01')
                  = COALESCE($10::date, DATE '0001-01-01')
            """,
            [
                (
                    u["code"],
                    u["name"],
                    u.get("normalized_name") or None,
                    u["type"],
                    u["admin_level"],
                    u.get("valid_to"),
                    u.get("status", "current"),
                    u.get("source"),
                    seed.get("generated_at"),
                    u.get("valid_from"),
                )
                for u in batch
            ],
        )

    # ── key -> unit_id resolution + parent links (second pass) ───────────
    rows = await conn.fetch(
        "SELECT unit_id, code, "
        "COALESCE(valid_from, DATE '0001-01-01') AS vf "
        "FROM administrative_units"
    )
    id_by_code_era = {(r["code"], str(r["vf"])): r["unit_id"] for r in rows}
    key_to_id = {key: id_by_code_era[(code, era)] for (code, era), key in key_by_code_era.items()}
    for i in range(0, len(units), batch_size):
        batch = [u for u in units[i : i + batch_size] if u.get("parent_key")]
        await conn.executemany(
            "UPDATE administrative_units SET parent_id = $2 WHERE unit_id = $1",
            [(key_to_id[u["key"]], key_to_id.get(u["parent_key"])) for u in batch],
        )

    # ── geometry (P14B): push simplified GeoJSON boundaries into PostGIS ─
    with_geom = [u for u in units if u.get("geometry")]
    for i in range(0, len(with_geom), batch_size):
        batch = with_geom[i : i + batch_size]
        await conn.executemany(
            """
            UPDATE administrative_units SET
                geometry = ST_MakeValid(ST_GeomFromGeoJSON($2))
            WHERE code = $1
              AND COALESCE(valid_from, DATE '0001-01-01')
                  = COALESCE($3::date, DATE '0001-01-01')
            """,
            [
                (
                    u["code"],
                    json.dumps(u["geometry"], separators=(",", ":")),
                    u.get("valid_from"),
                )
                for u in batch
            ],
        )
    if with_geom:
        logger.info("geometry updated for %d units", len(with_geom))

    # ── aliases ──────────────────────────────────────────────────────────
    missing_alias_units = 0
    inserted_aliases = 0
    for a in aliases:
        uid = key_to_id.get(a["unit_key"])
        if uid is None:
            missing_alias_units += 1
            continue
        tag = await conn.execute(
            """
            INSERT INTO administrative_aliases (
                unit_id, alias, normalized_alias, alias_type,
                valid_from, valid_to
            ) VALUES ($1,$2,$3,$4,$5::date,$6::date)
            ON CONFLICT (unit_id, COALESCE(normalized_alias, ''),
                         COALESCE(valid_from, DATE '0001-01-01'))
            DO UPDATE SET alias = EXCLUDED.alias,
                          alias_type = EXCLUDED.alias_type,
                          valid_to = EXCLUDED.valid_to
            """,
            uid,
            a["alias"],
            a.get("normalized_alias") or "",
            a.get("alias_type", "alternate"),
            a.get("valid_from"),
            a.get("valid_to"),
        )
        if "INSERT" in tag:
            inserted_aliases += 1
    if missing_alias_units:
        logger.warning("%d aliases skipped (unit missing)", missing_alias_units)

    # ── relations ────────────────────────────────────────────────────────
    missing_rel = 0
    inserted_rel = 0
    for r in relations:
        fid = key_to_id.get(r["from_key"])
        tid = key_to_id.get(r["to_key"])
        if fid is None or tid is None:
            missing_rel += 1
            continue
        tag = await conn.execute(
            """
            INSERT INTO administrative_relations (
                from_unit_id, to_unit_id, relation_type, effective_date, source
            ) VALUES ($1,$2,$3,$4::date,$5)
            ON CONFLICT (from_unit_id, to_unit_id, relation_type)
            DO UPDATE SET effective_date = EXCLUDED.effective_date,
                          source = EXCLUDED.source
            """,
            fid,
            tid,
            r["relation_type"],
            r.get("effective_date"),
            r.get("source"),
        )
        if "INSERT" in tag:
            inserted_rel += 1
    if missing_rel:
        logger.warning("%d relations skipped (endpoint missing)", missing_rel)

    stats = {
        "units_total": len(units),
        "units_inserted": len(to_insert),
        "units_with_geometry": len(with_geom),
        "aliases": len(aliases),
        "relations": len(relations),
        "relations_inserted": inserted_rel,
    }
    return stats


async def _amain(args: argparse.Namespace) -> int:
    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("No DSN: set HUB_DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2
    import asyncpg

    seed = load_seed(Path(args.seed) if args.seed else None)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        async with conn.transaction():
            stats = await seed_admin(conn, seed)
    finally:
        await conn.close()
    print("admin seed applied:", stats)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--seed", default=str(SEED_PATH))
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
