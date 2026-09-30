# Authority Scoring — Xếp hạng độ tin cậy nguồn

> Research note cho Search Hub v3 (SPEC-v3 §6). Kiến thức tổng hợp từ cách Exa, Kagi, Perplexity, Tavily xử lý + kinh nghiệm vận hành.

## Vì sao cần

Search không thể chỉ xếp theo semantic similarity. Hai trang cùng nói "BTC lên 100k" — một từ blog SEO vô danh (authority 0.3), một từ Reuters (authority 0.9) — phải xếp khác nhau.

## Phân loại source_type (SPEC-v3 §6)

| Loại | Score | Ví dụ |
|---|---|---|
| official docs | 1.00 | docs.docker.com, developer.mozilla.org |
| government | 1.00 | .gov, .gouv.fr, chinhphu.vn |
| research paper | 0.95 | arxiv.org, papers.ssrn.com, ieee.org |
| major publication | 0.90 | reuters.com, apnews.com, vnexpress.net, techcrunch.com |
| vendor website | 0.85 | openai.com, github.com (repo chính chủ) |
| specialist blog | 0.75 | blog của chuyên gia ngành, substack nổi tiếng |
| forum | 0.60 | stackoverflow.com, HN |
| reddit | 0.55 | reddit.com |
| unknown SEO site | 0.30 | trang content-farm, không rõ tác giả |

## Heuristics triển khai (không cần ML)

1. **TLD whitelist**: `.gov`, `.edu`, `.mil` → government/research (cần check theo quốc gia)
2. **Domain reputation list**: hard-code ~200 domain nổi tiếng → score tương ứng (nhỏ, đủ dùng cho personal engine)
3. **Pattern detection**:
   - Domain chứa "docs." → official
   - `github.com/<org>/<repo>` → vendor (repo official) hay specialist (repo cá nhân)
   - Domain đuôi `.blog`, `medium.com`, `substack.com` → specialist blog
   - `reddit.com`, `quora.com` → community
4. **Fallback**: không match gì → unknown_seo (0.30) — an toàn, không over-trust

## Kết hợp vào ranking

```python
final_score = (0.45 * semantic_relevance)   # reranker hiện có
            + (0.25 * source_authority)      # bảng trên
            + (0.15 * freshness)             # xem freshness-temporal.md
            + (0.15 * query_intent_match)    # domain có khớp intent không
```

## Khuyến nghị cho Search Hub

1. Tạo `ranking/authority.py`: `classify_source_type(domain) -> SourceAuthority` + `authority_score(domain) -> float`
2. Seed domain list ~100-200 domain phổ biến VN + quốc tế (tech, crypto, news)
3. Lưu score vào `SearchResult.authority_score` (models v2 đã có sẵn field)
4. Reranker hiện tại (`pipeline/reranker.py`) chỉ dùng semantic — **cần blend authority vào** (đừng thay thế, blend 0.25 weight)
