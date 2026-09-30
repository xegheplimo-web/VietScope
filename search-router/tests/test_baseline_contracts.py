"""P0: BASELINE-V3 contract freeze — drift guard.

Every frozen artifact under ``baseline/`` must byte-match the live build.
A failure means a public contract changed: if the change is intentional,
regenerate with ``python scripts/freeze_baseline.py`` (from search-router/)
and commit the new artifacts in the same PR.
"""

import pytest
from pipeline.baseline_contracts import BASELINE_DIR, BUILDERS, render


@pytest.mark.parametrize("rel", sorted(BUILDERS))
def test_contract_matches_frozen_baseline(rel):
    builder, is_json = BUILDERS[rel]
    path = BASELINE_DIR / rel
    assert path.exists(), f"missing frozen contract baseline/{rel} — run scripts/freeze_baseline.py"
    assert path.read_text(encoding="utf-8") == render(builder, is_json), (
        f"contract drift in baseline/{rel} — if intentional, run "
        "search-router/scripts/freeze_baseline.py and commit the result"
    )
