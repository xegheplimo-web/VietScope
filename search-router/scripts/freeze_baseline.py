#!/usr/bin/env python3
"""Freeze the BASELINE-V3 contracts into ``baseline/`` at the repo root.

Usage (from search-router/):
    python scripts/freeze_baseline.py           # write all artifacts
    python scripts/freeze_baseline.py --check   # report drift, write nothing

Also records ``baseline/results.json`` (git SHA + collected test count) —
informational only, not drift-checked (it carries a timestamp).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))

from pipeline.baseline_contracts import BASELINE_DIR, drifted, freeze  # noqa: E402


def _git_sha() -> str:
    import shutil

    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        return subprocess.check_output(  # noqa: S603 — fixed argv, shell=False
            [git, "rev-parse", "HEAD"], cwd=_SEARCH_ROUTER_DIR.parent, text=True
        ).strip()
    except Exception:
        return "unknown"


def _tests_collected() -> int | None:
    try:
        out = subprocess.check_output(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=_SEARCH_ROUTER_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in reversed(out.strip().splitlines()):
            if "test" in line and "collected" in line:
                return int(line.split()[0])
    except Exception:
        pass
    return None


def _write_results() -> Path:
    import datetime

    results = {
        "name": "BASELINE-V3",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "tests_collected": _tests_collected(),
    }
    path = BASELINE_DIR / "results.json"
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report drift only")
    args = parser.parse_args()

    if args.check:
        bad = drifted()
        if bad:
            print("contract drift detected:")
            for rel in bad:
                print(f"  baseline/{rel}")
            return 1
        print("baseline contracts clean")
        return 0

    written = freeze()
    results = _write_results()
    for path in written:
        print(f"wrote {path.relative_to(BASELINE_DIR.parent)}")
    print(f"wrote {results.relative_to(BASELINE_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
