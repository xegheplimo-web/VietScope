"""CLI for the Search Quality Eval Harness.

Commands:
    python -m eval list-datasets
    python -m eval run --dataset vi_general --top-k 10 [options]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import diff as diffmod
from .datasets import list_datasets, load_dataset
from .metrics import report_domain_overlap, report_source_diversity
from .report import (
    REPORTS_DIR,
    build_report,
    format_per_query,
    format_summary,
    write_report,
)
from .runner import RunConfig, run_dataset


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset",
        required=True,
        help="Dataset name (eval/datasets/<name>.jsonl) or a file path.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of results per query (default 10).",
    )
    parser.add_argument(
        "--endpoint",
        choices=["legacy", "v1", "answer"],
        default="legacy",
        help=(
            "Search Router endpoint: POST /search (legacy), /v1/search (v1) "
            "or /v1/answer (answer — adds answer-level metrics)."
        ),
    )
    parser.add_argument(
        "--server-url",
        default="http://localhost:8888",
        help="Search Router base URL (default http://localhost:8888).",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="Per-request timeout in seconds."
    )
    parser.add_argument(
        "--providers",
        default="searxng",
        help="Comma-separated provider names to record in the report (informational).",
    )
    parser.add_argument(
        "--search-type",
        choices=["web", "news"],
        default="web",
        help="Search type passed to the API (web|news).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the deterministic mock client (no HTTP).",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable HTTP->mock fallback; fail the run if the server is down.",
    )
    parser.add_argument(
        "--cost-per-query",
        type=float,
        default=0.0,
        help="Estimated USD cost per query when the API does not report it.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to a previous report JSON to diff against.",
    )
    parser.add_argument("--note", default=None, help="Free-form note stored in the report meta.")
    parser.add_argument(
        "--out-dir",
        default=REPORTS_DIR,
        help="Directory to write the report to (default eval/reports).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress the summary table on stdout."
    )
    parser.add_argument(
        "--authority",
        action="store_true",
        help=(
            "Score each query's retrieved domains against the VN authority "
            "table (ranking/authority.py) and add an 'authority' metric."
        ),
    )
    parser.add_argument(
        "--authority-vertical",
        default=None,
        help="Optional vertical for intent-dependent authority (e.g. legal, market).",
    )
    parser.add_argument(
        "--answer-mode",
        choices=["fast", "balanced", "deep", "research"],
        default="balanced",
        help="/v1/answer mode when --endpoint answer (default balanced).",
    )


def _cmd_list(_: argparse.Namespace) -> int:
    names = list_datasets()
    if not names:
        print("No datasets found in eval/datasets/.")
        return 1
    print("Available datasets:")
    for n in names:
        try:
            qs = load_dataset(n)
            print(f"  {n:<24} {len(qs):>3} queries")
        except ValueError as exc:
            print(f"  {n:<24} (invalid: {exc})")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        dataset = load_dataset(args.dataset)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    dataset_name = os.path.splitext(os.path.basename(args.dataset))[0]
    config = RunConfig(
        top_k=args.top_k,
        server_url=args.server_url,
        endpoint=args.endpoint,
        timeout=args.timeout,
        providers=[p.strip() for p in args.providers.split(",") if p.strip()],
        search_type=args.search_type,
        force_mock=args.mock,
        fallback=not args.no_fallback,
        cost_per_query_usd=args.cost_per_query,
        answer_mode=args.answer_mode,
    )

    scorer = None
    if args.authority:
        from .vn_authority import load_scorer

        scorer = load_scorer(vertical=args.authority_vertical)
        if scorer is None:
            print(
                "warning: VN authority table unavailable — authority metric skipped",
                file=sys.stderr,
            )

    if config.endpoint == "answer":
        from .runner import run_answer_dataset

        result = run_answer_dataset(dataset, config, authority_scorer=scorer)
    else:
        result = run_dataset(dataset, config, authority_scorer=scorer)
    report = build_report(result, dataset_name, note=args.note)

    if not args.quiet:
        print(format_summary(report))
        if "answer_present" in report["summary"]:
            from .report import format_answer_summary

            print()
            print(format_answer_summary(report))
        print()
        print(format_per_query(report))
        print()

    # Baseline diff mode.
    if args.baseline:
        try:
            with open(args.baseline, encoding="utf-8") as fh:
                baseline = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"warning: could not read baseline {args.baseline}: {exc}",
                file=sys.stderr,
            )
        else:
            print("=== Baseline diff vs", os.path.basename(args.baseline), "===")
            print(diffmod.format_diff(report["summary"], baseline.get("summary", {})))
            per_q = diffmod.format_per_query_diffs(report, baseline)
            if per_q:
                print()
                print(per_q)
            print()

    path = write_report(report, dataset_name, out_dir=args.out_dir)
    print(f"Report written: {path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Analyze an existing report (optionally vs another) for diversity metrics.

    Computes source_diversity from a single report, and Jaccard domain_overlap
    between two reports. Works on pre-8C reports too (only reads
    per_query[].retrieved_domains).
    """

    def _load(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read {path}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    report_a = _load(args.file)
    div = report_source_diversity(report_a)
    print(f"source_diversity (mean): {div['mean']:.4f}  over {div['queries']} queries")
    if args.authority:
        from .metrics import report_authority
        from .vn_authority import load_scorer

        scorer = load_scorer(vertical=args.authority_vertical)
        auth = report_authority(report_a, scorer)
        if auth.get("note"):
            print(f"authority             : unavailable ({auth['note']})")
        else:
            print(f"authority (mean)      : {auth['mean']:.4f}  over {auth['queries']} queries")
    if args.compare:
        report_b = _load(args.compare)
        ov = report_domain_overlap(report_a, report_b)
        print(f"domain_overlap (mean) : {ov['mean']:.4f}  over {ov['compared']} aligned queries")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Search Quality Eval Harness for Search Hub.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-datasets", help="List available datasets.")
    p_list.set_defaults(func=_cmd_list)

    p_run = sub.add_parser("run", help="Run an eval against a dataset.")
    _add_run_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser(
        "report",
        help="Compute diversity metrics from an existing report JSON.",
    )
    p_report.add_argument("--file", required=True, help="Report JSON path (A).")
    p_report.add_argument(
        "--compare",
        default=None,
        help="Optional second report JSON (B) for Jaccard domain_overlap A vs B.",
    )
    p_report.add_argument(
        "--authority",
        action="store_true",
        help="Also compute the mean VN-authority of retrieved domains.",
    )
    p_report.add_argument(
        "--authority-vertical",
        default=None,
        help="Optional vertical for intent-dependent authority scoring.",
    )
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
