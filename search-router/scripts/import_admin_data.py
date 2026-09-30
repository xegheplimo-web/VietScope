#!/usr/bin/env python3
"""Import VN administrative units into hub-postgres (P14).

Reads ``data/admin_units_vn.json`` (default) — or any file in the same
shape — and upserts ``administrative_units`` + ``administrative_aliases``
(migration 002). The 29 merged-away provinces are kept as
``former:<old-code>`` units anchored on their former capital, so "Bà Rịa
- Vũng Tàu" resolves to Vũng Tàu, not the TP.HCM centroid. Idempotent:
units upsert ON CONFLICT (code); aliases are replaced per unit.

    python -m scripts.import_admin_data [--file PATH] [--dsn DSN]
    python scripts/import_admin_data.py          # same, from search-router/

DSN resolution matches db.migrate: ``--dsn`` > ``HUB_DATABASE_URL`` >
``HUB_PG_*`` defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_SEED = _SEARCH_ROUTER_DIR / "data" / "admin_units_vn.json"

_UPSERT_UNIT_SQL = """
INSERT INTO administrative_units
    (code, name, type, parent_id, valid_from, valid_to, geometry)
VALUES (
    $1, $2, $3,
    (SELECT unit_id FROM administrative_units WHERE code = $4),
    $5, $6,
    CASE WHEN $7::float8 IS NULL OR $8::float8 IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint($8::float8, $7::float8), 4326) END
)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    type = EXCLUDED.type,
    parent_id = EXCLUDED.parent_id,
    valid_from = EXCLUDED.valid_from,
    valid_to = EXCLUDED.valid_to,
    geometry = EXCLUDED.geometry
"""

_DELETE_ALIASES_SQL = """
DELETE FROM administrative_aliases
WHERE unit_id = (SELECT unit_id FROM administrative_units WHERE code = $1)
"""

_INSERT_ALIAS_SQL = """
INSERT INTO administrative_aliases (unit_id, alias, valid_from, valid_to)
VALUES ((SELECT unit_id FROM administrative_units WHERE code = $1), $2, $3, $4)
"""


def load_units(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    units = data.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError(f"{path}: expected a non-empty 'units' list")
    for u in units:
        for key in ("code", "name", "type"):
            if not u.get(key):
                raise ValueError(f"{path}: unit missing {key!r}: {u!r}")
    return units


def topo_order(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parents before children; unknown parents tolerated (left last)."""
    by_code = {u["code"] for u in units}
    done: set[str] = set()
    out: list[dict[str, Any]] = []
    pending = list(units)
    while pending:
        ready = [
            u
            for u in pending
            if not u.get("parent") or u["parent"] in done or u["parent"] not in by_code
        ]
        if not ready:
            raise ValueError(
                "parent cycle or missing parent for: " + ", ".join(u["code"] for u in pending)
            )
        for u in ready:
            out.append(u)
            done.add(u["code"])
        pending = [u for u in pending if u["code"] not in done]
    return out


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def import_units(units: list[dict[str, Any]], dsn: str) -> int:
    import asyncpg

    conn = await asyncpg.connect(dsn=dsn, timeout=10)
    try:
        count = 0
        for u in topo_order(units):
            await conn.execute(
                _UPSERT_UNIT_SQL,
                u["code"],
                u["name"],
                u["type"],
                u.get("parent"),
                _as_date(u.get("valid_from")),
                _as_date(u.get("valid_to")),
                u.get("lat"),
                u.get("lon"),
            )
            await conn.execute(_DELETE_ALIASES_SQL, u["code"])
            for alias in u.get("aliases") or []:
                await conn.execute(
                    _INSERT_ALIAS_SQL,
                    u["code"],
                    alias,
                    _as_date(u.get("valid_from")),
                    _as_date(u.get("valid_to")),
                )
            count += 1
        return count
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="import VN admin divisions")
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--dsn", help="Postgres DSN (overrides env/default)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _load_dotenv()
    units = load_units(args.file)
    count = asyncio.run(import_units(units, _resolve_dsn(args.dsn)))
    print(f"imported {count} administrative units from {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
