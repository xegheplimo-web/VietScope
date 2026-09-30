"""L16 Citation Validator — 4 tiers (ID whitelist, lexical, numeric, NLI).

Tier 4 uses NLI entailment to verify claims against evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.inference_gateway import ModelRole

from pipeline.rag import llm_chat


@dataclass
class NLIResult:
    """Result of NLI entailment check."""

    claim: str
    evidence: str
    label: str  # entailment | neutral | contradiction
    score: float


class NLICitationValidator:
    """Tier 4: NLI entailment for citation validation."""

    def __init__(self, model: str = "cross-encoder/nli-deberta-v3-base"):
        self.model = model

    async def validate(
        self,
        claims: list[str],
        evidence: list[dict[str, Any]],
    ) -> list[NLIResult]:
        """Validate claims against evidence using NLI."""
        results: list[NLIResult] = []

        for claim in claims:
            for ev in evidence:
                ev_text = ev.get("text", "")
                if not ev_text:
                    continue

                # Call NLI model
                nli_result = await self._nli_check(claim, ev_text)
                if nli_result:
                    results.append(nli_result)

        return results

    async def _nli_check(self, claim: str, evidence: str) -> NLIResult | None:
        """Check entailment between claim and evidence."""
        try:
            # Use LLM as NLI proxy (can be replaced with dedicated NLI model)
            prompt = f"""Determine if the claim is supported by the evidence.
Claim: {claim}
Evidence: {evidence}

Respond with ONLY one word: entailment, neutral, or contradiction."""

            response = await llm_chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=10,
                role=ModelRole.VERIFIER,
            )

            if not response:
                return None

            response = response.strip().lower()

            if "entailment" in response:
                return NLIResult(claim=claim, evidence=evidence, label="entailment", score=0.9)
            elif "contradiction" in response:
                return NLIResult(claim=claim, evidence=evidence, label="contradiction", score=0.1)
            else:
                return NLIResult(claim=claim, evidence=evidence, label="neutral", score=0.5)

        except Exception:
            return None

    async def validate_with_repair(
        self,
        answer: str,
        citations: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate citations and suggest repairs."""
        # Extract claims from citations
        claims = [c.get("text", "") for c in citations]

        # Run NLI validation
        nli_results = await self.validate(claims, evidence)

        # Group by citation
        citation_results: dict[str, list[NLIResult]] = {}
        for result in nli_results:
            for citation in citations:
                if citation.get("text", "") == result.claim:
                    citation_results.setdefault(citation.get("citation_id", ""), []).append(result)

        # Determine valid/invalid citations
        valid = []
        invalid = []
        for citation_id, results in citation_results.items():
            # If any result is entailment, citation is valid
            if any(r.label == "entailment" for r in results):
                valid.append(citation_id)
            else:
                invalid.append(citation_id)

        return {
            "valid_citations": valid,
            "invalid_citations": invalid,
            "repair_needed": len(invalid) > 0,
            "repair_prompt": self._build_repair_prompt(invalid) if invalid else "",
        }

    def _build_repair_prompt(self, invalid_citations: list[str]) -> str:
        """Build prompt for citation repair."""
        return f"""The following citations are not supported by evidence: {", ".join(invalid_citations)}.
Please rewrite the answer using ONLY supported evidence, or remove unsupported claims."""
