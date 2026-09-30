#!/usr/bin/env python3
"""P15/P15.1 ingestion CLI: run one source adapter into raw staging.

Examples:
    # gosom/google-maps-scraper NDJSON output
    uv run python -m scripts.ingest --provider google_maps --file results.ndjson

    # nationwide OSM bootstrap (Geofabrik extract, NOT Overpass)
    uv run python -m scripts.ingest --provider osm --file vietnam-latest.osm.pbf

    # resume a crashed/failed run from its checkpoint
    uv run python -m scripts.ingest --provider osm --file vietnam-latest.osm.pbf --resume-run 8421

    # stream the existing Search-Hub corpus (documents table, no file)
    uv run python -m scripts.ingest --provider web_corpus --corpus-db

Exit code: 0 only when the run reports status=done — failed/aborted
runs exit 1, so cron/Kubernetes/orchestrators see fatal ingestion as
failure. ``--resume-run`` loads the prior run's parameters+checkpoint
(new run row, ``resume_of`` lineage).
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


def _stream_docs(path: Path):
    """documents-shaped JSONL rows, lazy — no corpus materialization."""
    for line in path.open("r", encoding="utf-8", errors="replace"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _exit_code(result: dict | None) -> int:
    """0 ⇔ run finished clean; anything else is an orchestration failure."""
    if result is None:
        return 2
    return 0 if result.get("status") == "done" else 1


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 source ingestion → raw staging")
    ap.add_argument("--provider", required=True, choices=["google_maps", "osm", "web_corpus"])
    ap.add_argument(
        "--file",
        type=Path,
        default=None,
        help="source payload: NDJSON (google_maps/web_corpus) or .osm.pbf (osm)",
    )
    ap.add_argument(
        "--param",
        action="append",
        default=[],
        help="run parameter key=value (province, category, grid_cell, backend...)",
    )
    ap.add_argument(
        "--corpus-db",
        action="store_true",
        help="web_corpus only: stream the documents table (no --file needed)",
    )
    ap.add_argument(
        "--resume-run", type=int, default=None, help="resume from run N's checkpoint (new run row)"
    )
    ap.add_argument("--batch", type=int, default=2000, help="COPY batch size")
    ap.add_argument("--dsn", default=None, help="postgres DSN (else env)")
    args = ap.parse_args(argv)

    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("no database DSN configured", file=sys.stderr)
        return 2

    params = dict(p.split("=", 1) for p in args.param)

    import asyncpg
    from ingestion.runner import run_ingestion

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    try:
        if args.provider == "google_maps":
            from ingestion.adapters.gmaps import GoogleMapsAdapter

            if not args.file and not args.resume_run:
                print("--file required for google_maps (or --resume-run)", file=sys.stderr)
                return 2
            if args.file:
                params.setdefault("ndjson", str(args.file))
            adapter = GoogleMapsAdapter()
        elif args.provider == "osm":
            from ingestion.adapters.osm_pbf import OsmPbfAdapter

            if not args.file and not args.resume_run:
                print("--file required for osm (or --resume-run)", file=sys.stderr)
                return 2
            if args.file:
                params.setdefault("pbf", str(args.file))
            adapter = OsmPbfAdapter()
        else:
            from ingestion.adapters.web_corpus import WebCorpusAdapter

            if args.corpus_db:
                adapter = WebCorpusAdapter(pool=pool)
            elif args.file:
                adapter = WebCorpusAdapter(docs=_stream_docs(args.file))
            else:
                print("web_corpus needs --file or --corpus-db", file=sys.stderr)
                return 2

        result = await run_ingestion(
            adapter,
            parameters=params,
            pool=pool,
            batch_size=args.batch,
            resume_of=args.resume_run,
        )
    finally:
        await pool.close()
    if result is None:
        print("ingestion unavailable", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
