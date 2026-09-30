# Quy trình chất lượng code — Search-Hub

> Cài từ *Quality Pipeline 2025* (`F:\modern-code-quality-workflow`), stack **Python**.
> Ngưỡng ở `quality-gates.yml` — thresholds as code, có owner, review mỗi quý.

## Nguyên tắc

1. Phát hiện lỗi càng sớm càng rẻ — mỗi lớp chỉ bắt được một phần lỗi.
2. Máy cưỡng chế cái đã biết; người review thiết kế và rủi ro.
3. **Finding chỉ chặn trên code mới/đã đổi** — baseline legacy được dọn dần, không block vĩnh viễn.
4. Code do AI sinh đi qua đúng pipeline như code người viết.

## Các lớp đã cài

### Local — `lefthook.yml`

| Hook | Lệnh | Ngân sách |
|---|---|---|
| pre-commit | `ruff check --fix` + `ruff format` trên staged `.py` | < 3s |
| pre-commit | `gitleaks protect --staged` (bỏ qua nếu chưa cài) | < 3s |
| commit-msg | `commitlint` (qua npx, bỏ qua nếu fail) | < 3s |
| pre-push | `compileall` + `pytest tests/ -x -q` trong `search-router/` | nặng — cố ý đặt ở push |

Cài: `winget install evilmartians.lefthook` → `lefthook install`.

### Lint — `ruff.toml` (repo root)

- Scope: first-party code (`search-router/`, `eval/`). Vendor/submodule
  (`firecrawl`, `searxng`) bị exclude. `qdrant-master`,
  `Vane-master`, `AI-Search-Hub`, `llm-answer-engine` đã xóa/archive Phase 0.
- Rules: E, W, F, I, UP, B, BLE, S, C4, SIM.
- `BLE001`/`S110`/`S101`/`E501` tắt — blind-except là degrade-by-design
  pattern có chủ đích của codebase (mọi provider/worker phải fail-soft).
- Baseline cleanup đã chạy một lần (`ruff check --fix` + `format`):
  352 lỗi auto-fixed, 130 file reformat; còn ~80 lỗi style thủ công để lại
  cho diff-gate + dọn dần.

### CI — `.github/workflows/quality.yml`

| Job | Nội dung | Blocking |
|---|---|---|
| quality | actionlint · ruff + format **chỉ trên file đổi** · PR ≤ 400 LOC · cấm xoá test file | ✅ |
| test | `cd search-router && pytest --cov` (cwd bắt buộc — `eval/` name collision) | ✅ |
| security | gitleaks · osv-scanner trên `requirements.txt` | ✅ secrets / ⚠️ sca soft |
| nightly | full `ruff check --statistics` (informational) | — |

`ci.yml` cũ (pytest + docker-build) giữ nguyên — không trùng job.

## Ngưỡng (`quality-gates.yml`)

`diff_coverage ≥ 80%` · `PR ≤ 400 LOC` · `CI p50 ≤ 10′` · `dep age ≥ 7 ngày`
· `test_floor: 700` (suite không được co lại) · `critical_bugs: 0`.

## Chạy tay

```powershell
ruff check . && ruff format --check .          # full report (sẽ còn baseline)
bash scripts/changed-py.sh origin/main         # file py đã đổi vs main
cd search-router; python -m pytest tests/ -q   # 700 tests
```

## Còn thiếu (backlog)

- Diff-coverage machine-check (`scripts/diff-coverage.mjs`) — chưa port.
- Commitlint config (`commitlint.config.js`) — hook đang dùng default rules.
- ~~Vane (TS) stack~~ — Vane đã archive sang `archive/vane-master` (Phase 0);
  nếu revive thì re-add `tsc --noEmit` + eslint gate.
- Mutation testing (nightly) — chưa đủ eval data, theo spec v2.1.
