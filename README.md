# 人在回路论文评审流程

单篇论文阅读 → 八节评审 → 人工判断 → 跨论文复用。

本仓库是**公开代码仓**：只含流程代码、模板、测试与运行约定，不含文献数据、成品文稿或个人课题资料。本地还有若干**不入库的私有工作区**，各有自己的 README，见下方索引。

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

## 文件夹索引

**agent 进入任何目录前，先读该目录的 README。** 下表是唯一入口。

### 仓库内容（入库）

| 目录 | 是什么 | 先读 |
|---|---|---|
| `docs/` | 精简上下文四片：契约、决策、风险、待复核 | `docs/README.md` |
| `evidence-loop/` | 运行内核：状态机、离线评审页、本机服务、模板与 prompts | `evidence-loop/README.md` |
| `review_workflow/` | 兼容壳（4 个模块）+ 项目适配：队列、筛选、写作审计、Zotero 桥 | `review_workflow/README.md` |
| `tests/` | 回归测试（当前基线 108 passed） | `tests/README.md` |
| `artifacts/` | 过程产物与验证工具；仅 `review_ui/`、`figure_repair/` 的代码入库 | `artifacts/README.md` |

### 本地私有工作区（不入库，仅本机 agent 参考）

| 目录 | 是什么 | 先读 |
|---|---|---|
| `文献知识库/` | 证据、阅读、写作主库：候选、原文、评审任务、论点包、正文 | `文献知识库/README.md` |
| `汇报PPT/` | 汇报材料与可复用资产 | `汇报PPT/00_资产索引/README.md` |
| `文献精读/` | 已完成的精读材料（按批次归档，只读） | `文献精读/README.md` |
| `文献自动下载/` | 自动抓取的候选 PDF 缓存（下载 ≠ 精读） | `文献自动下载/README.md` |
| `archive/` | 归档区：按日期分批，`manifest.json` 可回溯 | `archive/README.md` |
| `tmp/` | 临时区：可随时清空 | `tmp/README.md` |

### 根文件

| 文件 | 作用 |
|---|---|
| `README.md` | 本页，唯一总入口 |
| `AGENTS.md` | 项目长期约定（本地，不入库） |
| `启动人工评审.cmd` / `一键更新评审.cmd` | 两个双击入口 |
| `AI论文人工评审.exe` | 评审程序本体（由 cmd 调用） |
| `.gitignore` | 白名单规则 |

## 文档

| 文件 | 内容 |
|---|---|
| `docs/CONTRACTS.md` | **唯一契约汇总**：批次边界、状态协议、八节与许可、哈希锚点、图表裁切、目录、命令、验证判据、白名单 |
| `docs/DECISIONS.md` | 9 条关键决策，每条含「决策 / 理由 / 否决方案」 |
| `docs/RISKS.md` | 未闭合风险与待办 |
| `docs/OPEN.md` | 待复核事项与已处理的事实冲突 |
| `evidence-loop/docs/protocol.md` | 框架级请求/结果协议原文（契约，勿改语义） |
| `evidence-loop/docs/ai-review.md` | AI 核验报告字段与 `ai_verified` 分支 |

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
