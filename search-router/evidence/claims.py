"""Claim extraction.

Extracts information-bearing claims from a generated answer. The default path is
a deterministic heuristic (sentence split + informative-sentence detection).
An optional LLM path can be used through ``InferenceGateway`` when an API key is
configured; if the gateway is unavailable or returns unusable output we always
fall back to the heuristic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core.inference_gateway import ModelRole


@dataclass
class Claim:
    """A single claim extracted from an answer."""

    claim_id: str
    text: str
    keywords: list[str] = field(default_factory=list)


STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "can",
        "could",
        "may",
        "might",
        "should",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "he",
        "she",
        "they",
        "we",
        "you",
        "i",
        "not",
        "no",
        "but",
        "if",
        "than",
        "then",
        "so",
        "also",
        "such",
        "into",
        "about",
        "over",
        "under",
        "between",
        "after",
        "before",
        "during",
        "which",
        "who",
        "whom",
        "whose",
        "there",
        "here",
        "their",
        "our",
        "your",
        "his",
        "her",
        "them",
        "us",
        "me",
        "my",
        # Vietnamese
        "là",
        "gì",
        "có",
        "mới",
        "nhất",
        "hôm",
        "nay",
        "của",
        "và",
        "hay",
        "không",
        "được",
        "như",
        "thế",
        "nào",
        "cách",
        "làm",
        "với",
        "cho",
        "về",
        "từ",
        "đến",
        "tại",
        "trên",
        "dưới",
        "cũng",
        "đã",
        "sẽ",
        "đang",
        "theo",
        "một",
        "hai",
        "ba",
        "bốn",
        "năm",
        "sáu",
        "bảy",
        "tám",
        "chín",
    }
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)

_INFORMATIVE_PATTERNS = [
    re.compile(r"\d"),
    re.compile(
        r"\b(is|are|was|were|has|have|had|will|would|can|could|may|might|"
        r"should|must|says|said|reports|reported|announces|announced)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(là|được|có|đã|sẽ|đang|gồm|bao gồm|theo|cho biết|công bố|ra mắt|"
        r"đạt|tăng|giảm|triệu|tỷ|nghìn)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Z][a-z]{2,}\b"),
]

_EXCLUDE_PATTERNS = [
    re.compile(r"^\s*\[?\d+\]?\s*$"),
    re.compile(r"^\s*(source|nguồn)\s*:", re.IGNORECASE),
    re.compile(r"^\s*#+\s"),
    re.compile(r"^\s*[-*]\s+\[?\d+\]?"),
]


def tokenize(text: str) -> list[str]:
    """Return ordered lowercased word tokens."""
    return _WORD_RE.findall((text or "").lower())


def keywords(text: str) -> list[str]:
    """Return significant (non-stopword, len > 1) tokens for a text."""
    return [t for t in tokenize(text) if len(t) > 1 and t not in STOPWORDS]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving reasonably clean fragments."""
    if not text:
        return []
    raw = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    return [s.strip() for s in raw if s.strip()]


def _is_informative(sentence: str) -> bool:
    if len(sentence) < 8:
        return False
    if sentence.rstrip().endswith("?"):
        return False
    if any(p.search(sentence) for p in _EXCLUDE_PATTERNS):
        return False
    return any(p.search(sentence) for p in _INFORMATIVE_PATTERNS)


def extract_claims(answer: str, max_claims: int = 8) -> list[Claim]:
    """Extract claims from ``answer`` using deterministic heuristics.

    Each claim receives a stable ``claim_id`` (c1, c2, ...).
    """
    if not answer:
        return []
    claims: list[Claim] = []
    for sentence in split_sentences(answer):
        if len(claims) >= max_claims:
            break
        sentence = sentence.strip()
        if not _is_informative(sentence):
            continue
        claims.append(
            Claim(
                claim_id=f"c{len(claims) + 1}",
                text=sentence,
                keywords=keywords(sentence),
            )
        )
    return claims


def _parse_claim_list(raw: str, max_claims: int = 8) -> list[Claim] | None:
    """Parse an LLM claim extraction response (JSON array of strings/objects)."""
    raw = (raw or "").strip()
    # Strip markdown fences if the model wraps JSON.
    fence = re.search(r"\[.*\]", raw, re.DOTALL)
    if not fence:
        return None
    try:
        data = json.loads(fence.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    claims: list[Claim] = []
    for item in data[:max_claims]:
        text = ""
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("claim") or "").strip()
        if not text:
            continue
        claims.append(
            Claim(
                claim_id=f"c{len(claims) + 1}",
                text=text,
                keywords=keywords(text),
            )
        )
    return claims or None


async def extract_claims_with_llm(
    answer: str,
    inference,
    max_claims: int = 8,
) -> list[Claim]:
    """Extract claims with an optional LLM, falling back to heuristics."""
    if not answer:
        return []
    if inference is None or not getattr(inference, "api_key", None):
        return extract_claims(answer, max_claims=max_claims)

    prompt = (
        "Extract the factual, information-bearing claims from the following "
        "answer. Return ONLY a JSON array of claim strings. Do not include "
        "questions, section headings, or vague filler.\n\n"
        f"Answer:\n{answer}"
    )
    try:
        raw = await inference.complete(
            [
                {"role": "system", "content": "You extract factual claims as JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
            role=ModelRole.EXTRACTOR,
        )
        parsed = _parse_claim_list(raw, max_claims=max_claims)
        if parsed:
            return parsed
    except Exception:
        pass
    return extract_claims(answer, max_claims=max_claims)
