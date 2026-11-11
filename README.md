# 人在回路论文评审流程

单篇论文阅读 → 八节评审 → 人工判断 → 跨论文复用。本仓库只包含**流程代码、模板、测试与运行约定**，不含文献数据、成品文稿或个人课题资料。

## 快速开始

**方式一：双击**

| 文件 | 作用 |
|---|---|
| `启动人工评审.cmd` | 启动本机评审服务并打开浏览器（内部调用 `AI论文人工评审.exe`） |
| `一键更新评审.cmd` | 重建阅读台账并拉起 WorkBuddy 刷新队列 |

**方式二：命令行**

```bash
# 领取 / 提交单篇
python -m evidence_loop --workspace <workspace> issue <job-id> --reader workbuddy
python -m evidence_loop --workspace <workspace> submit <job-id> <attempt-id> <result.json>

# 启动评审页面（等价于双击 启动人工评审.cmd）
bash .workbuddy/serve_reviews.sh 8765 <token>
```

评审服务读操作无需认证；写操作由核心状态机处理。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/CONTRACTS.md` | **唯一契约汇总**：任务与批次边界、状态协议、八节与许可、哈希锚点、图表裁切硬约束、目录、验证判据、白名单 |
| `docs/DECISIONS.md` | 9 条关键决策，每条含「决策 / 理由 / 否决方案」 |
| `docs/RISKS.md` | 未闭合风险与待办 |
| `docs/OPEN.md` | 待复核事项与已处理的事实冲突 |
| `evidence-loop/docs/protocol.md` | 框架级请求/结果协议原文（契约，勿改语义） |
| `evidence-loop/docs/ai-review.md` | AI 核验报告字段与 `ai_verified` 分支 |

## 目录

| 路径 | 作用 |
|---|---|
| `evidence-loop/` | 统一运行核心：状态机、离线评审页渲染、本机评审服务、模板与 prompts |
| `review_workflow/` | 兼容壳（4 个模块 `import *`）+ 项目适配：队列、筛选、写作审计、Zotero 桥 |
| `tests/` | 队列、协议、渲染、门禁、锁的回归测试 |
| `artifacts/review_ui/` | 页面验证栈：`build_preview.py → verify.py → dom_test.js → probe.py → shoot.py` |
| `artifacts/figure_repair/` | 图表提取、身份核验与批量裁切 |
| `docs/` | 契约、决策、风险、待复核四片精简上下文 |
| `.workbuddy/serve_reviews.sh` | 本机评审服务启动（固定 token） |

## 源码来源

`evidence-loop/` 是**自包含的源码快照**，不是 git submodule，克隆后无需额外拉取。它源自早期一个独立框架仓库；该仓库已于 2026-11-11 删除，本仓库内的快照即为唯一准据。

`review_workflow/` 里的 `core.py` / `html_review.py` / `registry.py` / `review_server.py` 只是转发壳（`from evidence_loop.X import *`）。**改真源请改 `evidence-loop/src/evidence_loop/`**。

## 提交约定

1. **一个 bug 一个 commit**，不把无关改动混进同一个提交。
2. 提交信息用 `fix(范围): 根因 → 改动`，正文写明复现与验收证据（命令、计数、截图结论）。
3. 决策与否决理由写进 `docs/DECISIONS.md`；未闭合项写进 `docs/RISKS.md`。
4. 修复涉及模板或渲染时，同步 portable kit 副本、重渲染受影响页面，并核对 `reading_receipt.artifacts` 哈希。
5. 核验通过后立即推送；推送失败如实报告，不伪造完成状态。
6. 不得提交凭据、文献数据、成品文稿或个人课题资料。
