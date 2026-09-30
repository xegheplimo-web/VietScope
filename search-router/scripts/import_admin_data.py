#!/usr/bin/env python3
"""DEPRECATED — VN admin import moved to the canonical P14A pipeline.

This importer targeted the retired migration-002 shape (naive
UNIQUE(code) units, point geometry from ``data/admin_units_vn.json``).
Migration 006 replaced it with the temporal admin graph
(``administrative_units`` / ``administrative_aliases`` /
``administrative_relations``) seeded by ``python -m db.seed_admin`` from
``db/seeds/vn_admin_units.json``. The generic seed validators
(``load_units`` / ``topo_order``, still used by tests) live here; the
import itself delegates to the canonical seeder.

    python -m scripts.import_admin_data [--dsn DSN]   # deprecated wrapper
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

_DEPRECATION_NOTICE = (
    "DEPRECATED: scripts.import_admin_data targets the retired migration-002 "
    "shape; use `python -m db.seed_admin` (seed: db/seeds/vn_admin_units.json)."
)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="import VN admin divisions (deprecated)")
    parser.add_argument("--file", type=Path, default=None, help="legacy alias for --seed")
    parser.add_argument("--dsn", help="Postgres DSN (overrides env/default)")
    args = parser.parse_args(argv)
    print(_DEPRECATION_NOTICE, file=sys.stderr)
    from db.seed_admin import SEED_PATH as CANONICAL_SEED
    from db.seed_admin import _amain

    seed = args.file if args.file is not None else CANONICAL_SEED
    return asyncio.run(_amain(argparse.Namespace(dsn=args.dsn, seed=str(seed))))


if __name__ == "__main__":
    raise SystemExit(main())
