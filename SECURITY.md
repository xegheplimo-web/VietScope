# Security Policy

VietScope / Search-Hub is a self-hosted stack. We take vulnerability reports
seriously and triage them ahead of feature work.

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main` branch | ✅ |
| Released tags | ✅ latest tag only |
| Forked/vendor trees (`firecrawl/`, `searxng/`) | ❌ report upstream |

## Reporting a Vulnerability

**Do not open a public issue for a vulnerability.**

Use GitHub's private reporting instead:

1. Go to **Security → Advisories → Report a vulnerability** on this repo, or
2. Open a draft security advisory via
   `https://github.com/xegheplimo-web/VietScope/security/advisories/new`

Please include:

- affected component / endpoint (e.g. `search-router /answer`, Firecrawl proxy)
- reproduction steps or PoC
- impact assessment (RCE, auth bypass, SSRF, secret exposure, …)
- suggested remediation if you have one

## What to Expect

- **Acknowledgement:** within 72 hours
- **Triage & severity call:** within 7 days
- **Fix or mitigation plan:** within 30 days for confirmed High/Critical issues
- Credit in the release notes unless you prefer to stay anonymous

## Scope Notes

- SSRF findings are in scope for `search-router` — the `/fetch` and crawler
  lanes intentionally fetch arbitrary URLs, but must honour the netguard
  rules (private-range blocking, scheme allowlist) in `search-router/security/`.
- Exposed secrets in git history are always in scope.
- Denial-of-service against the public demo endpoints is in scope only if it
  demonstrates a missing rate-limit or resource bound, not raw traffic volume.
- Issues in upstream engines (SearXNG, Firecrawl) should be
  reported to those projects; we track their fixes via submodule updates.

## Hardening Baseline

Current enforcement (see `.github/workflows/`):

- gitleaks secret scanning on every PR + pre-commit hook
- pip-audit + osv-scanner dependency scanning
- Trivy image scan on built containers
- CodeQL SAST
- Dependency min-release-age of 7 days (uv `exclude-newer` + Dependabot cooldown)
