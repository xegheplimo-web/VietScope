#!/usr/bin/env bash
# List first-party Python files changed vs a base ref (default: origin/main).
# Vendor trees/submodules are excluded — same boundary as ruff.toml.
# Usage: scripts/changed-py.sh [base-ref]
set -euo pipefail

BASE="${1:-origin/main}"
git diff --name-only --diff-filter=ACMR "$BASE"...HEAD -- '*.py' \
  | grep -vE '^(firecrawl|searxng)/' \
  || true
