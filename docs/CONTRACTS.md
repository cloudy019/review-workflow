# 契约与运行边界

> 公开仓唯一的契约汇总。原分散在流程说明、测试说明、队列协议、图表修复工作流中的**生效约定**已合并到这里；框架级协议原文仍保留在 `evidence-loop/docs/`。

## 1. 任务与批次边界

- 一个阅读任务只有一个 `paper_id`、最多一个 PDF、一个隔离输出目录 `agent-output/`。
- 每批最多 **3 篇**；上一批未全部到达人工终态（`human_confirmed` / `excluded` / `blocked_source`）时不得开下一批。自主核验模式下，有效的 `ai_verified` 也可释放批次。
- `review-jobs/jobs/*/job.json` 是**唯一状态源**；`library.json`、台账、矩阵、图谱、仪表盘都是可重建视图。不得依据聊天记录、Agent 记忆或旧日志判断状态。
- 共享文件（README、台账、矩阵、图谱、仪表盘、正文）只由主任务**串行**更新；执行者只写自己的 attempt 目录。

## 2. 状态与结果协议

状态序列：

```
candidate → identity_verified → source_ready → reading → ai_note_ready
→ human_reviewing → human_confirmed → indexed → synthesis_eligible
```

`result` 顶层字段固定为 `schema_version` / `paper_id` / `pdf_sha256` / `title` / `summary_markdown` / `sections` / `claims` / `figure_decisions`。八节 id 固定为 `relevance`、`question_contribution`、`methods_data`、`results_evidence`、`figures_equations`、`limitations`、`knowledge_product`、`synthesis_permission`。

`figure_decisions` 必须对 `figure_inventory` **逐条**给出 `include` / `omit` 与理由，条数不等直接拒绝。读者**不得**在结果里提交人工判断、许可或状态迁移。

完整字段与校验见 [`evidence-loop/docs/protocol.md`](../evidence-loop/docs/protocol.md)；AI 核验报告字段与 `ai_verified` 分支见 [`evidence-loop/docs/ai-review.md`](../evidence-loop/docs/ai-review.md)。

## 3. 八节判断与许可

- 八节：相关性与证据角色、问题与贡献、方法与数据、结果与证据、图表与公式、作者局限与人工批判、知识产物与迁移边界、论文关系与综合许可。
- 论文许可与具体主张批准**分离**；未批准候选不得当作已批准写作主张。
- `ai_verified` 与人工确认**分开保存**；AI 核验不产生人工批准，也不冒充人工答案。
- 只有许可有效、且 PDF / 笔记 / 结果 / 页面哈希均未变化的卡片能进入综合与写作。

## 4. 哈希锚点（禁止直接改写）

以下文件被许可的 sha256 绑定，改动即作废许可：

```
deep-paper-note.md
reading-result.json
review-sections.json
runs/<attempt>/request.json
runs/<attempt>/source-pages/*.png
ai_reviews/archive/<digest>.json        # AI 核验归档，永久不改写
```

需要补充内容时另存新文件（如 `reader-with-figures.md`）再重渲染页面；需要换版本时走 `reopen`，不覆盖旧快照。撤销许可用 `revoke`。

## 5. 图表裁切硬约束

1. **不臆造、不重绘、不美化**：所有图片必须来自原 PDF 真实区域渲染。
2. 无法提取时写 `图表缺失：无法从当前 PDF 提取`，并给出具体原因（如 `no_figure_region_near_caption`）。
3. 身份核验：裁剪区回抽文本含图号，**或**同页 PDF 图题含图号；且 `ink_ratio >= 0.01`。未通过不得写入正式材料。
4. 自动图号与图题不符时，以 PDF 图题文字为准，并记录「自动匹配已人工校正」。
5. 整页 `source-pages/` 是哈希锚定的**整页渲染**，不是单图裁图，不得用于页面兜底，也不得当作「图已显示」。

## 6. 目录约定

| 路径 | 作用 |
|---|---|
| `evidence-loop/src/evidence_loop/` | 权威内核：状态机、渲染、服务、模板、prompts |
| `review_workflow/` | 兼容壳（4 个模块 `from evidence_loop.X import *`）+ 项目适配（队列、筛选、写作审计、Zotero 桥） |
| `tests/` | 队列、协议、渲染、门禁、锁的回归测试 |
| `artifacts/review_ui/` | 页面验证栈：`build_preview.py → verify.py → dom_test.js → probe.py → shoot.py` |
| `artifacts/figure_repair/` | 图表提取、身份核验与批量裁切 |
| `.workbuddy/serve_reviews.sh` | 本机评审服务（固定 token，写 ready/pid 文件） |

**改真源只能改 `evidence-loop/src/evidence_loop/`**；`review_workflow/` 中同名模块是转发壳，改壳无效。

## 7. 命令入口

```bash
# 领取 / 提交单篇
python -m evidence_loop --workspace <workspace> issue <job-id> --reader workbuddy
python -m evidence_loop --workspace <workspace> submit <job-id> <attempt-id> <result.json>

# 启动评审页面（等价于双击 启动人工评审.cmd）
bash .workbuddy/serve_reviews.sh 8765 <token>
```

旧入口保留在 `review_workflow.cli`（`screen-candidate-pool`、`select-next-batch`、`start-next-batch`、`render-review`、`import-human-review` 等），仅用于旧格式任务恢复与批量准备，不另建状态机。

## 8. 验证判据

- `pytest tests/` 全绿；`artifacts/review_ui/verify.py` 对 baseline 页面问题数为 0。
- `dom_test.js` 覆盖交互与后端 schema；`probe.py` 检查真实布局无溢出。
- 页面不得出现 `data-pane="pages"` / `class="page"` / 跳页按钮——原文页分区已撤掉。
- 页面图卡数应等于 `reader/assets/` 中的已验证裁图数，也应等于内嵌 `data:image` 数。
- 存在 `figure-crops.json` 时，缺失提示只认其中 `status=missing` 的逐条记录，不用决策条数反推。

## 9. 仓库白名单

公开仓只放流程代码、测试与文档。以下**不入库**：文献 PDF、Zotero 数据、综述正文与成品、证据卡与论点包、PPT、图片、缓存、凭据、个人课题资料。

`.gitignore` 采用白名单：`/*` 默认忽略，逐级 `!` 放行；新增文件必须显式 `git add`，禁止 `git add -A` 后不检查。
