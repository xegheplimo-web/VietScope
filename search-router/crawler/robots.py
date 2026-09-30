"""Robots.txt fetch + parse + cache (RFC 9309).

``RobotsCache`` fetches ``{scheme}://{host}/robots.txt`` once per origin
through ``netguard.guarded_get`` (per-hop SSRF validation, ≤5 redirects —
the RFC's own bound), parses it into groups of ``user-agent``/``allow``/
``disallow`` records with a stdlib-only parser, and caches the result for
``ttl_seconds`` in a small in-memory LRU keyed by ``scheme://host``.

Parsing + matching implement RFC 9309 directly (stdlib ``robotparser``
silently decodes ``%2A`` into a wildcard and takes the first matching
group, both wrong):

- §2.2.1 — every group whose user-agent line matches the product token
  contributes rules (merged in file order); ``*`` groups apply only when
  no specific group matches.
- §2.2.2 — longest-octet match wins; an equivalent-length ``allow``
  beats a ``disallow``. Paths are compared octet-wise after normalizing
  percent-encoding: unreserved octets (``%62`` → ``b``) decode, reserved
  ones stay encoded — a rule ``/file%2A`` matches the literal path
  ``/file*`` and is *not* a wildcard.
- §2.3.1.5 — parse is line-tolerant: unparseable lines are skipped, all
  parseable rules still apply.

Access results (§2.3.1) map to a tri-state verdict:

- 2xx   → parsed rules apply (default-allow where the file is silent)
- 4xx   → ``allowed`` — the file is "unavailable", access unrestricted
- 429   → ``unavailable`` — rate-limited; MUST NOT crawl, retry later
- 5xx / network / DNS / TLS / redirect failure → ``unavailable``
  (§2.3.1.4 "unreachable": assume complete disallow — never allow-all)

``unavailable`` is distinct from ``disallowed`` (explicit rule): the
pipeline retries ``unavailable`` on a short timer instead of parking the
URL as done. Entries expire quickly on the short error TTL; the cache
never raises — a broken robots pipeline degrades to ``allowed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from crawler.netguard import NetGuard, guarded_client, guarded_get

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "SearchHubBot/1.0 (+https://search.duyai.app/bot)"

_FETCH_TIMEOUT_S = 10.0
_MAX_BYTES = 512 * 1024  # RFC 9309 §2.5: parse limit MUST be ≥ 500 KiB
_MAX_REDIRECTS = 5  # RFC 9309 §2.3.1.2: follow at least five
_DEFAULT_TTL_S = 30 * 60.0
_ERROR_TTL_S = 5 * 60.0  # unavailable entries are retried sooner
_MAX_ENTRIES = 500
_MAX_LOCKS = 1024  # per-origin lock map bound (best-effort cleanup)

VERDICT_ALLOWED = "allowed"
VERDICT_DISALLOWED = "disallowed"
VERDICT_UNAVAILABLE = "unavailable"

_STATE_ALLOW_ALL = "allow_all"
_STATE_UNAVAILABLE = "unavailable"
_STATE_PARSED = "parsed"

_HEX = frozenset("0123456789abcdefABCDEF")
# RFC 3986 unreserved characters — the only %XX octets that decode.
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _norm_octets(text: str) -> str:
    """Normalize a path for RFC 9309 §2.2.2 octet-wise comparison.

    Percent-encoded unreserved octets decode to their character; every
    other non-unreserved character (reserved ASCII like ``*``/``$``/``?``,
    bare ``%``, non-ASCII text) becomes uppercase ``%XX`` UTF-8 octets.
    Applied identically to rule patterns and URL paths, an encoded
    reserved char in the file (``%2A``) matches the literal char in the
    URL (``*``) without ever becoming a wildcard.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n and text[i + 1] in _HEX and text[i + 2] in _HEX:
            octet = int(text[i + 1 : i + 3], 16)
            char = chr(octet)
            out.append(char if char in _UNRESERVED else f"%{octet:02X}")
            i += 3
        elif ch in _UNRESERVED:
            out.append(ch)
            i += 1
        else:
            for byte in ch.encode("utf-8", errors="replace"):
                out.append(f"%{byte:02X}")
            i += 1
    return "".join(out)


@dataclass
class _Rule:
    """One allow/disallow path pattern, pre-compiled for octet matching."""

    allow: bool
    regex: re.Pattern[str]
    octets: int  # octet length of the literal pattern segments


def _compile_rule(allow: bool, raw_path: str) -> _Rule | None:
    """Build a matchable rule; ``*`` → any run, trailing ``$`` → end anchor.

    Wildcard/anchor detection happens on the *raw* pattern so encoded
    ``%2A``/``%24`` stay literal; segments are then octet-normalized.
    """
    anchored = raw_path.endswith("$")
    if anchored:
        raw_path = raw_path[:-1]
    if not raw_path:
        return None  # empty pattern — no restriction, skipped
    segments = [_norm_octets(part) for part in raw_path.split("*")]
    octets = sum(len(seg) for seg in segments)
    pattern = ".*".join(re.escape(seg) for seg in segments)
    body = pattern + "$" if anchored else pattern
    return _Rule(allow=allow, regex=re.compile(body), octets=octets)


@dataclass
class _Group:
    """One user-agent group: UA lines + rules + optional crawl-delay."""

    uas: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


_ROBOTS_TXT_NORM = _norm_octets("/robots.txt")


class ParsedRobots:
    """Parsed robots.txt: grouped rules, crawl-delays, sitemaps."""

    def __init__(self, groups: list[_Group], sitemaps: list[str]) -> None:
        self.groups = groups
        self.sitemaps = sitemaps

    def _matching_groups(self, token: str) -> tuple[list[_Group], list[_Group]]:
        """Split into (specific-match groups, wildcard ``*`` groups)."""
        token = token.lower()
        specific: list[_Group] = []
        star: list[_Group] = []
        for group in self.groups:
            # ``ua`` must be non-empty — ``"" in token`` is True for every
            # token, which would let a UA-less group swallow all rules.
            if any(ua and ua != "*" and ua.lower() in token for ua in group.uas):
                specific.append(group)
            elif any(ua == "*" for ua in group.uas):
                star.append(group)
        return specific, star

    def _effective_groups(self, token: str) -> list[_Group]:
        """§2.2.1: merge matching groups; ``*`` only when nothing matches."""
        specific, star = self._matching_groups(token)
        return specific if specific else star

    def _rules_for(self, token: str) -> list[_Rule]:
        rules: list[_Rule] = []
        for group in self._effective_groups(token):
            rules.extend(group.rules)
        return rules

    def crawl_delay(self, token: str) -> float | None:
        for group in self._effective_groups(token):
            if group.crawl_delay is not None:
                return group.crawl_delay
        return None

    def allows(self, token: str, url: str) -> bool:
        """§2.2.2 verdict: longest-octet match; equivalent → allow wins."""
        parsed = urlsplit(url)
        raw = urlunsplit(("", "", parsed.path, parsed.query, ""))
        path = _norm_octets(raw or "/")
        if path == _ROBOTS_TXT_NORM:
            return True  # implicitly allowed per RFC 9309
        best = -1
        allowed = True
        for rule in self._rules_for(token):
            if rule.regex.match(path) is None:
                continue
            if rule.octets > best:
                best, allowed = rule.octets, rule.allow
            elif rule.octets == best:
                allowed = allowed or rule.allow
        return allowed


def parse_robots_text(text: str) -> ParsedRobots:
    """Line-tolerant RFC 9309 parser — every parseable rule is kept.

    A group is one or more ``user-agent`` lines followed by rules; a
    ``user-agent`` line after rules starts a new group. ``Sitemap`` (and
    other non-rule records) never terminates a group (§2.2.4). Rules
    before the first ``user-agent`` line are ignored (§2.2.2).
    """
    groups: list[_Group] = []
    sitemaps: list[str] = []
    group = _Group()
    saw_rules = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if saw_rules:  # a UA line after rules opens a fresh group
                groups.append(group)
                group = _Group()
                saw_rules = False
            # Empty/whitespace UA tokens match nothing — record nothing.
            # A group left with no valid token is dropped at finalize.
            if value:
                group.uas.append(value)
        elif key in ("allow", "disallow"):
            if not group.uas:
                continue
            saw_rules = True
            rule = _compile_rule(key == "allow", value)
            if rule is not None:
                group.rules.append(rule)
        elif key == "crawl-delay":
            # RFC 9309 treats Crawl-delay as an extension of the current
            # group — it does NOT close the group, so a following
            # User-agent line still joins it (only rules set saw_rules).
            if not group.uas:
                continue
            with contextlib.suppress(ValueError):
                group.crawl_delay = float(value)
        elif key == "sitemap":
            sitemaps.append(value)
    if group.uas:
        groups.append(group)
    return ParsedRobots(groups, sitemaps)


@dataclass
class _Entry:
    state: str
    parsed: ParsedRobots | None = None
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    expires_at: float = 0.0


@dataclass
class _OriginLease:
    """Per-origin fetch lock with a reference count.

    ``refs`` counts holders + queued waiters; the bound eviction may only
    drop entries with ``refs == 0`` — ``Lock.locked()`` alone is False
    while a waiter is scheduled-but-not-yet-holding, which would mint a
    second lock for the same origin and double-fetch robots.txt.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refs: int = 0


class RobotsCache:
    """Async robots.txt cache: allowed/disallowed/unavailable per origin."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        ttl_seconds: float = _DEFAULT_TTL_S,
        max_entries: int = _MAX_ENTRIES,
        client: httpx.AsyncClient | None = None,
        netguard: NetGuard | None = None,
    ) -> None:
        self._ua = user_agent
        # robotparser matches on the product token before the first "/".
        self._ua_token = user_agent.split("/", 1)[0].split(" ", 1)[0] or "*"
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._client = client
        self._netguard = netguard or NetGuard()
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._locks: dict[str, _OriginLease] = {}
        self._locks_guard = asyncio.Lock()

    # ── Public API ───────────────────────────────────────────────────────

    async def check(self, url: str) -> str:
        """RFC 9309 access verdict for ``url``: allowed|disallowed|unavailable."""
        try:
            entry = await self._entry_for(url)
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.debug("robots entry lookup failed for %s: %r", url, exc)
            return VERDICT_ALLOWED
        if entry.state == _STATE_UNAVAILABLE:
            return VERDICT_UNAVAILABLE
        if entry.state == _STATE_ALLOW_ALL or entry.parsed is None:
            return VERDICT_ALLOWED
        try:
            ok = await asyncio.to_thread(entry.parsed.allows, self._ua_token, url)
        except Exception as exc:  # noqa: BLE001 — degrade to allow
            logger.debug("robots eval failed for %s: %r", url, exc)
            return VERDICT_ALLOWED
        return VERDICT_ALLOWED if ok else VERDICT_DISALLOWED

    async def allowed(self, url: str) -> bool:
        """Whether the bot may fetch ``url`` now (unavailable → False)."""
        return await self.check(url) == VERDICT_ALLOWED

    async def crawl_delay(self, domain_or_url: str) -> float | None:
        """Declared Crawl-delay in seconds, or None."""
        entry = await self._entry_for(domain_or_url)
        return entry.crawl_delay

    async def sitemaps(self, domain_or_url: str) -> list[str]:
        """Sitemap URLs declared in robots.txt, or []."""
        entry = await self._entry_for(domain_or_url)
        return list(entry.sitemaps)

    # ── Cache plumbing ───────────────────────────────────────────────────

    @staticmethod
    def _origin(value: str) -> str:
        """Normalize a URL or bare domain to ``scheme://host``."""
        if "://" not in value:
            value = f"https://{value}"
        parts = urlsplit(value)
        return f"{parts.scheme or 'https'}://{parts.netloc}"

    async def _entry_for(self, url_or_domain: str) -> _Entry:
        origin = self._origin(url_or_domain)
        now = time.monotonic()
        entry = self._cache.get(origin)
        if entry is not None and entry.expires_at > now:
            self._cache.move_to_end(origin)
            return entry

        # One fetch per origin even under concurrent workers; the lock map
        # itself is bounded — entries with zero holders AND zero queued
        # waiters are evicted past _MAX_LOCKS.
        async with self._locks_guard:
            lease = self._locks.setdefault(origin, _OriginLease())
            lease.refs += 1
            if len(self._locks) > _MAX_LOCKS:
                self._locks = {k: v for k, v in self._locks.items() if v.refs > 0 or k == origin}
        try:
            async with lease.lock:
                entry = self._cache.get(origin)
                if entry is not None and entry.expires_at > time.monotonic():
                    self._cache.move_to_end(origin)
                    return entry
                entry = await self._fetch(origin)
                self._cache[origin] = entry
                self._cache.move_to_end(origin)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
                return entry
        finally:
            lease.refs -= 1

    async def _fetch(self, origin: str) -> _Entry:
        robots_url = f"{origin}/robots.txt"
        try:
            resp = await self._get(robots_url)
        except Exception as exc:  # noqa: BLE001 — unreachable per §2.3.1.4
            logger.debug("robots fetch failed for %s: %r", origin, exc)
            return _Entry(state=_STATE_UNAVAILABLE, expires_at=time.monotonic() + _ERROR_TTL_S)
        if resp.error:
            # redirect_no_location / too_many_redirects → unreachable.
            return _Entry(state=_STATE_UNAVAILABLE, expires_at=time.monotonic() + _ERROR_TTL_S)
        if 200 <= resp.status < 300:
            # §2.3.1.1: any 2xx is a successful retrieval — an empty body
            # (e.g. 204) parses to zero groups → unrestricted access.
            return await self._parse(robots_url, resp.body)
        if 400 <= resp.status < 500 and resp.status != 429:
            # §2.3.1.3: unavailable to the crawler → unrestricted access.
            return _Entry(state=_STATE_ALLOW_ALL, expires_at=time.monotonic() + self._ttl)
        # 429, 5xx, odd 3xx → §2.3.1.4 unreachable → disallow, retry soon.
        return _Entry(state=_STATE_UNAVAILABLE, expires_at=time.monotonic() + _ERROR_TTL_S)

    async def _get(self, url: str):
        headers = {"User-Agent": self._ua, "Accept": "text/plain,*/*;q=0.1"}
        if self._client is not None:
            return await guarded_get(
                self._client,
                url,
                headers=headers,
                timeout=_FETCH_TIMEOUT_S,
                max_redirects=_MAX_REDIRECTS,
                max_bytes=_MAX_BYTES,
                netguard=self._netguard,
            )
        async with guarded_client(self._netguard, timeout=_FETCH_TIMEOUT_S) as client:
            return await guarded_get(
                client,
                url,
                headers=headers,
                timeout=_FETCH_TIMEOUT_S,
                max_redirects=_MAX_REDIRECTS,
                max_bytes=_MAX_BYTES,
                netguard=self._netguard,
            )

    async def _parse(self, robots_url: str, body: bytes) -> _Entry:
        # utf-8-sig strips a leading BOM — a BOM'd User-agent line would
        # otherwise drop and every rule silently becomes allow-all
        # (observed live on tuoitre.vn).
        text = body.decode("utf-8-sig", errors="replace")
        try:
            parsed = await asyncio.to_thread(parse_robots_text, text)
        except Exception as exc:  # noqa: BLE001 — unreadable file → allow
            logger.debug("robots parse failed for %s: %r", robots_url, exc)
            return _Entry(state=_STATE_ALLOW_ALL, expires_at=time.monotonic() + self._ttl)
        return _Entry(
            state=_STATE_PARSED,
            parsed=parsed,
            crawl_delay=parsed.crawl_delay(self._ua_token),
            sitemaps=list(parsed.sitemaps),
            expires_at=time.monotonic() + self._ttl,
        )
