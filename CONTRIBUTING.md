# Contributing

## Setup

```powershell
# 1. Python deps — uv is the package manager (lockfile: search-router/uv.lock)
cd search-router
uv sync --locked            # creates .venv, installs deps incl. dev group

# 2. Git hooks — lefthook (pre-commit lint/secrets, pre-push tests)
winget install evilmartians.lefthook
lefthook install            # from repo root

# 3. Stack for integration work
docker compose up -d
```

## Daily Workflow

```powershell
cd search-router
uv run ruff check --fix . && uv run ruff format .
uv run pytest tests/ -q
```

- **Live / e2e tests** are opt-in: `LIVE=1`, `E2E=1` (see `pytest.ini` markers).
- **Add a dependency:** `uv add <pkg>` (never hand-edit `requirements.txt` —
  it's generated via `uv export`). Note `exclude-newer = "7 days"` in
  `pyproject.toml`: brand-new releases won't resolve for a week on purpose.
- **Regenerate requirements.txt** (host installs use it):
  `uv export --frozen --no-dev --format requirements-txt -o requirements.txt`

## Commits & PRs

- Conventional commits (`feat:`, `fix:`, `ci:`, `docs:`, …) — enforced by
  commitlint via the `commit-msg` lefthook; `cliff.toml` builds the changelog
  from them.
- PR gates that must go green (`.github/workflows/`):
  `quality` (ruff on diff, actionlint, zizmor, hadolint, PR-size, test-floor)
  → `test` (pytest + diff-coverage ≥ 80%)
  → `security`/`sca`/`typecheck` (gitleaks, osv-scanner, pip-audit, pyright)
  → `e2e` (crawl→search on a real compose stack)
- Keep PRs under the size gate (`GATE_PR_MAX_LOC`, see `quality-gates.yml`).
  Bigger change = split it.
- Never delete a test file — the test-floor gate blocks it.

## Repo Layout

`search-router/` is the only first-party Python tree. `firecrawl/` and
`searxng/` are upstream submodules — never lint, format, or edit them in
place; send fixes upstream or wrap them in `search-router/`.

## Reporting Security Issues

See [SECURITY.md](SECURITY.md) — private advisories only, never public issues.
