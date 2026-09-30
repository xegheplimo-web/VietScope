"""Answer-level metrics (P13) for ``/v1/answer`` responses.

Honest derivations from the HTTP response surface only — no internal pipeline
fields are assumed:

* ``answer_correctness`` — fraction of ``expected_facts`` found in the answer
  text (accent-folded substring match so Vietnamese facts match regardless of
  diacritics). Absent when the query declares no ``expected_facts``.
* ``citation_precision`` — fraction of cited URLs matching ``expected_urls``
  (same domain/URL semantics as retrieval matching).
* ``citation_recall`` — fraction of ``expected_urls`` covered by at least one
  cited URL.
* ``unsupported_claim_rate`` — fraction of CitationV2 entries carrying an
  empty ``evidence`` list. Lower-bound proxy: the API response does not expose
  claims that verification already dropped, so claims are counted as the
  emitted citation set.
* ``evidence_quote_rate`` — fraction of citations with >=1 non-empty quote —
  well-formedness of CitationV2 evidence.
* ``cited_source_coverage`` — fraction of response ``sources`` that were cited
  at least once (retrieval -> citation funnel).

Pure functions — no I/O, no network.
"""

from __future__ import annotations

import unicodedata

from .matching import (
    domain_from_url,
    matched_expected_indexes,
    normalize_domain,
    result_matches_any,
)


def fold(text: str) -> str:
    """Casefold + strip combining marks so 'Giá vàng' matches 'gia vang'.

    'đ' is a distinct letter (not base+mark) so it is mapped to 'd' first.
    """
    decomposed = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fact_coverage(answer: str, expected_facts: list[str] | None) -> float | None:
    """Fraction of expected_facts present in the answer (None when no facts)."""
    facts = [f for f in (expected_facts or []) if (f or "").strip()]
    if not facts:
        return None
    hay = fold(answer or "")
    return sum(1 for f in facts if fold(f) in hay) / len(facts)


def citation_precision(cited_urls: list[str], expected_urls: list[str] | None) -> float | None:
    """Fraction of cited URLs that match ground truth (None when none cited)."""
    cited = [u for u in (cited_urls or []) if u]
    if not cited or not (expected_urls or []):
        return None
    hits = sum(1 for u in cited if result_matches_any(u, expected_urls))
    return hits / len(cited)


def citation_recall(cited_urls: list[str], expected_urls: list[str] | None) -> float | None:
    """Fraction of expected URLs covered by >=1 cited URL (None w/o ground truth)."""
    if not (expected_urls or []):
        return None
    covered = matched_expected_indexes(cited_urls or [], expected_urls)
    return len(covered) / len(expected_urls)


def unsupported_claim_rate(citations: list[dict] | None) -> float | None:
    """Fraction of emitted citations whose ``evidence`` list is empty.

    None when the response carries no citations (nothing to rate).
    """
    cits = [c for c in (citations or []) if isinstance(c, dict)]
    if not cits:
        return None
    empty = sum(
        1 for c in cits if not [e for e in (c.get("evidence") or []) if isinstance(e, dict)]
    )
    return empty / len(cits)


def evidence_quote_rate(citations: list[dict] | None) -> float | None:
    """Fraction of citations whose evidence includes >=1 non-empty quote."""
    cits = [c for c in (citations or []) if isinstance(c, dict)]
    if not cits:
        return None
    ok = sum(
        1
        for c in cits
        if any(
            isinstance(e, dict) and (e.get("quote") or "").strip()
            for e in (c.get("evidence") or [])
        )
    )
    return ok / len(cits)


def cited_source_coverage(cited_urls: list[str], source_urls: list[str] | None) -> float | None:
    """Fraction of returned sources cited at least once (None when no sources)."""
    srcs = [u for u in (source_urls or []) if u]
    if not srcs:
        return None
    cited = {normalize_domain(domain_from_url(u)) for u in (cited_urls or []) if u}
    cited.discard("")
    hits = sum(1 for u in srcs if normalize_domain(domain_from_url(u)) in cited)
    return hits / len(srcs)


def evaluate_answer(
    answer: str,
    cited_urls: list[str],
    citations: list[dict] | None,
    source_urls: list[str],
    source_domains: list[str],
    expected_urls: list[str] | None,
    expected_facts: list[str] | None,
    verified: bool | None,
    coverage: float | None,
    authority_scorer=None,
) -> dict:
    """Build the flat per-query metric dict for one ``/v1/answer`` response.

    Metrics whose inputs are absent (no ground-truth URLs, no expected_facts,
    no citations) are simply not emitted, so aggregation averages over the
    queries that produced a value.
    """
    from .metrics import source_diversity

    out: dict[str, float] = {
        "answer_present": 1.0 if (answer or "").strip() else 0.0,
        "sources_count": float(len(source_urls or [])),
        "citations_count": float(len(citations or [])),
        "source_diversity": round(source_diversity(source_domains or []), 4),
    }
    if verified is not None:
        out["verified"] = 1.0 if verified else 0.0
    if coverage is not None:
        out["coverage"] = float(coverage)

    cov = fact_coverage(answer, expected_facts)
    if cov is not None:
        out["answer_correctness"] = round(cov, 4)
    cp = citation_precision(cited_urls, expected_urls)
    if cp is not None:
        out["citation_precision"] = round(cp, 4)
    cr = citation_recall(cited_urls, expected_urls)
    if cr is not None:
        out["citation_recall"] = round(cr, 4)
    ucr = unsupported_claim_rate(citations)
    if ucr is not None:
        out["unsupported_claim_rate"] = round(ucr, 4)
    eqr = evidence_quote_rate(citations)
    if eqr is not None:
        out["evidence_quote_rate"] = round(eqr, 4)
    csc = cited_source_coverage(cited_urls, source_urls)
    if csc is not None:
        out["cited_source_coverage"] = round(csc, 4)

    if authority_scorer is not None:
        from .metrics import authority_mean

        out["authority"] = authority_mean(source_domains or [], authority_scorer)
    return out
