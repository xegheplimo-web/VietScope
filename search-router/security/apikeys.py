"""API-key authentication for the public /v1 surface (P10).

Key format:
    dsa_test_<32 urlsafe chars>
    dsa_live_<32 urlsafe chars>

Only sha256(key) is stored — plaintext keys are never persisted.
Auth is OFF unless ``settings.api_auth_enabled`` is true; when off the
dependency is a pass-through so local/dev clients (Vane, Hermes MCP)
keep working without keys.

Scope model: each /v1 endpoint maps to a required scope. A key's
``scopes`` array must contain it, or the wildcard ``*`` / ``admin:*``.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field

from config import settings
from fastapi import HTTPException, Request
from storage import rate_limiter
from storage.pg_client import get_pool

logger = logging.getLogger(__name__)

_KEY_PREFIXES = ("dsa_test_", "dsa_live_")

# Endpoint → required scope. Matched on the path suffix under /v1.
_SCOPE_MAP = {
    "search": "search:read",
    "answer": "answer:use",
    "research": "research:use",
    "research/stream": "research:use",
    "news": "news:use",
    "images": "images:use",
    "read": "read:use",
    # Batch evidence fetch — same reader path as /v1/read (P11.1).
    "evidence": "read:use",
    "verify": "verify:use",
    "business/search": "business:use",
    # OpenAI-compat gateway (P1) — the chat endpoint reuses the answer
    # pipeline, so its scope is its own product scope, not admin:debug.
    "chat/completions": "chat:use",
    # Legacy tool endpoints on app (non-/v1) — the scope name mirrors the
    # /v1 endpoint that supersedes them.
    "fetch": "read:use",
    "code_search": "search:read",
}

# GET paths that stay public even when auth is enabled.
# "auth/check" is the credential probe — its response body IS the verdict,
# so it must never 401/403: a scoped key verifies itself without admin:debug.
_PUBLIC_GET = {"health", "capabilities", "auth/check"}

# GET paths with a real (non-admin) scope — the OpenAI-compat model list
# must be reachable by chat clients, and P2.0 places search/autocomplete/
# detail by scoped places readers (not just admin keys). GET scopes live
# here, NOT in _SCOPE_MAP — required_scope() only consults _SCOPE_MAP for
# non-GET methods. ``{param}`` segments are route templates matched
# per-segment (``places/{id}`` covers /v1/places/42); exact keys win first.
_GET_SCOPE_MAP = {
    "models": "chat:use",
    "places/search": "places:read",
    "places/autocomplete": "places:read",
    "places/{id}": "places:read",
}


def _template_scope(rel: str, scope_map: dict[str, str]) -> str | None:
    """Match ``rel`` against ``{param}`` templates in ``scope_map``."""
    r_parts = rel.split("/")
    for template, scope in scope_map.items():
        if "{" not in template:
            continue
        t_parts = template.split("/")
        if len(t_parts) != len(r_parts):
            continue
        if all(
            tp == rp or (tp.startswith("{") and tp.endswith("}") and rp)
            for tp, rp in zip(t_parts, r_parts, strict=True)
        ):
            return scope
    return None


@dataclass
class APIKeyContext:
    """Authenticated caller attached to request.state.api_key."""

    key_id: str
    tenant_id: str
    scopes: set[str] = field(default_factory=set)
    rpm_limit: int = 60
    daily_quota: int = 1000
    prefix: str = ""

    def has_scope(self, scope: str) -> bool:
        if scope in self.scopes or "*" in self.scopes:
            return True
        # admin:* covers admin-scoped checks only — not product scopes.
        return scope.startswith("admin:") and "admin:*" in self.scopes


def generate_api_key(kind: str = "live") -> tuple[str, str, str]:
    """Return (full_key, key_id, sha256_hash). full_key is shown once."""
    if kind not in ("live", "test"):
        raise ValueError("kind must be 'live' or 'test'")
    full_key = f"dsa_{kind}_{secrets.token_urlsafe(24)}"
    return full_key, str(uuid.uuid4()), hash_key(full_key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def required_scope(path: str, method: str) -> str | None:
    """Map a request to its required scope. None = public."""
    # path like /v1/search or /v1/research/stream (or bare /v1)
    rel = path.split("/v1", 1)[-1].strip("/")
    if method == "GET":
        if rel in _GET_SCOPE_MAP:
            return _GET_SCOPE_MAP[rel]
        templated = _template_scope(rel, _GET_SCOPE_MAP)
        if templated is not None:
            return templated
        return None if rel in _PUBLIC_GET or rel == "" else "admin:debug"
    return _SCOPE_MAP.get(rel, "admin:debug")


async def _lookup_key(key: str) -> APIKeyContext | None:
    pool = await get_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        """
        SELECT key_id, tenant_id, scopes, rpm_limit, daily_quota, key_prefix
        FROM api_keys
        WHERE key_hash = $1 AND revoked_at IS NULL
        """,
        hash_key(key),
    )
    if row is None:
        return None
    return APIKeyContext(
        key_id=row["key_id"],
        tenant_id=row["tenant_id"],
        scopes=set(row["scopes"]),
        rpm_limit=row["rpm_limit"],
        daily_quota=row["daily_quota"],
        prefix=row["key_prefix"],
    )


async def _quota_exceeded(ctx: APIKeyContext) -> bool:
    if ctx.daily_quota < 0:
        return False  # unlimited
    pool = await get_pool()
    if pool is None:
        return False  # DB down — don't hard-fail on quota
    used = await pool.fetchval(
        """
        SELECT COALESCE(SUM(requests), 0) FROM usage_daily
        WHERE day = CURRENT_DATE AND key_id = $1
        """,
        ctx.key_id,
    )
    return int(used) >= ctx.daily_quota


async def require_api_key(request: Request) -> APIKeyContext | None:
    """FastAPI dependency applied router-wide on /v1.

    Pass-through when API_AUTH_ENABLED=false or for public GET paths.
    Sets request.state.api_key for the usage-logger middleware.
    """
    scope = required_scope(request.url.path, request.method)
    request.state.required_scope = scope

    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.startswith("Bearer ") else ""

    if scope is None:
        # Public path — but if a Bearer key was presented, still resolve it
        # so gated detail (e.g. /v1/health services) can check admin scope.
        if key and settings.api_auth_enabled:
            ctx = await _lookup_key(key)
            if ctx is not None:
                request.state.api_key = ctx
                return ctx
        return None
    if not settings.api_auth_enabled:
        return None

    if not key.startswith(_KEY_PREFIXES):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_or_invalid_api_key"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    ctx = await _lookup_key(key)
    if ctx is None:
        # Auth on but DB unreachable → fail closed with a clean 503.
        pool = await get_pool()
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "auth_backend_unavailable"},
            )
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_or_invalid_api_key"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not ctx.has_scope(scope):
        raise HTTPException(
            status_code=403,
            detail={"error": "insufficient_scope", "required": scope},
        )

    if not await rate_limiter.allow(f"api:{ctx.key_id}", ctx.rpm_limit, 60):
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limit_exceeded", "rpm": ctx.rpm_limit},
        )

    if await _quota_exceeded(ctx):
        raise HTTPException(
            status_code=429,
            detail={"error": "daily_quota_exceeded", "quota": ctx.daily_quota},
        )

    request.state.api_key = ctx
    return ctx


async def bootstrap_admin_key() -> None:
    """Store sha256(HUB_ADMIN_KEY) once at startup when auth is on."""
    if not (settings.api_auth_enabled and settings.hub_admin_key):
        return
    pool = await get_pool()
    if pool is None:
        logger.warning("HUB_ADMIN_KEY set but hub-postgres unreachable")
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (tenant_id, name, tier) VALUES ('admin', 'Admin', 'internal') ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            """
            INSERT INTO api_keys (key_id, key_prefix, key_hash, tenant_id, scopes, rpm_limit, daily_quota)
            VALUES ($1, $2, $3, 'admin', '{*}', 10000, -1)
            ON CONFLICT (key_hash) DO NOTHING
            """,
            str(uuid.uuid4()),
            settings.hub_admin_key[:14],
            hash_key(settings.hub_admin_key),
        )
    logger.info("admin API key bootstrapped")
