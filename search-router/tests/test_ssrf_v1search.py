"""Tests cho SSRF guard + /v1/search (blocker review-codex)."""

import pytest
from security.ssrf import SSRFError, check_url


class TestSSRFGuard:
    def test_block_localhost(self):
        with pytest.raises(SSRFError):
            check_url("http://localhost:3002/", resolve=False)

    def test_block_127_loopback(self):
        with pytest.raises(SSRFError):
            check_url("http://127.0.0.1:8888/health", resolve=False)

    def test_block_private_10(self):
        with pytest.raises(SSRFError):
            check_url("http://10.0.0.5/", resolve=False)

    def test_block_private_192_168(self):
        with pytest.raises(SSRFError):
            check_url("http://192.168.1.1/", resolve=False)

    def test_block_link_local(self):
        with pytest.raises(SSRFError):
            check_url("http://169.254.169.254/latest/meta-data/", resolve=False)

    def test_block_ipv6_loopback(self):
        with pytest.raises(SSRFError):
            check_url("http://[::1]:8080/", resolve=False)

    def test_block_metadata_hostname(self):
        with pytest.raises(SSRFError):
            check_url("http://host.docker.internal/", resolve=False)

    def test_block_non_http_scheme(self):
        with pytest.raises(SSRFError):
            check_url("file:///etc/passwd", resolve=False)

    def test_allow_public_url(self):
        # Public domain — không raise (không resolve vì offline test)
        check_url("https://example.com/docs", resolve=False)

    def test_allow_public_ip(self):
        check_url("http://8.8.8.8/", resolve=False)

    def test_empty_url(self):
        with pytest.raises(SSRFError):
            check_url("", resolve=False)


class TestV1SearchEndpoint:
    def test_search_web(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post("/v1/search", json={"query": "docker compose", "type": "web", "max_results": 2})
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert "understanding" in data
        assert data["understanding"].get("intent") in (
            "current_fact",
            "definition",
            "howto",
            "comparison",
            "opinion",
        )

    def test_search_bad_type(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post("/v1/search", json={"query": "x", "type": "badtype"})
        assert r.status_code == 422


class TestV1ReadSSRF:
    def test_read_blocked_localhost(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post(
            "/v1/read",
            json={"url": "http://localhost:8080/", "chunk_size": 200, "chunk_overlap": 50},
        )
        assert r.status_code == 422
        assert "SSRF" in r.json()["detail"]

    def test_read_blocked_private(self):
        import main as app_module
        from fastapi.testclient import TestClient

        c = TestClient(app_module.app)
        r = c.post(
            "/v1/read", json={"url": "http://10.0.0.1/", "chunk_size": 200, "chunk_overlap": 50}
        )
        assert r.status_code == 422


class TestShingleClustering:
    """Near-duplicate clustering (review-codex #5): syndicated copies = 1 cluster."""

    def _mk_source(self, sid: str, title: str, content: str):
        from models import Source

        return Source(
            source_id=sid,
            url=f"https://example.com/{sid}",
            title=title,
            content=content,
            content_provider="test",
        )

    def test_exact_duplicate_single_cluster(self):
        from evidence.pack import build_clusters

        text = (
            "Bitcoin rose 5 percent today as regulators signaled a softer stance on digital assets."
        )
        a = self._mk_source("a", "BTC Up", text)
        b = self._mk_source("b", "Bitcoin Gains", text)  # same content, diff title
        clusters = build_clusters([a, b])
        assert len(clusters) == 1

    def test_syndicated_copy_merged(self):
        from evidence.pack import build_clusters

        base = (
            "Bitcoin rose 5 percent today as regulators signaled a softer stance on "
            "digital assets. The rally pushed the token past $60,000 for the first time "
            "in three weeks, with trading volumes surging across major exchanges."
        )
        copy = (
            "Bitcoin climbed 5% today after regulators hinted at a friendlier approach "
            "to digital assets. The rally drove the token above $60,000 for the first "
            "time in three weeks, and volumes surged across major exchanges."
        )
        a = self._mk_source("a", "Original", base)
        b = self._mk_source("b", "Copy", copy)
        clusters = build_clusters([a, b])
        assert len(clusters) == 1, f"expected merge, got {len(clusters)} clusters"

    def test_distinct_articles_separate(self):
        from evidence.pack import build_clusters

        t1 = (
            "Bitcoin rose 5 percent today as regulators signaled a softer stance on "
            "digital assets and institutional interest continued to grow."
        )
        t2 = (
            "Ethereum developers announced a major upgrade schedule for the next "
            "quarter, focusing on scalability improvements and lower fees."
        )
        a = self._mk_source("a", "BTC", t1)
        b = self._mk_source("b", "ETH", t2)
        clusters = build_clusters([a, b])
        assert len(clusters) == 2
