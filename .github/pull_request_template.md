## Summary

<!-- What changed and why. Link the issue/spec if one exists. -->

## Gates

- [ ] `quality` job green (ruff on diff, actionlint, zizmor, hadolint, size)
- [ ] `test` job green — **diff coverage ≥ 80%** on changed lines
- [ ] No new secrets (gitleaks) — `.env` values stay out of the diff
- [ ] `requirements.txt` regenerated via `uv export` if deps changed
- [ ] PR under the size gate or explicitly split into stacked PRs
- [ ] Docs updated (AGENTS.md / STATUS.md / docs/api/*) if behavior changed

## Test Plan

<!-- Commands run locally + expected output. Note if LIVE=1/E2E=1 needed. -->

## Risk

<!-- What breaks if this is wrong? Rollback plan for ops changes. -->
