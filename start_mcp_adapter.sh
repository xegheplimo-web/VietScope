#!/usr/bin/env bash
# Start MCP adapter for Hermes <-> Search-Hub integration.
# Repo-root is derived from this script's location — no hardcoded drive,
# so the repo can live at F:\VietScope-main, D:\VietScope, C:\dev\VietScope, ...
# Auth: scoped SEARCH_HUB_ROUTER_KEY from .env — the admin key must never
# sit on the normal MCP path (least privilege). Create it with:
#   python search-router/manage_keys.py create --tenant hermes \
#     --name "hermes-mcp" --scopes "search:read,read:use"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_ROOT/.env"
ROUTER_DIR="$REPO_ROOT/search-router"

if [ -f "$ENV_FILE" ]; then
  SEARCH_HUB_ROUTER_KEY="$(
    grep '^SEARCH_HUB_ROUTER_KEY=' "$ENV_FILE" 2>/dev/null |
    head -n 1 |
    cut -d= -f2-
  )" || true
  export SEARCH_HUB_ROUTER_KEY
fi

if [ -z "${SEARCH_HUB_ROUTER_KEY:-}" ]; then
  echo "WARN: SEARCH_HUB_ROUTER_KEY is empty — MCP calls run unauthenticated (dev auth-off mode)" >&2
fi

# MCP adapter settings
export MCP_TRANSPORT=http
export SEARCH_ROUTER_URL="${SEARCH_ROUTER_URL:-http://127.0.0.1:8888}"
export MCP_HOST="${MCP_HOST:-127.0.0.1}"
export MCP_PORT="${MCP_PORT:-8901}"
# Must exceed the router's OPENAI_TIMEOUT_S (300s) so the MCP layer never
# disconnects first when the router uses nearly all of its budget.
export MCP_TIMEOUT_S="${MCP_TIMEOUT_S:-360}"

# Default Hermes surface: search / fetch_evidence / code_search / search_places.
# Opt-in deep-research lane (fetch + research, source cap 8→15):
#   export MCP_ENABLE_DEEP_RESEARCH=true
export MCP_ENABLE_DEEP_RESEARCH="${MCP_ENABLE_DEEP_RESEARCH:-false}"

# Use the search-router venv's Python (Windows layout, Linux fallback)
if [ -f "$ROUTER_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON="$ROUTER_DIR/.venv/Scripts/python.exe"
elif [ -f "$ROUTER_DIR/.venv/bin/python" ]; then
  PYTHON="$ROUTER_DIR/.venv/bin/python"
else
  echo "ERROR: search-router venv not found — run: cd search-router && uv sync --extra mcp" >&2
  exit 1
fi

cd "$ROUTER_DIR"
exec "$PYTHON" -m adapters.mcp_server
