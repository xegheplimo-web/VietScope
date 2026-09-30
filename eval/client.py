"""Search Router client — real HTTP or deterministic mock.

The runner talks to the Search Router through a thin ``SearchClient`` interface
so that evals can run without the full Docker stack (mock mode) or against a
live server (``http://localhost:8888``).

Three endpoints are supported:
  * ``legacy``  -> POST /search  (Source objects: url/title/domain/score/retrieved_at)
  * ``v1``      -> POST /v1/search (raw results: url/title/description/engine)
  * ``answer``  -> POST /v1/answer (answer text + sources + CitationV2 entries)
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .matching import normalize_expected

DEFAULT_SERVER_URL = "http://localhost:8888"

_MOCK_DOMAIN_POOL = [
    "vnexpress.net",
    "dantri.com.vn",
    "tuoitre.vn",
    "thanhnien.vn",
    "wikipedia.org",
    "bbc.com",
    "github.com",
    "stackoverflow.com",
    "arxiv.org",
    "cafef.vn",
    "znews.vn",
    "vietnamnet.vn",
    "developer.mozilla.org",
    "news.ycombinator.com",
    "youtube.com",
]


@dataclass
class Result:
    url: str = ""
    title: str = ""
    description: str = ""
    score: float = 0.0
    domain: str = ""
    retrieved_at: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "score": self.score,
            "domain": self.domain,
            "retrieved_at": self.retrieved_at,
            "provider": self.provider,
        }


@dataclass
class SearchResponse:
    query: str
    results: list[Result] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class AnswerResponse:
    """Normalized ``/v1/answer`` payload.

    ``sources`` are the ranked backing documents (``{url, domain, title,
    score}``); ``citations`` are raw CitationV2 entries; ``cited_urls`` is the
    deduped union of URLs appearing inside citation evidence — the set of
    sources the answer actually cites.
    """

    query: str
    answer: str = ""
    sources: list[Result] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    cited_urls: list[str] = field(default_factory=list)
    verified: bool | None = None
    coverage: float | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


class SearchClient:
    """Base interface. Subclasses implement :meth:`search`."""

    def search(
        self,
        query: str,
        top_k: int,
        search_type: str = "web",
        expected_urls: list[str] | None = None,
    ) -> SearchResponse:
        raise NotImplementedError

    def answer(
        self,
        query: str,
        mode: str = "balanced",
        language: str = "vi",
        expected_urls: list[str] | None = None,
        expected_facts: list[str] | None = None,
    ) -> AnswerResponse:
        raise NotImplementedError


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class HTTPClient(SearchClient):
    """Calls the Search Router REST API."""

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        endpoint: str = "legacy",
        timeout: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.endpoint = endpoint
        self.timeout = timeout

    def _path(self) -> str:
        return "/search" if self.endpoint == "legacy" else "/v1/search"

    def search(
        self,
        query: str,
        top_k: int,
        search_type: str = "web",
        expected_urls: list[str] | None = None,
    ) -> SearchResponse:
        url = f"{self.server_url}{self._path()}"
        payload = {
            "query": query,
            "max_results": top_k,
            "type": search_type,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                start = time.perf_counter()
                resp = client.post(url, json=payload)
                latency_ms = (time.perf_counter() - start) * 1000.0
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:  # noqa: BLE001 — any transport/HTTP error
            return SearchResponse(
                query=query,
                latency_ms=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
        results = self._extract_results(raw)
        return SearchResponse(
            query=query,
            results=results,
            latency_ms=round(latency_ms, 2),
            cost_usd=raw.get("cost_usd"),
            raw=raw,
        )

    def _extract_results(self, raw: dict[str, Any]) -> list[Result]:
        items = raw.get("results") or []
        out: list[Result] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            url = it.get("url") or ""
            if not url:
                continue
            domain = it.get("domain") or ""
            if not domain and url:
                try:
                    domain = httpx.URL(url).host or ""
                except Exception:  # noqa: BLE001
                    domain = ""
            out.append(
                Result(
                    url=url,
                    title=it.get("title") or "",
                    description=it.get("description") or it.get("snippet") or "",
                    score=float(it.get("score") or 0.0),
                    domain=domain,
                    # Keep the server-provided provenance verbatim: an empty
                    # value means the endpoint does not expose retrieved_at,
                    # which the freshness_ok metric should detect.
                    retrieved_at=it.get("retrieved_at") or "",
                    provider=it.get("search_provider")
                    or it.get("engine")
                    or it.get("source")
                    or "unknown",
                )
            )
        return out

    def answer(
        self,
        query: str,
        mode: str = "balanced",
        language: str = "vi",
        expected_urls: list[str] | None = None,
        expected_facts: list[str] | None = None,
    ) -> AnswerResponse:
        url = f"{self.server_url}/v1/answer"
        payload = {
            "query": query,
            "mode": mode,
            "language": language,
            "citations": True,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                start = time.perf_counter()
                resp = client.post(url, json=payload)
                latency_ms = (time.perf_counter() - start) * 1000.0
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:  # noqa: BLE001 — any transport/HTTP error
            return AnswerResponse(query=query, error=f"{type(exc).__name__}: {exc}")

        sources: list[Result] = []
        for it in raw.get("sources") or []:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            domain = it.get("domain") or ""
            if not domain:
                try:
                    domain = httpx.URL(it["url"]).host or ""
                except Exception:  # noqa: BLE001
                    domain = ""
            sources.append(
                Result(
                    url=it["url"],
                    title=it.get("title") or "",
                    score=float(it.get("score") or 0.0),
                    domain=domain,
                    provider="answer",
                )
            )
        citations = [c for c in (raw.get("citations") or []) if isinstance(c, dict)]
        cited_urls: list[str] = []
        seen: set[str] = set()
        for c in citations:
            for ev in c.get("evidence") or []:
                if not isinstance(ev, dict):
                    continue
                u = ev.get("url") or ""
                if u and u not in seen:
                    seen.add(u)
                    cited_urls.append(u)
        timings = raw.get("timings") or {}
        return AnswerResponse(
            query=query,
            answer=raw.get("answer") or "",
            sources=sources,
            citations=citations,
            cited_urls=cited_urls,
            verified=raw.get("verified"),
            coverage=raw.get("coverage"),
            latency_ms=round(latency_ms, 2),
            cost_usd=raw.get("cost_usd"),
            raw={**raw, "server_total_ms": float(timings.get("total") or 0.0) * 1000.0},
        )


class MockClient(SearchClient):
    """Deterministic mock of the Search Router for offline tests/CI.

    Generates a stable result list per query (seeded by the query hash) that
    sometimes surfaces the expected domains, so metrics exercise every code
    path (matches, misses, empty results).
    """

    def __init__(self, seed_offset: int = 0):
        self.seed_offset = seed_offset

    def search(
        self,
        query: str,
        top_k: int,
        search_type: str = "web",
        expected_urls: list[str] | None = None,
    ) -> SearchResponse:
        expected = [normalize_expected(e) for e in (expected_urls or [])]
        expected_domains = [e["value"] for e in expected if e["kind"] == "domain"]
        dig = hashlib.sha256(f"{query}:{self.seed_offset}".encode()).digest()
        surface = (int.from_bytes(dig[:4], "big") % 2) == 0
        seed = int.from_bytes(dig[4:8], "big") % 10**9

        pool = list(_MOCK_DOMAIN_POOL)
        # Deterministic rotation so different queries yield different orders.
        pool = pool[seed % len(pool) :] + pool[: seed % len(pool)]
        if surface and expected_domains:
            pool = list(dict.fromkeys(expected_domains + pool))

        results: list[Result] = []
        seen: set[str] = set()
        for i in range(top_k):
            domain = pool[i % len(pool)]
            if domain in seen:
                continue
            seen.add(domain)
            relevant = domain in set(expected_domains)
            results.append(
                Result(
                    url=f"https://{domain}/mock/item{i}",
                    title=f"{domain} — mock result {i} for {query}",
                    description="deterministic mock result",
                    score=round(1.0 - i * 0.05, 3) if relevant else round(0.05 * i, 3),
                    domain=domain,
                    retrieved_at=_now_iso(),
                    provider="mock",
                )
            )
        return SearchResponse(
            query=query,
            results=results,
            latency_ms=round(seed % 400 + 20, 2),
        )

    def answer(
        self,
        query: str,
        mode: str = "balanced",
        language: str = "vi",
        expected_urls: list[str] | None = None,
        expected_facts: list[str] | None = None,
    ) -> AnswerResponse:
        resp = self.search(query, top_k=10, expected_urls=expected_urls)
        facts = [f for f in (expected_facts or []) if f]
        dig = hashlib.sha256(f"ans:{query}:{self.seed_offset}".encode()).digest()
        surface_facts = dig[0] % 3 != 0  # ~2/3 of queries surface all facts

        parts = [f"Mock answer for: {query}"]
        for f in facts if surface_facts else facts[: len(facts) // 2]:
            parts.append(f)
        for i, r in enumerate(resp.results[:3]):
            parts.append(f"[{i + 1}] {r.title} ({r.url})")
        answer = "\n".join(parts)

        citations: list[dict[str, Any]] = []
        for i, r in enumerate(resp.results[:2]):
            citations.append(
                {
                    "claim_id": f"c{i + 1}",
                    "citation_text": f"mock claim {i + 1} [{i + 1}]",
                    "evidence": [
                        {
                            "url": r.url,
                            "quote": f"mock evidence quote {i + 1}",
                            "retrieved_at": _now_iso(),
                        }
                    ],
                }
            )
        if dig[1] % 4 == 0:  # ~1/4 of queries: one claim carries no evidence
            citations.append({"claim_id": "cx", "citation_text": "unsupported", "evidence": []})

        cited_urls = [r.url for r in resp.results[:2]]
        return AnswerResponse(
            query=query,
            answer=answer,
            sources=resp.results,
            citations=citations,
            cited_urls=cited_urls,
            verified=dig[2] % 2 == 0,
            coverage=round(0.5 + (dig[3] % 50) / 100.0, 3),
            latency_ms=resp.latency_ms,
        )


class FallbackClient(SearchClient):
    """Tries HTTP first, falls back to the mock client on any failure."""

    def __init__(self, http: HTTPClient, mock: MockClient | None = None):
        self.http = http
        self.mock = mock or MockClient()

    def search(
        self,
        query: str,
        top_k: int,
        search_type: str = "web",
        expected_urls: list[str] | None = None,
    ) -> SearchResponse:
        resp = self.http.search(query, top_k, search_type, expected_urls)
        if resp.ok:
            return resp
        mock_resp = self.mock.search(query, top_k, search_type, expected_urls)
        mock_resp.raw = {"mock_fallback": True, "http_error": resp.error}
        return mock_resp

    def answer(
        self,
        query: str,
        mode: str = "balanced",
        language: str = "vi",
        expected_urls: list[str] | None = None,
        expected_facts: list[str] | None = None,
    ) -> AnswerResponse:
        resp = self.http.answer(query, mode, language, expected_urls, expected_facts)
        if resp.ok:
            return resp
        mock_resp = self.mock.answer(query, mode, language, expected_urls, expected_facts)
        mock_resp.raw = {"mock_fallback": True, "http_error": resp.error}
        return mock_resp


def build_client(
    server_url: str = DEFAULT_SERVER_URL,
    endpoint: str = "legacy",
    timeout: float = 60.0,
    force_mock: bool = False,
    fallback: bool = True,
) -> SearchClient:
    """Build the client according to the run config.

    ``force_mock`` -> always mock. Otherwise HTTP, and (when ``fallback``) a
    FallbackClient that degrades to the mock on unreachable servers.
    """
    http = HTTPClient(server_url=server_url, endpoint=endpoint, timeout=timeout)
    if force_mock:
        return MockClient()
    if fallback:
        return FallbackClient(http)
    return http
