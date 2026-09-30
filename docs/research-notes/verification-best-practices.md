# Verification Best Practices — Claim verification 6 trạng thái

> Research note cho Search Hub v3 (SPEC-v3 §9). Cách Exa/Perplexity xử lý + triển khai thực tế.

## Vấn đề

Verification hiện tại chỉ là regex `[1]` parse câu → nối source. Chưa có: 6 trạng thái verdict, independent-source check, cross-check số liệu. Sếp đánh giá 7/10 — cần nâng lên production.

## 6 trạng thái verdict (SPEC-v3 §9)

```python
class VerdictStatus(str, Enum):  # đã có trong models v2 (Kilo làm)
    SUPPORTED = "supported"  # ≥2 nguồn độc lập khớp
    PARTIALLY_SUPPORTED = "partially_supported"  # 1 nguồn mạnh, hoặc 1 phần claim đúng
    CONTRADICTED = "contradicted"  # nguồn A nói X, nguồn B nói ngược X
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # không đủ nguồn để kết luận
    OUTDATED = "outdated"  # nguồn cũ so với freshness_required
    SOURCE_CONFLICT = "source_conflict"  # nguồn mâu thuẫn chưa phân xử được
```

## Independent-source check (quan trọng nhất)

KHÔNG được: LLM đọc 1 trang → "supported". Phải:

```python
def check_independent(evidence: list[EvidenceCluster]) -> bool:
    # Đếm cluster độc lập (xem dedup-evidence-clustering.md)
    # >= 2 cluster độc lập khớp → corroborated
    # 1 cluster → không đủ, cần follow-up search
```

**Bẫy**: 5 trang đều trích Reuters = 1 nguồn. Verification phải dùng EvidenceCluster.is_independent, không dùng raw URL count.

## Verdict logic (deterministic, không cần LLM cho quyết định cuối)

```python
def verdict(claim, clusters) -> ClaimVerification:
    independent = [c for c in clusters if c.is_independent]
    supporting = [c for c in independent if c.supports(claim)]
    contradicting = [c for c in independent if c.contradicts(claim)]
    if supporting >= 2:
        return SUPPORTED
    if contradicting and supporting:
        return SOURCE_CONFLICT
    if contradicting:
        return CONTRADICTED
    if supporting == 1:
        return PARTIALLY_SUPPORTED
    return INSUFFICIENT_EVIDENCE
```

`supports()/contradicts()`: so sánh passage trích xuất với claim (embedding cosine hoặc keyword overlap + LLM extraction optional).

## Claim extraction từ answer

1. Tách câu mang thông tin (có số liệu, sự kiện, khẳng định) — bỏ câu mở đầu/liên kết
2. Mỗi claim → claim_id (c17...)
3. Citation passage-level: `quote_start/quote_end` trỏ vào passage thật (SPEC-v3 §10)

## Budget cho verification

- fast: verify top-5 claims, tối đa 3 cluster/claim
- normal: top-8 claims, 5 cluster/claim
- deep: tất cả claims, 8 cluster/claim
- Follow-up search chỉ khi: INSUFFICIENT_EVIDENCE hoặc SOURCE_CONFLICT (SPEC-v3 §12)

## Khuyến nghị cho Search Hub

1. Tạo `evidence/verifier.py`: verdict deterministic + independent check (P0)
2. Tạo `evidence/claims.py`: extract claims từ answer (regex câu + optional LLM)
3. Dùng EvidenceCluster từ clustering — KHÔNG đếm URL thô
4. `/v1/verify` endpoint: nhận claims → trả verdicts (SPEC-v3 §2)
5. LLM chỉ dùng để: trích claim, so sánh passage semantics — quyết định cuối deterministic
