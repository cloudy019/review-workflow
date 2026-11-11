# 待复核与已处理冲突

## 待复核（🔶）

无。本轮蒸馏未出现多文档结论冲突、结论溯源失败或取代关系不明的情形。

## 已处理的事实冲突（⚠️）

### C1. 框架 README 的 clone 地址指向已删除仓库

- **文档原文**（`evidence-loop/README.md`）：
  ```bash
  git clone https://github.com/cloudy019/evidence-loop.git
  cd evidence-loop
  ```
- **仓库事实**：该远端仓库已于 2026-11-11 删除；源码以普通文件快照收录在本仓 `evidence-loop/`，不是 submodule。
- **处理**：删除 clone 步骤，改为「源码已随本仓收录，直接进入目录安装」。
- **依据**：蒸馏阶梯第 1 级（文档与代码事实冲突）——保留记录、修正文档，不改代码。

### C2. 发布审计文档描述的历史已不存在

- **文档原文**（`evidence-loop/docs/release-audit.md`）：「This repository was initialized separately from its originating private research project…」
- **仓库事实**：该文档描述的是早期独立发布流程；当前仓库已重建为项目级单仓，沿用该叙述会误导读者以为存在另一套发布历史。
- **处理**：整篇清理，不再作为公开文档；其中「发布范围只含状态核心、渲染器、服务、schema、合成测试与 KaTeX；不含 PDF、数据库、工作区、凭据」的**结论**已并入 `CONTRACTS.md` 第 9 节「仓库白名单」。
- **依据**：阶梯第 6 级（被更晚的仓库结构取代），但保留其合规结论。
