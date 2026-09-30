import time
from dataclasses import dataclass, field

from models import SearchBudget as SearchBudgetModel


class BudgetExceeded(Exception):
    pass


@dataclass
class SearchBudget:
    mode: str
    max_queries: int
    max_fetches: int
    max_followups: int
    budget_used: dict = field(
        default_factory=lambda: {
            "queries": 0,
            "fetches": 0,
            "followups": 0,
            "duration": 0.0,
            "tokens": 0,
        }
    )
    _start: float = field(default_factory=time.time, init=False, repr=False)

    @classmethod
    def for_mode(cls, mode: str) -> "SearchBudget":
        presets = {
            "fast": (2, 3, 0),
            "normal": (5, 8, 1),
            "deep": (12, 20, 3),
        }
        q, f, fol = presets.get(mode.lower(), (5, 8, 1))
        return cls(mode=mode, max_queries=q, max_fetches=f, max_followups=fol)

    def _check(self) -> None:
        if (
            self.budget_used["queries"] > self.max_queries
            or self.budget_used["fetches"] > self.max_fetches
            or self.budget_used["followups"] > self.max_followups
        ):
            raise BudgetExceeded(f"Budget exceeded for {self.mode} mode")

    def _consume(self, resource: str, n: int, limit: int) -> None:
        if n < 0:
            raise ValueError("budget consumption must be non-negative")
        current = self.budget_used[resource]
        if current + n > limit:
            raise BudgetExceeded(
                f"{resource} budget exceeded for {self.mode} mode ({current + n} > {limit})"
            )
        self.budget_used[resource] = current + n

    def use_query(self, n: int = 1) -> None:
        self._consume("queries", n, self.max_queries)

    def use_fetch(self, n: int = 1) -> None:
        self._consume("fetches", n, self.max_fetches)

    def use_followup(self, n: int = 1) -> None:
        self._consume("followups", n, self.max_followups)

    def use_tokens(self, n: int) -> None:
        if n < 0:
            raise ValueError("token consumption must be non-negative")
        self.budget_used["tokens"] += n

    def elapsed(self) -> float:
        return time.time() - self._start

    def check(self) -> None:
        self._check()

    @property
    def remaining_queries(self) -> int:
        return max(0, self.max_queries - self.budget_used["queries"])

    @property
    def remaining_fetches(self) -> int:
        return max(0, self.max_fetches - self.budget_used["fetches"])

    @property
    def remaining_followups(self) -> int:
        return max(0, self.max_followups - self.budget_used["followups"])

    def to_model(self) -> SearchBudgetModel:
        """Return the public v2 budget contract with limits and actual usage."""
        duration = self.elapsed()
        self.budget_used["duration"] = duration
        return SearchBudgetModel(
            max_queries=self.max_queries,
            max_fetches=self.max_fetches,
            max_followups=self.max_followups,
            queries_used=self.budget_used["queries"],
            fetches_used=self.budget_used["fetches"],
            followups_used=self.budget_used["followups"],
            tokens_used=self.budget_used["tokens"],
            duration_used=duration,
        )
