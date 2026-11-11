# 单篇产物协议

## 来源与模型阅读

读取 `reading-task.json`，核对单篇身份，然后读取任务给出的技能文件。`nature-reader/manifest.yaml` 中的相邻 `nature-shared` 已随包提供。该项目只生成中文摘要速读器，不需要平行全文翻译；后台保留原文结构与页码。

全部阅读对象仅为任务指定 PDF。使用已核验的本地来源，DeepPaperNote 的来源输入设为本地 PDF，`zotero-mode=off`，工作目录设为本篇 `work-tmp`；已有身份与来源不重新搜索或下载。需要调用单步抽取时，使用包内脚本，不运行会重复获取来源的整条流水线。

可先运行本包 `scripts/review.py extract <reading-task.json>`，获得带页码的 `work-tmp/source-pages.jsonl`，随后由模型读取全文。结果中 `pages_requiring_ocr` 非空时对这些页做OCR或视觉阅读。图表根据原PDF定位并裁剪，不能仅凭抽出的正文宣称已看图。

按 `nature-reader → deeppapernote → 本篇条件专项核查` 完成阅读。原文抽取与图表处理属于脚本；研究问题、方法逻辑、关键结果、知识产物和综述价值必须由当前模型阅读原文后形成。不要把准备任务、生成清单或执行脚本当作AI阅读完成。

写作围绕当前项目的研究问题，使用自然中文。作者明示局限与AI判断分别标注，AI判断以本篇内容为依据。重要图表完整性在落盘时检查一次；未变化的产物直接复用。

## 隔离目录中的文件

- `reader/paper.md`：后台来源映射和原文定位材料。
- `reader/source_map.json`：按 nature-reader 约定保留页码、段落与图表来源锚点。
- `deep-paper-note.md`：面向用户的中文精读笔记。速览、方法、结果、局限、综述价值五类导航；关键数字、必要公式与原文图表嵌入相关段落，图片采用相对于本篇输出目录的路径。
- `review-sections.json`：`title`、`citation` 和 `sections`；八节ID为 `relevance`、`question_contribution`、`methods_data`、`results_evidence`、`figures_equations`、`limitations`、`knowledge_product`、`synthesis_permission`。每节填写 `ai_content` 和 `source_anchors`。原文锚点包含页码/图号/章节，判断暂存于AI内容，不填写人工答案或许可。
- `figure-completeness.json`：`{"items": [...]}`，列出本文所有图表，每项含 `id`、`source_page`、`decision`。`insert` 必须有已在速读笔记引用的本地图片 `asset`；`omit` 必须有具体 `reason`。无图表可用空数组。关键证据图表应插入。Supplement 仅在任务确已提供且属于该单篇来源时处理，不另读第二个PDF。

例如：`{"id":"Fig. 1","source_page":3,"decision":"insert","asset":"reader/assets/fig1.png"}`。本清单描述实际产物，不以清单代替图表视觉核验；裁图后查看图像，确认坐标、标签与图注清楚。

`render` 检查八节、文件与图片并嵌入离线页面，写出含产物哈希的 `reading-result.json`；仅表示材料可登记。成功后主任务运行 `accept`，进入 `human_reviewing`。打开本机评审页检查图片放大、公式和人工保存后交给用户。人工提交后才运行项目原有 `import-human-review`，并串行更新共享知识库。

## 迁移与交接

复制整个 `paper-review` 文件夹即可使用，资源不依赖原机器。运行 `scripts/review.py install --engine both --project <项目根目录>` 安装至两端默认目录；也可用 `--target <技能目录/paper-review>` 指定位置。模型由各宿主自行配置，本包没有外部API密钥。

已生成的任务包含当前机器的来源和工作路径；迁移能力不迁移私人 PDF 或人工记录。另一台机器以当地已核验来源创建/接续任务；移动现有项目时先更新任务路径，再重新生成 `reading-task.json`。同一机器两端交接须显式使用 `--handoff`，只允许一个执行者写本篇目录。

没有原项目代码的新环境，可对明确指定的一篇本地来源运行 `scripts/review.py prepare --kb <新知识库> --paper-id <DOI或单篇ID> --zotero-key <Key> --pdf <已核验本地PDF> --engine codex|workbuddy`；这会生成单篇job、批次和reading-task，之后执行extract、模型读写、render、accept。此入口仅用于用户明确给定单篇来源或新项目调试；本项目正式阅读仍由冻结规则自动选批。
