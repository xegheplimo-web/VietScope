"""L4c Direct Connectors — fetch structured data from known APIs.

Connectors for: HuggingFace, GitHub, arXiv, npm, PyPI, Wikipedia, OpenRouter.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DirectConnectorRouter:
    """Routes entity lookups to the appropriate direct connector."""

    def __init__(self):
        self._connectors: dict[str, Any] = {
            "huggingface": HuggingFaceConnector(),
            "github": GitHubConnector(),
            "arxiv": ArxivConnector(),
            "npm": NpmConnector(),
            "pypi": PyPiConnector(),
            "wikipedia": WikipediaConnector(),
            "openrouter": OpenRouterConnector(),
        }

    async def fetch(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        """Fetch structured data for an entity."""
        connector = self._connectors.get(entity_type)
        if not connector:
            return None
        return await connector.fetch(entity_id)


class HuggingFaceConnector:
    """HuggingFace API connector."""

    async def fetch(self, model_id: str) -> dict[str, Any] | None:
        """Fetch model info from HuggingFace API."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://huggingface.co/api/models/{model_id}",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "name": data.get("id", model_id),
                        "type": "model",
                        "url": f"https://huggingface.co/{model_id}",
                        "metadata": {
                            "downloads": data.get("downloads", 0),
                            "likes": data.get("likes", 0),
                            "tags": data.get("tags", []),
                        },
                    }
        except Exception as exc:
            logger.warning("HF connector failed for %s: %s", model_id, exc)
        return None


class GitHubConnector:
    """GitHub REST API connector."""

    async def fetch(self, repo: str) -> dict[str, Any] | None:
        """Fetch repo info from GitHub API."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}",
                    headers={"Accept": "application/vnd.github.v3+json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "name": data.get("full_name", repo),
                        "type": "repository",
                        "url": data.get("html_url", f"https://github.com/{repo}"),
                        "metadata": {
                            "stars": data.get("stargazers_count", 0),
                            "forks": data.get("forks_count", 0),
                            "language": data.get("language", ""),
                        },
                    }
        except Exception as exc:
            logger.warning("GitHub connector failed for %s: %s", repo, exc)
        return None


class ArxivConnector:
    """arXiv API connector."""

    async def fetch(self, paper_id: str) -> dict[str, Any] | None:
        """Fetch paper info from arXiv API."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://export.arxiv.org/api/query?id_list={paper_id}",
                    headers={"Accept": "application/xml"},
                )
                if resp.status_code == 200:
                    # Parse XML response (simplified)
                    return {
                        "name": paper_id,
                        "type": "paper",
                        "url": f"https://arxiv.org/abs/{paper_id}",
                        "metadata": {},
                    }
        except Exception as exc:
            logger.warning("arXiv connector failed for %s: %s", paper_id, exc)
        return None


class NpmConnector:
    """npm registry API connector."""

    async def fetch(self, package: str) -> dict[str, Any] | None:
        """Fetch package info from npm registry."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://registry.npmjs.org/{package}",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "name": data.get("name", package),
                        "type": "package",
                        "url": f"https://www.npmjs.com/package/{package}",
                        "metadata": {
                            "version": data.get("version", ""),
                            "description": data.get("description", ""),
                        },
                    }
        except Exception as exc:
            logger.warning("npm connector failed for %s: %s", package, exc)
        return None


class PyPiConnector:
    """PyPI JSON API connector."""

    async def fetch(self, package: str) -> dict[str, Any] | None:
        """Fetch package info from PyPI."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://pypi.org/pypi/{package}/json",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    info = data.get("info", {})
                    return {
                        "name": info.get("name", package),
                        "type": "package",
                        "url": info.get("home_page", f"https://pypi.org/project/{package}/"),
                        "metadata": {
                            "version": info.get("version", ""),
                            "summary": info.get("summary", ""),
                        },
                    }
        except Exception as exc:
            logger.warning("PyPI connector failed for %s: %s", package, exc)
        return None


class WikipediaConnector:
    """MediaWiki API connector."""

    async def fetch(self, title: str) -> dict[str, Any] | None:
        """Fetch article info from Wikipedia."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "titles": title,
                        "prop": "extracts",
                        "exintro": True,
                        "explaintext": True,
                        "format": "json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    pages = data.get("query", {}).get("pages", {})
                    for page in pages.values():
                        return {
                            "name": page.get("title", title),
                            "type": "article",
                            "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                            "metadata": {
                                "extract": page.get("extract", "")[:500],
                            },
                        }
        except Exception as exc:
            logger.warning("Wikipedia connector failed for %s: %s", title, exc)
        return None


class OpenRouterConnector:
    """OpenRouter API connector."""

    async def fetch(self, model_id: str) -> dict[str, Any] | None:
        """Fetch model info from OpenRouter API."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://openrouter.ai/api/v1/models/{model_id}",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "name": data.get("id", model_id),
                        "type": "model",
                        "url": f"https://openrouter.ai/{model_id}",
                        "metadata": {
                            "context_length": data.get("context_length", 0),
                            "pricing": data.get("pricing", {}),
                        },
                    }
        except Exception as exc:
            logger.warning("OpenRouter connector failed for %s: %s", model_id, exc)
        return None
