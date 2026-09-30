"""URL matching between expected ground-truth entries and retrieved results.

Ground truth ``expected_urls`` may be either:
  * a bare domain  -> matches any result whose registered domain equals it
  * a full URL     -> matches any result whose normalized URL equals it

Matching is case-insensitive and strips scheme/www/trailing-slash noise.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


def normalize_domain(domain: str) -> str:
    """Lowercase a registered domain, stripping 'www.' and a trailing dot."""
    d = (domain or "").strip().lower()
    while d.startswith("www."):
        d = d[4:]
    d = d.rstrip(".")
    return d


def domain_from_url(url: str) -> str:
    """Registered domain of a result URL ('' when unparseable)."""
    try:
        return normalize_domain(urlparse((url or "").strip()).hostname or "")
    except ValueError:
        return ""


def normalize_url(url: str) -> str:
    """Normalize a URL for exact comparison.

    Lowercases scheme/host/path, treats http/https as the same scheme, strips
    www., default ports, duplicate slashes and trailing slashes.
    """
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    orig_scheme = p.scheme.lower()
    scheme = "https" if orig_scheme in ("http", "https") else orig_scheme
    host = normalize_domain(p.hostname or "")
    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path).lower()
    if path != "/":
        path = path.rstrip("/")
    port = ""
    if p.port and not (
        (orig_scheme == "http" and p.port == 80) or (orig_scheme == "https" and p.port == 443)
    ):
        port = f":{p.port}"
    out = f"{scheme}://{host}{port}{path}".rstrip("/")
    if p.query:
        out = f"{out}?{p.query.lower()}"
    return out or "/"


def normalize_expected(expected: str) -> dict:
    """Normalize one expected entry -> {'kind': 'domain'|'url', 'value': str}."""
    e = (expected or "").strip()
    if not e:
        return {"kind": "domain", "value": ""}
    # Contains a path, query or fragment -> treat as full URL
    if "://" in e or "/" in e or "?" in e or "#" in e:
        return {"kind": "url", "value": normalize_url(e)}
    return {"kind": "domain", "value": normalize_domain(e)}


def result_matches_expected(result_url: str, expected: str) -> bool:
    """True if one result URL matches one expected ground-truth entry."""
    if not result_url:
        return False
    norm = normalize_expected(expected)
    if norm["kind"] == "domain":
        return normalize_domain(urlparse(result_url).hostname or "") == norm["value"]
    return normalize_url(result_url) == norm["value"]


def result_matches_any(result_url: str, expected_urls: list[str]) -> bool:
    """True if a result matches at least one expected entry."""
    return any(result_matches_expected(result_url, e) for e in expected_urls or [])


def matched_expected_indexes(result_urls: list[str], expected_urls: list[str]) -> list[int]:
    """Indexes of expected entries (in expected order) that are covered by results.

    Each expected entry is counted at most once even if several results hit it.
    """
    expected_urls = expected_urls or []
    matched = [False] * len(expected_urls)
    for url in result_urls or []:
        for i, exp in enumerate(expected_urls):
            if not matched[i] and result_matches_expected(url, exp):
                matched[i] = True
    return [i for i, m in enumerate(matched) if m]


def relevance_vector(result_urls: list[str], expected_urls: list[str], top_k: int) -> list[int]:
    """Binary relevance per ranked result (position <= top_k).

    A position is relevant if its URL matches at least one expected entry.
    Repeated matches on the same expected entry still count as relevant at each
    position (standard for graded/PRF metrics over ranked lists).
    """
    expected_urls = expected_urls or []
    return [1 if result_matches_any(u, expected_urls) else 0 for u in (result_urls or [])[:top_k]]
