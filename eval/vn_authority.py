"""VN authority scorer for the eval harness (P12).

Reuses the router's own table — ``search-router/ranking/authority.py`` —
instead of duplicating it: the scorer imports ``authority_for`` with the
``search-router/`` dir on sys.path. When the package isn't importable
(e.g. eval copied elsewhere), returns ``None`` and callers degrade.

    scorer = load_scorer(vertical="legal")   # None when unavailable
    scorer("vbpl.vn")  # -> 2.5 (official legal source)

Scores follow the router's scale (~0..2.5, higher = more authoritative).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

_SEARCH_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "search-router")


def load_scorer(vertical: str | None = None) -> Callable[[str], float] | None:
    """Return a ``domain -> float`` authority callable, or None if unavailable.

    ``vertical`` pins the intent-dependent lane (e.g. ``legal``, ``market``).
    """
    try:
        path = os.path.abspath(_SEARCH_ROUTER)
        if path not in sys.path:
            sys.path.insert(0, path)
        from ranking.authority import authority_for  # noqa: PLC0415

        return lambda domain: authority_for(domain, vertical=vertical, lang="vi")
    except Exception:
        return None
