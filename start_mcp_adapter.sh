#!/bin/bash
# Start MCP adapter for Hermes <-> Search-Hub integration
# Auth: scoped SEARCH_HUB_ROUTER_KEY from .env — the admin key must never
# sit on the normal MCP path (least privilege). Create it with:
#   python search-router/manage_keys.py create --tenant hermes \
#     --name "hermes-mcp" --scopes "search:read,read:use"

export SEARCH_HUB_ROUTER_KEY=$(grep "^SEARCH_HUB_ROUTER_KEY=" /f/Search-Hub/.env | cut -d= -f2-)

if [ -z "$SEARCH_HUB_ROUTER_KEY" ]; then
  echo "WARN: SEARCH_HUB_ROUTER_KEY is empty — MCP calls run unauthenticated (dev auth-off mode)" >&2
fi

# MCP adapter settings
export MCP_TRANSPORT=http
export SEARCH_ROUTER_URL=http://127.0.0.1:8888
export MCP_HOST=127.0.0.1
export MCP_PORT=8901
# Must exceed the router's OPENAI_TIMEOUT_S (300s) so the MCP layer never
# disconnects first when the router uses nearly all of its budget.
export MCP_TIMEOUT_S="${MCP_TIMEOUT_S:-360}"

# Default Hermes surface: search / fetch_evidence / code_search only.
# Opt-in deep-research lane (fetch + research, source cap 8→15):
#   export MCP_ENABLE_DEEP_RESEARCH=true
export MCP_ENABLE_DEEP_RESEARCH="${MCP_ENABLE_DEEP_RESEARCH:-false}"

# Use the search-router venv's Python
cd /f/Search-Hub/search-router && /f/Search-Hub/search-router/.venv/Scripts/python.exe -m adapters.mcp_server
