#!/usr/bin/env python3
"""P17 index-sync CLI: canonical places → OpenSearch ``places`` index.

Examples:
    # incremental catch-up from the durable cursor (the default)
    uv run python -m scripts.index_places

    # full rebuild into a fresh generation + atomic alias swap
    uv run python -m scripts.index_places --mode full

    # drop index docs whose canonical row is gone
    uv run python -m scripts.index_places --mode reconcile

    # tombstone one place (canonical delete / takedown)
    uv run python -m scripts.index_places --delete 12345

    # ledger + index stats
    uv run python -m scripts.index_places --mode status

Exit code: 0 only on a clean run — same contract as scripts/ingest.py and
scripts/resolve.py so orchestration treats a failed sync as failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_SR = Path(__file__).resolve().parents[1]
if str(_SR) not in sys.path:
    sys.path.insert(0, str(_SR))

from manage_keys import _load_dotenv, _resolve_dsn  # noqa: E402


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P17 places index synchronization")
    ap.add_argument(
        "--mode",
        choices=["incremental", "full", "reconcile", "status"],
        default="incremental",
    )
    ap.add_argument("--batch", type=int, default=1000, help="rows per page/bulk batch")
    ap.add_argument("--max-batches", type=int, default=None, help="cap incremental batches")
    ap.add_argument("--delete", type=int, default=None, help="tombstone a place_id")
    ap.add_argument("--dsn", default=None, help="postgres DSN (else env)")
    args = ap.parse_args(argv)

    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("no database DSN configured", file=sys.stderr)
        return 2

    import asyncpg
    from serving.places.indexer import PlaceIndexer
    from serving.places.os_index import PlaceOSIndex

    try:
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"postgres unreachable: {exc!r}", file=sys.stderr)
        return 2

    indexer = PlaceIndexer(pool, os_index=PlaceOSIndex())
    try:
        if args.delete is not None:
            result = await indexer.delete(args.delete)
        elif args.mode == "status":
            result = await indexer.status()
        elif args.mode == "full":
            result = await indexer.rebuild(batch_size=args.batch)
        elif args.mode == "reconcile":
            result = await indexer.reconcile(batch_size=args.batch)
        else:
            result = await indexer.sync(batch_size=args.batch, max_batches=args.max_batches)
    except Exception as exc:
        print(f"index sync failed: {exc!r}", file=sys.stderr)
        return 1
    finally:
        await pool.close()

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("done", None) or args.mode == "status" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
