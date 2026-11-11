# tests

> 回归测试。**改代码必须跑**；这里的失败代表行为契约被破坏，不是「测试过时」。

## 运行

```bash
.workbuddy/venv/Scripts/python.exe -m pytest tests/ -q
```

当前基线：**108 passed**。

## 覆盖范围

| 文件 | 覆盖 |
|---|---|
| `test_review_core.py` | 状态机、许可哈希、AI 核验门禁、视图重绑定 |
| `test_html_review.py` | 评审页渲染、图卡落位、缺失提示、离线与 XSS 安全 |
| `test_queue.py` | 批次队列、并发锁、陈旧锁回收 |
| `test_review_server.py` | 本机服务鉴权与路由 |
| `test_figure_crops.py` | 裁图几何、图题配对、背景位图处理 |
| `test_rerender.py` | 视图层重绑定的 AI 绑定校验 |
| `test_screening.py` / `test_skill_router_writing.py` | 初筛规则、写作审计 |
| 其余 | 注册表、库迁移、便携阅读、候选来源、CLI 集成 |

**不覆盖**：真实 PDF 的科学质量、真实模型输出质量、浏览器视觉渲染（后者用 `artifacts/review_ui/shoot.py` 截图人工看）。

## agent 规则

1. 修 bug 先加能复现的回归测试，再改代码；不删失败测试来「变绿」。
2. 不删除任何 XSS、离线、外链安全检查断言；模板改版时按现行契约更新断言，而不是删掉。
3. 测试使用合成 PDF 与临时目录，不读真实文献库、不产生模型账单。
