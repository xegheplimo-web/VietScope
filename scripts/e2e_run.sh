#!/usr/bin/env bash
# P11-T5 — full-stack E2E runner (git-bash friendly).
#
# Creates a throwaway dsa_test_ API key via search-router/manage_keys.py,
# runs the e2e-marked pytest suite against the live local stack, then
# revokes the key. Exit code = pytest's exit code.
#
# Prereqs:
#   - docker compose up -d  (search-router :8888, hub-postgres :5433, ...)
#   - MCP server on :8901   (scripts/start-mcp-server.ps1)
#   - host python with: httpx pytest asyncpg  (pip install -r search-router/requirements.txt + asyncpg)
#
# Env overrides:
#   E2E_BASE_URL  default http://127.0.0.1:8888
#   E2E_MCP_URL   default http://127.0.0.1:8901/mcp
#   E2E_API_KEY   reuse an existing key (skip create/revoke)
#   E2E_KEY_ID    key_id belonging to E2E_API_KEY (metering test needs it)
#   E2E_DSN       hub-postgres DSN override
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/search-router" || exit 2

export E2E=1
export E2E_BASE_URL="${E2E_BASE_URL:-http://127.0.0.1:8888}"
export E2E_MCP_URL="${E2E_MCP_URL:-http://127.0.0.1:8901/mcp}"

CREATED_KEY_ID=""
cleanup() {
    if [ -n "$CREATED_KEY_ID" ]; then
        echo "[e2e] revoking test key $CREATED_KEY_ID"
        python manage_keys.py revoke --key-id "$CREATED_KEY_ID" || true
    fi
}
trap cleanup EXIT

if [ -z "${E2E_API_KEY:-}" ]; then
    echo "[e2e] creating test API key (tenant=e2e, scopes=*, rpm=600, quota=-1)"
    OUT="$(python manage_keys.py create --tenant e2e --name "pytest-e2e run" \
        --scopes '*' --rpm 600 --quota -1 --test)"
    # Never echo the full key — show the redacted transcript instead.
    echo "$OUT" | sed -E 's/(full_key : ).*/\1<redacted>/'
    E2E_API_KEY="$(printf '%s\n' "$OUT" | sed -n -E 's/.*full_key : ([^[:space:]]+).*/\1/p')"
    CREATED_KEY_ID="$(printf '%s\n' "$OUT" | sed -n -E 's/.*key_id   : ([^[:space:]]+).*/\1/p')"
    if [ -z "$E2E_API_KEY" ] || [ -z "$CREATED_KEY_ID" ]; then
        echo "[e2e] ERROR: could not parse manage_keys output" >&2
        exit 2
    fi
    export E2E_API_KEY E2E_KEY_ID="$CREATED_KEY_ID"
else
    echo "[e2e] reusing E2E_API_KEY from env"
fi

echo "[e2e] running pytest -m e2e against $E2E_BASE_URL"
env -u PYTHONPATH python -m pytest tests/e2e -m e2e -q -p no:cacheprovider "$@"
rc=$?
echo "[e2e] pytest exit code: $rc"
exit "$rc"
