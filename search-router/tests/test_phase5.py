"""Tests for Phase 5: freshness worker, direct connectors."""

from connectors.direct_connectors import (
    ArxivConnector,
    DirectConnectorRouter,
    GitHubConnector,
    HuggingFaceConnector,
    NpmConnector,
    OpenRouterConnector,
    PyPiConnector,
    WikipediaConnector,
)
from workers.freshness_worker import FreshnessWorker, RecrawlTask

# ─── FreshnessWorker ────────────────────────────────────────────────────────


class TestFreshnessWorker:
    def setup_method(self):
        self.worker = FreshnessWorker()

    def test_compute_priority(self):
        priority = self.worker.compute_priority(
            url="https://example.com",
            query_popularity_7d=0.8,
            source_authority=0.9,
            observed_change_rate=0.3,
            freshness_requirement="high",
            rss_activity=0.5,
            time_since_last_crawl=86400,
        )
        assert 0.0 <= priority <= 1.0

    def test_add_and_get_tasks(self):
        task1 = RecrawlTask(url="https://a.com", priority=0.9, scheduled_at=0)
        task2 = RecrawlTask(url="https://b.com", priority=0.5, scheduled_at=0)
        self.worker.add_task(task1)
        self.worker.add_task(task2)

        batch = self.worker.get_next_batch(1)
        assert len(batch) == 1
        assert batch[0].url == "https://a.com"

    def test_update_change_rate(self):
        task = RecrawlTask(
            url="https://example.com",
            priority=0.5,
            scheduled_at=0,
            change_rate_estimate=0.0,
        )
        self.worker.add_task(task)
        self.worker.update_change_rate("https://example.com", 0.8)

        assert self.worker._queue[0].change_rate_estimate == 0.8


# ─── DirectConnectorRouter ──────────────────────────────────────────────────


class TestDirectConnectorRouter:
    def setup_method(self):
        self.router = DirectConnectorRouter()

    def test_router_init(self):
        assert "huggingface" in self.router._connectors
        assert "github" in self.router._connectors
        assert "arxiv" in self.router._connectors
        assert "npm" in self.router._connectors
        assert "pypi" in self.router._connectors
        assert "wikipedia" in self.router._connectors
        assert "openrouter" in self.router._connectors

    def test_unknown_connector(self):
        import asyncio

        result = asyncio.run(self.router.fetch("unknown", "test"))
        assert result is None


# ─── Individual Connectors ──────────────────────────────────────────────────


class TestHuggingFaceConnector:
    def test_init(self):
        connector = HuggingFaceConnector()
        assert connector is not None


class TestGitHubConnector:
    def test_init(self):
        connector = GitHubConnector()
        assert connector is not None


class TestArxivConnector:
    def test_init(self):
        connector = ArxivConnector()
        assert connector is not None


class TestNpmConnector:
    def test_init(self):
        connector = NpmConnector()
        assert connector is not None


class TestPyPiConnector:
    def test_init(self):
        connector = PyPiConnector()
        assert connector is not None


class TestWikipediaConnector:
    def test_init(self):
        connector = WikipediaConnector()
        assert connector is not None


class TestOpenRouterConnector:
    def test_init(self):
        connector = OpenRouterConnector()
        assert connector is not None
