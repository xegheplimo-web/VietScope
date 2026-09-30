# Budget + Stopping Criteria — Kiểm soát tài nguyên research loop

> Research note cho Search Hub v3 (SPEC-v3 §11-12). Sếp đánh giá budgeting 3/10 — thấp nhất, cần ưu tiên.

## Vấn đề

Search agent recursive có thể: query → 8 queries → 100 results → scrape 50 URLs → follow-up → thêm 100 URLs → tốn tài nguyên cực nhanh. KHÔNG có giới hạn = thảm họa cost/latency.

## SearchBudget (models v2 đã có từ Kilo)

```python
class SearchBudget(BaseModel):  # đã merge
    max_queries: int = 5
    max_results: int = 10
    max_fetches: int = 8
    max_tokens: Optional[int] = None
    max_duration: Optional[int] = None
    max_followups: int = 1
    max_cost: Optional[float] = None
    cost_used: float = 0.0
```

## Defaults theo mode (SPEC-v3 §11)

```python
BUDGETS = {
    "fast": SearchBudget(max_queries=2, max_results=5, max_fetches=3, max_followups=0),
    "normal": SearchBudget(max_queries=5, max_results=10, max_fetches=8, max_followups=1),
    "deep": SearchBudget(max_queries=12, max_results=20, max_fetches=20, max_followups=3),
}
```

## Budget tracking (bắt buộc)

```python
class BudgetExceeded(Exception): ...

class BudgetController:
    def __init__(self, budget: SearchBudget): ...
    def spend_query(self):  # ++queries_used, raise nếu > max_queries
    def spend_fetch(self):  # ++fetches_used, raise nếu > max_fetches
    def spend_tokens(self, n):  # ++tokens_used, check max_tokens
    def check_duration(self):  # time.time() - start > max_duration → raise
    def remaining(self) -> SearchBudget  # snapshot còn lại
```

Mọi vòng lặp (search, fetch, follow-up) PHẢI đi qua controller — không tự do gọi provider.

## Stopping criteria (SPEC-v3 §12)

```python
def should_stop(state) -> bool:
    return (
        state.coverage >= 0.9  # % câu hỏi đã có evidence
        and state.confidence >= 0.85  # trung bình confidence claims
        and state.critical_corroborated  # mọi critical claim có >= 2 nguồn độc lập
        and not state.has_unresolved_conflict
    )
```

**Ví dụ**: coverage=0.92, confidence=0.88, corroborated=true → STOP.

## Follow-up search heuristics

Chỉ follow-up khi (SPEC-v3 §1):
- `INSUFFICIENT_EVIDENCE` (thiếu nguồn)
- `SOURCE_CONFLICT` (mâu thuẫn chưa phân xử)

Follow-up plan: sinh query bổ sung từ claim thiếu (query expansion), budget giảm dần sau mỗi vòng. Tối đa `max_followups` lần — hết budget là dừng, trả kết quả hiện có kèm `confidence` thấp.

## Token/cost control

- `max_tokens`: cắt context synthesis (top-N chunks theo budget)
- `max_cost`: ước tính cost = tokens_in * price_in + tokens_out * price_out (InferenceGateway trả giá)
- fast mode mặc định KHÔNG gọi LLM synthesis nếu không có key (fallback extractive — đã có trong pipeline/rag.py)

## Khuyến nghị cho Search Hub

1. Tạo `core/budget.py`: BudgetController + BudgetExceeded (Devin đang làm — spec này cho reference)
2. Mọi provider call trong orchestrator PHẢI qua budget.spend_*()
3. Response EvidencePack phải gồm `budget_used` (models v2 đã có field)
4. default_mode từ config (fast cho MCP interactive, deep cho /v1/research)
5. Timeout tổng: fast 30s, normal 90s, deep 300s (bảo vệ latency)
