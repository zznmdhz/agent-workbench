# Windows 使用说明

当前安装版为 **v0.4.1 多 Agent 用量测试版**。安装、首次登录、日期范围、Agent 筛选、页面检查与问题反馈步骤请看 [Windows 测试说明](MVP_WINDOWS_TEST.md)。

当前源码为 v0.7.0，包含 Workbench 持久底库及按设备交换包的双机同步逻辑。Windows 默认查找 `B:\Sync_AI`，实际位置不同可在页面顶部设置。模型报表和会话高级检索已加入源码，操作与口径见[模型分析与会话检索](project/MODEL_REPORT_AND_SESSION_FILTERS_V0.7.md)。Mac 已做基础验证；Windows 安装、Agent 发现与 NAS 双机同步仍待 Windows 实机验证。详见[底库与双机同步](project/DURABLE_ARCHIVE_SYNC_V0.6.md)。

工作分支的本机预览已移除密码，打开页面直接查看数据；该修改尚未进入已发布安装包。

程序安装在 `%LOCALAPPDATA%\Programs\AgentWorkbench`；工作台自己的数据库在 `%LOCALAPPDATA%\AgentWorkbench\data`。原始 Codex 会话仍留在 Codex 自己的目录，应用仅只读扫描。卸载程序通常不会删除工作台数据库；需要彻底重置时，先关闭程序并备份需要保留的旧工作台数据，再删除该目录。

v0.2.x 的会话正文、文件、设备与跨机操作文档属于历史版本，不适用于当前 MVP 页面。产品分期见 [MVP 规格](project/CC_SWITCH_MVP.md)。
