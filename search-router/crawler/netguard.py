"""SSRF guard + redirect-validated streaming GET for the crawl engine.

``NetGuard.check`` validates a destination *before* any bytes are
requested: only ``http``/``https`` schemes, no userinfo (embedded
credentials), and the resolved address must be publicly routable —
loopback, RFC-1918, link-local (incl. ``169.254.169.254``), unique-local,
multicast, and reserved ranges are rejected. Hostnames are resolved up
front via ``socket.getaddrinfo`` so numeric oddities (``2130706433``,
``0x7f000001``, ``0177.0.0.1``) and IPv4-mapped IPv6 literals are caught
by the same public-IP check.

``guarded_get`` performs a streaming GET with *manual* redirect handling:
every hop's URL passes through ``NetGuard.check`` before the request is
sent, so a public seed cannot redirect the crawler into internal space.
It also enforces the body byte cap *before* each chunk is appended
(``oversize=True`` when the body would exceed the cap) and reports
redirect chains that end without a ``Location`` header or exceed
``max_redirects`` as explicit errors.

Resolution is fail-*closed*: an unresolvable host or an empty answer
raises ``SSRFError`` — the crawler must not egress toward a destination
it could not vet.

``ValidatedTransport`` enforces egress policy *at the connection layer*:
its connection pool's ``ValidatingNetworkBackend`` resolves the host
itself, requires at least one publicly-routable answer, and dials the
validated IPs in order — the checked resolution IS the connected one, so
a DNS-rebinding host cannot serve a public answer to the check and a
private one to the dial (G1). A refused or unreachable answer falls back
to the next validated one within a bounded budget (``_MAX_DIAL_ATTEMPTS``
tries, the caller's timeout split evenly), so e.g. an IPv6-first answer
on an IPv4-only network does not sink the crawl (R1). TLS is unaffected:
httpcore still handshakes with the request's original ``server_hostname``,
so SNI and certificate verification match the URL's host, not the dialed
address.
``handle_async_request`` retains the URL-level policy (scheme, userinfo,
host shape); IP validation lives solely in the backend. Pooled
keep-alive connections stay pinned to the IP they were validated
against — a rebind cannot retarget an open socket either.
``guarded_client`` builds an ``AsyncClient`` on that transport; the
fetcher/robots/sitemap code paths use it for every client they create
themselves (injected transports in tests never touch DNS, so they need
no guard).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# How many validated addresses a single connect may try — the caller's
# connect timeout is divided across them, so the budget stays constant.
_MAX_DIAL_ATTEMPTS = 3


class SSRFError(ConnectionError):
    """Destination URL failed egress policy (scheme, userinfo, or non-public IP).

    A ``ConnectionError`` because the refusal surfaces from the connect
    path — the dial never happened, which is exactly what a connection
    failure means to the transport.
    """


def _default_resolve(host: str) -> list[str]:
    """All A/AAAA answers for ``host`` (also parses IP literals)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    # is_global covers loopback/private/link-local/ULA/reserved/multicast/
    # unspecified plus special registries like 169.254.0.0/16 and 100.64/10.
    return addr.is_global


class NetGuard:
    """Egress policy check for URLs the crawler is about to request.

    ``resolver`` is injectable (``host -> iterable of IP strings``) for
    tests; the default consults real DNS. ``allow_private`` exists only
    for tests/tools that intentionally crawl internal hosts.
    """

    def __init__(self, *, resolver=None, allow_private: bool = False) -> None:
        self._resolver = resolver or _default_resolve
        self._allow_private = allow_private

    def check_shape(self, url: str) -> str:
        """URL-level policy only — scheme, userinfo, host presence. No DNS.

        Returns the URL's hostname. Runs wherever a request URL crosses
        the boundary (``ValidatedTransport.handle_async_request``);
        resolution+IP vetting is the connection layer's job.
        """
        try:
            parts = urlsplit(url)
            host = parts.hostname
            userinfo = parts.username is not None or parts.password is not None
        except ValueError as exc:
            raise SSRFError(f"unparseable url {url!r}: {exc}") from exc
        if parts.scheme not in ("http", "https"):
            raise SSRFError(f"scheme {parts.scheme!r} not allowed")
        if userinfo:
            raise SSRFError("userinfo (embedded credentials) not allowed")
        if not host:
            raise SSRFError("missing host")
        return host

    async def _resolve(self, host: str) -> list[str]:
        """All answers for ``host`` (IP literals skip DNS). Fail-closed."""
        host = host.rstrip(".")
        try:
            return [str(ipaddress.ip_address(host))]  # literal → no DNS needed
        except ValueError:
            pass
        try:
            ips = await asyncio.to_thread(self._resolver, host)
        except Exception as exc:  # noqa: BLE001 — DNS failure → refuse
            raise SSRFError(f"{host} failed to resolve: {exc}") from exc
        if not ips:
            raise SSRFError(f"{host} resolved to zero addresses") from None
        return list(ips)

    async def check(self, url: str) -> str:
        """Validate ``url`` as a fetch destination; returns it or raises SSRFError.

        Vetting is strict: *every* resolved answer must be public — a host
        serving a mixed public+private answer is refused outright. (The
        connect layer's ``resolve_validated`` is weaker but safe: it pins
        the dial to provably-public answers only.)
        """
        host = self.check_shape(url)
        if self._allow_private:
            return url
        for ip in await self._resolve(host):
            if not _is_public(ip):
                raise SSRFError(f"{host} resolves to non-public address {ip}")
        return url

    async def resolve_validated(self, host: str) -> list[str]:
        """Resolve ``host`` and return its publicly-routable answers.

        The connect layer MUST dial only the returned addresses —
        validation and connection share this one resolution, so a rebind
        between check and dial cannot retarget the socket (G1). The full
        validated list (deduped, resolver order) is returned so a refused
        or unreachable dial can fall back to the next answer (R1).
        Fail-closed: resolver error, empty answer, or no public answer
        all raise ``SSRFError``. ``allow_private`` returns every answer
        unvetted (the test/internal-crawl escape hatch still pins the
        dial).
        """
        ips = await self._resolve(host)
        if self._allow_private:
            return list(dict.fromkeys(ips))
        public = [ip for ip in dict.fromkeys(ips) if _is_public(ip)]
        if not public:
            raise SSRFError(f"{host} resolved no public address")
        return public


class ValidatingNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that dials only answers it validated.

    ``connect_tcp`` resolves ``host`` through the NetGuard resolver and
    delegates the dial to the wrapped backend with the validated IP
    literals — trying each public answer in turn within the caller's
    connect budget until one accepts (R1). httpcore still runs TLS with
    ``server_hostname`` taken from the request origin (the original
    hostname), so SNI and certificate verification are unchanged — only
    the socket's destination is pinned, closing the check→connect
    DNS-rebinding window entirely.
    """

    def __init__(self, netguard: NetGuard, backend: httpcore.AsyncNetworkBackend) -> None:
        self._netguard = netguard
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        candidates = (await self._netguard.resolve_validated(host))[:_MAX_DIAL_ATTEMPTS]
        # Bounded fallback: the caller's connect timeout is split evenly
        # across the validated answers so N answers cost ≈ one dial. A
        # refused/unreachable answer (e.g. IPv6-first on an IPv4-only
        # network) falls through to the next; the last error propagates
        # once every candidate is spent (R1).
        per_attempt = timeout / len(candidates) if timeout is not None else None
        last_exc: Exception | None = None
        for ip in candidates:
            try:
                return await self._backend.connect_tcp(
                    ip,
                    port,
                    timeout=per_attempt,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001 — next validated answer
                last_exc = exc
        assert last_exc is not None  # resolve_validated never yields []
        raise last_exc

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class ValidatedTransport(httpx.AsyncHTTPTransport):
    """HTTP transport enforcing egress policy at the connection layer.

    ``handle_async_request`` applies URL-level policy (scheme, userinfo,
    host shape) — no DNS. IP validation lives in the pool's
    ``ValidatingNetworkBackend``: it resolves the host and dials the
    validated answers itself, so check and connect share ONE resolution —
    a rebind between vetting and dialing cannot redirect the socket (G1).
    TLS is unaffected: httpcore handshakes with the origin
    ``server_hostname`` regardless of the dialed address.
    """

    def __init__(self, netguard: NetGuard, **kwargs) -> None:
        super().__init__(**kwargs)
        self._netguard = netguard
        # The pool consults _network_backend lazily at connection-create
        # time — swapping it post-init pins every dial this transport
        # makes (pool, proxy, and socks pools all honor the attribute).
        pool = self._pool
        pool._network_backend = ValidatingNetworkBackend(netguard, pool._network_backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._netguard.check_shape(str(request.url))
        return await super().handle_async_request(request)


def guarded_client(netguard: NetGuard, **kwargs) -> httpx.AsyncClient:
    """AsyncClient whose transport enforces ``netguard`` at dial time."""
    return httpx.AsyncClient(transport=ValidatedTransport(netguard), **kwargs)


@dataclass
class GuardedResponse:
    """Result of a redirect-validated streaming GET."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    final_url: str = ""
    redirects: list[str] = field(default_factory=list)
    oversize: bool = False
    # redirect_no_location | too_many_redirects | <policy_check refusal>
    error: str | None = None


async def guarded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    max_redirects: int,
    max_bytes: int,
    netguard: NetGuard,
    policy_check=None,
) -> GuardedResponse:
    """Stream ``url`` with per-hop SSRF validation and a strict byte cap.

    Redirects are followed manually (never the client's own follower) so
    every hop — not just the origin — is validated and each ``Location``
    is resolved against the hop URL. A redirect response without
    ``Location``, or a chain longer than ``max_redirects``, returns
    ``error`` set instead of silently passing a 3xx through as success.
    Bodies exceeding ``max_bytes`` stop at the chunk boundary with
    ``oversize=True`` — never silently truncated into a valid-looking
    document.

    ``policy_check`` is an optional ``async (url) -> str | None`` run
    *before* every hop's request — including the first — so robots and
    politeness apply to each redirect target, not just the origin. A
    non-``None`` return aborts the chain: the hop is never requested and
    ``error`` carries the refusal reason verbatim (the pipeline maps it
    to a crawl outcome).
    """
    redirects: list[str] = []
    current = url
    for _ in range(max_redirects + 1):
        await netguard.check(current)
        if policy_check is not None:
            refusal = await policy_check(current)
            if refusal is not None:
                return GuardedResponse(final_url=current, redirects=redirects, error=refusal)
        async with client.stream(
            "GET", current, headers=headers, timeout=timeout, follow_redirects=False
        ) as resp:
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    return GuardedResponse(
                        status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        final_url=str(resp.url),
                        redirects=redirects,
                        error="redirect_no_location",
                    )
                current = urljoin(str(resp.url), location)
                redirects.append(current)
                continue
            chunks: list[bytes] = []
            size = 0
            oversize = False
            async for chunk in resp.aiter_bytes():
                if size + len(chunk) > max_bytes:
                    # Keep the prefix up to the cap — RFC 9309 §2.5 wants
                    # robots.txt parsed to the limit — but flag it: an
                    # oversize body is never a durable document.
                    chunks.append(chunk[: max_bytes - size])
                    oversize = True
                    break
                chunks.append(chunk)
                size += len(chunk)
            return GuardedResponse(
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=b"".join(chunks),
                final_url=str(resp.url),
                redirects=redirects,
                oversize=oversize,
            )
    return GuardedResponse(final_url=current, redirects=redirects, error="too_many_redirects")
