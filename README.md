# 人在回路论文评审流程（代码仓库）

本仓库收录**单篇论文阅读 → 八节评审 → 人工判断 → 跨论文综合**这套流程的可执行代码、模板、测试与运行约定。远端为 GitHub 公开仓库 `cloudy019/review-workflow`，是唯一活跃的代码仓库。

**不包含**：文献 PDF、Zotero 数据、综述正文与 Word/PDF 成品、证据卡与论点包、PPT、临时文件、缓存和任何凭据。项目根是现有工作区，因此本仓库采用 `.gitignore` 白名单方式跟踪，新增文件需显式 `git add`；个人课题资料不在放行范围内。

## 目录

| 路径 | 作用 |
|---|---|
| `evidence-loop/` | 统一运行核心：单篇请求/结果协议、状态机、HTML 渲染、评审服务、模板与 prompts |
| `review_workflow/` | 兼容导入层（状态/台账/CLI 旧入口），不重复实现状态机 |
| `tests/` | 队列、协议、渲染、锁等回归测试 |
| `artifacts/review_ui/` | 评审页验证栈：`build_preview.py` → `verify.py` → `dom_test.js` → `probe.py` → `shoot.py` |
| `artifacts/figure_repair/` | PDF 图表提取与身份核验工具（`extract_figures.py`、`batch_verified_crops.py`） |
| `.workbuddy/serve_reviews.sh` | 本机人工评审服务启动（固定 token） |
| `CHANGELOG.md` | 每个 bug 修复的整理记录 |

## 源码来源

`evidence-loop/` 是**自包含的源码快照**，不是 git submodule，克隆后无需额外拉取。它源自早期一个独立框架仓库；该仓库已于 2026-11-11 归档删除，本仓库内的快照即为唯一准据，旧历史不再单独提供。

`review_workflow/` 里的 `core.py` / `html_review.py` / `registry.py` / `review_server.py` 只是转发壳（`from evidence_loop.X import *`），**改真源请改 `evidence-loop/src/evidence_loop/`**；其余模块是队列、筛选、写作与 Zotero 适配。

## 常用入口

```bash
# 领取单篇任务
python -m evidence_loop --workspace <workspace> issue <job-id> --reader workbuddy
# 提交结果（校验 + 渲染 + 登记一次完成）
python -m evidence_loop --workspace <workspace> submit <job-id> <attempt-id> <result.json>
# 启动人工评审页面
bash .workbuddy/serve_reviews.sh 8765 <token>
```

评审服务读操作无需认证；写操作由核心状态机处理。不要直接改 `deep-paper-note.md`、`reading-result.json`、`review-sections.json`、`runs/*/request.json`——它们是人工许可的哈希锚点。

## 提交约定（每次修复好 bug 都要整理）

1. **一个 bug 一个 commit**，不把无关改动混进同一个提交。
2. 提交信息用 `fix(范围): 根因 → 改动`，正文写明复现方式与验收证据（命令、计数、截图/目视结论）。
3. 修复涉及模板或渲染时，必须同步模板副本并重新渲染受影响页面，核对 `reading_receipt.artifacts` 哈希，避免任务掉到 `needs_clarification`。
4. 修复后在 `CHANGELOG.md` 追加一条，注明影响范围与未改动项。
5. 核验通过后**立即推送**到远端；推送失败如实报告，不伪造完成状态。
6. 不得提交凭据、文献数据、成品文稿或个人课题资料。

## 已整理的修复

完整清单见 `CHANGELOG.md`；每个提交都写明现象、根因、验收证据与未改动项。
