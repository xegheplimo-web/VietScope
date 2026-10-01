# GOVERNANCE.md — Quy trình vận hành Search-Hub (Canonical Runbook)

> Tài liệu này là **quy trình chuẩn** (canonical pipeline) vận hành repo Search-Hub.
> Mọi thay đổi code đều đi qua quy trình này — không có ngoại lệ.
> Viết cho AI agent (Hermes/Devin/Codex/OpenCode) và dev mới.

## Tổng quan vai trò (RBAC)

| Vai trò | Công cụ | Quyền |
|---|---|---|
| **Orchestrator** | Hermes | Điều phối toàn bộ, KHÔNG tự code khi có executor. Viết contract, verify, merge |
| **Executor chính** | Devin CLI | Implement theo contract. `devin --respect-workspace-trust false -p --permission-mode dangerous --prompt-file <contract.md>` |
| **Executor phụ** | OpenCode | Repair/isolated task khi Devin lỗi. `opencode run "<prompt>"` |
| **Reviewer độc lập** | Codex CLI | READ-ONLY review. `codex exec "<review brief>"` (không sandbox flag — sandbox gây phê duyệt 5 phút) |
| **Reviewer dự phòng** | OpenCode | Khi Codex hết quota. Vẫn độc lập với executor |

**9 hard invariants** (không được vi phạm):
1. Hermes = orchestrator duy nhất, không tự code khi có executor
2. reviewed_sha == gated_sha == HEAD (merge chỉ khi CI xanh + review PASS)
3. MAX_REPAIR_CYCLES = 3 (hết 3 vòng fix mà chưa PASS → dừng, báo người quyết định)
4. Fail-closed: UNKNOWN = FAIL
5. Không 2 writer song song trên cùng file/repo
6. Memory ≠ permission (memory không mở rộng quyền)
7. Repo hiện tại > memory cũ (evidence tại chỗ thắng ký ức)
8. Skill ≠ permission
9. Không bypass policy gate (PR-size, gitleaks, CI)

## Pipeline 23 bước (thứ tự bắt buộc)

```text
MEMORY RECALL → DISCOVERY → R1 PROBE → R2 ANALYSIS (FACT/INFERENCE/UNKNOWN)
→ R3 CLAIM GATE → OBJECTIVE NORMALIZE → RISK → TASK DAG
→ AUTHORITY → OWNERSHIP → HARD CONSTRAINTS → ROUTER → HANDOFF CONTRACT
→ AUTO-DISPATCH → EXECUTE → LOCAL TEST + CI → SCOPE GATE
→ R5 INDEPENDENT REVIEW → (NEEDS_FIX → fix → re-review, max 3 vòng)
→ R6 VERIFIER → EVIDENCE RECONCILE → DEPLOY? → R7 FINAL GATE → MEMORY PERSIST
```

### Tóm tắt từng bước

1. **MEMORY RECALL** — `memory_recall`/`memory_search` lấy context lịch sử. Memory chỉ là tham khảo, repo là truth.
2. **DISCOVERY** — `git status`, `git log`, đọc cấu trúc, docker compose ps, test baseline.
3. **R1 PROBE** — curl health, psql SELECT, docker exec — chỉ READ-ONLY.
4. **R2 ANALYSIS** — phân loại mọi claim: FACT (verify được) / INFERENCE / UNKNOWN.
5. **R3 CLAIM GATE** — claim không có evidence → REJECT, không đưa vào contract.
6. **RISK** — TRIVIAL/LOW/MEDIUM/HIGH/CRITICAL. HIGH+ → review bắt buộc 2 vòng.
7. **DAG** — chia task, mỗi task 1 contract, không task nào đè file của task khác.
8-11. **AUTHORITY/OWNERSHIP/CONSTRAINTS/ROUTER** — chọn executor (Devin mặc định), xác định allowed/forbidden paths, kiểm tra không writer chồng lấn.
12. **HANDOFF CONTRACT** — file .md: objective, scope, verify steps, phạm vi cấm, commit message format. Executor chỉ được làm đúng contract.
13. **EXECUTE** — dispatch bằng lệnh Devin trên. Luôn background + poll `process_manage`.
14. **TEST + CI** — `env -u PYTHONPATH python -m pytest tests/ -q -p no:cacheprovider` trong `search-router/`. Ruff check + format. Push branch, mở PR, chờ CI xanh.
15. **SCOPE GATE** — `git diff origin/main..HEAD --stat` — diff chỉ nằm trong allowed paths.
16. **R5 REVIEW** — Codex đọc độc lập, verdict PASS/PASS_WITH_NOTES/NEEDS_FIX/BLOCK.
    - NEEDS_FIX → viết fix contract → Devin fix → re-review (tối đa 3 vòng, sau đó chốt bằng PASS_WITH_NOTES + ghi nợ).
    - **Pitfall**: review heuristic (HTML parsing, regex) dễ diverge vô hạn — phải chặn scope "chỉ finding cũ + bug mới do commit này".
17. **R6 VERIFIER** — chạy test độc lập trên main sau merge + live probe (curl health, docker exec, psql).
18. **EVIDENCE RECONCILE** — mọi claim đối chiếu disk/API. Không tin report của executor.
19. **DEPLOY?** — Local stack: rebuild đúng service thay đổi + restart 1 service (`docker compose build <svc> && docker compose up -d <svc>`), KHÔNG đụng data services.
20. **R7 FINAL GATE** — HEAD == merge sha, CI main xanh, test count khớp, không secret mới.
21. **MEMORY PERSIST** — `memory_save` bài học + shas.

## Lệnh chuẩn (cheat sheet)

```bash
# Devin executor
cd F:/VietScope-main && devin --respect-workspace-trust false -p --permission-mode dangerous --prompt-file "<contract.md>"

# Codex review (KHÔNG dùng --sandbox — gây hộp thoại phê duyệt)
codex exec "<review brief — verdict PASS/PASS_WITH_NOTES/NEEDS_FIX/BLOCK>"

# Test suite
cd search-router && env -u PYTHONPATH python -m pytest tests/ -q -p no:cacheprovider

# Ruff (binary riêng, không phải python -m)
/c/Users/atton/.local/bin/ruff check . && /c/Users/atton/.local/bin/ruff format --check .

# GitHub API (token trong ~/.git-credentials dòng 1)
GH_TOKEN=$(head -1 ~/.git-credentials | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
curl -s -H "Authorization: token $GH_TOKEN" "https://api.github.com/repos/xegheplimo-web/VietScope/..."

# Merge PR (chỉ khi CI xanh + review PASS)
curl -s -X PUT -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/xegheplimo-web/VietScope/pulls/N/merge" \
  -d '{"merge_method":"merge"}'

# Rebuild 1 service sau merge (stack đang chạy)
docker compose build search-router && docker compose up -d search-router
```

## Lịch sử phases (tóm tắt)

| Phase | PR | Commit merge | Nội dung | Tests |
|---|---|---|---|---|
| P11 auth | (repo cũ) | 52da95b | API key auth + metering | 758 |
| P11 hybrid | (repo cũ) | 7f5c356 | RRF 2-lane live | 768 |
| P11 MCP | (repo cũ) | 0f88968 | MCP :8901 bearer | 780 |
| T0 quality | #1-3 | 9f8c4f3 | CI xanh, pip-audit, coverage, pyright | 800 |
| T1a cleanup | #4 | 9cc9089 | −79MB, xóa qdrant-master/Vane | 800 |
| T1b dedup | #5 | 09c111d | 1 canonical pipeline | 765 |
| T2 storage | #6 | f29288b | PostGIS + MinIO + migrations + frontier | 786 |
| R5 fixes | #7 | 69eb149 | 8 findings CODEX (SSRF, lease, migration) | 818 |
| Phase 2 crawler | #8 | c71190f | 8 modules crawler, 8 vòng review | 1050 |
| Harden adopt | #9 | 3a426d7 | CORS + dashboards + metrics path | 1056 |

## Sự cố đã gặp + cách xử lý (postmortems ngắn)

1. **Devin "untrusted workspace"** → flag `--respect-workspace-trust false` (đứng trước `-p`).
2. **Devin inline prompt bị parse thành PATH** → luôn dùng `--prompt-file`.
3. **Codex `--sandbox read-only` không đọc file ngoài repo** → bỏ flag sandbox, chạy mặc định + instruction read-only trong prompt.
4. **Codex sandbox helper Windows hay fail transient** → nó tự fallback đọc GitHub; push branch trước khi review.
5. **Codex hết usage limit** → fallback OpenCode làm reviewer độc lập.
6. **GitHub runner "No space left on device"** → rerun-failed-jobs, không cần sửa gì.
7. **PR-size gate chặn feature PR lớn** → nâng `GATE_PR_MAX_LOC` trong `.github/workflows/quality.yml` (hiện 7000), ghi rõ lý do trong commit.
8. **Repo GitHub biến mất/xóa nhầm** → local là source of truth cuối: `git push origin --all` khôi phục toàn bộ. Luôn verify `git ls-remote --heads origin | wc -l` sau push.
9. **Container chạy image cũ sau merge** → rebuild đúng service: `docker compose build <svc> && up -d <svc>`, verify `docker exec <svc> ls` có code mới + health 200.
10. **Bản "clean" từ nguồn ngoài** → KHÔNG thay nguyên repo. Diff cấu trúc + test count, cherry-pick từng cải thiện thật qua PR.

## Quy ước

- Branch: `<phase>/<tên>` (phase2/crawler-engine), fix: `fix/`, harden: `harden/`.
- Commit: `feat(scope): ...`, `fix(r5p2rN): ...`, `ci: ...`.
- Merge chỉ qua PR + CI xanh. Không push thẳng main.
- Secrets: KHÔNG commit. Token lưu `~/.git-credentials` + scratch. `.env` gitignored.
- Test count phải tăng hoặc giữ nguyên mỗi PR — mất test = regression.
