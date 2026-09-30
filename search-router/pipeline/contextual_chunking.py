"""L9 Contextual Chunking — structure-first chunking with header path prefix.

Chunks are split by structure (H2 > H3 > table > code > paragraph),
not by fixed size.  Each chunk carries its heading path for context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """A single text chunk with context."""

    chunk_id: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    char_start: int = 0
    char_end: int = 0
    token_count: int = 0
    dom_role: str = "paragraph"  # paragraph | table | code | list | faq
    contains_numbers: list[str] = field(default_factory=list)
    contains_dates: list[str] = field(default_factory=list)


class ContextualChunker:
    """Structure-first chunker with header path prefix."""

    def __init__(
        self,
        chunk_size: int = 450,
        chunk_overlap: int = 60,
        max_chunks_per_source: int = 8,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chunks_per_source = max_chunks_per_source

    def chunk(self, text: str, *, source_url: str = "") -> list[Chunk]:
        """Split text into contextual chunks."""
        if not text:
            return []

        # Split by structure
        sections = self._split_by_structure(text)

        chunks: list[Chunk] = []
        for section in sections:
            section_chunks = self._chunk_section(section, source_url)
            chunks.extend(section_chunks)

        # Limit chunks per source
        if len(chunks) > self.max_chunks_per_source:
            # Keep first N/2 and last N/2 (most important)
            half = self.max_chunks_per_source // 2
            chunks = chunks[:half] + chunks[-half:]

        return chunks

    def _split_by_structure(self, text: str) -> list[dict[str, Any]]:
        """Split text by HTML/markdown structure."""
        sections: list[dict[str, Any]] = []

        # Split by headers (H1-H6)
        header_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        parts = header_pattern.split(text)

        current_heading = []
        i = 0
        while i < len(parts):
            # Check if current part is a header marker
            if parts[i].startswith("#"):
                # This is a header marker
                level = len(parts[i])
                heading_text = parts[i + 1].strip() if i + 1 < len(parts) else ""
                current_heading = current_heading[: level - 1] + [heading_text]
                i += 2  # skip marker and heading text; content comes next
            else:
                # This is content
                content = parts[i].strip()
                if content:
                    sections.append(
                        {
                            "heading_path": list(current_heading),
                            "content": content,
                            "dom_role": self._detect_dom_role(content),
                        }
                    )
                i += 1

        # If no headers found, treat as single section
        if not sections and text.strip():
            sections.append(
                {
                    "heading_path": [],
                    "content": text.strip(),
                    "dom_role": "paragraph",
                }
            )

        return sections

    def _chunk_section(self, section: dict[str, Any], source_url: str) -> list[Chunk]:
        """Chunk a single section."""
        content = section["content"]
        heading_path = section["heading_path"]
        dom_role = section["dom_role"]

        # For tables and code blocks, keep as single chunk
        if dom_role in ("table", "code"):
            return [
                self._create_chunk(content, heading_path, 0, len(content), dom_role, source_url)
            ]

        # For paragraphs, split by size
        chunks = []
        start = 0
        while start < len(content):
            end = min(start + self.chunk_size, len(content))
            # Try to break at sentence boundary
            if end < len(content):
                # Look for sentence end
                for sep in [". ", "! ", "? ", "\n\n"]:
                    pos = content.rfind(sep, start, end)
                    if pos > start:
                        end = pos + len(sep)
                        break

            chunk_text = content[start:end].strip()
            if chunk_text:
                chunks.append(
                    self._create_chunk(chunk_text, heading_path, start, end, dom_role, source_url)
                )

            start = end - self.chunk_overlap if end < len(content) else end

        return chunks

    def _create_chunk(
        self,
        text: str,
        heading_path: list[str],
        char_start: int,
        char_end: int,
        dom_role: str,
        source_url: str,
    ) -> Chunk:
        """Create a chunk with metadata."""
        # Extract numbers and dates
        numbers = re.findall(r"\b\d+(?:\.\d+)?\b", text)
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", text)

        # Estimate token count (rough: 1 token ≈ 4 chars)
        token_count = len(text) // 4

        # Build chunk ID
        chunk_id = f"chunk_{hash(text) % 100000:05d}"

        return Chunk(
            chunk_id=chunk_id,
            text=text,
            heading_path=heading_path,
            char_start=char_start,
            char_end=char_end,
            token_count=token_count,
            dom_role=dom_role,
            contains_numbers=numbers[:10],  # limit
            contains_dates=dates[:5],
        )

    def _detect_dom_role(self, content: str) -> str:
        """Detect DOM role of content."""
        # Code block
        if content.startswith("```") or content.startswith("    ") or "\n```" in content:
            return "code"

        # Table
        if "|" in content and content.count("|") > 2:
            return "table"

        # List
        if re.match(r"^\s*[-*•]\s", content, re.MULTILINE):
            return "list"

        # FAQ
        if "?" in content and len(content) < 200:
            return "faq"

        return "paragraph"

    def add_header_prefix(self, chunk: Chunk) -> str:
        """Add header path prefix to chunk text."""
        if not chunk.heading_path:
            return chunk.text

        prefix = " > ".join(chunk.heading_path)
        return f"[{prefix}]\n\n{chunk.text}"
