"""P10 — public API auth, scopes, rate limit, usage logging tests.

Covers: key generation/hashing, path→scope mapping, require_api_key
dependency (disabled pass-through, 401/403/429), usage schedule_log.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from security.apikeys import (
    APIKeyContext,
    generate_api_key,
    hash_key,
    required_scope,
)


def run(coro):
    return asyncio.run(coro)


# ─── Key format ──────────────────────────────────────────────────────────────


def test_generate_key_formats():
    for kind, prefix in (("live", "dsa_live_"), ("test", "dsa_test_")):
        full, key_id, h = generate_api_key(kind)
        assert full.startswith(prefix)
        assert len(full) > len(prefix) + 20
        assert h == hashlib.sha256(full.encode()).hexdigest()
        assert key_id  # uuid string


def test_hash_key_deterministic():
    assert hash_key("dsa_live_abc") == hash_key("dsa_live_abc")
    assert hash_key("a") != hash_key("b")


# ─── Scope mapping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,scope",
    [
        ("/v1/search", "search:read"),
        ("/v1/answer", "answer:use"),
        ("/v1/research", "research:use"),
        ("/v1/research/stream", "research:use"),
        ("/v1/images", "images:use"),
        ("/v1/news", "news:use"),
        ("/v1/read", "read:use"),
        ("/v1/evidence", "read:use"),
        ("/v1/business/search", "business:use"),
        ("/v1/unknown", "admin:debug"),
    ],
)
def test_required_scope_post(path, scope):
    assert required_scope(path, "POST") == scope


@pytest.mark.parametrize("path", ["/v1/health", "/v1/capabilities", "/v1"])
def test_public_get_paths(path):
    assert required_scope(path, "GET") is None


def test_auth_check_is_public_get():
    # The credential probe must be reachable by any scoped key — and by
    # callers holding a WRONG key (the body reports authenticated:false).
    assert required_scope("/v1/auth/check", "GET") is None


def test_non_public_get_requires_admin():
    assert required_scope("/v1/providers", "GET") == "admin:debug"


def test_scope_wildcards():
    ctx = APIKeyContext(key_id="k", tenant_id="t", scopes={"*"})
    assert ctx.has_scope("research:use")
    admin = APIKeyContext(key_id="k", tenant_id="t", scopes={"admin:*"})
    assert admin.has_scope("admin:debug")
    assert not admin.has_scope("search:read")


# ─── require_api_key dependency ──────────────────────────────────────────────


def _req(path="/v1/search", method="POST", auth=None):
    req = MagicMock()
    req.url.path = path
    req.method = method
    req.headers = {"authorization": auth} if auth else {}
    req.state = MagicMock()
    return req


def test_auth_disabled_passthrough():
    with patch("security.apikeys.settings") as s:
        s.api_auth_enabled = False
        from security.apikeys import require_api_key

        assert run(require_api_key(_req())) is None


def test_missing_key_401():
    with patch("security.apikeys.settings") as s:
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        with pytest.raises(Exception) as ei:
            run(require_api_key(_req()))
        assert getattr(ei.value, "status_code", None) == 401


def test_bad_key_401():
    row = None

    class Pool:
        async def fetchrow(self, *a):
            return row

    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=Pool())),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        with pytest.raises(Exception) as ei:
            run(require_api_key(_req(auth="Bearer dsa_live_wrong")))
        assert getattr(ei.value, "status_code", None) == 401


def test_insufficient_scope_403():
    row = {
        "key_id": "k1",
        "tenant_id": "t1",
        "scopes": ["search:read"],
        "rpm_limit": 60,
        "daily_quota": 1000,
        "key_prefix": "dsa_live_x",
    }

    class Pool:
        async def fetchrow(self, *a):
            return row

    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=Pool())),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        # key only has search:read but endpoint needs research:use
        with pytest.raises(Exception) as ei:
            run(require_api_key(_req(path="/v1/research", auth="Bearer dsa_live_k")))
        assert getattr(ei.value, "status_code", None) == 403


def test_valid_key_sets_state():
    row = {
        "key_id": "k1",
        "tenant_id": "t1",
        "scopes": ["search:read"],
        "rpm_limit": 60,
        "daily_quota": -1,
        "key_prefix": "dsa_live_x",
    }

    class Pool:
        async def fetchrow(self, *a):
            return row

    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=Pool())),
        patch("security.apikeys.rate_limiter.allow", new=AsyncMock(return_value=True)),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        req = _req(auth="Bearer dsa_live_valid")
        ctx = run(require_api_key(req))
        assert ctx is not None and ctx.tenant_id == "t1"
        assert req.state.api_key is ctx


def test_rate_limit_429():
    row = {
        "key_id": "k1",
        "tenant_id": "t1",
        "scopes": ["search:read"],
        "rpm_limit": 60,
        "daily_quota": -1,
        "key_prefix": "dsa_live_x",
    }

    class Pool:
        async def fetchrow(self, *a):
            return row

    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=Pool())),
        patch("security.apikeys.rate_limiter.allow", new=AsyncMock(return_value=False)),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        with pytest.raises(Exception) as ei:
            run(require_api_key(_req(auth="Bearer dsa_live_valid")))
        assert getattr(ei.value, "status_code", None) == 429


# ─── /v1/auth/check probe ────────────────────────────────────────────────────


def _key_row(**over):
    row = {
        "key_id": "k1",
        "tenant_id": "hermes",
        "scopes": ["search:read", "read:use"],
        "rpm_limit": 60,
        "daily_quota": -1,
        "key_prefix": "dsa_live_x",
    }
    row.update(over)
    return row


class _Pool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *a):
        return self._row


def test_auth_check_valid_key_resolves_ctx():
    """Public path still resolves a presented key → state.api_key set."""
    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=_Pool(_key_row()))),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        req = _req(path="/v1/auth/check", method="GET", auth="Bearer dsa_live_ok")
        ctx = run(require_api_key(req))
        assert ctx is not None
        assert ctx.tenant_id == "hermes"
        assert req.state.api_key is ctx


def test_auth_check_bad_key_no_raise():
    """A wrong key must NOT 401 — the probe body reports authenticated:false."""
    with (
        patch("security.apikeys.settings") as s,
        patch("security.apikeys.get_pool", new=AsyncMock(return_value=_Pool(None))),
    ):
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        req = _req(path="/v1/auth/check", method="GET", auth="Bearer dsa_live_bad")
        req.state = SimpleNamespace()  # state.api_key must stay unset
        assert run(require_api_key(req)) is None
        assert getattr(req.state, "api_key", None) is None


def test_auth_check_no_key_no_raise():
    with patch("security.apikeys.settings") as s:
        s.api_auth_enabled = True
        from security.apikeys import require_api_key

        req = _req(path="/v1/auth/check", method="GET")
        assert run(require_api_key(req)) is None


def test_auth_check_endpoint_shape():
    from api.v1 import auth_check

    ctx = APIKeyContext(
        key_id="k1",
        tenant_id="hermes",
        scopes={"read:use", "search:read"},
    )
    req = MagicMock()
    req.state.api_key = ctx
    with patch("api.v1.settings") as s:
        s.api_auth_enabled = True
        out = run(auth_check(req))
    assert out.authenticated is True
    assert out.auth_enabled is True
    assert out.tenant == "hermes"
    assert out.scopes == ["read:use", "search:read"]  # sorted


def test_auth_check_endpoint_anonymous():
    from api.v1 import auth_check

    req = MagicMock()
    req.state.api_key = None
    with patch("api.v1.settings") as s:
        s.api_auth_enabled = True
        out = run(auth_check(req))
    assert out.authenticated is False
    assert out.tenant is None
    assert out.scopes == []


def test_auth_check_endpoint_auth_disabled():
    """Dev mode: auth_enabled=false so clients know there's nothing to verify."""
    from api.v1 import auth_check

    req = MagicMock()
    req.state.api_key = None
    with patch("api.v1.settings") as s:
        s.api_auth_enabled = False
        out = run(auth_check(req))
    assert out.authenticated is False
    assert out.auth_enabled is False


# ─── Usage logging ───────────────────────────────────────────────────────────


def test_schedule_log_noop_without_ctx():
    from security.usage import schedule_log

    req = MagicMock()
    req.state.api_key = None
    req.url.path = "/v1/search"
    schedule_log(req, 200, 10)  # must not raise


def test_query_hash_never_stores_plaintext():
    from security.usage import _hash_query

    h = _hash_query(b'{"query": "secret medical question"}')
    assert h and "secret" not in h
    assert _hash_query(b"") is None
