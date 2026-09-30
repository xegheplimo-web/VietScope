"""Image Search — web_images extraction, CLIP/SigLIP embeddings, Qdrant image collection.

Phase 9: Add image search capability:
- Extract images from web pages (URL, alt, caption, surrounding text)
- Generate CLIP/SigLIP embeddings
- Store in Qdrant image collection
- Search by text query → image results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config import settings
from qdrant.client import QdrantClient, QdrantPoint

logger = logging.getLogger(__name__)


@dataclass
class WebImage:
    """Extracted image from a web page."""

    image_id: str
    page_url: str
    src_url: str
    alt: str = ""
    caption: str = ""
    surrounding_text: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None
    mime_type: str = ""
    phash: str = ""  # perceptual hash for dedup


@dataclass
class ImageSearchResult:
    """Result from image search."""

    image_id: str
    score: float
    src_url: str
    page_url: str
    alt: str = ""
    caption: str = ""
    width: int | None = None
    height: int | None = None


class ImageSearchService:
    """Image search via Qdrant."""

    def __init__(
        self,
        qdrant_client: QdrantClient,
        collection: str = "web_images_v1",
        embedding_service_url: str | None = None,
        top_k: int = 20,
    ):
        self.qdrant = qdrant_client
        self.collection = collection
        self.embedding_service_url = embedding_service_url or settings.embedding_service_url
        self.top_k = top_k

    async def search_by_text(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[ImageSearchResult]:
        """Search images by text query.

        1. Embed query text → image embedding (CLIP/SigLIP)
        2. Search Qdrant image collection
        3. Return image results with metadata
        """
        import httpx

        # Get image embedding from embedding service
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.embedding_service_url}/embed",
                json={"texts": [query], "modal": "image"},
            )
            resp.raise_for_status()
            data = resp.json()
            query_vector = data.get("vectors", [[]])[0]

        if not query_vector:
            return []

        # Search Qdrant
        results = await self.qdrant.search_dense(
            collection=self.collection,
            query_vector=query_vector,
            top_k=top_k or self.top_k,
            filters=filters,
        )

        return [
            ImageSearchResult(
                image_id=r.point_id,
                score=r.score,
                src_url=r.payload.get("src_url", ""),
                page_url=r.payload.get("page_url", ""),
                alt=r.payload.get("alt", ""),
                caption=r.payload.get("caption", ""),
                width=r.payload.get("width"),
                height=r.payload.get("height"),
            )
            for r in results
        ]

    async def index_image(
        self,
        image: WebImage,
        embedding: list[float],
    ) -> bool:
        """Index a single image into Qdrant."""
        point = QdrantPoint(
            point_id=image.image_id,
            dense=embedding,
            payload={
                "src_url": image.src_url,
                "page_url": image.page_url,
                "alt": image.alt,
                "caption": image.caption,
                "surrounding_text": image.surrounding_text,
                "title": image.title,
                "width": image.width,
                "height": image.height,
                "mime_type": image.mime_type,
                "phash": image.phash,
            },
        )
        return await self.qdrant.upsert_points(
            collection=self.collection,
            points=[point],
        )

    def extract_images_from_page(
        self,
        page_url: str,
        page_content: str,
    ) -> list[WebImage]:
        """Extract images from HTML content.

        This is a simplified extraction — in production, use
        a proper HTML parser or Firecrawl's image extraction.
        """
        import hashlib
        import re

        images: list[WebImage] = []

        # Simple regex extraction (production: use BeautifulSoup or similar)
        img_pattern = re.compile(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
            re.IGNORECASE,
        )
        alt_pattern = re.compile(r'alt=["\']([^"\']*)["\']', re.IGNORECASE)

        for match in img_pattern.finditer(page_content):
            src = match.group(1)
            # Skip data URIs and tracking pixels
            if src.startswith("data:") or src.startswith("blob:"):
                continue

            # Extract alt text
            alt_match = alt_pattern.search(match.group(0))
            alt = alt_match.group(1) if alt_match else ""

            # Generate image ID
            image_id = f"img_{hashlib.md5(f'{page_url}:{src}'.encode(), usedforsecurity=False).hexdigest()[:16]}"

            images.append(
                WebImage(
                    image_id=image_id,
                    page_url=page_url,
                    src_url=src,
                    alt=alt,
                )
            )

        return images
