"""Tests for crawler/netguard.py — SSRF policy + redirect-validated GET.

Literal-IP cases use the real resolver (getaddrinfo parses them without
network); hostname cases inject a fake resolver. ``guarded_get`` runs over
``httpx.MockTransport`` — no sockets.
"""

from __future__ import annotations

import asyncio

import httpcore
import httpx
import pytest
from crawler.netguard import (
    _MAX_DIAL_ATTEMPTS,
    NetGuard,
    SSRFError,
    ValidatedTransport,
    ValidatingNetworkBackend,
    guarded_client,
    guarded_get,
)

LOCAL_HTML = b"<html><body>ok</body></html>"


def _run(coro):
    return asyncio.run(coro)


def _guard(*ips: str) -> NetGuard:
    """NetGuard whose resolver always answers ``ips``."""
    return NetGuard(resolver=lambda host: list(ips))


# ─── URL policy ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "https://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",  # v4-mapped loopback
        "http://[fd00::1]/",  # unique-local
        "http://0.0.0.0/",
    ],
)
def test_private_and_special_ips_rejected(url):
    with pytest.raises(SSRFError):
        _run(NetGuard().check(url))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "http://user:pass@example.com/",
        "https://user@example.com/",
        "http:///path-no-host",
        "not-a-url",
    ],
)
def test_scheme_userinfo_and_shape_rejected(url):
    with pytest.raises(SSRFError):
        _run(NetGuard().check(url))


def test_public_host_allowed_with_resolver():
    guard = _guard("93.184.216.34")
    assert _run(guard.check("https://example.com/")) == "https://example.com/"


def test_hostname_resolving_private_rejected():
    guard = _guard("10.1.2.3")
    with pytest.raises(SSRFError):
        _run(guard.check("https://evil.example.com/"))


def test_hostname_resolving_mixed_rejected():
    # One private answer among publics still rejects the destination.
    guard = _guard("93.184.216.34", "127.0.0.1")
    with pytest.raises(SSRFError):
        _run(guard.check("https://example.com/"))


def test_unresolvable_host_refused():
    # DNS failure is fail-closed — an unvettable destination is refused
    # instead of deferred to the client (F1).
    def boom(host):
        raise OSError("NXDOMAIN")

    guard = NetGuard(resolver=boom)
    with pytest.raises(SSRFError):
        _run(guard.check("https://no-such-host.invalid/"))


def test_empty_dns_answer_refused():
    # A resolver answering zero addresses is also a refusal.
    guard = NetGuard(resolver=lambda host: [])
    with pytest.raises(SSRFError):
        _run(guard.check("https://empty-answer.example/"))


def test_resolver_returning_garbage_refused():
    # Non-IP answers must not slip through the public-IP check.
    guard = NetGuard(resolver=lambda host: ["not-an-ip"])
    with pytest.raises(SSRFError):
        _run(guard.check("https://weird.example/"))


def test_allow_private_escape_hatch():
    guard = NetGuard(allow_private=True)
    assert _run(guard.check("http://127.0.0.1/")) == "http://127.0.0.1/"


# ─── guarded_get: redirect validation + caps ─────────────────────────────


def _client(routes: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        if isinstance(route, Exception):
            raise route
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _get(client, url, **kw):
    kw.setdefault("headers", {})
    kw.setdefault("timeout", 5.0)
    kw.setdefault("max_redirects", 5)
    kw.setdefault("max_bytes", 1024)
    kw.setdefault("netguard", _guard("93.184.216.34"))
    return await guarded_get(client, url, **kw)


def test_guarded_get_follows_public_redirect_chain():
    client = _client(
        {
            "https://a.vn/old": (301, b"", {"location": "https://b.vn/new"}),
            "https://b.vn/new": (200, LOCAL_HTML, {"content-type": "text/html"}),
        }
    )
    resp = _run(_get(client, "https://a.vn/old"))
    assert resp.error is None
    assert resp.status == 200
    assert resp.body == LOCAL_HTML
    assert resp.final_url == "https://b.vn/new"
    assert resp.redirects == ["https://b.vn/new"]


def test_guarded_get_blocks_redirect_to_private_host():
    # Origin is public; hop 2 resolves to loopback → SSRFError on the hop.
    client = _client(
        {
            "https://a.vn/r": (302, b"", {"location": "http://169.254.169.254/x"}),
        }
    )
    with pytest.raises(SSRFError):
        _run(_get(client, "https://a.vn/r"))


def test_guarded_get_redirect_without_location_is_error():
    client = _client({"https://a.vn/r": (302, b"", {})})
    resp = _run(_get(client, "https://a.vn/r"))
    assert resp.error == "redirect_no_location"
    assert resp.status == 302


def test_guarded_get_too_many_redirects():
    routes = {
        f"https://a.vn/{i}": (301, b"", {"location": f"https://a.vn/{i + 1}"}) for i in range(10)
    }
    client = _client(routes)
    resp = _run(_get(client, "https://a.vn/0", max_redirects=3))
    assert resp.error == "too_many_redirects"


def test_guarded_get_oversize_marks_not_truncates():
    big = b"x" * 5000
    client = _client({"https://a.vn/big": (200, big, {})})
    resp = _run(_get(client, "https://a.vn/big", max_bytes=1024))
    assert resp.oversize is True
    assert len(resp.body) <= 1024


def test_guarded_get_exact_cap_not_oversize():
    body = b"y" * 1024
    client = _client({"https://a.vn/exact": (200, body, {})})
    resp = _run(_get(client, "https://a.vn/exact", max_bytes=1024))
    assert resp.oversize is False
    assert resp.body == body


def test_guarded_get_rejects_private_origin():
    client = _client({"http://127.0.0.1/": (200, LOCAL_HTML, {})})
    with pytest.raises(SSRFError):
        _run(_get(client, "http://127.0.0.1/", netguard=NetGuard()))


def test_guarded_get_rejects_redirect_to_userinfo_url():
    client = _client({"https://a.vn/r": (301, b"", {"location": "https://user@b.vn/x"})})
    with pytest.raises(SSRFError):
        _run(_get(client, "https://a.vn/r"))


# ─── F3: per-hop policy_check ────────────────────────────────────────────


def test_guarded_get_policy_check_blocks_hop_before_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://a.vn/r":
            return httpx.Response(302, headers={"location": "https://b.vn/x"})
        return httpx.Response(200, content=LOCAL_HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def policy(target: str):
        return "skipped_robots" if "b.vn" in target else None

    resp = _run(_get(client, "https://a.vn/r", policy_check=policy))
    assert resp.error == "skipped_robots"
    assert resp.final_url == "https://b.vn/x"
    # The refused hop was never contacted — only the origin was fetched.
    assert calls == ["https://a.vn/r"]


def test_guarded_get_policy_check_blocks_origin_too():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=LOCAL_HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def refuse(_target: str):
        return "politeness"

    resp = _run(_get(client, "https://a.vn/x", policy_check=refuse))
    assert resp.error == "politeness"
    assert calls == []  # not a single request was sent


def test_guarded_get_policy_check_none_default_unchanged():
    client = _client({"https://a.vn/x": (200, LOCAL_HTML, {})})
    resp = _run(_get(client, "https://a.vn/x"))
    assert resp.error is None
    assert resp.status == 200


# ─── F1: ValidatedTransport re-checks at dial time ───────────────────────


def test_validated_transport_validates_then_dials(monkeypatch):
    dialed: list[str] = []

    async def fake_dial(self, request):
        dialed.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_dial)
    client = guarded_client(_guard("93.184.216.34"))
    resp = _run(client.get("https://example.com/"))
    assert resp.status_code == 200
    assert dialed == ["https://example.com/"]


def test_validated_transport_refuses_private_resolution():
    # Even without a prior check() call, the transport refuses a host
    # that resolves non-public at request time.
    client = httpx.AsyncClient(transport=ValidatedTransport(_guard("10.9.9.9")))
    with pytest.raises(SSRFError):
        _run(client.get("https://sneaky.example/"))


def test_validated_transport_refuses_dns_failure():
    def boom(host):
        raise OSError("NXDOMAIN")

    client = httpx.AsyncClient(transport=ValidatedTransport(NetGuard(resolver=boom)))
    with pytest.raises(SSRFError):
        _run(client.get("https://gone.example/"))


# ─── G1: DNS pinning — the checked answer is the dialed one ──────────────


class _FakeBackend(httpcore.AsyncNetworkBackend):
    """Records dialed (host, port); returns a placeholder stream."""

    def __init__(self):
        self.dialed: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialed.append((host, port))
        return httpcore.AsyncNetworkStream()

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds):
        pass


def test_backend_dials_the_validated_public_answer():
    # Mixed answers — the private one is never dialed: the socket goes to
    # the validated public answer even when DNS listed it second.
    fake = _FakeBackend()
    backend = ValidatingNetworkBackend(
        NetGuard(resolver=lambda host: ["10.0.0.9", "93.184.216.34"]), fake
    )
    _run(backend.connect_tcp("evil.example", 443))
    assert fake.dialed == [("93.184.216.34", 443)]


class _FlakyBackend(_FakeBackend):
    """Fails connect_tcp for addresses in ``fail_on``; records timeouts."""

    def __init__(self, fail_on=()):
        super().__init__()
        self.fail_on = set(fail_on)
        self.timeouts: list[float | None] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.timeouts.append(timeout)
        self.dialed.append((host, port))
        if host in self.fail_on:
            raise httpcore.ConnectError(f"refused {host}")
        return httpcore.AsyncNetworkStream()


# ─── R1: multi-address fallback — every validated answer gets a dial ─────


def test_backend_falls_back_to_next_validated_answer():
    # R1: a refused dial on the first public answer must not fail the
    # connection while other validated answers remain — IPv6-first on an
    # IPv4-only network is the motivating case.
    fake = _FlakyBackend(fail_on={"2606:4700:4700::1111"})
    backend = ValidatingNetworkBackend(
        NetGuard(resolver=lambda host: ["2606:4700:4700::1111", "93.184.216.34"]), fake
    )
    stream = _run(backend.connect_tcp("multi.example", 443))
    assert isinstance(stream, httpcore.AsyncNetworkStream)
    assert fake.dialed == [("2606:4700:4700::1111", 443), ("93.184.216.34", 443)]


def test_backend_exhausted_validated_answers_raise_last_error():
    # Every validated answer refuses → the LAST attempt's error surfaces.
    fake = _FlakyBackend(fail_on={"93.184.216.34", "93.184.216.35"})
    backend = ValidatingNetworkBackend(
        NetGuard(resolver=lambda host: ["93.184.216.34", "93.184.216.35"]), fake
    )
    with pytest.raises(httpcore.ConnectError, match="93.184.216.35"):
        _run(backend.connect_tcp("dead.example", 443))
    assert fake.dialed == [("93.184.216.34", 443), ("93.184.216.35", 443)]


def test_backend_dial_attempts_bounded():
    # More public answers than the dial budget — only the first N tried.
    ips = ["93.184.216.34", "93.184.216.35", "93.184.216.36", "93.184.216.37"]
    fake = _FlakyBackend(fail_on=ips)
    backend = ValidatingNetworkBackend(NetGuard(resolver=lambda host: list(ips)), fake)
    with pytest.raises(httpcore.ConnectError):
        _run(backend.connect_tcp("many.example", 443))
    assert fake.dialed == [(ip, 443) for ip in ips[:_MAX_DIAL_ATTEMPTS]]


def test_backend_per_attempt_timeout_splits_budget():
    # The caller's connect budget is divided across the validated answers
    # — N answers never multiply the total connection wait.
    ips = ["93.184.216.34", "93.184.216.35"]
    fake = _FlakyBackend(fail_on=ips)
    backend = ValidatingNetworkBackend(NetGuard(resolver=lambda host: list(ips)), fake)
    with pytest.raises(httpcore.ConnectError):
        _run(backend.connect_tcp("multi.example", 443, timeout=9.0))
    assert fake.timeouts == [4.5, 4.5]


def test_backend_single_answer_keeps_full_timeout():
    ips = ["93.184.216.34"]
    fake = _FlakyBackend(fail_on=ips)
    backend = ValidatingNetworkBackend(NetGuard(resolver=lambda host: list(ips)), fake)
    with pytest.raises(httpcore.ConnectError):
        _run(backend.connect_tcp("one.example", 443, timeout=9.0))
    assert fake.timeouts == [9.0]


def test_backend_all_private_answers_refused():
    fake = _FakeBackend()
    backend = ValidatingNetworkBackend(
        NetGuard(resolver=lambda host: ["10.0.0.9", "192.168.1.1"]), fake
    )
    with pytest.raises(ConnectionError):
        _run(backend.connect_tcp("internal.example", 443))
    assert fake.dialed == []


def test_backend_resolver_error_refused_fail_closed():
    def boom(host):
        raise OSError("NXDOMAIN")

    fake = _FakeBackend()
    backend = ValidatingNetworkBackend(NetGuard(resolver=boom), fake)
    with pytest.raises(ConnectionError):
        _run(backend.connect_tcp("gone.example", 443))
    assert fake.dialed == []


def test_backend_delegates_timeout_and_socket_options():
    seen: dict = {}

    class SpyBackend(_FakeBackend):
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            seen.update(timeout=timeout, local_address=local_address, socket_options=socket_options)
            return await super().connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    backend = ValidatingNetworkBackend(_guard("93.184.216.34"), SpyBackend())
    _run(
        backend.connect_tcp(
            "example.com", 443, timeout=9.0, local_address="1.2.3.4", socket_options=[(1, 2, 3)]
        )
    )
    assert seen == {"timeout": 9.0, "local_address": "1.2.3.4", "socket_options": [(1, 2, 3)]}


def test_validated_transport_installs_validating_backend():
    transport = ValidatedTransport(NetGuard())
    assert isinstance(transport._pool._network_backend, ValidatingNetworkBackend)


def test_validated_transport_url_policy_still_request_level():
    # Scheme/userinfo stay checked in handle_async_request — no DNS needed.
    guard = NetGuard(resolver=lambda host: pytest.fail(f"DNS ran for request-level check: {host}"))
    client = httpx.AsyncClient(transport=ValidatedTransport(guard))
    with pytest.raises(SSRFError):
        _run(client.get("ftp://example.com/x"))
    with pytest.raises(SSRFError):
        _run(client.get("https://user:pw@example.com/x"))
