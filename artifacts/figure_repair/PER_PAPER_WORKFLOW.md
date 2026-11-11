# 单篇论文图表修复工作流（本轮共用）

适用范围：当前开放批次（batch-5e9fbe6b8939）中的三篇论文，每篇由一个独立子任务处理。

## 硬约束（违反即判 blocked）

1. **只处理自己的一篇**：一个 Zotero Key、一个 PDF、一个隔离输出目录 `文献知识库/review-jobs/jobs/<job-id>/agent-output/`。
2. **不改 `deep-paper-note.md`**：该文件哈希已与人工引用许可绑定（`job.json.citation_permission.bound_ai_note_sha256`），改动会作废许可。补充后的阅读材料另存为 `reader-with-figures.md`。
3. **不改 `review-sections.json`、`job.json`、`human_reviews/`、任何共享文件**（library.json、论文文件总索引.md、README、知识图谱、仪表盘、review-app）。
4. **不写 Zotero**，不复制 PDF 进项目，PDF 只读。
5. **不臆造、不重绘、不美化图表**：所有图片必须来自原 PDF 的真实区域渲染；无法提取就写 `图表缺失：无法从当前 PDF 提取` 并说明具体原因。
6. **不改动论文原始结论**；作者自述局限与 WorkBuddy 批判性判断分栏记录。

## 步骤

1. **列现状**：读自己的 `deep-paper-note.md`、`reader/paper.md`、`review-sections.json`、`paper-review.html`，列出已展示的图表（HTML 中内嵌 base64 的 `<img>` 的 alt 即图名）与正文引用到但没有图的图号。
2. **建台账**：
   ```
   python artifacts/figure_repair/extract_figures.py captions --pdf "<PDF>" \
       --json-out "<job>/agent-output/work-tmp/figure-captions.json"
   python artifacts/figure_repair/extract_figures.py scan --pdf "<PDF>" \
       --json-out "<job>/agent-output/work-tmp/figure-zones.json"
   ```
   交叉核对图号、原始图题、页码；图号以 PDF 原文为准，自动匹配错时以正文/图题文字人工校正。
3. **裁剪缺失的重要图**：
   ```
   python artifacts/figure_repair/extract_figures.py crop --pdf "<PDF>" \
       --out-dir "<job>/agent-output/reader/assets" \
       --json-out "<job>/agent-output/work-tmp/figure-crop-<批次标记>.json" \
       --dpi 170 --include-caption [--pages 3,4,6] [--include "Fig\. 2$|Table\. 1"]
   ```
   只裁真正承担证据的图：方法流程、模型结构、数据/实验流程、核心结果、对比实验、消融实验、机制示意、限制条件。
4. **程序化核验**：读 crop 清单里每个 `asset.verification`，要求 `verified=true`（区域文本含图号 或 PDF 抽取图题含图号，且 `ink_ratio >= 0.01`）。
   - `verified=false` 的图**不得**写进阅读材料，改记为缺失并写原因。
   - 自动图号与图题明显不符时，以 PDF 图题文字为准改名，并在台账里写明"自动匹配已人工校正"。
5. **写图表完整性检查**：`<job>/agent-output/figure-completeness.md`，含：重要图表总数、已展示数量、缺失数量、逐条缺失原因、对应页码、是否影响综述引用。
6. **生成补充阅读材料**：`reader-with-figures.md` = `deep-paper-note.md` 全文（逐字复制，不改结论）＋在相应位置插入新图（`![图号 图题中文](reader/assets/xxx.png)`，图下方写：图号、原始图题英文原文、页码、来源 DOI/Zotero Key）＋末尾附"图表完整性检查"。
7. **重新渲染 HTML**：
   ```
   python -m review_workflow.cli render-review <job-id> \
       "<job>/agent-output/review-sections.json" \
       --reader "<job>/agent-output/reader-with-figures.md" \
       --output "<job>/agent-output/paper-review.html"
   ```
8. **交付前自检**：
   - HTML 中 `<img src="data:image/...` 数量 ≥ 原数量＋新增图数；
   - 无 `[图片未嵌入` 标记；
   - 放大脚本（lightbox）仍在；
   - `deep-paper-note.md` 的 sha256 与 `job.json.ai_note_sha256` 一致（未改动）。
9. **回报**：按模板返回处理结果，任何未通过项标记 `blocked`。
