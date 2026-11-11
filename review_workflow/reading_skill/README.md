# paper-review 可迁移能力包

把完整文件夹交给Codex或WorkBuddy，读取 `SKILL.md`。当前模型完成单篇AI阅读，Python负责原文抽取、离线页面渲染和产物登记。所有依赖资源在包内，模型与工具权限由宿主提供。

迁移安装：`python paper-review/scripts/review.py install --engine both --project <项目根目录>`。依赖安装到宿主实际使用的Python：`python -m pip install -r paper-review/requirements.txt`。检查：`python paper-review/scripts/review.py doctor`。

接续项目已有reading任务使用 `task`；新项目对明确给定的单篇本地来源使用 `prepare`。完整操作与文件协议见 `references/single-paper.md`。能力包不含私人PDF、文库状态、密钥或虚拟环境。

非交互WorkBuddy须预先获得读取、执行和隔离目录写入权限。安装不会修改权限设置或使用跳过权限审查的参数；可以先在WorkBuddy交互会话中正常授权。
