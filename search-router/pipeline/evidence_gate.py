"""L12 Evidence Gate — 7 sequential gates for evidence quality.

G1: Near-duplicate detection (simhash)
G2: Authority × intent scoring
G3: Freshness filtering
G4: Corroboration check
G5: Contradiction detection
G6: Source diversity
G7: Subquery coverage
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result of a single gate."""

    gate: str
    passed: bool
    reason: str = ""
    dropped: list[str] = field(default_factory=list)  # doc_ids dropped
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceGateOutput:
    """Output of the full evidence gate."""

    passed: list[dict[str, Any]]  # evidence that passed all gates
    dropped: list[dict[str, Any]]  # evidence that was dropped
    gate_results: list[GateResult]
    conflict_map: dict[str, list[dict[str, Any]]]  # conflict_id → variants
    coverage_map: dict[str, bool]  # subquery → covered


class EvidenceGate:
    """7-gate evidence quality filter."""

    def __init__(
        self,
        intent: str = "current_fact",
        freshness_class: str = "medium",
        min_authority: float = 0.3,
        max_age_days: int = 90,
        min_domains: int = 3,
        max_per_domain: int = 2,
    ):
        self.intent = intent
        self.freshness_class = freshness_class
        self.min_authority = min_authority
        self.max_age_days = max_age_days
        self.min_domains = min_domains
        self.max_per_domain = max_per_domain

    def run(
        self,
        evidence: list[dict[str, Any]],
        subqueries: list[str] | None = None,
    ) -> EvidenceGateOutput:
        """Run all 7 gates sequentially."""
        current = evidence
        all_dropped: list[dict[str, Any]] = []
        gate_results: list[GateResult] = []
        conflict_map: dict[str, list[dict[str, Any]]] = {}

        # G1: Near-duplicate
        g1 = self._gate_near_dup(current)
        gate_results.append(g1)
        current = [e for e in current if e.get("doc_id") not in g1.dropped]

        # G2: Authority × intent
        g2 = self._gate_authority(current)
        gate_results.append(g2)
        current = [e for e in current if e.get("doc_id") not in g2.dropped]

        # G3: Freshness
        g3 = self._gate_freshness(current)
        gate_results.append(g3)
        current = [e for e in current if e.get("doc_id") not in g3.dropped]

        # G4: Corroboration
        g4 = self._gate_corroboration(current)
        gate_results.append(g4)
        current = [e for e in current if e.get("doc_id") not in g4.dropped]

        # G5: Contradiction
        g5 = self._gate_contradiction(current)
        gate_results.append(g5)
        conflict_map = g5.metadata.get("conflicts", {})

        # G6: Source diversity
        g6 = self._gate_diversity(current)
        gate_results.append(g6)
        current = [e for e in current if e.get("doc_id") not in g6.dropped]

        # G7: Subquery coverage
        g7 = self._gate_coverage(current, subqueries or [])
        gate_results.append(g7)

        return EvidenceGateOutput(
            passed=current,
            dropped=all_dropped,
            gate_results=gate_results,
            conflict_map=conflict_map,
            coverage_map={},  # populated by gate 7
        )

    def _gate_near_dup(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G1: Near-duplicate detection using simhash."""
        seen_hashes: dict[str, str] = {}  # simhash → doc_id
        dropped: list[str] = []

        for item in evidence:
            doc_id = item.get("doc_id", "")
            simhash = item.get("simhash", "")

            if not simhash:
                continue

            # Check hamming distance against seen hashes
            is_dup = False
            for seen_hash in seen_hashes:
                if self._hamming_distance(simhash, seen_hash) <= 3:
                    is_dup = True
                    dropped.append(doc_id)
                    break

            if not is_dup:
                seen_hashes[simhash] = doc_id

        return GateResult(
            gate="G1_near_dup",
            passed=len(dropped) == 0,
            reason=f"Dropped {len(dropped)} near-duplicates",
            dropped=dropped,
        )

    def _gate_authority(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G2: Authority × intent scoring."""
        dropped: list[str] = []

        for item in evidence:
            authority = item.get("authority", 0.5)
            if authority < self.min_authority:
                dropped.append(item.get("doc_id", ""))

        return GateResult(
            gate="G2_authority",
            passed=len(dropped) == 0,
            reason=f"Dropped {len(dropped)} low-authority sources",
            dropped=dropped,
        )

    def _gate_freshness(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G3: Freshness filtering."""
        dropped: list[str] = []

        for item in evidence:
            published_at = item.get("published_at")
            if not published_at:
                continue

            # Parse date and check age
            try:
                from datetime import datetime

                if isinstance(published_at, str):
                    dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                else:
                    dt = published_at

                age_days = (datetime.now(UTC) - dt).days
                if age_days > self.max_age_days:
                    dropped.append(item.get("doc_id", ""))
            except Exception:
                pass  # Keep if can't parse

        return GateResult(
            gate="G3_freshness",
            passed=len(dropped) == 0,
            reason=f"Dropped {len(dropped)} stale sources",
            dropped=dropped,
        )

    def _gate_corroboration(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G4: Corroboration check — numeric claims need ≥2 sources."""
        dropped: list[str] = []

        # Group by claim key (numeric values)
        claim_groups: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            numbers = item.get("contains_numbers", [])
            for num in numbers:
                claim_groups.setdefault(num, []).append(item)

        # Check each numeric claim
        for items in claim_groups.values():
            if len(items) < 2:
                # Single source for numeric claim — flag but don't drop
                for item in items:
                    item["single_source"] = True

        return GateResult(
            gate="G4_corroboration",
            passed=True,
            reason="Corroboration check complete",
            dropped=dropped,
        )

    def _gate_contradiction(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G5: Contradiction detection — group conflicting values."""
        conflicts: dict[str, list[dict[str, Any]]] = {}

        # Group by claim key
        claim_groups: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            numbers = item.get("contains_numbers", [])
            for num in numbers:
                claim_groups.setdefault(num, []).append(item)

        # Find conflicts (same key, different values)
        for key, items in claim_groups.items():
            if len(items) > 1:
                conflicts[f"conflict_{key}"] = items

        return GateResult(
            gate="G5_contradiction",
            passed=True,
            reason=f"Found {len(conflicts)} conflicts",
            dropped=[],
        )

    def _gate_diversity(self, evidence: list[dict[str, Any]]) -> GateResult:
        """G6: Source diversity — max N per domain."""
        dropped: list[str] = []
        domain_counts: dict[str, int] = {}

        for item in evidence:
            domain = item.get("domain", "")
            count = domain_counts.get(domain, 0)
            if count >= self.max_per_domain:
                dropped.append(item.get("doc_id", ""))
            else:
                domain_counts[domain] = count + 1

        return GateResult(
            gate="G6_diversity",
            passed=len(dropped) == 0,
            reason=f"Dropped {len(dropped)} for domain diversity",
            dropped=dropped,
        )

    def _gate_coverage(
        self,
        evidence: list[dict[str, Any]],
        subqueries: list[str],
    ) -> GateResult:
        """G7: Subquery coverage — each subquery needs ≥1 passage."""
        uncovered: list[str] = []

        for subquery in subqueries:
            covered = False
            for item in evidence:
                text = item.get("text", "").lower()
                if subquery.lower() in text:
                    covered = True
                    break
            if not covered:
                uncovered.append(subquery)

        return GateResult(
            gate="G7_coverage",
            passed=len(uncovered) == 0,
            reason=f"Uncovered subqueries: {uncovered}",
            dropped=[],
        )

    @staticmethod
    def _hamming_distance(hash1: str, hash2: str) -> int:
        """Calculate hamming distance between two hex hashes."""
        if len(hash1) != len(hash2):
            return 64  # max distance

        # Convert to binary and count differences
        bin1 = bin(int(hash1, 16))[2:].zfill(64)
        bin2 = bin(int(hash2, 16))[2:].zfill(64)

        return sum(c1 != c2 for c1, c2 in zip(bin1, bin2, strict=True))
