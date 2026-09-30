"""P11 — live API-key auth + manage_keys CLI tests (mocked, no real DB).

Covers: required_scope mapping for every /v1 endpoint, APIKeyContext.has_scope
semantics (wildcard / admin:* / exact), and manage_keys create/list/revoke
logic against a fake asyncpg connection.
"""

from __future__ import annotations

import asyncio

import pytest
from security.apikeys import APIKeyContext, hash_key, required_scope


def run(coro):
    return asyncio.run(coro)


# ─── required_scope — every /v1 endpoint ─────────────────────────────────────


class TestRequiredScopeMapping:
    @pytest.mark.parametrize(
        "path,scope",
        [
            ("/v1/search", "search:read"),
            ("/v1/answer", "answer:use"),
            ("/v1/research", "research:use"),
            ("/v1/research/stream", "research:use"),
            ("/v1/news", "news:use"),
            ("/v1/images", "images:use"),
            ("/v1/read", "read:use"),
            ("/v1/verify", "verify:use"),
            ("/v1/business/search", "business:use"),
        ],
    )
    def test_post_endpoints(self, path, scope):
        assert required_scope(path, "POST") == scope

    def test_unknown_post_is_admin(self):
        assert required_scope("/v1/nope", "POST") == "admin:debug"

    @pytest.mark.parametrize("path", ["/v1/health", "/v1/capabilities", "/v1", "/v1/"])
    def test_public_gets(self, path):
        assert required_scope(path, "GET") is None

    def test_non_public_get_is_admin(self):
        assert required_scope("/v1/providers", "GET") == "admin:debug"


# ─── APIKeyContext.has_scope ──────────────────────────────────────────────────


class TestHasScope:
    def test_exact_scope(self):
        ctx = APIKeyContext(key_id="k", tenant_id="t", scopes={"search:read"})
        assert ctx.has_scope("search:read")
        assert not ctx.has_scope("answer:use")

    def test_wildcard_covers_everything(self):
        ctx = APIKeyContext(key_id="k", tenant_id="t", scopes={"*"})
        assert ctx.has_scope("search:read")
        assert ctx.has_scope("admin:debug")
        assert ctx.has_scope("anything:at-all")

    def test_admin_wildcard_only_covers_admin(self):
        ctx = APIKeyContext(key_id="k", tenant_id="t", scopes={"admin:*"})
        assert ctx.has_scope("admin:debug")
        assert ctx.has_scope("admin:keys")
        assert not ctx.has_scope("search:read")
        assert not ctx.has_scope("answer:use")

    def test_multiple_scopes(self):
        ctx = APIKeyContext(key_id="k", tenant_id="t", scopes={"search:read", "answer:use"})
        assert ctx.has_scope("search:read")
        assert ctx.has_scope("answer:use")
        assert not ctx.has_scope("research:use")


# ─── manage_keys CLI ──────────────────────────────────────────────────────────


class FakeConn:
    """Minimal asyncpg-Connection stand-in recording every query."""

    def __init__(self, rows=None):
        self.queries: list[tuple[str, tuple]] = []
        self._rows = rows or []
        self._exec_result = "UPDATE 1"

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return self._exec_result

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self._rows

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self._rows[0] if self._rows else None


class TestParseScopes:
    def test_csv(self):
        from manage_keys import _parse_scopes

        assert _parse_scopes("search:read,answer:use") == ["search:read", "answer:use"]

    def test_spaces_and_empty(self):
        from manage_keys import _parse_scopes

        assert _parse_scopes(" search:read , , answer:use ") == [
            "search:read",
            "answer:use",
        ]
        assert _parse_scopes("") == []
        assert _parse_scopes(None) == []


class TestCreateKey:
    def test_inserts_hash_never_plaintext(self):
        from manage_keys import create_key

        conn = FakeConn()
        result = run(
            create_key(
                conn,
                tenant_id="t1",
                name="ci bot",
                scopes=["search:read"],
                rpm=60,
                quota=1000,
                kind="live",
            )
        )

        assert result["full_key"].startswith("dsa_live_")
        assert result["key_id"]
        assert any("INTO api_keys" in q for q, _ in conn.queries)
        all_args = [str(v) for _, args in conn.queries for v in args]
        assert hash_key(result["full_key"]) in all_args
        assert result["full_key"] not in all_args

    def test_test_kind_prefix(self):
        from manage_keys import create_key

        conn = FakeConn()
        result = run(
            create_key(
                conn,
                tenant_id="t1",
                name="x",
                scopes=["*"],
                rpm=10,
                quota=-1,
                kind="test",
            )
        )
        assert result["full_key"].startswith("dsa_test_")

    def test_upserts_tenant_first(self):
        from manage_keys import create_key

        conn = FakeConn()
        run(
            create_key(
                conn,
                tenant_id="newt",
                name="x",
                scopes=["search:read"],
                rpm=60,
                quota=1000,
                kind="live",
            )
        )
        assert any("INTO tenants" in q for q, _ in conn.queries)

    def test_scopes_stored_as_pg_array(self):
        from manage_keys import create_key

        conn = FakeConn()
        run(
            create_key(
                conn,
                tenant_id="t1",
                name="x",
                scopes=["search:read", "answer:use"],
                rpm=60,
                quota=1000,
                kind="live",
            )
        )
        _, args = next(qa for qa in conn.queries if "INTO api_keys" in qa[0])
        assert ["search:read", "answer:use"] in [a for a in args if isinstance(a, list)]


class TestRevokeKey:
    def test_revoke_by_key_id(self):
        from manage_keys import revoke_key

        conn = FakeConn()
        n = run(revoke_key(conn, key_id="abc-123"))
        assert n == 1
        q, args = conn.queries[-1]
        assert "revoked_at" in q and "key_id" in q
        assert "abc-123" in args

    def test_revoke_by_prefix(self):
        from manage_keys import revoke_key

        conn = FakeConn()
        n = run(revoke_key(conn, prefix="dsa_live_Ab3"))
        assert n == 1
        q, args = conn.queries[-1]
        assert "key_prefix" in q
        assert "dsa_live_Ab3" in args

    def test_revoke_requires_identifier(self):
        from manage_keys import revoke_key

        with pytest.raises(ValueError):
            run(revoke_key(FakeConn()))

    def test_revoke_reports_zero(self):
        from manage_keys import revoke_key

        conn = FakeConn()
        conn._exec_result = "UPDATE 0"
        assert run(revoke_key(conn, key_id="nope")) == 0


class TestListKeys:
    def test_list_all(self):
        rows = [
            {
                "key_id": "k1",
                "key_prefix": "dsa_live_a",
                "tenant_id": "t1",
                "name": "bot",
                "scopes": ["search:read"],
                "rpm_limit": 60,
                "daily_quota": 1000,
                "created_at": None,
                "revoked_at": None,
            }
        ]
        from manage_keys import list_keys

        conn = FakeConn(rows=rows)
        out = run(list_keys(conn))
        assert len(out) == 1 and out[0]["tenant_id"] == "t1"

    def test_list_filtered_by_tenant(self):
        from manage_keys import list_keys

        conn = FakeConn(rows=[])
        run(list_keys(conn, tenant_id="t9"))
        q, args = conn.queries[-1]
        assert "tenant_id" in q and "t9" in args


class TestResolveDSN:
    def test_cli_arg_wins(self, monkeypatch):
        from manage_keys import _resolve_dsn

        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://env/db")
        assert _resolve_dsn("postgresql://cli/db") == "postgresql://cli/db"

    def test_env_fallback(self, monkeypatch):
        from manage_keys import _resolve_dsn

        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://env/db")
        assert _resolve_dsn(None) == "postgresql://env/db"

    def test_default_is_host_loopback(self, monkeypatch):
        from manage_keys import _resolve_dsn

        monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
        dsn = _resolve_dsn(None)
        assert "127.0.0.1" in dsn and "searchhub" in dsn
