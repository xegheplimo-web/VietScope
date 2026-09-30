"""L13 Sufficiency Loop — agentic loop for research mode.

Evaluates evidence sufficiency and triggers re-planning when needed.
Only enabled for research mode; normal/fast modes are deterministic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SufficiencyResult:
    """Result of sufficiency evaluation."""

    sufficient: bool
    coverage_ratio: float
    missing_facts: list[str]
    conflicts: list[dict[str, Any]]
    need_more_search: bool
    proposed_queries: list[str] = field(default_factory=list)


class SufficiencyLoop:
    """Evaluates evidence sufficiency and triggers re-planning."""

    def __init__(
        self,
        max_rounds: int = 3,
        coverage_threshold: float = 0.8,
        min_domains: int = 3,
    ):
        self.max_rounds = max_rounds
        self.coverage_threshold = coverage_threshold
        self.min_domains = min_domains

    def evaluate(
        self,
        query: str,
        evidence: list[dict[str, Any]],
        subqueries: list[str],
        round_num: int = 0,
    ) -> SufficiencyResult:
        """Evaluate if evidence is sufficient for the query."""
        # Check coverage
        covered = 0
        missing: list[str] = []

        for subquery in subqueries:
            is_covered = False
            for item in evidence:
                text = item.get("text", "").lower()
                if subquery.lower() in text:
                    is_covered = True
                    break
            if is_covered:
                covered += 1
            else:
                missing.append(subquery)

        coverage_ratio = covered / len(subqueries) if subqueries else 1.0

        # Check domain diversity
        domains = {item.get("domain", "") for item in evidence}
        domain_diverse = len(domains) >= self.min_domains

        # Check for conflicts
        conflicts = self._detect_conflicts(evidence)

        # Determine sufficiency
        sufficient = coverage_ratio >= self.coverage_threshold and domain_diverse and not conflicts

        # Generate proposed queries for missing facts
        proposed = self._generate_queries(missing) if missing else []

        return SufficiencyResult(
            sufficient=sufficient,
            coverage_ratio=coverage_ratio,
            missing_facts=missing,
            conflicts=conflicts,
            need_more_search=not sufficient and round_num < self.max_rounds,
            proposed_queries=proposed,
        )

    def _detect_conflicts(
        self,
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Detect conflicting claims in evidence."""
        conflicts: list[dict[str, Any]] = []

        # Group by numeric values
        value_groups: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            numbers = item.get("contains_numbers", [])
            for num in numbers:
                value_groups.setdefault(num, []).append(item)

        # Find conflicts (same key, different sources)
        for key, items in value_groups.items():
            if len(items) > 1:
                conflicts.append(
                    {
                        "claim_key": key,
                        "variants": [
                            {"value": key, "sources": [i.get("source_id", "") for i in items]}
                        ],
                    }
                )

        return conflicts

    def _generate_queries(self, missing_facts: list[str]) -> list[str]:
        """Generate follow-up queries for missing facts."""
        return [f"latest {fact}" for fact in missing_facts[:3]]
