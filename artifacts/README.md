# artifacts

> 过程产物与验证工具。**不是正式资产**：只有 `review_ui/` 与 `figure_repair/` 下的代码入库，其余为本地中间物。

## 结构

| 子目录 | 内容 | 入库 |
|---|---|---|
| `review_ui/` | 页面验证栈：`build_preview.py` → `verify.py` → `dom_test.js` → `probe.py` → `shoot.py`、`rerender.py`、`audit_ai_gate.py` | 是（py/js/md） |
| `figure_repair/` | 图表提取、身份核验、批量裁切（`extract_figures.py`、`batch_verified_crops.py`） | 是（py/md） |
| `md_xai_search/` | 文献检索与 Zotero 导入脚本（本地使用，含候选数据） | 否 |
| `20261015_statistics_*` | 汇报统计与图表的构建中间产物（deck/figures/template） | 否 |
| `20261015_xai_ai_share/` | 单次 PPT 构建草稿（含 `node_modules`） | 否 |
| `portable-paper-review/` | 发行包副本，可由构建命令重新生成 | 否 |
| `evidence-loop-*` | 迁移备份、原生 demo、wheel smoke 输出 | 否 |
| `diagrams/` | 示意图源文件 | 否 |

## agent 规则

1. 验证页面结构的顺序固定：`build_preview.py` → `verify.py` → `dom_test.js` → `probe.py` → `shoot.py`。`verify.py` 只检查结构，布局问题必须看截图。
2. 只有 `review_ui/` 与 `figure_repair/` 的 **py/js/md** 入库；`*.png`、`preview/`、`shots/`、`*.json` 等产物一律不入库。
3. 图表裁切**不臆造、不重绘**；无法提取就写明确缺失原因，不用整页图兜底。细则见 `docs/CONTRACTS.md` §5。
4. `portable-paper-review/` 与 `evidence-loop-*` 是历史构建残留，可清理；清理前确认没有唯一副本。
5. 新增长期工具请放进 `review_ui/` 或 `figure_repair/` 并在本表登记；一次性脚本不要留在 `artifacts/`。
