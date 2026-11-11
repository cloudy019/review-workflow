# 未闭合风险与待办

> 由原 `CHANGELOG.md` 的「边界 / 未改动」与运行约定蒸馏。以下均为**仍未闭合**的事项，不是已解决记录。

## 1. 存量 AI 核验未重审

- 有 12 篇任务在门禁上线前已登记为 `ai_verified`。按现行门禁（图表裁图 + 视觉 QA）审计，它们都会被拦下。
- 未撤销：撤销属破坏性状态变更，需重新核验或人工决定。**不得把它们当作已通过现行门禁**。
- 影响：跨论文综合若引用这些卡片，依据仍是旧口径。

## 2. 部分图表无法从 PDF 提取

- 两篇任务各有缺失：`Fig. 5b` p.14、`Fig. 7c` p.17；`Fig. 6` p.13。原因均为 `no_figure_region_near_caption`。
- 处理：页面显示「部分图表缺失」并写明原因；**不臆造、不重绘、不用整页图兜底**。
- 待办：如需补齐，需人工确认图区或换用含可提取图层的 PDF 版本。

## 3. 论点包尚无人工批准

- 若干论点包停在 `ai_verified`、`approvals=0`。AI 核验不等于人工批准，正文引用前必须走人工批准或明确标注依据。

## 4. 旧入口默认路径已改

- `review_workflow` 的 `screen-candidate-pool`、`migrate-legacy`、`workbuddy-bridge` 默认路径已改为 `workspace/candidates/papers-screened.json`（原先绑定具体课题目录）。
- 影响：本地直接跑旧入口会找不到文件，需显式传入你的真实候选池路径。日常 `evidence_loop issue/submit/serve` 不受影响。

## 5. 筛选默认论点已改为占位

- `ScreeningRubric.central_thesis` 默认值改为「由项目配置提供的研究问题」。
- 影响：依赖默认值生成提示词上下文的流程，需从 `review-project.json` 显式传入研究问题，否则上下文变弱（关键词匹配逻辑不变）。

## 6. 发行副本靠人工同步

- `.workbuddy/skills/paper-review/references/runtime/evidence_loop/` 是 portable kit 副本，被 `.gitignore` 排除、不入库。
- 改动 `evidence-loop/src/evidence_loop/` 后必须手动 `cp` 同步，并用 `cmp` 校验；漏同步会导致「改真源但运行的是旧副本」。

## 7. 历史重写的残留

- 公开仓于 2026-11-11 以单 commit 重建并 force push。旧 commit 在 GitHub GC 前仍可能通过**直接 SHA** 访问（不能从分支或 PR 发现）。
- 本地完整历史在 `refs/local/history` 与 `.workbuddy/trash/*.bundle`，**禁止推送**。

## 8. 本机回环与代理

- VPN 残留的系统代理会让命令行请求 `127.0.0.1` 返回 `502`，这是代理响应而非服务错误。
- 排查本机服务时统一加 `curl --noproxy '*'`；浏览器通常自动绕过回环，不受影响。

## 9. 主张区重复内嵌导致页面变大

- 同一张裁图会同时出现在正文流和主张卡里，单页最大约 12 MB（改造前约 6 MB）。
- 影响：本机服务加载变慢，但可接受；若要压缩，需要生成缩略图或在页内共享同一 data URI。
- 未做：缩略图依赖图像库或前端 canvas，当前没有新增依赖。
