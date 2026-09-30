# VietScope Search API — Examples

All examples target `http://localhost:8888`. Set `KEY` to a real key (see
[getting-started.md](./getting-started.md)); when `API_AUTH_ENABLED=false`
the `Authorization` header may be omitted.

```bash
export BASE=http://localhost:8888
export KEY=dsa_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Windows PowerShell: replace `export X=y` with `$env:X="y"` and use
`curl.exe` (or `Invoke-RestMethod`); keep `curl -N` for SSE.

## 1. Create an API key

Out-of-band, on the host (keys live in `hub-postgres`, compose port
`127.0.0.1:5433`):

```bash
python search-router/manage_keys.py create \
    --tenant acme --name "my app" \
    --scopes "search:read,answer:use,research:use,read:use,news:use" \
    --rpm 60 --quota 1000
# → prints key_id, prefix, tenant, full_key (shown ONCE — store it)

python search-router/manage_keys.py list --tenant acme
python search-router/manage_keys.py revoke --prefix dsa_live_Ab3
```

`--test` issues a `dsa_test_…` key; `--quota -1` = unlimited/day.

## 2. Health & discovery

```bash
# Public minimal health → {"status":"ok","version":"3.0.0"}
curl $BASE/v1/health

# Detailed health (needs admin:debug key, or auth disabled)
curl -H "Authorization: Bearer $KEY" $BASE/v1/health

# Capabilities — modes, providers, feature flags (public)
curl $BASE/v1/capabilities

# Provider statuses (admin:debug)
curl -H "Authorization: Bearer $KEY" $BASE/v1/providers
```

## 3. Search — raw mode

```bash
curl -X POST $BASE/v1/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "rust async tokio", "type": "web", "max_results": 5}'
```

Response:

```json
{
  "query": "rust async tokio",
  "type": "web",
  "understanding": {"intent": "current_fact", "entities": ["rust", "async", "tokio"], "language": "en", "time_sensitive": false, "freshness_required": false, "max_age": null, "preferred_sources": ["general"]},
  "results": [{"url": "https://…", "title": "…", "description": "…", "published_at": null, "engine": "brave", "thumbnail": ""}],
  "count": 5
}
```

`type` selects the lane: `web` (default), `news`, `image`, `video`. Image and
video rows carry a non-empty `thumbnail` (the image itself / the preview
frame):

```bash
curl -X POST $BASE/v1/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "phở bò hà nội", "type": "video", "max_results": 5}'
```

```python
import httpx

resp = httpx.post(
    "http://localhost:8888/v1/search",
    headers={"Authorization": f"Bearer {KEY}"},
    json={"query": "rust async tokio", "type": "web", "max_results": 5},
    timeout=30,
)
resp.raise_for_status()
print(resp.json()["count"])
```

## 4. Search — research mode (hybrid lane)

`mode` switches `/v1/search` to the research pipeline; with
`HYBRID_RETRIEVAL_ENABLED=true` the response carries `timings.hybrid`
(`os_hits`, `qdrant_hits`, `fused`, `degraded`, optional `reason`,
`merged`).

```bash
curl -X POST $BASE/v1/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "compare rust and go for CLI tools", "mode": "deep", "max_results": 10}'
```

```json
{
  "query": "compare rust and go for CLI tools",
  "type": "web",
  "mode": "deep",
  "answer": "…",
  "confidence": 0.82,
  "sources": [{"title": "…", "url": "https://…", "score": 0.91, "domain": "…"}],
  "search": {"generated_queries": ["…"], "plan_source": "llm", "raw_results": 87, "unique_results": 42, "pages_read": 12, "followup_rounds": 1, "passages": 20},
  "verification": {"claims_total": 9, "claims_verified": 8, "claims_removed": 1, "all_removed": false},
  "timings": {"analyze": 0.01, "plan": 1.2, "search": 3.4, "hybrid": {"os_hits": 30, "qdrant_hits": 25, "fused": 55, "degraded": false, "merged": 40}, "total": 21.7},
  "understanding": {"…": "…"},
  "results": [{"url": "…", "title": "…", "description": "…", "published_at": null, "engine": "searxng"}],
  "count": 10,
  "citations": [{"claim": "…", "verified": true, "evidence_count": 3}]
}
```

Modes: `fast` | `balanced` | `deep`; aliases `quick`→fast,
`normal`/`auto`→balanced, `research`→deep.

## 5. Answer — SSE stream

`stream: true` on `/v1/answer` returns `text/event-stream`. `curl -N`
disables buffering so events arrive live:

```bash
curl -N -X POST $BASE/v1/answer \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is retrieval augmented generation", "stream": true, "mode": "balanced"}'
```

```
event: init
data: {"query": "what is retrieval augmented generation", "query_id": "srch_4f2ab9c1d8e0", "mode": "balanced"}

event: source
data: {"title": "…", "url": "https://…", "score": 0.88, "domain": "…"}

event: answer.delta
data: {"text": "Retrieval-augmented generation (RAG) …"}

event: citation
data: {"claim": "…", "verified": true, "evidence_count": 2}

event: done
data: {"query_id": "srch_4f2ab9c1d8e0", "coverage": 0.82, "verified": true, "timings": {"total": 18.4}}
```

Non-streaming (default):

```bash
curl -X POST $BASE/v1/answer \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is docker compose", "mode": "fast"}'
```

→ `{"query_id", "answer", "sources", "conflicts", "coverage", "verified", "timings", "citations"}`

httpx + SSE (parse `event:`/`data:` pairs):

```python
import httpx, json

with httpx.stream(
    "POST",
    "http://localhost:8888/v1/answer",
    headers={"Authorization": f"Bearer {KEY}"},
    json={"query": "what is RAG", "stream": True},
    timeout=180,
) as r:
    event = None
    for line in r.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            data = json.loads(line[6:])
            if event == "answer.delta":
                print(data["text"], end="", flush=True)
            elif event == "done":
                print(f"\nverified={data['verified']}")
            event = None
```

## 6. Research — SSE stream

```bash
curl -N -X POST $BASE/v1/research/stream \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "state of local LLM inference 2026", "mode": "deep"}'
```

Event sequence: `init` → `planning`(+`plan`) → `searching`×N →
`fetching` → `fetched` → `verifying` → `evidence` → `source`×N →
`answer.delta`×N → `answer` `{pack}` → `done` `{claims, sources,
coverage}`.

Non-streamed variant returns the whole pack at once:

```bash
curl -X POST $BASE/v1/research \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "compare qdrant and opensearch", "mode": "normal", "max_hops": 2}'
```

→ `EvidencePack`: `{answer, claims[], citations[], sources[], coverage, confidence, budget_used{…}}`

## 7. Read a URL

```bash
curl -X POST $BASE/v1/read \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/docs", "chunk_size": 800, "chunk_overlap": 200}'
```

→ `{url, title, error, passages: [{passage_id, source_id, text, quote_start, quote_end, retrieved_at, metadata{url,title}}]}`

Blocked URLs (localhost, private IPs, `file://`, …) → `422`
`{"detail": "URL blocked by SSRF policy: …"}`.

## 8. News & images

```bash
# News — freshness-aware, fast budget
curl -X POST $BASE/v1/news \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "openai announcements", "max_results": 5}'

# Images — Qdrant corpus first (QDRANT_IMAGES_ENABLED=true), then live web
# backfill (SearXNG images -> DDGS); results=[] + note only when no lane answered
curl -X POST $BASE/v1/images \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "golden gate bridge sunset", "max_results": 5}'
```

Image hits: `{image_id, score, src_url, page_url, alt, caption, title, source}`
where `source` is `corpus` | `searxng` | `ddgs`; hits are deduped by
`src_url`/`page_url` across lanes.

## 9. Verify claims

```bash
curl -X POST $BASE/v1/verify \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "claims": ["Docker Compose runs multi-container apps"],
    "sources": [{"url": "https://docs.docker.com/compose/", "content": "Docker Compose is a tool for defining and running multi-container applications…", "source_id": "s1"}],
    "clusters": [{"cluster_id": "cl1", "sources": ["s1"], "is_independent": true}],
    "max_age_days": 30
  }'
```

→ `[{claim_id, claim_text, status, evidence, verdict, confidence, contradictions, sources_conflict, timestamp}]`
(`status`: `supported | partially_supported | contradicted | insufficient_evidence | outdated | source_conflict`)

## 10. Business search

```bash
curl -X POST $BASE/v1/business/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "coffee shop", "lat": 10.7769, "lon": 106.7009, "radius_km": 2, "category": "cafe", "limit": 10}'
```

→ `{query, lat, lon, radius_km, provider, anchor: null, entities: [{name, category, address, phone, rating, …}], evidence: [urls], count}`
(`provider`: contributing lanes joined by `+` — `geo` PostGIS, `osm` Overpass, `web` web-extract — e.g. `geo+osm+web`; `none` when empty)

Omit `lat`/`lon` (both — 422 if only one is set) to resolve the anchor from the query text:

```bash
curl -X POST $BASE/v1/business/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "nhà thuốc gần chợ Bến Thành", "radius_km": 1, "limit": 10}'
```

→ `{query, lat, lon, radius_km, provider: "osm+web", anchor: {name, lat, lon, display_name, resolved_from: "entity" | "phrase"}, entities, evidence, count}`
(`lat`/`lon` echo the resolved anchor; when nothing resolves they are `null`, `anchor` is `null` and only the `web` lane runs)

## Error examples

```bash
# Missing/invalid key → 401 {"detail":{"error":"missing_or_invalid_api_key"}}
curl -i -X POST $BASE/v1/search -H "Content-Type: application/json" -d '{"query":"x"}'

# Missing scope → 403 {"detail":{"error":"insufficient_scope","required":"research:use"}}
curl -i -X POST $BASE/v1/research -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"query":"x"}'

# Rate limit → 429 {"detail":{"error":"rate_limit_exceeded","rpm":60}}
# Daily quota → 429 {"detail":{"error":"daily_quota_exceeded","quota":1000}}
```
