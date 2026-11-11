# review_workflow

> 兼容层 + 项目适配。**状态机与渲染的真源不在这里**，而在 `evidence-loop/src/evidence_loop/`。

## 哪些是转发壳

以下 4 个模块只有一行 `from evidence_loop.X import *`，**改它们没有任何效果**：

- `core.py` — 状态机
- `html_review.py` — 评审页渲染
- `registry.py` — 派生索引
- `review_server.py` — 本机评审服务

## 哪些是项目模块

| 模块 | 作用 |
|---|---|
| `cli.py` | 旧命令行入口（选批、渲染、导入、重建台账） |
| `queue.py` | 批次队列与锁 |
| `screening.py` | 候选初筛与评分规则 |
| `library.py` | 阅读台账与矩阵 |
| `reading.py` / `reading_kit.py` | 阅读任务准备与能力包 |
| `writing.py` | 正文写作审计（过程语言、未确认引用等） |
| `candidate_sources.py` | 候选来源与附件索引 |
| `attachment_index.py` | PDF 附件键索引 |
| `framework_worker.py` / `workbuddy_bridge.py` | 与宿主的文件式交接 |
| `zotero_plugin.py` | Zotero 插件层只读核验 |
| `skill_router.py` / `style_memory.py` | 技能路由与风格记忆 |
| `migrate_legacy.py` | 旧库迁移（默认只预览） |

## agent 规则

1. 改状态机、渲染、服务 → 改 `evidence-loop/src/evidence_loop/`；改壳无效。
2. 旧 CLI 只用于旧格式任务恢复与批量准备；新任务走 `evidence_loop issue/submit`。
3. 共享文件（台账、矩阵、图谱、仪表盘）只由主任务串行更新。
4. 默认路径为 `workspace/candidates/papers-screened.json`；本地跑旧入口需显式传你的候选池路径。
