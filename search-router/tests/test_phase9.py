"""Tests for Phase 9: image search, web image extraction, Qdrant image collection."""

from pipeline.image_search import ImageSearchResult, ImageSearchService, WebImage


class TestWebImage:
    def test_image_creation(self):
        image = WebImage(
            image_id="img_001",
            page_url="https://example.com",
            src_url="https://example.com/img.jpg",
            alt="Example image",
            caption="A caption",
            width=800,
            height=600,
            mime_type="image/jpeg",
        )
        assert image.image_id == "img_001"
        assert image.src_url == "https://example.com/img.jpg"
        assert image.alt == "Example image"

    def test_image_minimal(self):
        image = WebImage(
            image_id="img_002",
            page_url="https://example.com",
            src_url="https://example.com/img2.jpg",
        )
        assert image.alt == ""
        assert image.caption == ""
        assert image.width is None


class TestImageSearchResult:
    def test_result_creation(self):
        result = ImageSearchResult(
            image_id="img_001",
            score=0.95,
            src_url="https://example.com/img.jpg",
            page_url="https://example.com",
            alt="Example",
            width=800,
            height=600,
        )
        assert result.image_id == "img_001"
        assert result.score == 0.95
        assert result.src_url == "https://example.com/img.jpg"


class TestImageSearchService:
    def test_service_init(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        service = ImageSearchService(qdrant_client=client)
        assert service.collection == "web_images_v1"
        assert service.top_k == 20

    def test_service_init_custom(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://qdrant:6333")
        service = ImageSearchService(
            qdrant_client=client,
            collection="custom_images",
            embedding_service_url="http://emb:8892",
            top_k=50,
        )
        assert service.collection == "custom_images"
        assert service.embedding_service_url == "http://emb:8892"
        assert service.top_k == 50

    def test_extract_images_from_page(self):
        from qdrant.client import QdrantClient

        client = QdrantClient(base_url="http://localhost:6333")
        service = ImageSearchService(qdrant_client=client)

        html = """
        <html>
        <body>
          <img src="https://example.com/img1.jpg" alt="First image">
          <img src="https://example.com/img2.jpg" alt="Second image">
          <img src="data:image/png;base64,..." alt="Data URI">
        </body>
        </html>
        """
        images = service.extract_images_from_page(
            page_url="https://example.com",
            page_content=html,
        )
        assert len(images) == 2  # data URI skipped
        assert images[0].alt == "First image"
        assert images[1].alt == "Second image"
