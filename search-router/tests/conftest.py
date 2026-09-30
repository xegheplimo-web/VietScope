"""Test bootstrap: make the search-router package importable from any cwd.

Running ``python -m pytest`` from the worktree root only adds the root to
``sys.path``, so ``import config`` / ``import providers`` fail for every test.
Inserting the ``search-router/`` directory keeps tests runnable both from the
worktree root (``pytest search-router/tests``) and from inside ``search-router/``
(``pytest tests/``).
"""

import sys
from pathlib import Path

import pytest

_SEARCH_ROUTER_DIR = Path(__file__).resolve().parents[1]
if str(_SEARCH_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_SEARCH_ROUTER_DIR))


@pytest.fixture(autouse=True)
def _isolate_answer_cache(monkeypatch):
    """Give every test a private memory-only answer cache.

    ``pipeline.semantic_cache.semantic_cache`` is a process-wide singleton:
    without isolation a response cached by one test is served to a later
    identical query in another test (the pipeline then never runs). A fresh
    instance with Redis disabled makes every test hermetic.
    """
    import pipeline.semantic_cache as sc

    cache = sc.SemanticCache()
    cache._redis_enabled = False
    monkeypatch.setattr(sc, "semantic_cache", cache)
