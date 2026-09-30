# A10 — Document Intelligence Pipeline (WeKnora-informed)

> Planning note. Queued **after P17 serving + P17.5 hardening** — không
> chen vào P17. Source analysis: Tencent/WeKnora v0.8.2 (MIT,
> released 2026-09-24). Verdict: học Document/RAG/Knowledge pipeline;
> **không** lấy Agent/MCP/Sandbox/Wiki, không vendor WeKnora vào
> production — benchmark và port các module/pattern.

## WeKnora vs Search-Hub — hai bài toán khác nhau

- WeKnora = Enterprise Knowledge / RAG platform: "có 50.000 tài liệu
  công ty, tìm và hỏi đáp trong chúng" — RAG, agent, wiki, knowledge
  graph, document parsing, hybrid retrieval, rerank, data-source sync,
  RBAC, tracing, task queue, MCP, sandbox.
- Search-Hub = Live Search / Answer / Vietnam Intelligence: "tìm mọi
  nguồn hiện có về chủ đề này ở Việt Nam, kiểm tra mới nhất, so sánh
  nhiều nguồn, trả lời có citation". Hai bài toán giao nhau nhưng không
  giống nhau — không dùng WeKnora làm core.

## Vị trí trong bức tranh repo review

| Repo | Thứ đáng lấy |
|---|---|
| MindSearch | Research DAG / search graph |
| Zvec | Retrieval/vector backend benchmark |
| LocalAI | Local inference runtime |
| NVIDIA Model Optimizer | Quantization/optimization |
| WeKnora | Document/RAG/KB pipeline |

## Phần WeKnora mạnh hơn Search-Hub hiện tại: file documents

Định dạng covered: PDF, DOC/DOCX, PPT/PPTX, XLS/XLSX, EPUB, MHTML,
XMind, ảnh, audio, HTML.

Parsers: simple parser, anydoc, MinerU, PaddleOCR-VL, OpenDataLoader,
multimodal/VLM.

```
FILE
  ↓ Parse
  ↓ Structure preservation
  ↓ Chunk
  ↓ Parent/child chunk
  ↓ Embedding
  ↓ Vector + BM25
  ↓ RRF
  ↓ Rerank
  ↓ MMR
  ↓ Citation
```

Search-Hub hiện mạnh ở WEB document (Trafilatura / Firecrawl /
Playwright) — HTML web và file Office/PDF là hai thế giới khác nhau.
Tách lane:

```
WEB DOCUMENT  → Trafilatura / Firecrawl / Playwright   (existing)
FILE DOCUMENT → Document Parser Pipeline (new)
                 ├── anydoc
                 ├── MinerU
                 ├── PaddleOCR-VL
                 └── fallback
```

## Retrieval composite — reference, không copy nguyên

WeKnora: Vector + BM25 → RRF fusion → Reranker → composite score
(rerank 0.6 / retrieval 0.3 / source weight 0.1) → MMR → final evidence.

Search-Hub đã có hybrid retrieval + RRF + reranker. Thứ đáng học là
operational details:

- BM25 score normalization
- rerank rejection
- keyword retry
- retrieval mode fallback
- output token budget
- document-local context

## Two-stage reading: `search_knowledge` → `read_document`

Pattern sạch đáng lấy nhất. Thay vì search → nhét ~50 chunk vào context
→ LLM đọc hết:

```
Agent
  ↓ search_knowledge(query)
  ↓ top candidate chunks
  ↓ read_document(chunk/document)
  ↓ đọc sâu context quanh đúng chỗ cần
```

Map vào Search-Hub deep research:

```
search_sources()
  ↓ evidence IDs
  ↓ read_source(source_id, passage_id, context=3)
```

Giảm token đáng kể — agent đọc thêm context quanh chunk cần thiết thay
vì nuốt toàn bộ result set.

## Structure-aware + parent-child chunking

```
Điều 10
  ├── Khoản 1
  ├── Khoản 2
  ├── Khoản 3   ← query match ở đây
  └── Khoản 4
```

- small chunk → retrieval precision
- parent chunk (Điều 10 + Khoản 3) → generation context

Cực hợp với luật, nghị định, hợp đồng, báo cáo, technical docs.

## Legal/Gov corpus — fit trực tiếp với vertical hiện có

```
Government document (nghị định/thông tư PDF, luật DOCX, công báo,
quyết định, báo cáo Excel)
  ↓ Document Parser
  ↓ preserve heading / article / chapter / tables / page references
  ↓ semantic chunks
  ↓ OpenSearch + Qdrant
  ↓ Search-Hub
```

Đúng hướng own Vietnamese corpus + Legal/Gov vertical + OpenSearch/
Qdrant đã chọn.

## Data Source Sync pattern

Đáng học không phải connector cụ thể (Notion/Confluence/GitLab/Feishu/
DingTalk/Yuque) mà là kiến trúc:

```
External Source
  ↓ sync cursor
  ↓ incremental changes
  ↓ parse
  ↓ index
  ↓ version history
```

Áp dụng cho: RSS báo VN, gov portals, legal datasets, company sites,
merchant feeds. (P15 ingestion runner đã có run/checkpoint/resume —
pattern này nâng lên thành scheduled incremental sync.)

## Knowledge graph — deferred

WeKnora hỗ trợ Neo4j GraphRAG (chunk search + entity search → merge →
dedup → rerank). **Không thêm Neo4j lúc này**: Search-Hub đang xây
Postgres entity graph (LegalEntity → Business → Place → Product →
Inventory) + P16 resolution — thêm Neo4j = hai nguồn sự thật. Chỉ cân
nhắc lại nếu có query thật cần traversal kiểu `Công ty A sở hữu Công ty
B đầu tư Dự án C liên quan Quyết định D`.

## Explicitly NOT taken

ReAct agent, Skills, MCP, BrowserSkill, Docker sandbox, long-term
memory, wiki agents — Search-Hub đã có orchestrator; chèn thêm sẽ lặp
planner/retrieval/RAG/memory/agent hai lần. Không làm
`Search-Hub → WeKnora Agent → Search-Hub retrieval`.

## Phase plan — A10 (sau P17/P17.5)

- **A10.1 Document parser benchmark** — anydoc vs MinerU vs
  PaddleOCR-VL vs existing parser trên corpus pháp luật/chính phủ/
  doanh nghiệp thật.
- **A10.2 Structure-aware chunking** — heading, article, table,
  parent-child, page anchors.
- **A10.3 Document retrieval** — OpenSearch BM25 + Qdrant semantic +
  RRF + rerank + MMR.
- **A10.4 Document reading API** — `search_document()`,
  `read_document()`.
- **A10.5 Legal/Gov corpus integration.**

## Nếu chỉ chọn 3 thứ (priority)

1. Structure-aware + parent-child chunking
2. `search_knowledge` → `read_document` two-stage retrieval
3. Document parsing pipeline cho PDF/Office/OCR

Ba phần này bổ sung nhiều hơn rất nhiều so với Agent, MCP hay Wiki, và
phù hợp trực tiếp với hướng own Vietnamese corpus + Legal/Gov/business
data + search chuyên sâu.
