# 首版测试说明

## 1. 自动验收

在项目根目录运行：

```powershell
python -m pytest -q
```

测试覆盖单篇输入、三篇并行门、重复领取、来源哈希、论文速读分类、标题复制、公式离线渲染、图片放大、本机自动保存、HTML 草稿/导出、人工许可失效、旧库迁移、确认卡聚合和正文写作阻断。

## 2. 正式流程冒烟测试

正常流程不要求用户输入论文 ID：

```powershell
python -m review_workflow.cli screen-candidate-pool workspace\candidates\papers-screened.json
python -m review_workflow.cli verify-candidate-sources --max-attempts 60
python -m review_workflow.cli select-next-batch
python -m review_workflow.cli start-next-batch
```

检查 `文献知识库/screening/next-batch.md`：应有最多三篇 AI 自动选择的论文。检查每个 `agent-task.json`：只能有一个 `paper_id`、一个 `pdf_path` 和一个独占输出目录。上一批仍在阅读或人工评审时，再次启动下一批必须失败。

## 3. 人工界面测试

阅读代理完成后，双击项目根目录的 `AI论文人工评审.exe`，等程序显示“已启动”后，浏览器首页应先显示“未评审 / 已评审 / 全部”三个入口及总篇数。点击一篇有阅读页面的论文，逐节选择“正确、部分正确、错误、无法判断”或“跳过”；可随时关闭网页，已保存答案会从论文目录恢复。点击“保存评审”后，记录应直接出现在该论文的 `human_reviews/`，不会弹出位置选择窗口。用户只判断证据角色和论文价值，不逐篇手写具体主张；AI候选主张在章节论点包阶段再交给用户选择。

点击程序中的“交给WorkBuddy更新”后，应生成 `文献知识库/review-app/workbuddy/inbox/queue-refresh.request.json` 和 WorkBuddy 运行日志。请求中的已读/未读数量必须与 `review-app/library.json`、`review-app/论文文件总索引.md` 一致；WorkBuddy 一次只处理一批，不能越过三篇人工门，每个子任务只能有一篇论文。

把正式 JSON 放入 `文献知识库/review-inbox/`，运行 `import-human-review`。随后修改该论文 PDF 或 AI 笔记并再次导入旧 JSON，系统应因哈希不一致拒绝旧许可。三篇均进入 `human_confirmed`、`excluded` 或 `blocked_source` 后，下一批才可自动启动。
