# OpenAI-compatible API (VietScope /v1)

Search-Hub exposes a thin OpenAI-compatible gateway so any client that
understands `chat/completions` (Open WebUI, the official `openai` Python
SDK, LangChain, …) can use it with just a base URL and an API key.

The adapter owns wire translation only — routing, retrieval, synthesis and
citation verification run in the same engine as `/v1/answer`. Clients never
choose a tool; the router decides search vs answer vs research per query.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | Model list (currently `duyai-search`) |
| `POST` | `/v1/chat/completions` | Chat completions, `stream` true/false |

`duyai-search` routes to the research/answer pipeline (`mode="balanced"`).

## Auth

Same API-key system as the rest of `/v1` (`API_AUTH_ENABLED`). The chat
endpoints require the `chat:use` scope — a key with `{"scopes": ["chat:use"]}`
(or `*`) works. `GET /v1/models` also accepts `chat:use`.

```bash
KEY=dsa_live_...   # an api key with chat:use scope
BASE=http://localhost:8888

curl $BASE/v1/models -H "Authorization: Bearer $KEY"
```

## curl

```bash
curl -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "duyai-search",
    "messages": [{"role": "user", "content": "Giá vàng hôm nay bao nhiêu?"}],
    "stream": false
  }'
```

Response is a standard `chat.completion`. Sources are embedded in the
assistant content as a human-readable footer (`**Sources:**` + numbered
`[n] title — url` lines) so every OpenAI client renders them; the full
structured `citations`/`verification`/`timings` payload rides in a
`search_hub` extension field that stock clients ignore.

## Python (openai SDK)

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8888/v1", api_key=os.environ["KEY"])

resp = client.chat.completions.create(
    model="duyai-search",
    messages=[{"role": "user", "content": "Nghị định mới về hóa đơn điện tử?"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

## Open WebUI

Settings → Connections → Add OpenAI API:

- **URL**: `http(s)://<search-hub-host>/v1`
- **Key**: a `dsa_live_`/`dsa_test_` key with `chat:use` scope

Model `duyai-search` then appears in the model picker.

## Parameter policy

| Param | Behavior |
|---|---|
| `model`, `messages`, `stream` | Honored |
| `max_tokens` / `max_completion_tokens` | Content budget ~4 chars/token → `finish_reason: "length"`, stream and non-stream |
| `stop` | Post-hoc truncation at first match, `finish_reason: "stop"` |
| `temperature`, `top_p`, `seed`, `presence_penalty`, `frequency_penalty`, `user`, `stream_options`, `tools`, `tool_choice` | Accepted, **ignored** — the pipeline has no knobs for them today; `tools` must be tolerated because Open WebUI's default builtin-tools capability sends a non-empty array on every request |
| forced `tool_choice` (`"required"` or a specific function), `response_format` ≠ `text`, `n > 1` | Rejected — `400 unsupported_parameter` (a forced tool call cannot be honestly produced) |
| unknown fields | Ignored |

`system` messages are passed to the conversation resolver as context turns
(they help follow-up resolution) but are never injected into the retrieval
or synthesis pipeline — there is no system-prompt channel by design.

## Notes / limits

- `usage` reports zeros — the engine tracks timings, not token counts yet.
- All errors on this surface — auth, validation, upstream — return the
  OpenAI `{"error": {message, type, param, code}}` shape.
- Stream and non-stream share the same `OPENAI_TIMEOUT_S` deadline and
  content-budget semantics; a client disconnect cancels the pipeline run.

## Env

| Var | Default | Meaning |
|---|---|---|
| `OPENAI_MODELS` | `duyai-search` | Comma-separated model ids |
| `OPENAI_TIMEOUT_S` | `300` | Request deadline, stream and non-stream |
| `API_AUTH_ENABLED` | `false` | Gate /v1 behind API keys |

## Open WebUI networking note

The compose stack publishes the router on loopback only
(`127.0.0.1:8888`), so `host.docker.internal:8888` is NOT reachable from
inside another container. Attach Open WebUI to the compose network and
point it at the service name instead:

```bash
docker run -d --name open-webui --network search-hub_search-hub-net \
  -p 3000:8080 ghcr.io/open-webui/open-webui:main
# then: URL http://search-router:8888/v1  ·  key: a chat:use key
```

or seed it non-interactively with `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`.
