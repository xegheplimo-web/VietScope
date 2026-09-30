# Freshness / Temporal — Xử lý câu hỏi nhạy cảm thời gian

> Research note cho Search Hub v3 (SPEC-v3 §7). Gồm kiểm chứng thực tế SearXNG local.

## Kiểm chứng thực tế (2026-08-15, SearXNG local :8080)

**Kết quả test:**
- `GET /search?q=react+19&format=json` → **KHÔNG trả JSON** (trống — format=json cần cấu hình hoặc bị chặn)
- `POST /search` qua Search Router (type=web) → 2 results, **`published_at: None`** cho cả 2
- `POST /search` (type=news) → 3 results, **`published_at: None`** — tin Bitcoin không có ngày xuất bản!

**Kết luận: provider hiện tại KHÔNG lấy được publishedDate** dù code có đọc `r.get("publishedDate")` (providers/searxng.py:47). Có 2 khả năng: (1) SearXNG JSON API không trả field này, (2) response format khác. **Cần kiểm tra response thô của SearXNG.**

## Detect intent cần freshness (Query Understanding)

```python
FRESHNESS_KEYWORDS = [
    "hôm nay",
    "mới nhất",
    "latest",
    "today",
    "hiện tại",
    "current",
    "giá vàng",
    "CEO",
    "release",
    "vừa ra mắt",
]
# heuristic: có keyword → time_sensitive=True, freshness_required=True, max_age="24h"
```

Ví dụ từ SPEC-v3:
- "GPT mới nhất là gì?" → current_fact, freshness_required=true
- "giá vàng hôm nay" → current_fact, max_age=24h
- "React có gì mới?" → current_fact (news-ish)
- "cách cài postgres" → howto, freshness_required=false (kiến thức ổn định)

## Cách SearXNG hỗ trợ

SearXNG có tham số `time_range` trong UI (day/week/month/year) — truyền qua query param:
```
/search?q=react&time_range=day
```
**NHƯNG** cần kiểm tra: (1) có hoạt động qua JSON API không, (2) engine nào hỗ trợ (không phải engine nào cũng có time filter). News engine (`categories=news`) là nguồn chính cho freshness.

## Fallback khi không có publishedDate

1. **Ưu tiên category news** cho query freshness (news engines thường trả tin mới)
2. **Heuristic domain**: domain news (reuters, vnexpress) + query có "mới nhất" → chấp nhận kết quả không có date nhưng ưu tiên
3. **Crawl để lấy date**: Firecrawl scrape trả metadata có thể chứa datePublished (JSON-LD) — dùng cho top-3 results
4. **Hạ điểm** nguồn có content cũ (nếu parse được ngày từ markdown)

## Khuyến nghị cho Search Hub

1. **Fix provider**: debug response thô SearXNG để lấy `publishedDate` — thêm `time_range` param khi query freshness (P0)
2. Thêm `time_range` vào SearchQuery → searxng provider truyền qua khi `freshness_required=true`
3. Parse `datePublished` từ Firecrawl metadata (JSON-LD) cho top results
4. Query Understanding: keyword heuristic + optional LLM classify (InferenceGateway.classify)
