#!/usr/bin/env python3
"""P16 resolution CLI: staged source records → canonical places.

Examples:
    # resolve everything staged so far
    uv run python -m scripts.resolve

    # one provider only
    uv run python -m scripts.resolve --provider osm

    # resume a crashed resolution run from its cursor
    uv run python -m scripts.resolve --resume-run 11

Exit code: 0 only on status=done — same contract as scripts/ingest.py so
orchestration treats a fatal resolution run as failure.
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
    ap = argparse.ArgumentParser(description="P16 entity resolution → canonical places")
    ap.add_argument("--provider", default=None, help="only resolve this provider's rows")
    ap.add_argument("--since-id", type=int, default=0, help="start after source record id N")
    ap.add_argument("--batch", type=int, default=200, help="records per batch")
    ap.add_argument("--threshold", type=float, default=0.62, help="merge threshold 0..1")
    ap.add_argument(
        "--resume-run", type=int, default=None, help="resume resolution_runs N (cursor+params)"
    )
    ap.add_argument(
        "--relink-stale",
        action="store_true",
        help="drop places whose links a different matcher_version wrote, then re-resolve",
    )
    ap.add_argument("--dsn", default=None, help="postgres DSN (else env)")
    args = ap.parse_args(argv)

    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("no database DSN configured", file=sys.stderr)
        return 2

    import asyncpg
    from resolution.runner import run_resolution

    try:
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    except (OSError, asyncpg.PostgresError) as exc:
        # connection refused / auth failed → clean exit, not a traceback
        print(f"postgres unreachable: {exc!r}", file=sys.stderr)
        return 2
    try:
        result = await run_resolution(
            pool,
            provider=args.provider,
            since_id=args.since_id,
            batch_size=args.batch,
            threshold=args.threshold,
            resume_of=args.resume_run,
            relink_stale=args.relink_stale,
        )
    finally:
        await pool.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
