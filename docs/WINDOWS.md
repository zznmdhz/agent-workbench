# Windows 安装与使用

## 安装

从 [GitHub Releases](https://github.com/zznmdhz/agent-workbench/releases/tag/v0.2.4) 下载 `AgentWorkbench-Setup-0.2.4-Windows-x64.exe`。安装程序使用当前用户权限，不需要管理员权限。安装后从开始菜单打开 **Agent Workbench**，默认浏览器会打开 `http://127.0.0.1:8765/`。首次使用直接在网页创建至少 12 位管理员密码，完成后自动进入工作台；之后打开会显示网页登录。程序在后台运行，网页左侧的“关闭工作台”可停止服务和采集。再次点击快捷方式会打开已有工作台。

首次启动自动检测本机的 Codex 会话目录和 Hermes 数据库，并以**仅统计**策略只读采集。历史数据较多时，首次导入需要几分钟；总览显示待处理数量并每 15 秒自动更新，“设备与设置”显示来源、最近扫描和待传队列。总览默认显示香港时区的当天，可以切换单日、近 7/10/15 天，以及设备、Agent、模型和报告时区。单日时间线默认只显示前 8 条轮次，按需展开；多日视图显示逐日趋势。

总览把逐请求 Token 与 Codex 累计计数差值分别显示；两种记录可能重叠，不能相加为全部 Agent 的总量。Codex 差值只包含同日相邻快照可确认的输入/输出。缓存输入包含在累计输入 Token 内，不再相加。历史日志在升级后会自动重新只读扫描一次，数值可能稍后才出现。开头余额、跨日间隔及 Hermes 仅会话聚合的用量无法按日分配，仍显示“未采集”，不会补造拆分值。旧版已导入的 Codex 子 Agent 身份错误需按下方的离线修复步骤处理，安装升级不会悄悄改写旧数据库。

如果升级后看到登录页，说明这台电脑已有管理员密码，原密码会继续有效。忘记密码时，先在登录页点击“关闭工作台”，然后从开始菜单打开“重设管理员密码”。程序会确认是否清除旧密码与网页登录状态，随后在网页重新创建密码。设备、会话和统计数据会保留。

如希望查看对话正文，在“设备与设置”把本机来源切换为“保存脱敏后的正文”；新记录随后按此策略采集。旧记录还需在网页的历史正文补采区选择来源、日期或项目范围，查看可恢复条数和隐私提示后启动补采。补采只读取仍存在的 Codex/Hermes 本机原始记录，运行进度会显示在页面；原始来源从未保存、已删除或超出所选范围的正文不能恢复。每条正文最多保存 16,000 字符，并做有限规则脱敏；此规则不能保证去掉所有隐私信息，开启前请自行判断。默认保持仅统计。已删除会话的正文会在本地队列中被阻止再次导入，重复扫描不会让它重新出现。

会话页可通过标题、时间和设备辨认记录，按轮次或文件切换。轮次中有原生关联时显示原生依据；只有唯一时间窗能对应时标为推断。文件栏目前只对成功且可配对的 Codex `apply_patch` 操作建立证据，因此没有文件条目并不代表没有产物。点击本机文件检查会读取**当前**文件的大小、修改时间和检查时间，不把当前状态当作历史产出大小。Token 卡片中的缓存输入是输入的一部分；跨日余额和未归属部分仍会明确标为未知或未分配。

## 数据、备份和升级

安装文件默认位于 `%LOCALAPPDATA%\Programs\AgentWorkbench`；本机数据库、采集队列和设备凭据位于 `%LOCALAPPDATA%\AgentWorkbench\data`。卸载程序不会删除这些私人数据；重装同一版本或升级会继续使用原数据。请勿分享或上传 `data` 文件夹。

在 PowerShell 中运行以下命令生成一致性备份；可以在工作台运行时执行：

```powershell
& "$env:LOCALAPPDATA\Programs\AgentWorkbench\AgentWorkbenchCLI.exe" backup --db "$env:LOCALAPPDATA\AgentWorkbench\data\agent-workbench.db" --output "$env:USERPROFILE\Documents\agent-workbench-backup.zip"
```

恢复时先在网页点击“关闭工作台”，使用 `AgentWorkbenchCLI.exe restore-to <备份 ZIP> <空目录>` 校验并解包，再根据[运行手册](RUNBOOK.md)核对恢复代际、设备队列与备份后的数据缺口。恢复备份不代表自动补回已经从原始来源和 outbox 中删除的事件。

从旧便携预览版迁移：先关闭旧版服务与新版工作台；将旧版解压目录的 `data` 文件夹完整复制到 `%LOCALAPPDATA%\AgentWorkbench\data`，覆盖前先备份已有的新版本数据。数据库、`collector.json` 和 `outbox.db` 必须一起迁移。启动新版后核对设备、来源和待传状态，再删除旧版目录。不要让两套程序同时使用同一份数据。升级 v0.2.0 安装版时，旧密码与数据会保留，无需重新创建密码；如果旧版仍占用 8765 端口，请先关闭旧版命令窗口。

### 已导入旧版 Codex 数据的离线修复（高级操作）

仅当旧版已导入的 Codex 记录出现子 Agent 归属错误或异常 Token 比例时使用。全新安装无需运行。先等待“设备与设置”里的待传队列归零、历史补采结束，再在网页点击“关闭工作台”；不要让另一份工作台同时运行。修复会只读重放仍保留的 Codex JSONL，先生成备份与候选数据库，不会在预览阶段改写原库；来源文件不存在、队列未清空或数据无法安全保留时会中止。以下命令需在**同一个 PowerShell 窗口**依次运行：

```powershell
$awbCli = Join-Path $env:LOCALAPPDATA 'Programs\AgentWorkbench\AgentWorkbenchCLI.exe'
$awbData = Join-Path $env:LOCALAPPDATA 'AgentWorkbench\data'
$repairRoot = Join-Path $env:LOCALAPPDATA ('AgentWorkbench\repairs\codex-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$stageDir = Join-Path $repairRoot 'stage'
$backupDir = Join-Path $repairRoot 'backup'
& $awbCli repair-codex-identity --db (Join-Path $awbData 'agent-workbench.db') --outbox (Join-Path $awbData 'outbox.db') --config (Join-Path $awbData 'collector.json') --stage-dir $stageDir --backup-dir $backupDir --offline-confirmed
if ($LASTEXITCODE -ne 0) { throw '预览失败，请勿执行 --apply' }
Get-Content -LiteralPath (Join-Path $stageDir 'repair-report.json') -Raw
```

先查看报告中的 `status` 是否为 `staged_not_applied`，以及 `body_salvage.restored` 是否等于 `body_salvage.old_readable`、`ambiguous` 和 `unavailable` 是否均为 0；报告同时列出归档的旧孤儿会话及新旧数量。如果命令报错或报告有疑问，就不要执行下一步。确认后仍保持工作台关闭，应用刚才的候选数据库和队列：

```powershell
& $awbCli repair-codex-identity --db (Join-Path $awbData 'agent-workbench.db') --outbox (Join-Path $awbData 'outbox.db') --config (Join-Path $awbData 'collector.json') --stage-dir $stageDir --backup-dir $backupDir --offline-confirmed --apply
```

命令会再次核对原库指纹并在 `$backupDir` 保留可回滚副本；若数据库旁仍有 SQLite `-wal` 或 `-shm` 文件，会中止，防止旧日志重放到新库。此时保持工作台关闭并排查仍在使用数据库的进程，完成离线检查后重新预览，不要手工删除尚未核实的日志。修复目录与备份可能包含私人会话内容，请仅保存在本机，勿上传 GitHub。重开工作台后，在总览核对来源状态、会话与 Token 依据。

## 当前范围

本安装程序为 Windows 单机版。它支持本机 Codex/Hermes 只读采集、网页查看、搜索、来源策略、交接包和手工备份。Mac/NAS 实机接入、真正的双机交接、系统服务/登录自启动、自动更新和代码签名不包含在本次 Windows 验收内。跨机部署请先看[公开路线图](project/ROADMAP.md)，不要把本机通过视为跨机功能已验收。
