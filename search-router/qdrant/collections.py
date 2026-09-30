"""Qdrant collection schemas and bootstrap.

Collections:
- web_passages_v1: dense + sparse vectors for web passages
- web_images_v1: CLIP/SigLIP image embeddings
- user_documents_v1: user file embeddings (tenant-isolated)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from qdrant.client import QdrantClient

logger = logging.getLogger(__name__)


@dataclass
class CollectionSchema:
    """Schema for a Qdrant collection."""

    name: str
    vector_size: int = 1024
    distance: str = "Cosine"
    sparse_enabled: bool = True
    description: str = ""


# ─── Collection definitions ──────────────────────────────────────────────────

WEB_PASSAGES_V1 = CollectionSchema(
    name="web_passages_v1",
    vector_size=1024,
    distance="Cosine",
    sparse_enabled=True,
    description="Web passage dense + sparse vectors (BGE-M3)",
)

WEB_IMAGES_V1 = CollectionSchema(
    name="web_images_v1",
    vector_size=768,  # CLIP ViT-B/32
    distance="Cosine",
    sparse_enabled=False,
    description="Web image embeddings (CLIP/SigLIP)",
)

USER_DOCUMENTS_V1 = CollectionSchema(
    name="user_documents_v1",
    vector_size=1024,
    distance="Cosine",
    sparse_enabled=True,
    description="User document embeddings (tenant-isolated)",
)

ALL_COLLECTIONS = [WEB_PASSAGES_V1, WEB_IMAGES_V1, USER_DOCUMENTS_V1]


async def bootstrap_collections(
    client: QdrantClient,
    collections: list[CollectionSchema] | None = None,
) -> dict[str, bool]:
    """Create collections if they don't exist."""
    results: dict[str, bool] = {}
    for schema in collections or ALL_COLLECTIONS:
        exists = await client.collection_exists(schema.name)
        if exists:
            results[schema.name] = True
            continue
        created = await client.create_collection(
            collection=schema.name,
            vector_size=schema.vector_size,
            distance=schema.distance,
            sparse_enabled=schema.sparse_enabled,
        )
        results[schema.name] = created
        if created:
            logger.info("Created Qdrant collection: %s", schema.name)
        else:
            logger.warning("Failed to create Qdrant collection: %s", schema.name)
    return results
