"""Usage metering into hub-postgres (P10).

Fire-and-forget: logging failures are swallowed (metering must never break
serving). Reads ``request.state.api_key`` set by the auth dependency.
"""

import asyncio
import contextlib
import hashlib
import logging
from typing import Any

from fastapi import Request
from storage.pg_client import get_pool

logger = logging.getLogger(__name__)


def _hash_query(body: bytes) -> str | None:
    if not body:
        return None
    # Never store raw queries — hash them.
    return hashlib.sha256(body[:2048]).hexdigest()[:32]


async def _write(
    ctx: Any, endpoint: str, status: int, latency_ms: int, query_hash: str | None
) -> None:
    pool = await get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO query_logs (tenant_id, key_id, endpoint, status_code, latency_ms, query_hash)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                ctx.tenant_id,
                ctx.key_id,
                endpoint,
                status,
                latency_ms,
                query_hash,
            )
            await conn.execute(
                """
                INSERT INTO usage_daily (day, tenant_id, key_id, endpoint, requests, errors)
                VALUES (CURRENT_DATE, $1, $2, $3, 1, $4)
                ON CONFLICT (day, tenant_id, key_id, endpoint)
                DO UPDATE SET requests = usage_daily.requests + 1,
                              errors = usage_daily.errors + EXCLUDED.errors
                """,
                ctx.tenant_id,
                ctx.key_id,
                endpoint,
                1 if status >= 400 else 0,
            )
    except Exception as exc:
        logger.debug("usage log failed: %r", exc)


def schedule_log(request: Request, status: int, latency_ms: int) -> None:
    """Schedule a usage write — call from middleware after the response."""
    ctx = getattr(request.state, "api_key", None)
    if ctx is None:
        return
    endpoint = request.url.path.split("/v1/", 1)[-1] or request.url.path
    with contextlib.suppress(RuntimeError):
        # no running loop (tests, shutdown) — drop the log
        asyncio.get_running_loop().create_task(_write(ctx, endpoint, status, latency_ms, None))
