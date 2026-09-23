# 运行手册（开发预览）

此版本尚未经过 NAS 与 Mac 实机验收。生产部署前应完成 `docs/verification/README.md` 对应门槛。以下步骤用于本地试用和部署验证。

## 中心服务

需要 Python 3.12、uv 0.12.17。`uv sync --frozen` 后运行 `uv run awb init-owner --db .local/server.db`，交互输入至少 12 字符密码，再运行 `uv run awb serve --db .local/server.db`。浏览器打开 `http://127.0.0.1:8765`。首次初始化只允许一次。服务器只使用一个 Uvicorn worker。

NAS 使用 `deploy/compose.example.yaml` 作为模板，在真实 NAS 上按实际路径和架构调整。容器只对 NAS 本机监听 8765，外部访问应经可信 HTTPS 反向代理，并保持应用登录。部署卷 `/data` 只存本工具数据，绝不挂载 Codex/Hermes 凭据目录。首次设置 owner：`docker compose run --rm agent-workbench init-owner --db /data/agent-workbench.db`；构建启动后检查 `/health/ready`。该镜像在当前 Windows 主机没有 Docker，尚未实测。

## 采集端

在每台需要采集的机器安装 Python 3.12 与 uv，在仓库运行 `uv sync --frozen`。网页生成 10 分钟一次性配对码，再运行 `uv run awb pair https://YOUR-HOST CODE`。设备 token 保存在 `.local/collector.json`，此文件必须保持私有。`awb add-source` 仅把只读根目录和策略保存在本机；网页的 owner 仍须确认来源 ID、设备、Agent、Profile 与策略，才能上传。`stats_only` 不上传正文；`full_content` 仅对用户/助手文本上传经过规则脱敏的正文，不能保证识别所有秘密。源目录缺失或版本不兼容会在心跳中报错，不修改原 Agent 数据。

`uv run awb collect-once` 做一次扫描和传输，`uv run awb collect --interval 15` 持续采集。Windows 用户登录任务或 macOS LaunchAgent 的自启动脚本尚未打包；暂时需由用户自行保持进程。采集器 outbox 在 `.local/outbox.db`，断网时保留待传内容。不要把 `.local` 打包公开。

## 文件核验与交接

文件核验只接受逻辑根下的相对路径。先在目标机器执行 `uv run awb map-root ROOT_ID PATH --map-version 1`，再通过 owner API `PUT /v1/roots/ROOT_ID/maps/DEVICE_ID` 登记相同路径与版本；`POST /v1/file-checks` 生成 10 分钟任务。采集器在下一轮主动轮询自身任务，在本机验证解析后的路径仍在授权根内；哈希检查默认最多 20 MiB。结果只证明该目标机器当时的文件状态。

`POST /v1/handoffs` 接受已采集的源会话 ID 和目标设备 ID，生成固定内容 ZIP，`GET /v1/handoffs/{id}/download` 只允许 owner 下载。用户应手动在目标 Agent 创建新会话，确认导入历史，再通过 `/continuations` 绑定目标会话。交接包不代表任务已经成功续做。

## 备份与恢复

`uv run awb backup --db .local/server.db --output .local/backup.zip` 使用 SQLite 在线备份 API 创建一致性快照和 SHA256 清单。`uv run awb restore-to .local/backup.zip .local/restore-check` 在**空目录**验签、检查数据库完整性并更换 `server_epoch`。这是验证恢复，不自动替换现有中心。备份含 owner 密码哈希与设备 token 哈希，须按私有数据保护。建议用 NAS 任务计划每天运行备份并保留 14 天，另行复制至另一设备；自动轮转和安全撤销日志的恢复合并尚未实现，见路线图。

## 资源采样

在 `.local/collector.json` 设置 `"sample_resources": true` 可启用采样。默认关闭。当前只记录进程名包含 codex/hermes 的 PID、创建时间、RSS 与短时 CPU 样本，保留原始样本 7 天。它不能证明会话级资源归属，也不是连续峰值。
