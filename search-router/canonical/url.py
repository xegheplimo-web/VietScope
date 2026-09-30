"""URL canonicalization and alias clustering."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMETERS = {
    "_ga",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def canonical_url(url: str) -> str:
    """Return a deterministic canonical form of a web URL.

    The host is lower-cased, common mobile/``www`` prefixes and tracking
    parameters are removed, semantic query parameters are sorted, and the
    fragment and trailing slash are discarded. The input's scheme is
    preserved — ``http`` stays ``http``: a canonical URL must remain
    fetchable, and upgrading the scheme without redirect evidence can
    produce dead links. Scheme-less inputs default to ``https``. Invalid
    or empty inputs return an empty string.
    """

    if not isinstance(url, str) or not url.strip():
        return ""

    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""

    if not hostname or any(char.isspace() for char in hostname):
        return ""

    hostname = hostname.casefold().rstrip(".")
    while hostname.startswith(("www.", "m.")):
        hostname = hostname.split(".", 1)[1]

    if not hostname:
        return ""

    host = f"[{hostname}]" if ":" in hostname else hostname
    if port not in (None, 80, 443):
        host = f"{host}:{port}"

    path = parsed.path.rstrip("/")
    if path and not path.startswith("/"):
        path = f"/{path}"

    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMETERS
    ]
    query_items.sort(key=lambda item: (item[0].casefold(), item[0], item[1]))
    query = urlencode(query_items, doseq=True)

    scheme = parsed.scheme.lower() or "https"
    return urlunsplit((scheme, host, path, query, ""))


def canonical_identity(url: str) -> str:
    """Return the scheme-less identity key — ``host/path?query``.

    ``http`` and ``https`` spellings of the same resource collapse to one
    identity; use ``canonical_url`` when the fetchable form matters
    (display links, crawl targets, ``doc_id`` derivation).
    """

    canonical = canonical_url(url)
    if not canonical:
        return ""
    return canonical.split("://", 1)[-1]


def cluster_sources(urls: list[str]) -> dict[str, list[str]]:
    """Group URL aliases by canonical identity, preserving input order.

    Aliases differing only in scheme (``http`` vs ``https``) merge into
    one cluster; the cluster key is the first alias's canonical URL — a
    fetchable URL, not a bare identity. Empty or invalid URLs are
    ignored, as are repeated copies of the exact same alias.
    """

    keys: dict[str, str] = {}  # identity -> canonical key of first-seen alias
    clusters: dict[str, list[str]] = {}
    for url in urls:
        identity = canonical_identity(url)
        if not identity:
            continue
        key = keys.setdefault(identity, canonical_url(url))
        aliases = clusters.setdefault(key, [])
        if url not in aliases:
            aliases.append(url)
    return clusters
