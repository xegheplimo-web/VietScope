"""L5 URL Normalize & Filter — canonicalization, SSRF guard, spam filter.

All URLs from search results pass through this module before fetching.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Tracking parameters to strip
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "source",
    "_ga",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
    "feature",
    "ref_src",
    "ref_url",
}

# Private IP ranges (SSRF guard)
PRIVATE_IP_PATTERNS = [
    re.compile(r"^127\."),
    re.compile(r"^10\."),
    re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^169\.254\."),
    re.compile(r"^0\."),
    re.compile(r"^::1$"),
    re.compile(r"^fc00:"),
    re.compile(r"^fe80:"),
]

# Spam/SEO-farm indicators
SPAM_INDICATORS = [
    "seo-farm",
    "content-mill",
    "ai-generated-content",
    "clickbait",
    "viral-news",
    "trending-now",
]


@dataclass
class NormalizedURL:
    """Canonical URL with metadata."""

    original: str
    canonical: str
    domain: str
    path: str
    query: str
    is_private: bool = False
    is_spam: bool = False
    spam_score: float = 0.0
    content_hash: str = ""


class URLNormalizer:
    """URL canonicalization and filtering."""

    def __init__(self):
        self._seen_hashes: set[str] = set()

    def normalize(self, url: str) -> NormalizedURL | None:
        """Normalize URL. Returns None if URL is invalid or blocked."""
        if not url or not isinstance(url, str):
            return None

        # Parse URL
        try:
            parsed = urlparse(url.strip())
        except Exception:
            return None

        # Must have scheme and netloc
        if not parsed.scheme or not parsed.netloc:
            return None

        # Only allow http/https
        if parsed.scheme not in ("http", "https"):
            return None

        # Lowercase scheme and host
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # Remove default port (only if it's actually the default port)
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]

        # Remove www. prefix
        netloc = netloc.removeprefix("www.")

        # Normalize path
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")

        # Remove fragment (except for docs anchors)
        fragment = ""
        if parsed.fragment and self._is_doc_anchor(parsed.fragment):
            fragment = parsed.fragment

        # Strip tracking parameters
        query_params = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_params = [
            (k, v)
            for k, v in query_params
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]
        query = urlencode(sorted(filtered_params), doseq=True) if filtered_params else ""

        # Rebuild canonical URL
        canonical = urlunparse((scheme, netloc, path, "", query, fragment))

        # SSRF guard
        is_private = self._is_private_ip(netloc)

        # Spam detection
        is_spam, spam_score = self._detect_spam(netloc, path)

        # Content hash for dedupe
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]

        return NormalizedURL(
            original=url,
            canonical=canonical,
            domain=netloc,
            path=path,
            query=query,
            is_private=is_private,
            is_spam=is_spam,
            spam_score=spam_score,
            content_hash=content_hash,
        )

    def _is_private_ip(self, netloc: str) -> bool:
        """Check if netloc is a private IP address."""
        # Extract host from netloc (may include port)
        host = netloc.split(":")[0]

        # Check against private IP patterns
        for pattern in PRIVATE_IP_PATTERNS:
            if pattern.match(host):
                return True

        # Check for localhost
        return host in ("localhost", "localhost.localdomain")

    def _detect_spam(self, domain: str, path: str) -> tuple[bool, float]:
        """Detect spam/SEO-farm indicators."""
        score = 0.0

        # Check domain against known spam patterns
        for indicator in SPAM_INDICATORS:
            if indicator in domain:
                score += 0.5

        # Check URL depth (deeper = more suspicious)
        path_depth = path.count("/")
        if path_depth > 5:
            score += 0.1 * (path_depth - 5)

        # Check for excessive query parameters
        if path.count("?") > 0:
            score += 0.1

        # Check for suspicious TLDs
        suspicious_tlds = [".xyz", ".top", ".click", ".link", ".info"]
        if any(domain.endswith(tld) for tld in suspicious_tlds):
            score += 0.2

        return score >= 0.5, min(score, 1.0)

    def _is_doc_anchor(self, fragment: str) -> bool:
        """Check if fragment is a documentation anchor."""
        # Keep anchors that look like section IDs
        return bool(re.match(r"^[a-zA-Z0-9_-]+$", fragment))

    def dedupe(self, urls: list[NormalizedURL]) -> list[NormalizedURL]:
        """Deduplicate URLs by content hash."""
        seen: set[str] = set()
        result: list[NormalizedURL] = []
        for url in urls:
            if url.content_hash not in seen:
                seen.add(url.content_hash)
                result.append(url)
        return result

    def filter_blocked(self, urls: list[NormalizedURL]) -> list[NormalizedURL]:
        """Filter out private IPs and spam URLs."""
        return [url for url in urls if not url.is_private and not url.is_spam]
