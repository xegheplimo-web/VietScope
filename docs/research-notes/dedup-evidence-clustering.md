# Dedup + Evidence Clustering — 3 cấp dedupe

> Research note cho Search Hub v3 (SPEC-v3 §8). Mục tiêu: 20 website copy cùng 1 bài Reuters = 1 nguồn độc lập, không phải 20 evidence.

## Vấn đề

Metasearch hiện tại trả URL gốc — 20 site copy cùng 1 nguồn sẽ "đánh lừa" verification (tưởng 20 evidence, thực tế 1 nguồn). Evidence clustering giải quyết điều này.

## 3 cấp dedupe (SPEC-v3 §8)

### Cấp 1: URL dedupe (rẻ nhất, chạy đầu tiên)
```python
def normalize_url(url: str) -> str:
    # lowercase domain, strip trailing slash, strip tracking params
    # utm_*, fbclid, gclid, ref=, source=
    # sort query params (src=1&dst=2 == dst=2&src=1)
```

### Cấp 2: Canonical URL dedupe
- Kiểm tra `<link rel="canonical">` khi fetch (Firecrawl metadata có thể có)
- Redirect chain: `http→https`, `www→non-www` map về 1
- `/article/123` vs `/article/123?page=all` vs `/article/123/` → 1

### Cấp 3: Content fingerprint (hash shingling)
```python
def fingerprint(text: str) -> str:
    # 1. normalize: lowercase, strip punctuation/whitespace
    # 2. shingles: 5-gram word windows
    # 3. hash each shingle (md5/sha1)
    # 4. minhash: giữ 64-128 hash nhỏ nhất → signature
    # 5. Jaccard ≈ % hash trùng → >0.8 là near-duplicate
```
- KHÔNG cần embedding cho cấp này — shingling đủ phát hiện copy nguyên văn (Reuters + site copy)
- O(n) per page, chạy được trên Redis

### Cấp 4 (optional): Semantic near-duplicate
- Dùng embedding cosine > 0.92 — chỉ khi có embedding model (InferenceGateway.embed)
- Bắt được bài "viết lại" (paraphrase) không trùng shingle

## Evidence Cluster

```python
class EvidenceCluster:
    cluster_id: str  # hash của canonical source
    sources: list[str]  # mọi URL trong cluster
    canonical_source: str  # URL gốc (authority cao nhất)
    is_independent: bool  # cluster này có độc lập với cluster khác không
```

**Quy tắc independent-source check:**
- 2 cluster độc lập khi: khác domain gốc AND fingerprint khác nhau AND không cùng syndication network
- Chỉ tính **1 nguồn độc lập** cho mỗi cluster trong verification (SPEC-v3 §9)

## Thuật toán triển khai (không cần thư viện nặng)

1. Chạy URL dedupe → canonical → fingerprint trên **kết quả search** (chưa fetch — dùng title+description làm fingerprint sơ bộ)
2. Sau khi fetch: fingerprint nội dung thật → cluster lại lần 2
3. Lưu cluster trong `EvidenceCluster` (models v2 đã có)

## Khuyến nghị cho Search Hub

1. Tạo `ranking/dedup.py`: normalize_url + canonical + shingling fingerprint (P0 — đơn giản, không cần lib)
2. Tạo `evidence/cluster.py`: build EvidenceCluster từ danh sách Source
3. Fingerprint dùng `hashlib` stdlib — KHÔNG cần minhash lib phức tạp ở MVP (shingle 5-gram + set overlap đủ)
4. Tích hợp vào orchestrator: sau normalize, trước rank (SPEC-v3 §1 pipeline)
5. Verification chỉ đếm cluster độc lập (không đếm từng URL)
