# 人在回路综述编排器

这个目录是单篇阅读的编排和数据层。Codex、WorkBuddy 使用同一 `paper-review` 能力包，由各自当前模型完成AI阅读与写作，程序负责隔离、渲染、登记和人工门。

## 固定边界

- 一个阅读任务只能有一个 `paper_id` 和最多一个 PDF。
- 每个读取器只写自己的 `jobs/<job-id>/agent-output/`；共享知识库由主任务串行更新。支持子代理时可并行，不支持时逐篇执行。
- 每批最多三篇，上一批未进入人工终态时不得启动下一批。
- 左栏是由 `deeppapernote` 驱动的论文速读器，固定提供“速览、方法、结果、局限、综述价值”五类导航，保留关键数字、公式、图表、证据页码和批判性判断；不复制整篇PDF，也不制作平行全文翻译。`nature-reader` 退到后台负责来源映射和图表锚定。需要原文上下文时，用户复制标题到Zotero阅读；领域 Skill 仅核查同一篇论文。
- LaTeX 公式由本地 KaTeX 离线渲染，论文图片可点击放大；页面不加载网络资源。
- 双击项目根目录的 `AI论文人工评审.exe`，程序会在确认本机页面真正启动后再打开浏览器。点击“保存评审”会直接写入当前论文的 `human_reviews/<ZoteroKey>.human-review.json`，无需绑定文件夹或选择位置；“下载备用JSON”只用于故障备份。
- `review-app/library.json` 与 `review-app/论文文件总索引.md` 是论文已读/未读状态的唯一事实来源，任何 Agent 都不得凭对话记忆判断。
- 程序中的“交给WorkBuddy更新”会先重建上述台账，再创建 `review-app/workbuddy/inbox/queue-refresh.request.json` 并启动本机 WorkBuddy。模型与外部 API 在 WorkBuddy 中配置，项目不保存密钥。
- 一键入口：双击项目根目录的 `一键更新评审.cmd`，或运行 `python -m review_workflow.cli request-workbuddy-refresh --launch`。刷新先生成本地 Zotero 条目键到 PDF 附件键的缓存并输出统计；缺键保留为 `missing_pdf_key`，不会触发 Zotero 查询。需要补键时，在刷新后单独运行 `python -m review_workflow.cli backfill-pdf-keys --limit 20`。
- 单篇评审允许选择“跳过（不评审本节）”。论文级判断只决定是否进入综合候选池；AI候选主张在章节论点包阶段再由人选择，不要求逐篇手写具体主张。
- 只有 `human_confirmed` 且引用许可有效、PDF/笔记哈希未变化的卡片能进入综合和写作。
- Zotero 只通过 Codex 自带插件。已有条目加入集合由用户手动完成，程序仅做插件层只读核验。

## 目录约定

- `review_workflow/`：编排、HTML、迁移、写作检查和 Schema。
- `workspace/review-project.json`：当前项目的研究问题、知识分类和人工门配置。
- `文献知识库/papers/<ZoteroKey>/`：单篇来源、AI产物、人工记录、正式卡片和 HTML。
- `文献知识库/review-jobs/`：子代理隔离任务与批次状态。
- `文献知识库/review-inbox/`：从 HTML 导出的待导入人工评审 JSON。

## 推荐操作顺序

1. `screen-candidate-pool` 按冻结标准逐篇初筛候选池。
2. `verify-candidate-sources` 通过 Codex 内置 Zotero 插件核验优先候选的唯一 PDF 和哈希。
3. `select-next-batch` 自动挑选最多三篇；用户不需要逐篇选论文。
4. `start-next-batch` 创建独立单篇任务和代理清单。
5. 单篇代理调用 `nature-reader`、`deeppapernote` 和条件触发的领域 Skill。
6. `render-review` 生成独立本机 HTML。
7. 双击 `AI论文人工评审.exe`，逐节判断并点击“保存评审”。
8. `import-human-review` 验证哈希并写入单篇卡片。
9. `rebuild-library` 串行更新矩阵、知识图谱、README和仪表盘。
10. `build-section` 从确认卡片生成待人工审批的论点包。

命令统一从项目根目录执行：

```powershell
python -m review_workflow.cli screen-candidate-pool <含records数组的候选池.json>
python -m review_workflow.cli verify-candidate-sources --max-attempts 60
python -m review_workflow.cli select-next-batch
python -m review_workflow.cli start-next-batch
python -m review_workflow.cli prepare-paper <DOI或唯一ID> --zotero-key <8位Key> --pdf <单篇PDF>
python -m review_workflow.cli dispatch-reading-batch <job-id-1> [job-id-2] [job-id-3]
python -m review_workflow.cli attach-note <job-id> <单篇AI笔记.md>
python -m review_workflow.cli render-review <job-id> <八节阅读材料.json>
python -m review_workflow.cli serve-reviews --open
python -m review_workflow.cli import-human-review <HTML导出的评审.json>
python -m review_workflow.cli rebuild-library
python -m review_workflow.cli build-section <章节ID> --goal <本节目标>
python -m review_workflow.cli request-workbuddy-refresh --launch
```

前四个命令是正常入口；`prepare-paper` 和 `dispatch-reading-batch` 保留给单篇调试，不要求用户提供正式阅读批次的论文 ID。

`migrate_legacy` 默认仅预览；只有显式加入 `--apply` 才会创建旧库骨架，而且不会覆盖已有单篇卡片。

依赖版本记录在 `dependencies.lock.json`。所有论文和私人笔记默认只保存在本地。

## 双端能力安装与迁移

使用方法见 [可迁移阅读能力.md](可迁移阅读能力.md)。能力包携带阅读技能及其脚本、共享术语资源、离线HTML模板和KaTeX；不带PDF、文库、任务状态、Python虚拟环境或密钥。两端使用相同 `kit-manifest.json` 内容哈希；路径均从技能包位置解析。

```powershell
python -m review_workflow.cli build-reading-kit --output artifacts/portable-paper-review/paper-review
python -m review_workflow.cli install-reading-kit --kit artifacts/portable-paper-review/paper-review --engine both
python -m review_workflow.cli prepare-reading-task <job-id> --engine workbuddy
python -m review_workflow.cli render-reading-output <reading-task.json>
python -m review_workflow.cli accept-reading-output <job-id>
```

后三条依次由主任务准备、单篇读取器完成AI产物后渲染、主任务串行登记。`dispatch-reading-batch` 仍只创建任务，不是模型调用；宿主读取 `paper-review/SKILL.md` 后必须实际阅读和写作才会产生页面。
