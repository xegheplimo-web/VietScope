#!/usr/bin/env python3
"""Fail when stale Search-Hub path/repository references appear in the repo.

The project moved to the VietScope repo and the canonical dev checkout is
``F:\\VietScope-main``. Any runtime/script/config/doc that still points
agents or processes at the old checkout (``F:\\Search-Hub``, ``/f/Search-Hub``,
``E:\\search-hub``, old CI-box paths) or the old repository slug
(``xegheplimo-web/search-hub``) is drift — this gate blocks it at CI.

Deliberately NOT matched (compatibility identifiers, not filesystem paths):
``SEARCH_HUB_*`` / ``HUB_*`` env vars, the ``search-hub`` compose project
name, ``search-hub-net``, ``search-hub-*`` containers, database names,
metric prefixes. Renaming those is an infrastructure migration, not a
cleanup — see SETUP.md "VietScope vs Search-Hub".

Excluded from the scan:
- ``docs/research-notes/`` — historical research notes may describe the past.
- ``CHANGELOG.md`` — generated from git history (git-cliff).
- vendored/embedded trees (``firecrawl/``, ``searxng/``) and env/cache dirs.
- this script itself (it must contain the patterns it matches).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Case-insensitive: catch F:\Search-Hub, F:\search-hub, /F/Search-Hub, ...
PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"F:[/\\]search-hub\b",
        r"/f/search-hub\b",
        r"E:[/\\]search-hub\b",
        r"C:[/\\]users[/\\]administrator[/\\]repos[/\\]search-hub\b",
        r"/home/ubuntu/repos/search-hub\b",
        r"xegheplimo-web[/\\]search-hub\b",
    )
]

# Directory names to prune during the walk (relative to any parent).
PRUNE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    # Vendored upstream trees (git submodules / embedded repo) — not ours.
    "firecrawl",
    "searxng",
    # Historical research notes may legitimately describe the past.
    "research-notes",
}

# Files that are generated from history and may embed old references.
SKIP_FILES = {"CHANGELOG.md", "check_stale_paths.py"}


def _iter_files() -> list[Path]:
    """Return scannable files while pruning excluded trees before descent."""
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO_ROOT, topdown=True):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS)
        root_path = Path(root)
        for name in sorted(names):
            if name in SKIP_FILES:
                continue
            files.append(root_path / name)
    return files


def main() -> int:
    hits: list[str] = []
    for path in _iter_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — not a config surface
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in PATTERNS:
                if pat.search(line):
                    hits.append(f"{rel.as_posix()}:{lineno}: {line.strip()[:160]}")
                    break

    if hits:
        print(
            "ERROR: stale Search-Hub path/repository reference detected "
            f"({len(hits)}). Update to the VietScope repo slug "
            "(xegheplimo-web/VietScope), the canonical dev checkout "
            "(F:\\VietScope-main), or derive the repo root from the script "
            "location instead of hardcoding a drive.",
            file=sys.stderr,
        )
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    print("stale-path check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
