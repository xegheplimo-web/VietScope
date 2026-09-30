# P18.2 — Optional External Acquisition (Apify-informed)

> Planning note. Queued **sau P17 serving + P17.5 retrieval benchmark +
> P18 coverage/freshness core** — không chen vào P17. Source analysis:
> `apify/apify-mcp-server` + Apify platform pricing/docs (2026-09-25).
> Verdict: **CÓ DÙNG, NHƯNG KHÔNG THÊM VÀO CORE** — một Optional
> External Acquisition Layer trên write path, không phải Search
> Provider mặc định trên read path.

## Apify MCP thực sự là gì

`apify-mcp-server` là MCP gateway tới hệ sinh thái Apify Actors —
không phải một scraper. Agent flow:

```
search-actors → fetch-actor-details → call-actor → get run → get dataset items
```

qua đó truy cập hàng nghìn Actor: Google Search, Google Maps,
Facebook, Instagram, e-commerce, website crawler, web fetch,
RAG browser, social data, lead extraction, …

Điểm hay cho agent research: mỗi Actor tự công bố input schema,
output schema, pricing, README — LLM khám phá tool động được.

## Vì sao không đưa vào Search-Hub core

Query path hiện kiểm soát chặt từng tầng:

```
Query → SourceRouter → Provider Registry → Health/Circuit Breaker
      → Fan-out → Normalize → Canonicalize/Dedup → Evidence
      → Authority/Freshness → Citation
```

Nếu LLM tự `search-actors` → chọn Actor bất kỳ → `call-actor`, ta tạo
một đường vòng quanh SourceRouter và mất quyền kiểm soát: cost, source
authority, legal policy, provenance, latency, failure behavior, dedup,
output schema, quality. Đây là vấn đề kiến trúc, không phải vấn đề
chất lượng Actor.

## "Local" chỉ là MCP process — execution vẫn trên Apify cloud

`npx @apify/actors-mcp-server` chạy stdio local, nhưng Actor inputs/
requests được gửi tới Apify API để chạy:

```
Search-Hub machine → local MCP process → Apify API → Actor → website
```

không phải `machine → local scraper`. Hệ quả: vẫn phụ thuộc cloud +
có chi phí + latency ngoài. Self-host `apify-mcp-server` ≠ self-host
Apify. Hosted `https://mcp.apify.com` còn mới hơn (output-schema
inference); local stdio hạn chế với Actor rental/dynamic Store access.
Nếu mục tiêu là chủ động + miễn phí, custom crawler hiện có giá trị
hơn.

## Chi phí — lý do không dùng cho free realtime search

Apify Free plan ~$5 usage/tháng; CU ~$0.20 / GB·h (Free/Starter);
proxy/storage và một số Actor tính thêm hoặc pay-per-event. Ví dụ
`apify/web-fetch` niêm yết từ ~$1 / 1.000 fetch thành công.

Bài toán hot path: 100.000 search/ngày × 5 URL fetch = 500.000
fetch/ngày ≈ **$500/ngày** chỉ riêng web-fetch — chưa tính Actor khác.
Với mục tiêu user free search, Apify không thể nằm trên hot path mặc
định.

## Nhưng rất hợp cho P18 coverage repair (write path)

```
READ PATH (user realtime)        ACQUISITION PATH (background)
─────────────────────────        ─────────────────────────────
OpenSearch / PostGIS             Coverage Engine
Qdrant / Redis                       ↓
      ↓                          Discovery Queue
 answer fast                  ┌───────┼────────┐
                              ▼       ▼        ▼
                             OSM    own web  Google Maps
                                    crawl    scraper
                                               │
                                        coverage vẫn kém
                                               ▼
                                     Apify FALLBACK
                                               ▼
                                          P15 STAGING
                                               ▼
                                     P16 Entity Resolution
                                               ▼
                                        Canonical Graph
```

Ví dụ P18 phát hiện "cửa hàng camera ở Lục Nam" (coverage_score 0.31,
query_demand HIGH) → background job thử OSM → own crawl → gmaps
scraper → Apify Actor. Kết quả Actor **vào staging, không trả thẳng
cho user** — normalize + resolve + provenance như mọi source khác.

Điểm mấu chốt: **Apify chỉ nằm WRITE PATH, không nằm READ PATH.**
Ngoại lệ duy nhất có thể cân nhắc sau: Deep Research mode khi user
chấp nhận latency/cost, có budget cap.

## Chỗ thứ hai đáng dùng: Hermes / agent nghiên cứu nội bộ

Hermes hỗ trợ phát triển Search-Hub:

```
Hermes ── GitHub / browser / shell
      └── Apify MCP ── web-fetch · Google Maps · social · ecommerce
```

"Tìm xem 20 website này có cấu trúc dữ liệu sản phẩm thế nào",
"lấy thử 100 cửa hàng khu vực này" → Hermes thử nghiệm qua Apify.
Đây là developer/research tool, không ảnh hưởng kiến trúc Search-Hub.

## Adapter contract — không nhúng MCP rải khắp code

Không `agent.call_mcp("apify")` rải rác. Một protocol giống pattern
`PlaceSourceAdapter` của P15:

```
ExternalAcquisitionProvider
  ├── ApifyAdapter
  ├── OwnCrawlerAdapter
  └── MapsAdapter
```

```python
class AcquisitionProvider(Protocol):
    async def discover(
        self,
        request: DiscoveryRequest,
        budget: AcquisitionBudget,
    ) -> list[SourceObservation]: ...
```

`ApifyAdapter` = allowlisted_actor + input mapper + timeout +
max spend + source policy + output validator + staging writer.
Tắt Apify = tắt một adapter — Search-Hub vẫn chạy bình thường.

## Actor nào thử trước (3 loại)

1. **`apify/web-fetch`** — chỉ là fallback cuối khi HTTP → Trafilatura
   → Firecrawl → Playwright đều thất bại. Không gọi trước.
2. **Google Maps / local-business Actor** — chỉ cho coverage repair,
   specific-region discovery, one-off enrichment. Không crawl cả
   Việt Nam bằng Apify — `gosom` scraper + `adapters/gmaps.py` giữ
   vai trò chính.
3. **E-commerce / social Actor** — chỗ Apify có giá trị lớn nhất:
   marketplaces, social pages, reviews, seller/product pages — vùng
   Search-Hub chưa có hạ tầng tự xây (Tiki bị block, Facebook/Instagram
   public pages…). Mua thời gian engineering, không xây dependency
   nền tảng.

## `RAG Web Browser` — không đưa vào default

Search backend Google của Actor này thiên US/English — trang Actor
chính thức cảnh báo truy vấn địa phương có thể trả kết quả Mỹ. Không
hợp vai trò retrieval backend chính cho "Perplexity Việt Nam";
Source Federation hiện tại tốt hơn ở điểm này.

## Caveats khi tích hợp

- **Budget guard của `call-actor` không đủ tin**: `memory`, `timeout`,
  `maxItems`, `maxTotalChargeUsd` tồn tại, nhưng `maxItems` chỉ có
  nghĩa với pay-per-result Actor và `maxTotalChargeUsd` chỉ với
  pay-per-event Actor — repo tự nói các cap này giới hạn cách billing,
  không nhất thiết giới hạn amount of work. Vẫn cần budget guard riêng
  ở Search-Hub.
- **Không bật dynamic discovery cho user-facing path**: default tool
  set (`search-actors`/`fetch-actor-details`/`call-actor`/
  `rag-web-browser`/`web-fetch`) đổi giữa các phiên bản; Actor bên
  thứ ba khác nhau về pricing/quality/privacy/failure. Production =
  explicit allowlist (`tools=apify/web-fetch,…`), không `tools=actors`.
- **Bug hiện hữu**: issue mở 2026-09-24 — `call-actor` với MCP Actor
  forward sai proxied tool name → upstream "Unknown tool" (ví dụ
  `web-fetch` khỏe nhưng agent nhận lỗi giả outage). Không loại Apify,
  nhưng củng cố quyết định: không đặt MCP marketplace động vào
  critical serving path.
- **Telemetry mặc định bật**: cấu hình `TELEMETRY_ENABLED=false`
  (hoặc `?telemetry-enabled=false`) cho dùng nội bộ.
- **Data policy**: requests/Actor inputs đi lên Apify API — không gửi
  dữ liệu private/user-sensitive qua Actor khi chưa có policy rõ.

## Đối chiếu thẳng với hệ thống hiện có

| Chức năng | Search-Hub hiện có | Apify có lợi? |
|---|---|---|
| General web search | SearXNG/providers | Ít |
| Web reader | Trafilatura/Firecrawl/Playwright | Fallback tốt |
| Google Maps acquisition | gosom scraper | Chỉ backup |
| OSM | PBF ingestion | Không |
| Gov/legal crawl | own corpus/crawler | Không thay |
| E-commerce | Chưa sâu | Có lợi |
| Social scraping | Chưa mạnh | Có lợi |
| Difficult JS/anti-bot sites | Có fallback | Có lợi |
| Dynamic unknown sources | Hạn chế | Rất có lợi |
| Free realtime search | Mục tiêu chính | Không phù hợp |
| Agent development | Hermes | Rất phù hợp |

## 6 điều kiện bắt buộc nếu tích hợp production

1. Explicit Actor allowlist — không dynamic discovery.
2. Telemetry disabled.
3. Hard cost budget per-run + per-day (riêng, không tin cap của Actor).
4. Timeout + retry/circuit breaker ở adapter.
5. Output → P15 staging (`place_source_records`), không canonical
   trực tiếp.
6. Source policy/provenance lưu `actor_id` + `run_id` + `observed_at`.

## Decision matrix

| Use case | Quyết định |
|---|---|
| Hermes development tools | ✅ thêm |
| P18 discovery fallback | ✅ thêm |
| Enrichment difficult sites | ✅ thêm |
| Social/ecommerce acquisition | ✅ đáng thử |
| P17 local serving | ❌ không |
| Normal user search | ❌ không |
| Primary web provider | ❌ không |
| Primary maps provider | ❌ không |
| Replacement crawler | ❌ không |
| Dynamic Actor marketplace cho user path | ❌ không |

Vị trí roadmap: **P18.2 — Optional External Acquisition**, sau P18
coverage/freshness core (vì nó là consumer của Discovery Queue). Vai
trò: "con dao đa năng dự phòng" cho social/ecommerce/web khó crawl —
đưa vào luồng tìm kiếm mặc định sẽ vừa tốn tiền, tăng dependency,
vừa phá mô hình free-search/self-controlled.
