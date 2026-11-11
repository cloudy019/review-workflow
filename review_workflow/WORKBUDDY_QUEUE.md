# WorkBuddy 论文评审队列协议

本任务只维护人工评审队列，不写综述正文。`文献知识库/review-jobs/jobs/*/job.json` 是唯一任务状态源；`review-app/library.json` 与阅读台账是可重建视图。既有未领取论文的题录卡片只提供候选元数据，不能覆盖核心中的人工状态。不得依据聊天记录、Agent 记忆或旧日志判断。

1. 只处理 `文献知识库/review-app/workbuddy/inbox/queue-refresh.request.json` 中 `status=pending` 的请求。开始时改为 `processing` 并记录时间。
2. 先导入当前批次新保存的人工评审，串行重建 `library.json` 和阅读台账。刷新时只读本地 `screening/zotero-attachment-index.json`；`pdf_attachment_key` 缺失时标记 `missing_pdf_key`、记一次 warning 并继续，不因缺键阻塞整批，也不逐篇重试 Zotero。人工只判断论文价值；不得要求人工逐篇重写具体主张。
3. 当前批次尚有论文未进入 `human_confirmed`、`excluded` 或 `blocked_source` 时，把请求改为 `waiting_human_review`，不得选择下一批。
4. 批次门释放后，由冻结规则自动选择下一批，最多三篇，用户不逐篇挑论文。先运行项目现有的 `screen-candidate-pool`、来源核验、`select-next-batch` 与 `start-next-batch`，不得自行另建候选清单。只有入选阅读任务需要通过 PDF 来源核验；没有 PDF 的候选留在队列并跳过本轮选批。
5. 每篇论文是一个独立任务；输入只有一个 Zotero Key、一个 PDF、一个隔离输出目录。Codex 或 WorkBuddy 均可执行。支持子代理时可分配不同论文；不支持时由当前模型按单篇任务顺序完成，不将缺少Codex子代理作为阻塞原因。共享文件仍由主任务串行更新。
6. 使用 `.workbuddy/skills/evidence-loop/SKILL.md`。现有项目的选批与 Zotero 来源准备继续使用项目入口；阅读、页面生成和人工状态统一交给 Evidence Loop。运行 `python -m evidence_loop --workspace 文献知识库 issue <job-id> --reader workbuddy`，读取返回的 request.json，当前模型完成单篇阅读并写入规定 JSON；再运行 `python -m evidence_loop --workspace 文献知识库 submit <job-id> <attempt-id> <result.json>`，一次完成校验、渲染和登记。`nature-reader → deeppapernote → 条件专项 Skill` 可提供阅读方法，不能另建状态机或要求必须由 Codex 子代理代读。已有旧格式产物可以由旧accept接入；领取新attempt后禁止旧入口覆盖。另一执行端接续须先停下原执行者，并显式使用 `--handoff`。
7. 所有共享 README、台账、矩阵、图谱和仪表盘只由主任务串行更新。不得直接操作 Zotero 数据库，不得复制 PDF 到项目中。
8. 队列状态写回后，才可独立运行 `python -m review_workflow.cli backfill-pdf-keys --limit 20` 补缺失映射；补键失败不回滚队列。完成后更新请求的 `status`、完成时间、批次号、任务号与页面路径；失败时写 `blocked` 和具体原因。简报使用请求中的总论文数、cache hit、missing、Zotero 查询次数、总耗时。不要把执行过程写进综述正文。
