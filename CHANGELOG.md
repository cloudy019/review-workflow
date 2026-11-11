# 修复记录

每次修复好一个 bug 单独整理为一个 commit，格式与验收要求见 `README.md`。

## 2026-11-10 · `fix(review-docx)` 中文稿的图与文字被强制分页拆开

- **现象**：11 页 PDF 的第 4 / 7 / 10 页各只有一张图和图注，讨论这张图的正文段落留在上一页。
- **根因**：内部排版脚本（未随仓库公开）对图片段落设置 `page_break_before = True`，图注之后又执行 `add_page_break()`；图片固定 6.4 英寸宽，按 PNG 原始比例高约 6.4–6.8 英寸，即使不强制分页也难与正文同页。不是渲染意外。
- **改动**：删掉两处强制分页；新增 `image_width()` 按 PNG IHDR 比例限高 5.4 英寸（只缩不放）；引出段、图片、图注分别加 `keep_with_next` / `keep_together`，把「文字—图—图注」绑成一组。
- **验收**：docx 内 `pageBreakBefore=0`、`br type=page=0`、`keepNext=8`；Word 重新导出后 PDF 11 → 10 页，第 4 / 7 / 9 页均为「引出段 → 图 → 图注」同页紧邻；PyMuPDF 逐页文本/图片计数与三页渲染目视均通过。
- **未改动**：正文内容、图表本身、论点包、引用许可、Evidence Loop 任务状态、Zotero 数据。修改前的 docx/pdf 备份在 `.workbuddy/trash/`（不入库）。
- **环境**：项目 venv 无 python-docx、默认 3.13 缺 lxml，`py -3.12` 同时具备两者，是内部排版脚本的可用解释器；未安装任何新依赖。

## 2026-11-10 · `fix(figure-pairing)` 误检型 omit 理由抢占真实裁图

- **现象**：该篇的 `p2-f2` 决策理由是「正文对Table1的指引误检，不是独立图表」，本意是丢弃一条误检的清单行；但 `_decision_target()` 从理由里解析出 `Table 1`，使这条 `omit` 决策先于真实决策配对到 `p002_Table_1.png`，核心图表被打上「不作正文图」徽标。
- **根因**：决策 `id` 是「该页第几条」而不是图号，配对依赖理由中的图号；负向描述同样含图号，代码未区分「指向这张图」与「这张图不存在」。
- **改动**：新增 `_NEGATIVE_FIGURE_REASON_RE` 与 `_is_false_positive_decision()`；`omit` 且理由为误检/重复/纯文字引用时，`_collect_figures()` 整行跳过，不占用任何裁图。
- **验收**：新增 `test_false_positive_omit_reason_does_not_claim_a_real_crop`，`1 passed`；把 `_is_false_positive_decision` 回退成恒 False 复现旧行为时「不作正文图」重新出现，测试确实能拦住回归。
- **未改动**：任何论文产物、任务状态、引用许可；仅渲染代码与测试。

## 2026-11-10 · `fix(review-html)` 零图页面静默留白且「只看图」清空速读区

- **现象**：若干任务的 `paper-review.html` 是纯文字页；阅读结果里 11 条「纳入证据」的图表决策全部落空，页面不给任何解释。点「只看图」时 `section.hidden = !figures` 会把每一节都藏掉，只剩空白页。
- **根因**：`_collect_figures()` 在 `reader/assets/` 不存在时静默返回空列表；模板的分区筛选把「没有图」当成「这一节不用显示」。整页原图虽在 `source-pages/`，但模板/验证器已禁止原文页分区，不能兜底。
- **改动**：
  - 新增 `_figure_gap_notice()`：仅当存在图表决策却配不齐裁图时，在正文最前面输出「图表缺失」说明，写明决策条数、裁图数、整页图不能兜底的原因、逐图清单位置，以及「不臆造、不重绘」边界。
  - 模板加 `.fig-missing` 样式；零图时「只看图」按钮改标为「图表缺失」，点击后保留全部速读分区、滚到说明处并给出提示，不再隐藏任何节。
  - 同步 portable kit 的模板与 `html_review.py` 副本（保留真源已有的 `ai_review_sha256` 逻辑）。
- **验收**：3 项新回归测试通过；`artifacts/review_ui/verify.py` 三篇 baseline 页面问题数均为 0（含 JS 语法检查）；用该篇的真实记录渲染得 `class="fig-missing"=1`、`11 条` 说明、`runs/` 残留 0。
- **未改动**：论文产物哈希、任务状态、引用许可、Zotero 数据。

## 2026-11-10 · `fix(ai-gate)` 零图且视觉 QA 未通过仍能登记为 AI 核验

- **现象**：9 篇某批次任务的 `visual_review=False`、`html_visual_qa` 为 `pending`/`not_performed`，且没有一张单图裁图，却全部登记成 `ai_verified`。
- **根因**：`import_ai_review()` 只校验核验报告自身的字段与哈希绑定，从不检查「阅读结果声称的图表是否真的有图」「视觉 QA 是否完成」，缺的是前置条件门禁，不是报告格式问题。
- **改动**：
  - 新增 `ReviewOrchestrator.visual_evidence_report()`：给出可核查的图表决策数/裁图数、`visual_review`、`html_visual_qa.status` 与问题清单；`has_visual_qa_waiver()` 只接受带 `approved_by` / `reason` / `approved_at` 的人工签署豁免。
  - `import_ai_review()` 有问题且无豁免时直接拒绝，并把检查结果写入 `job.ai_verification_gate` 备查。
  - 新增只读审计 `artifacts/review_ui/audit_ai_gate.py`，不改任何任务状态。
- **验收**：`tests/test_review_core.py` 10 项全过（新增 4 项：门禁报告、拒绝登记、豁免可追溯、残缺豁免不生效）；真实库审计：12 篇已登记 AI 核验中，按现行门禁会被拦下 12 篇（其中 9 篇零裁图），报告在 `tmp/ai_gate_audit_before.json`。
- **边界**：门禁是**面向今后登记**的；存量 12 篇 `ai_verified` 未被撤销（撤销属破坏性状态变更，需重新核验或人工决定），本提交只把事实审计出来，不伪造合规。

## 2026-11-10 · `fix(tests)` v2 模板改版后 10 项旧版精确字符串断言长期失败

- **现象**：`pytest tests/test_html_review.py` 长期为 `10 failed, 9 passed`，掩盖真实回归；失败全部是断言旧版模板文案/结构，不是产品缺陷。
- **根因**：v2 评审页把八节卡改成 JS 动态渲染、灯箱改名 `id="lb"`、下载按钮文案改为「下载备用 JSON」、许可区改为「已批准的具体主张」、阅读区改成 `<div class="note" id="paper-reader">` 且 H1 交给页头标题；测试未同步。
- **改动**：逐条按现行模板契约更新断言（判定标签、导出 `allowed` 表达式、按 `dataset.summaryFilter` 的筛选、灯箱、复制提示、草稿恢复提示等）；`reader_markdown` 优先测试改用可见正文句，不再依赖被有意丢弃的 H1。**未删除任何 XSS / 离线 / 外链安全检查**。
- **验收**：`tests/test_html_review.py` 22 项全过；全量 `pytest tests/` 为 `92 passed`。

## 2026-11-10 · `fix(figure-repair)` 9 篇某批次任务从未生成单图裁图资产

- **现象**：该批 9 篇任务的 `agent-output/reader/assets/` 根本不存在，`figure_decisions` 共 115 条全部空转，评审页是纯文字页。整页 `source-pages/*.png` 虽在盘上且被哈希锚定，但模板已撤掉原文页分区，`verify.py` 明确禁止 `data-pane="pages"` / `class="page"` / 跳页按钮，连整页兜底也没有。这与 `fix(review-docx)` 修的中文稿是两条独立产物链。
- **根因**：这批任务只落了「意图清单」，没落「可显示的裁图资产」；`_collect_figures()` 读的是 `reader/assets/`，缺目录即返回空列表。
- **改动**：新增 `artifacts/figure_repair/batch_verified_crops.py`，从**原 PDF 真实区域**逐图裁切并输出 `reader/figure-crops.json`：
  - `candidate_zones()` 忽略单个近整页背景位图，避免出版社整页底图把所有 drawing 并成一个被过滤的整页 cluster（某篇初始 0 张的根因）。
  - 真图题判据要求 PDF 文本以图号本身开头，正文里 `Fig. 2 shows ...` 这类顶格交叉引用不再抢占真图题；取消 first-hit-wins 去重，渲染后按 `status=verified` 优先保留。
  - `_pair_captions_to_zones()` 改为全局贪心一对一配对；`table_text_region()` 取图题下方连续文本带的完整左右边界（保住 `Drawbacks` 最右列）；`graphic_band_region()` 按图题所在栏聚合上方图形标记，不切断 Output 标签、不框入邻栏正文。
  - 身份核验沿用 `verify_crop()`：裁剪区回抽文本含图号或同页 PDF 图题含图号，且 `ink_ratio >= 0.01`；只写 `verified=true` 的 PNG。
- **验收**：9 篇共 **109 张**通过身份核验的裁图；3 张按规则记为缺失并写明具体原因（两篇任务各自的 Fig. 5b p.14 / Fig. 7c p.17 与 Fig. 6 p.13，均为 `no_figure_region_near_caption`）。目视抽查多篇的 Table 1/2、Figure 1/5、Fig 1/5 与 Table 1/4 均完整干净。新增 3 项几何回归测试。
- **未改动**：`deep-paper-note.md`、`reading-result.json`、`review-sections.json`、`runs/*/request.json`、`source-pages/*` 等哈希锚定产物一律未动；不臆造、不重绘、不美化。裁图 PNG 与清单属任务产物，按 `.gitignore` 不入库。

## 2026-11-10 · `fix(html-figures)` 决策白名单让正式裁图漏显、决策条数被误当缺图依据

- **现象**：补齐 109 张裁图后，按旧渲染逻辑某任务仍只显示 1 张图卡（18 条决策全是同一句样板理由且都提到无关的「Fig9」，一张 Fig.9 裁图把决策全占掉）；另外只要 `figure_decisions` 条数多于图卡数，页面就报「图表缺失」，即使裁图清单显示一张不缺。
- **根因**：旧 `_collect_figures()` 把 `figure_decisions` 当作**唯一**图卡来源，先按理由里的图号抢占裁图、其余按页码填空；裁图资产本身已通过身份核验，却没有任何独立入口。旧 `_figure_gap_notice()` 又把「决策条数」和「PDF 图题条数」当成同一本账。
- **改动**：
  - `_collect_figures()` 改为「每一张 `reader/assets/` 下的 verified crop 各生成一张图卡」；决策只作为徽标/理由标注，按图号 + 页码（±1）认领，认领不到就回退清单里的原 PDF 图题。
  - 新增 `_manifest_captions()`；`_figure_card()` / `_figure_cards()` 用 `pdf_caption` 兜底，不再让无标注的图静默消失。
  - `_figure_gap_notice()` 在清单存在且有裁图时，以 `figure-crops.json` 中 `status=missing` 的逐条记录为唯一真值，输出「部分图表缺失：无法从当前 PDF 提取」并写明图号、页码、具体原因；清单齐全就不提示。零裁图且无清单时保留原「全部落空」说明。
- **验收**：9 篇落盘页面图卡数 = 正式裁图数 = 内嵌 `data:image` 数（5/7/12/6/7/18/15/17/22，合计 109）；2 篇有真实缺失项显示提示、7 篇不显示。`verify.py` 对 3 篇 baseline 与 9 篇落盘页面均 0 问题；`dom_test.js` 三篇 ALL PASS；`probe.py` 三种窗口尺寸 `overflow: none`；Edge headless 截图目视确认该篇结果节「正文 Fig5 → Fig5 图卡+图注 → 正文 Fig6 → Fig6 图卡」同屏紧邻，缺图页顶部浅黄提示框可见。新增 3 项回归测试，全量 `pytest tests/` 102 passed。
- **未改动**：模板真源/副本（本条只改渲染器，副本已与真源逐字节一致）、论文产物、任务状态、引用许可。

## 2026-11-10 · `fix(rerender)` 视图层重渲染把 AI 核验打到 needs_clarification

- **现象**：`rerender.py` 只同步 `reading_receipt.artifacts['paper-review.html']`，但 `ai_citation_permission.bound_artifacts` 也绑定了同一页面的哈希。补图后重渲染，`refresh_artifacts()` 判成「AI核验产物变更」→ `reading_receipt_invalid=True` → `ai_verified` 掉到 `needs_clarification`，人工引用许可作废。
- **根因**：两本哈希账只更新了一本；但也不能无条件把新哈希写回 AI 绑定，否则会掩盖笔记、阅读结果、请求、`source-pages` 等其它核验输入被改动。
- **改动**：
  - 新增 `ai_binding_check()`：逐字节校验除 `paper-review.html` 外的所有 AI 核验绑定输入，任一不一致或缺失即拒绝重绑定，要求重新核验。
  - 新增 `--rebind-ai-view`：仅在校验通过时同步 `bound_artifacts['paper-review.html']`，并写 `job.view_rebind_log`（时间、前后哈希、原因、其余输入的合并摘要与数量）。
  - 未加该参数却检测到 AI 绑定时输出 stderr 警告，避免误操作。
- **验收**：9 篇全部 `state_before=ai_verified` → `state_after=ai_verified`、`receipt_invalid=false`、`ai_view_rebound=true`；逐篇复核 `reading_receipt.artifacts`、`ai_citation_permission.bound_artifacts`、`view_rebind_log[-1].sha_after` 三者都等于落盘 HTML 的 sha256，且其余绑定输入逐字节未变。每篇 `job.json` 已备份到 `.workbuddy/trash/`。新增 4 项门禁回归测试。
- **边界**：只允许「视图层」重绑定；任何 AI 核验输入变化都必须重新走核验，不静默重绑定。存量 `ai_verified` 未撤销、未伪造合规。

## 2026-11-10 · `fix(review-server-gate)` 视图层重渲染后评审服务整篇返回 404

- **现象**：9 篇页面补齐图并重渲染后，评审服务首页正常，但点进任一篇返回 `404 {"ok": false, "message": "AI review content no longer matches current reading"}`。用户刷新窗口会看到 404，比原先「没有图」更糟。
- **根因**：这是**第三本哈希账**。`review_server.py:review_page()` 在 `state=ai_verified` 时先调 `core.ai_review_content()`，它把 `ai_reviews/archive/<digest>.json` 归档的 `artifact_sha256`（含 `paper-review.html` 旧哈希）与当前 `ai_review_bindings()` 逐字段比较，任何不一致抛 `StaleReviewError` → 服务转 404。`--rebind-ai-view` 只同步了 `reading_receipt.artifacts` 与 `ai_citation_permission.bound_artifacts`，没碰归档。
- **决策**：**不改归档**。归档是 AI 核验当时的证据，改写它等于伪造核验记录；也不放宽服务的整体校验。
- **改动**：
  - 新增 `ReviewOrchestrator._view_rebind_explains_page_change()`：仅当唯一不一致字段是 `artifact_sha256`、且 `view_rebind_log` 构成从**归档页面哈希**到**当前落盘页面哈希**的连续链（首条 `sha_before` 接归档值、逐条 `sha_before` 接上一条 `sha_after`、末条 `sha_after` 等于落盘哈希）、且除 `paper-review.html` 外所有产物哈希与 key 集合完全一致时才放行。
  - `ai_review_content()` 改为列出全部不一致字段，只在上述严格条件下豁免；身份字段（PDF/笔记）或其它产物变化照旧抛 `StaleReviewError`。
  - 同步 portable kit 的 `core.py` 副本。
- **验收**：新增 6 项回归测试（无日志仍拦、合法日志放行且归档字节未变、断链拒绝、其它产物变化拒绝、PDF 身份变化拒绝、连续两次重渲染链有效），`tests/test_review_core.py` 16 项全过；服务重启后无凭据 `403`、带凭据 `200`，9 篇 `/jobs/<id>` 全部 `200`，图卡数 5/7/12/6/7/18/15/17/22 与落盘页面完全一致，2 篇显示缺失提示。
- **未改动**：`ai_reviews/archive/` 归档文件、`reading-result.json`、`review-sections.json`、`deep-paper-note.md`、`runs/*`、任务状态与引用许可。
