"""Domain Profiles — learned authority scoring with citation feedback.

Stores per-domain authority scores that adapt based on observed
citation precision from L16.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DomainProfile:
    """Authority profile for a single domain."""

    domain: str
    base_authority: float = 0.5
    authority_by_intent: dict[str, float] = field(default_factory=dict)
    source_type: str = "unknown"
    is_official_for: list[str] = field(default_factory=list)
    avg_change_rate: float = 0.0
    crawl_delay: int = 1
    spam_score: float = 0.0
    ai_content_ratio: float = 0.0
    observed_citation_precision: float = 0.5
    total_served: int = 0
    total_useful: int = 0

    def update_citation_precision(self, precision: float) -> None:
        """Update observed citation precision with exponential moving average."""
        alpha = 0.1  # learning rate
        self.observed_citation_precision = (
            1 - alpha
        ) * self.observed_citation_precision + alpha * precision
        self.total_served += 1
        if precision >= 0.8:
            self.total_useful += 1

    def get_effective_authority(self, intent: str) -> float:
        """Get effective authority for a specific intent."""
        base = self.authority_by_intent.get(intent, self.base_authority)
        # Blend with observed citation precision
        return 0.7 * base + 0.3 * self.observed_citation_precision


class DomainProfileStore:
    """Store and manage domain profiles."""

    def __init__(self, storage_path: str = "domain_profiles.json"):
        self.storage_path = Path(storage_path)
        self._profiles: dict[str, DomainProfile] = {}
        self._load()

    def _load(self) -> None:
        """Load profiles from disk."""
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text())
                for domain, profile_data in data.items():
                    self._profiles[domain] = DomainProfile(**profile_data)
            except Exception as exc:
                logger.warning("Failed to load domain profiles: %s", exc)

    def _save(self) -> None:
        """Save profiles to disk."""
        data = {
            domain: {
                "domain": p.domain,
                "base_authority": p.base_authority,
                "authority_by_intent": p.authority_by_intent,
                "source_type": p.source_type,
                "is_official_for": p.is_official_for,
                "avg_change_rate": p.avg_change_rate,
                "crawl_delay": p.crawl_delay,
                "spam_score": p.spam_score,
                "ai_content_ratio": p.ai_content_ratio,
                "observed_citation_precision": p.observed_citation_precision,
                "total_served": p.total_served,
                "total_useful": p.total_useful,
            }
            for domain, p in self._profiles.items()
        }
        self.storage_path.write_text(json.dumps(data, indent=2))

    def get_profile(self, domain: str) -> DomainProfile:
        """Get or create profile for domain."""
        if domain not in self._profiles:
            self._profiles[domain] = DomainProfile(domain=domain)
        return self._profiles[domain]

    def update_citation_feedback(self, domain: str, precision: float) -> None:
        """Update citation precision feedback for domain."""
        profile = self.get_profile(domain)
        profile.update_citation_precision(precision)
        self._save()

    def get_authority(self, domain: str, intent: str) -> float:
        """Get effective authority for domain + intent."""
        profile = self.get_profile(domain)
        return profile.get_effective_authority(intent)
