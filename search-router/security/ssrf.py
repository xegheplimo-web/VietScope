"""SSRF guard — chặn fetch nội bộ qua /v1/read và /v1/research.

Chặn: localhost, private IP, link-local, loopback (IPv4+IPv6), DNS rebinding
(resolve hostname rồi check toàn bộ IP trả về), redirect tới host bị chặn.

Dùng chung cho mọi URL fetch vào hệ thống (Firecrawl scrape, httpx...).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Private / reserved ranges (SPEC review-codex: SSRF policy)
_BLOCKED_NETWORKS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmark
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local v6
    ipaddress.ip_network("ff00::/8"),  # multicast v6
]

_LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "::1",
    "0.0.0.0",  # noqa: S104 — hostname blocklist entry, not a bind address
    "127.0.0.1",
    "host.docker.internal",
    "gateway.docker.internal",
    "docker.for.win.localhost",
    "docker.for.mac.localhost",
    "docker.for.win.host.internal",
}


class SSRFError(Exception):
    """URL bị chặn bởi SSRF policy."""


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return True  # không parse được → chặn (fail-closed)
    return any(addr in net for net in _BLOCKED_NETWORKS)


def check_url(url: str, resolve: bool = True) -> None:
    """Validate URL. Raise SSRFError nếu host bị chặn.

    resolve=True → DNS resolve hostname và check toàn bộ IP (chống DNS rebinding).
    """
    if not url:
        raise SSRFError("Empty URL")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"Blocked scheme: {scheme or 'none'} (only http/https)")

    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        raise SSRFError("Missing host")

    # Hostname tường minh bị chặn
    if host in _LOCAL_HOSTNAMES:
        raise SSRFError(f"Blocked hostname: {host}")

    # Host là IP literal → check trực tiếp
    try:
        ipaddress.ip_address(host)
        if _is_blocked_ip(host):
            raise SSRFError(f"Blocked IP: {host}")
        return  # literal IP an toàn, không cần resolve
    except ValueError:
        pass  # là hostname thật, resolve bên dưới

    if not resolve:
        return  # không resolve → chỉ check hostname literal

    # DNS resolve — check mọi IP (chống rebinding)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {host}: {exc}") from exc

    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise SSRFError(f"Blocked resolved IP {ip_str} for {host}")

    if not infos:
        raise SSRFError(f"No addresses for {host}")


def check_redirect_url(current_url: str, target_url: str) -> None:
    """Check URL redirect target — dùng khi provider báo redirect."""
    check_url(target_url)
    # Cho phép redirect khác host nhưng vẫn phải pass check_url riêng
