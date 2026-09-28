# Agent Workbench Windows 便携版

解压整个 ZIP 后双击 `AgentWorkbench.exe`。浏览器会打开 `http://127.0.0.1:8765/`，首次直接在网页创建至少 12 位管理员密码。用量来自本机 Codex 原生日志；应用只读扫描日志，并在解压目录的 `data/` 保存统计索引。请勿把 `data/` 上传或分享。

当前版本提供 Codex、Claude Code 与 Hermes 用量统计，支持自选起止日期。Hermes 的历史 Token 仅有会话模型汇总，无法准确拆到每日。不要同时运行便携版与安装版，因为两者默认使用同一端口。详细测试步骤见[Windows 测试说明](https://github.com/zznmdhz/agent-workbench/releases)。
