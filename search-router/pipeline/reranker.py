"""Reranker — relevance scoring and source ranking.

DEPRECATED(phase0): canonical result ranking lives in ``ranking/``
(``engine``/``fusion``/``quality``/``rerank`` over ``RankedItem``).  This
module is retained only for (a) the legacy tool endpoints in ``main.py``
(``/search`` image mode, ``/answer`` fallback) and (b) the shared lexical
helpers (``_tokenize``, ``_bm25_score``, ``_chunk_text``,
``_keyword_overlap_score``, ``_phrase_bonus``) consumed by
``pipeline/passage_reranker.py`` on the live research path.  When the legacy
endpoints are removed, ``rerank_search_results`` / ``rerank_scraped_content``
/ ``rerank_semantic`` die with them and the helpers should migrate into
``ranking/``.  See ``docs/phase0-dedup-map.md``.

Inspired by llm-answer-engine's similarity search approach, but simplified
to work without embeddings (keyword + heuristic scoring) so it runs
without an external LLM. When an LLM is available, semantic reranking
can be layered on top.

v3 enhancements (backward compatible):
  - BM25-ish term-frequency scoring with IDF weighting.
  - Phrase bonus: exact multi-word query phrases appearing in text.
  - Title boost: title matches weighted higher than body/description.
  - Diversity penalty: if a single domain occupies >50% of top-N, that
    domain's lower-ranked entries are penalized (active deduplication).

The original scoring path (keyword overlap + domain authority + searxng
score) is preserved as the deterministic baseline; the new signals are
*additive* on top, so existing callers and tests continue to work.
"""

import math
import re
from collections import Counter
from urllib.parse import urlparse

from models import ScrapeResult, SearchResultItem
from ranking.authority import vn_boost

# ── Tokenization ──────────────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "there",
        "here",
        "where",
        "when",
        "why",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "will",
        "would",
        "can",
        "could",
        "should",
        "shall",
        "may",
        "might",
        "must",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "having",
        "not",
        "no",
        "nor",
        "so",
        "than",
        "too",
        "very",
        "only",
        "own",
        "same",
        "such",
        "about",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "once",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
    ]
)


def _tokenize(text: str) -> list[str]:
    """Tokenize — lowercase, split on non-word chars, drop stopwords."""
    return [t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS]


def _tokenize_set(text: str) -> set[str]:
    """Tokenize as a set (for overlap/intersection checks)."""
    return set(_tokenize(text))


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _query_language(query: str) -> str:
    """Detect query language ("vi" | "en") using QueryUnderstanding.

    Falls back to "en" on any error so ranking never crashes.
    """
    try:
        from core.query_understanding import QueryUnderstanding

        return QueryUnderstanding().analyze(query).language
    except Exception:
        return "en"


# ── BM25-ish scoring ──────────────────────────────────────────────────────────


def _bm25_score(
    query_terms: list[str],
    doc_terms: list[str],
    avg_doc_len: float,
    k1: float = 1.5,
    b: float = 0.75,
    idf_map: dict[str, float] | None = None,
) -> float:
    """A simplified BM25 score for a single document.

    Unlike full BM25, this does not require a corpus statistics pass —
    IDF is estimated as ``log(1 + N / (1 + df))`` with N=10 (a reasonable
    default for a single search result page) and df=1 when no corpus
    stats are provided. This gives rare query terms higher weight.
    """
    if not query_terms or not doc_terms:
        return 0.0
    doc_len = len(doc_terms)
    tf = Counter(doc_terms)
    score = 0.0
    for term in query_terms:
        f = tf.get(term, 0)
        if f == 0:
            continue
        # Heuristic IDF fallback: rare terms score higher.
        idf = idf_map[term] if idf_map and term in idf_map else math.log(1 + 10.0 / (1 + 1))
        denom = f + k1 * (1 - b + b * (doc_len / max(avg_doc_len, 1.0)))
        score += idf * (f * (k1 + 1)) / denom
    return score


# ── Phrase bonus ──────────────────────────────────────────────────────────────


def _phrase_bonus(query: str, text: str) -> float:
    """Bonus for exact multi-word phrase matches.

    For each contiguous 2-gram (or longer) from the query that appears
    verbatim in the text, add a small bonus. This rewards results that
    contain the *concept* rather than just scattered keywords.
    """
    q_terms = _tokenize(query)
    if len(q_terms) < 2:
        return 0.0
    text_lower = text.lower()
    bonus = 0.0
    # Check 2-grams and 3-grams.
    for n in (3, 2):
        for i in range(len(q_terms) - n + 1):
            phrase = " ".join(q_terms[i : i + n])
            if phrase in text_lower:
                bonus += 0.05 * n  # 3-gram worth more than 2-gram
    return min(bonus, 0.3)  # cap


# ── Title boost ───────────────────────────────────────────────────────────────


def _title_boost(query_terms: list[str], title: str) -> float:
    """Extra score when query terms appear in the title."""
    if not title:
        return 0.0
    title_tokens = _tokenize_set(title)
    if not title_tokens or not query_terms:
        return 0.0
    overlap = sum(1 for t in query_terms if t in title_tokens)
    return (overlap / len(query_terms)) * 0.25  # up to 0.25


# ── Domain authority (legacy, preserved) ──────────────────────────────────────


def _domain_authority_boost(url: str) -> float:
    """Heuristic authority boost for known high-quality domains."""
    high_quality = {
        "github.com": 0.15,
        "stackoverflow.com": 0.12,
        "docs.": 0.10,
        "wikipedia.org": 0.10,
        "arxiv.org": 0.12,
        "official": 0.08,
        "python.org": 0.10,
        "rust-lang.org": 0.10,
        "developer.mozilla.org": 0.12,
        "kubernetes.io": 0.10,
    }
    boost = 0.0
    url_lower = url.lower()
    for domain, score in high_quality.items():
        if domain in url_lower:
            boost = max(boost, score)
    return boost


# ── Diversity penalty ─────────────────────────────────────────────────────────


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _apply_diversity_penalty(
    results: list[SearchResultItem],
    top_n: int = 10,
    domain_threshold: float = 0.5,
    penalty: float = 0.15,
) -> list[SearchResultItem]:
    """Penalize lower-ranked entries from over-represented domains.

    If a single domain occupies more than ``domain_threshold`` of the
    top-N slots, every entry from that domain *after the first* gets a
    score penalty. This keeps the top results diverse without discarding
    relevant sources entirely.
    """
    if len(results) <= 1:
        return results
    n = min(top_n, len(results))
    domain_counts: Counter[str] = Counter()
    for r in results[:n]:
        d = _domain_from_url(r.url)
        if d:
            domain_counts[d] += 1
    over_domains = {d for d, c in domain_counts.items() if c / n > domain_threshold}
    if not over_domains:
        return results
    seen: set[str] = set()
    for r in results:
        d = _domain_from_url(r.url)
        if d in over_domains:
            if d in seen:
                r.score = max(0.0, r.score - penalty)
            else:
                seen.add(d)
    # Re-sort after penalty.
    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ── Legacy keyword overlap (preserved for backward compat) ────────────────────


def _keyword_overlap_score(query_tokens: set[str], text: str) -> float:
    """Score based on keyword overlap between query and text."""
    if not text or not query_tokens:
        return 0.0
    text_tokens = _tokenize_set(text)
    if not text_tokens:
        return 0.0
    overlap = query_tokens & text_tokens
    return len(overlap) / math.sqrt(len(query_tokens) * len(text_tokens))


# ── Public API ────────────────────────────────────────────────────────────────


def rerank_search_results(
    query: str,
    results: list[SearchResultItem],
    *,
    apply_diversity: bool = True,
) -> list[SearchResultItem]:
    """Rerank search results by combined keyword + BM25 + authority + searxng score.

    Backward compatible: the original 3-signal blend (keyword overlap,
    domain authority, SearXNG score) is preserved as the baseline. New
    signals (BM25-ish, phrase bonus, title boost) are added on top, then
    an optional diversity penalty is applied.

    Args:
        query: The user query.
        results: List of SearchResultItem to rerank (mutated in place).
        apply_diversity: If True, apply domain diversity penalty (default).
    """
    query_tokens = _tokenize_set(query)
    query_terms = _tokenize(query)
    query_lang = _query_language(query)

    # Estimate avg doc length for BM25 normalization.
    doc_lens = [len(_tokenize(f"{r.title} {r.description}")) for r in results]
    avg_doc_len = sum(doc_lens) / len(doc_lens) if doc_lens else 1.0

    for r in results:
        title = r.title or ""
        desc = r.description or ""
        text = f"{title} {desc}"

        # Legacy baseline signals (preserved).
        kw_score = _keyword_overlap_score(query_tokens, text)
        authority = _domain_authority_boost(r.url)
        searx_score = min(r.score / 10.0, 1.0)  # normalize searxng score

        # New v3 signals.
        doc_terms = _tokenize(text)
        bm25 = _bm25_score(query_terms, doc_terms, avg_doc_len)
        # Normalize BM25 to ~0-1 range (empirical cap at 3.0).
        bm25_norm = min(bm25 / 3.0, 1.0)
        phrase = _phrase_bonus(query, text)
        title_b = _title_boost(query_terms, title)

        # Weighted combination — baseline weights preserved, new signals added.
        r.score = (
            0.25 * kw_score
            + 0.20 * authority
            + 0.15 * searx_score
            + 0.20 * bm25_norm
            + 0.10 * phrase
            + 0.10 * title_b
        )

        # Vietnamese queries: prefer local VN sources (applied before sort).
        if query_lang == "vi":
            r.score += vn_boost(_domain_from_url(r.url))

    results.sort(key=lambda r: r.score, reverse=True)

    if apply_diversity:
        results = _apply_diversity_penalty(results)

    return results


def rerank_scraped_content(
    query: str,
    scraped: list[ScrapeResult],
    max_chunks_per_source: int = 3,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
) -> list[dict]:
    """Chunk and rerank scraped content by relevance to query.

    Returns list of {source_url, title, chunk_text, score} sorted by score.
    """
    query_tokens = _tokenize_set(query)
    query_terms = _tokenize(query)
    ranked_chunks: list[dict] = []

    # Estimate avg chunk length for BM25.
    all_chunk_texts: list[str] = []
    for doc in scraped:
        if doc.error or not doc.markdown:
            continue
        all_chunk_texts.extend(_chunk_text(doc.markdown, chunk_size, chunk_overlap))
    avg_len = sum(len(_tokenize(c)) for c in all_chunk_texts) / max(1, len(all_chunk_texts))

    for doc in scraped:
        if doc.error or not doc.markdown:
            continue

        text = doc.markdown
        chunks = _chunk_text(text, chunk_size, chunk_overlap)

        for i, chunk in enumerate(chunks[: max_chunks_per_source * 2]):
            kw = _keyword_overlap_score(query_tokens, chunk)
            doc_terms = _tokenize(chunk)
            bm25 = _bm25_score(query_terms, doc_terms, avg_len)
            bm25_norm = min(bm25 / 3.0, 1.0)
            phrase = _phrase_bonus(query, chunk)
            score = 0.5 * kw + 0.3 * bm25_norm + 0.2 * phrase
            ranked_chunks.append(
                {
                    "source_url": doc.url,
                    "title": doc.title,
                    "chunk_index": i,
                    "chunk_text": chunk[:chunk_size],
                    "score": score,
                }
            )

    ranked_chunks.sort(key=lambda c: c["score"], reverse=True)
    return ranked_chunks[: max_chunks_per_source * len(scraped)]


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks by character count."""
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # Try to break at a paragraph or sentence boundary.
        if end < len(text):
            for sep in ["\n\n", "\n", ". ", " "]:
                last_sep = chunk.rfind(sep)
                if last_sep > chunk_size // 2:
                    end = start + last_sep + len(sep)
                    chunk = text[start:end]
                    break
        chunks.append(chunk.strip())
        start = end - overlap
        if start >= len(text):
            break

    return chunks


# ── Semantic rerank (optional) ───────────────────────────────────────────────


def rerank_semantic(
    query: str,
    results: list[SearchResultItem],
    llm_reranker,
    top_n: int = 10,
) -> list[SearchResultItem]:
    """Blend semantic (embedding) similarity with the keyword score.

    Combined score = ``0.6 * semantic`` + ``0.4 * keyword``, then sorted
    descending. When the embedding reranker is not available (no endpoint
    configured) or embedding fails, ``results`` is returned unchanged so the
    existing keyword-only behavior is preserved exactly.

    Args:
        query: The user query.
        results: Results to rerank.
        llm_reranker: An ``EmbeddingReranker``-like object exposing
            ``available()`` and ``embed(texts)``.
        top_n: Keep only the top-N results when available (default 10).
    """
    if not results:
        return results
    available = getattr(llm_reranker, "available", None)
    if available is None or not available():
        return results

    docs = [f"{r.title or ''} {r.description or ''}".strip() for r in results]
    vectors = llm_reranker.embed([query] + docs)
    if vectors is None or len(vectors) != len(docs) + 1:
        return results

    q_vec = vectors[0]
    query_tokens = _tokenize_set(query)
    scored: list[tuple[SearchResultItem, float]] = []
    for r, doc_vec in zip(results, vectors[1:], strict=False):
        semantic = _cosine_sim(q_vec, doc_vec)
        keyword = _keyword_overlap_score(query_tokens, f"{r.title or ''} {r.description or ''}")
        scored.append((r, 0.6 * semantic + 0.4 * keyword))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    ranked = [r for r, _ in scored]
    return ranked[: max(0, top_n)] if top_n else ranked
