# BASELINE-V3 — frozen contracts

P0 of the VN Expansion Program. This directory freezes the public contracts
of search-hub v3 so later phases cannot silently change them. Every file
here is **generated** — never edit by hand.

| Artifact | Contract |
|---|---|
| `api.openapi.json` | FastAPI OpenAPI surface (`main.app.openapi()`) |
| `schemas/*.json` | Pydantic JSON schemas — `SearchResult`, `ProviderResult`, `CitationV2`, `SourceResult` |
| `db/schema.sql` | `db/init.sql` + `db/migrations/*.sql` concatenated in apply order |
| `opensearch/mappings.json` | `opensearch/mappings/*.json` as `{index: mapping}` |
| `qdrant/collections.json` | `qdrant.collections.ALL_COLLECTIONS` |
| `sse/events.json` | canonical SSE event names + legacy→canonical renames |
| `results.json` | metadata only (timestamp, git SHA, collected test count) — not drift-checked |

## Rule

Any diff vs. the live build fails `tests/test_baseline_contracts.py` in CI.
If the contract change is intentional:

```bash
cd search-router
python scripts/freeze_baseline.py      # or --check to preview drift
```

and commit the regenerated artifacts in the same PR. A phase must never
make the baseline worse — extend, don't regress.
