# Quality Workflow — Search Hub

Tài liệu tham chiếu quy trình chất lượng chuẩn (rút gọn từ bộ pipeline 31 stage /
8 nhóm của *modern-code-quality-workflow*), đã map sang stack Python/FastAPI của
repo. Đọc trong ~10 phút. Bảng gap-analysis ở cuối cho biết Search-Hub đang ở đâu.

Nguyên tắc xuyên suốt: **gate trên diff, không gate trên cả repo** — nợ kỹ thuật
cũ không được phép chặn code mới, nhưng code mới không được mang nợ mới. Đo
trước, siết sau: bước mới luôn chạy ở chế độ "visibility" trước khi thành gate.

## 1 · GUARD — Chuẩn hoá & guardrail

> Không có chuẩn thì mọi bước kiểm sau chỉ là ý kiến cá nhân.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| conventions-as-code | Quy ước đặt tên/cấu trúc phải nằm trong file máy cưỡng chế được | Lint + arch-rule fail = fail; không disable rule nếu thiếu lý do kèm ticket | `ruff check` + `ruff format`; import-linter/tach cho layer rule |
| definition-of-done | DoD nói rõ code do AI sinh cũng phải đạt chuẩn người | PR không đạt DoD không vào review queue | PR template có checkbox DoD |
| git-workflow | PR nhỏ — biến số mạnh nhất của tỷ lệ lỗi lọt | Trung bình < 400 LOC/PR; nhánh sống < 3 ngày | PR-size gate script (`GATE_PR_MAX_LOC`) |

## 2 · LOCAL — Shift-left trên máy dev

> Lỗi sửa ở đây rẻ gấp 30–100 lần so với production.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| inner-loop | Vòng lặp 5 giây: type + lint + format-on-save | Không diagnostic đỏ trong file đang sửa trước khi commit | pyright/ty trong IDE + ruff format-on-save |
| hook-budget | Hook chỉ chứa thứ chạy < 3s: format, secret, size, msg | Test KHÔNG nằm trong pre-commit | `lefthook` + `gitleaks protect --staged` |

## 3 · BUILD — Môi trường & artefact

> CI phải dựng lại được thế giới của dev, không phải một phiên bản khác.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| hermetic-env | Dev/CI dùng chung ảnh; pin toolchain một nguồn | CI xoá cache vẫn pass; không cài ngoài requirements trong CI | `setup-python` cache trên requirements.txt; Docker compose |
| compile-artefact | Một artefact build một lần, đẩy đi mọi môi trường | Không type error; image có SBOM/attestation | `docker buildx --sbom`; pyright ở bước build |

## 4 · TEST — Kiểm thử tự động

> Test là tài liệu thực thi được — và là thứ duy nhất chứng minh logic.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| fast-layer | Unit test logic, < 90s, chạy không cần mạng/DB | Fail phải nói được "cái gì đổi" | `pytest -q -p no:cacheprovider` |
| diff-coverage | Coverage trên lines đã đổi, không cả repo | Baseline trước → gate ≥80% branch sau | `pytest --cov --cov-branch`; diff-cover/Codecov patch |
| testcontainers | Integration với hạ tầng thật trong container | Đổi schema/service ⇒ bắt buộc integration test | testcontainers (redis/postgres), httpx |
| schema-first | OpenAPI là hợp đồng — sinh validate + fuzz | Không breaking change ngoài flag api-major | FastAPI sinh OpenAPI + Schemathesis + oasdiff |
| critical-journeys | E2E ít mà đau: 5–20 kịch bản mất tiền nếu hỏng | < 10 phút (shard); flaky < 0.5%, quarantine có hạn | Playwright |
| test-the-tests | Mutation chứng minh test có răng | Score ≥70% module lõi, nightly — không chặn PR | mutmut / cosmic-ray |
| continuous-perf | Perf regress ngay trong PR | p95 critical không tăng >5% baseline | k6 / locust + py-spy |

## 5 · SEC — Bảo mật & chuỗi cung ứng

> Phần lớn sự cố 2024–2025 đến từ dependency, không từ code bạn viết.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| sast | Quét pattern nguy hiểm, gate trên diff | 0 high/critical trên lines đã đổi | Semgrep (rule tự viết) + Bandit |
| secrets | Chặn secret trước khi vào history; ít secret để leak | 0 secret mới; CI dùng OIDC, không PAT dài hạn | `gitleaks` (repo config `.gitleaks.toml`) + GitHub push protection |
| supply-chain | CVE + provenance + tuổi package | pip-audit visibility → enforce high/critical; package mới ≥7 ngày tuổi | `pip-audit`, `osv-scanner`, SBOM (syft) |
| dast | Tấn công app đang chạy trên staging | Không high/critical mới; không scan prod khi chưa xin phép | OWASP ZAP baseline, nuclei |
| infra-scan | Cấu hình hỏng giết nhiều hơn code hỏng | 0 CRITICAL mới trên IaC/container | `trivy config`, checkov cho compose/Dockerfile |
| ai-guardrail | App có LLM ⇒ có eval + prompt-injection test | Eval set không tụt điểm khi đổi prompt/model | promptfoo / DeepEval (relevant: rag.py synthesis) |

## 6 · REVIEW — Con người

> Máy quét cái đã biết; người lo cái chưa từng xảy ra.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| human-review | Bot review vòng 1; người review nghiệp vụ/thiết kế | ≤2 reviewer, SLA < 4h; reviewer nêu 1 rủi ro đã kiểm | Bot review (CodeRabbit/Codex) + PR template |
| design-review | Threat model TRƯỚC khi viết code | Chạm auth/tiền/PII/migration lớn ⇒ RFC 1 trang | ADR + STRIDE checklist |
| human-probe | Exploratory có charter, có thời lượng | Mỗi session để lại ≥1 test tự động | Session-based testing |

## 7 · SHIP — Release & production

> Deploy là một thí nghiệm có kiểm soát, không phải một sự kiện.

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| release-gate | Checklist release = kiểm tra tự động + 2 chữ ký | Migration expand/contract; rollback đã test | alembic `--sql` dry-run / atlas lint |
| safe-rollout | Progressive delivery + auto-rollback | Deploy theo % ≥2 bước; abort khi vượt SLO | Feature flag (Flagsmith/Unleash) |
| observability | OTel + SLO + error budget + profiling | 100% endpoint có trace_id; alert theo burn-rate | OTel SDK, Prometheus/Grafana, Sentry |
| incident | Sev matrix + blameless postmortem đóng vòng lặp | Sev1/2 có postmortem ≤5 ngày; ≥1 action thành check mới | Runbook-as-code, postmortem template |

## 8 · LOOP — Đo lường & phản hồi

> Quy trình không có số liệu thì không ai có quyền nói nó "tốt nhất".

| Stage | Goal | Gate | Tool (Python) |
|-------|------|------|---------------|
| quality-metrics | 6 số đọc mỗi tuần: DORA + escape rate + CI cost + flaky | p50 PR-to-merge < 8h; CI p95 < 10′ | DORA metrics, dashboard CI |
| test-impact | Chạy test theo tác động của diff | PR: affected + smoke; merge main: full | `pytest --last-failed`, nightly full |
| defect-taxonomy | Mỗi lớp lỗi có 1 bước chặn + 1 số liệu | Sev1/2 gán được vào 1 dòng của ma trận | Bug-label taxonomy + ma trận lỗi↔gate |

## Gap-analysis: Search-Hub hiện tại

| Stage | Trạng thái | Ghi chú |
|-------|-----------|---------|
| ruff lint+format (conv) | ✅ ĐÃ CÓ | `quality` job — ruff diff-scoped trên file đổi (`scripts/changed-py.sh`) |
| pre-commit hook | ✅ ĐÃ CÓ | `lefthook.yml` ở root |
| unit test | ✅ ĐÃ CÓ | pytest ~780 test, job `test` (quality.yml + ci.yml) |
| PR-size gate | ✅ ĐÃ CÓ | `GATE_PR_MAX_LOC=400` trong `quality` job |
| secret scan | ✅ ĐÃ CÓ | `security` job — gitleaks-action + `.gitleaks.toml` allowlist (PR này) |
| SCA / dependency audit | 🆕 PR NÀY | Job `sca`: `pip-audit -r requirements.txt` — visibility trước, enforce sau ~2 tuần |
| coverage | 🆕 PR NÀY | `--cov --cov-branch` + in baseline vào step summary; chưa đặt threshold |
| type check | 🆕 PR NÀY | Job `typecheck`: pyright `--level error`, `continue-on-error` giai đoạn baseline |
| mutation test | ⏳ ĐỂ SAU | mutmut/cosmic-ray cho `pipeline/` khi coverage baseline ổn định |
| DAST | ⏳ ĐỂ SAU | ZAP baseline trên staging khi có môi trường staging cố định |
| Schemathesis | ⏳ ĐỂ SAU | Fuzz OpenAPI `:8888` — cần fixture dựng service trong CI trước |

Nguyên tắc vận hành 3 gate mới: **đo trước, gate sau**. Ba job mới (sca,
coverage, typecheck) đều chạy ở chế độ báo cáo — sinh baseline trong step
summary/artifact — và chỉ được siết thành hard-gate sau khi baseline ổn định
(~2 tuần), tránh đỏ CI đột ngột.
