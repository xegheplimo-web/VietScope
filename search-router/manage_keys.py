#!/usr/bin/env python3
"""API-key management CLI for hub-postgres (P11).

Runs on the host with plain ``python`` — no container needed. Connects to
hub-postgres (compose port 127.0.0.1:5433) via asyncpg.

Usage:
    python manage_keys.py create --tenant acme --name "ci bot" \
        [--scopes "search:read,answer:use"] [--rpm 60] [--quota 1000] [--test]
    python manage_keys.py list [--tenant acme]
    python manage_keys.py revoke --key-id <uuid> | --prefix dsa_live_Ab3

DSN resolution order: --dsn flag > HUB_DATABASE_URL env > built from
HUB_PG_* env vars (defaults match docker-compose: 127.0.0.1:5433,
searchhub/searchhub). The repo-root .env is loaded for HUB_* vars only.

The full key is printed exactly once at creation — only sha256 is stored.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_ROOT_ENV = Path(__file__).resolve().parents[1] / ".env"


def _load_dotenv() -> None:
    """setdefault() HUB_* vars from the repo-root .env (real env wins)."""
    if not _ROOT_ENV.exists():
        return
    for line in _ROOT_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("HUB_"):
            os.environ.setdefault(key, value.strip())


def _resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        return cli_dsn
    env = os.getenv("HUB_DATABASE_URL", "")
    if env:
        return env
    host = os.getenv("HUB_PG_HOST", "127.0.0.1")
    port = os.getenv("HUB_PG_PORT", "5433")
    user = os.getenv("HUB_PG_USER", "searchhub")
    password = os.getenv("HUB_PG_PASSWORD", "searchhub")
    db = os.getenv("HUB_PG_DB", "searchhub")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _parse_scopes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


async def create_key(
    conn,
    *,
    tenant_id: str,
    name: str,
    scopes: list[str],
    rpm: int,
    quota: int,
    kind: str,
) -> dict:
    """Insert tenant (idempotent) + api_keys row. Returns dict incl. full_key."""
    from security.apikeys import generate_api_key

    full_key, key_id, key_hash = generate_api_key(kind)
    await conn.execute(
        "INSERT INTO tenants (tenant_id, name, tier) VALUES ($1, $1, 'free')"
        " ON CONFLICT (tenant_id) DO NOTHING",
        tenant_id,
    )
    await conn.execute(
        """
        INSERT INTO api_keys
            (key_id, key_prefix, key_hash, tenant_id, name, scopes, rpm_limit, daily_quota)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        key_id,
        full_key[:14],
        key_hash,
        tenant_id,
        name,
        scopes,
        rpm,
        quota,
    )
    return {
        "key_id": key_id,
        "full_key": full_key,
        "key_prefix": full_key[:14],
        "tenant_id": tenant_id,
    }


async def list_keys(conn, tenant_id: str | None = None) -> list[dict]:
    base = """
        SELECT key_id, key_prefix, tenant_id, name, scopes,
               rpm_limit, daily_quota, created_at, revoked_at
        FROM api_keys
    """
    if tenant_id:
        rows = await conn.fetch(base + " WHERE tenant_id = $1 ORDER BY created_at DESC", tenant_id)
    else:
        rows = await conn.fetch(base + " ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def revoke_key(conn, *, key_id: str | None = None, prefix: str | None = None) -> int:
    """Soft-revoke by key_id or key_prefix. Returns number of rows revoked."""
    if not key_id and not prefix:
        raise ValueError("revoke requires --key-id or --prefix")
    if key_id:
        result = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE key_id = $1 AND revoked_at IS NULL",
            key_id,
        )
    else:
        result = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE key_prefix = $1 AND revoked_at IS NULL",
            prefix,
        )
    # asyncpg returns e.g. "UPDATE 2"
    return int(result.rsplit(" ", 1)[-1])


def _print_table(rows: list[dict]) -> None:
    headers = ("key_prefix", "tenant", "name", "scopes", "rpm", "quota", "created", "revoked")
    data = [
        (
            r["key_prefix"],
            r["tenant_id"],
            r.get("name") or "",
            ",".join(r["scopes"]) if r["scopes"] else "",
            str(r["rpm_limit"]),
            str(r["daily_quota"]),
            str(r["created_at"] or "")[:19],
            "yes" if r["revoked_at"] else "no",
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in data)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in data:
        print(fmt.format(*row))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search-Hub API key management")
    parser.add_argument("--dsn", help="Postgres DSN (overrides env/default)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a key (full key shown once)")
    p_create.add_argument("--tenant", required=True, help="tenant_id")
    p_create.add_argument("--name", default="", help="human label")
    p_create.add_argument(
        "--scopes", default="*", help='csv, e.g. "search:read,answer:use" (default *)'
    )
    p_create.add_argument("--rpm", type=int, default=60, help="requests/minute")
    p_create.add_argument("--quota", type=int, default=1000, help="requests/day, -1=unlimited")
    p_create.add_argument("--test", action="store_true", help="issue dsa_test_ key")

    p_list = sub.add_parser("list", help="list keys")
    p_list.add_argument("--tenant", help="filter by tenant_id")

    p_revoke = sub.add_parser("revoke", help="revoke by key-id or prefix")
    grp = p_revoke.add_mutually_exclusive_group(required=True)
    grp.add_argument("--key-id")
    grp.add_argument("--prefix")

    args = parser.parse_args(argv)
    _load_dotenv()
    dsn = _resolve_dsn(args.dsn)

    async def _run() -> int:
        import asyncpg

        conn = await asyncpg.connect(dsn=dsn, timeout=10)
        try:
            if args.command == "create":
                result = await create_key(
                    conn,
                    tenant_id=args.tenant,
                    name=args.name,
                    scopes=_parse_scopes(args.scopes),
                    rpm=args.rpm,
                    quota=args.quota,
                    kind="test" if args.test else "live",
                )
                print("API key created — store it now, it is shown only once:")
                print(f"  key_id   : {result['key_id']}")
                print(f"  prefix   : {result['key_prefix']}")
                print(f"  tenant   : {result['tenant_id']}")
                print(f"  full_key : {result['full_key']}")
            elif args.command == "list":
                rows = await list_keys(conn, args.tenant)
                if rows:
                    _print_table(rows)
                else:
                    print("no keys found")
            elif args.command == "revoke":
                n = await revoke_key(conn, key_id=args.key_id, prefix=args.prefix)
                print(f"revoked {n} key(s)")
                return 0 if n else 1
        finally:
            await conn.close()
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
