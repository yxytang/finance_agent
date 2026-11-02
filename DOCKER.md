# Docker 部署

容器环境包含 PostgreSQL 17、数据库迁移、单进程 API 和独立文档 worker；按需叠加 Kafka、事件发布器及两个财务投影消费者。API 与文档 worker 使用同一应用镜像，应用进程以固定非 root 用户运行。

## 启动基础服务

需要可运行 Linux 容器的 Docker Engine，以及 Docker Compose v2.24 及以上版本（也支持 v5）。Windows 可使用已启用 Linux 容器的 Docker Desktop；以下命令在项目根目录的 PowerShell 中执行。

保留现有 `.env` 中的 `DEEPSEEK_API_KEY`、`RAG_API_KEY` 等配置，增加数据库密码即可。不要用示例文件覆盖已有 `.env`。Compose 默认从项目根目录读取 `.env`，shell 中同名变量优先；变量规则见 [Docker 文档](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。

```powershell
# 将这里替换为自己保存的固定密码；首次创建数据库后，后续启动使用同一密码。
$env:FINANCE_DB_PASSWORD='填入自己的数据库密码'
docker compose config --quiet
docker compose up --build -d
docker compose ps -a
```

访问 <http://127.0.0.1:8000>。启动需要下载镜像和 Python/OCR/向量库依赖，第一次构建耗时较长。`migrate` 正常状态为 `Exited (0)`，不是常驻服务。PostgreSQL 健康检查通过后才执行迁移；迁移成功后再启动 API 和文档 worker，依赖规则见 [Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)。

```powershell
# 确认 API 就绪；该探针不调用模型。
Invoke-RestMethod http://127.0.0.1:8000/api/health
docker compose logs --tail 100 api worker migrate
```

没有模型 key 也能启动服务、导入账单和处理本地文档；Agent 对话需要 `DEEPSEEK_API_KEY`。缺少 `RAG_API_KEY` 时检索退回已有词法链路。首次启动导入镜像内示例账单，数据库中已有记录会跳过；容器不会自动读取宿主机上现有的 SQLite 数据库。

镜像包含 PDF、PPTX 和图片 OCR 依赖。旧版 `.ppt` 请先转换为 `.pptx`；当前镜像未打包 LibreOffice 转换程序。

## 配置

`.env.docker.example` 列出可用配置，凭证均留空。已有 `.env` 时只把需要的变量加入原文件；若希望单独保存 Docker 配置，可复制为 `.env.docker`、填写数据库密码，再显式同时加载两个文件：

```powershell
# 仅首次创建；如果文件已存在，直接编辑已有文件。
Copy-Item .env.docker.example .env.docker
docker compose --env-file .env --env-file .env.docker config --quiet
docker compose --env-file .env --env-file .env.docker up --build -d
```

第二种方式下，后续管理命令也使用相同的两个 `--env-file`。不要把空白模型 key 加入 `.env.docker` 覆盖原配置；示例中的模型配置为注释。检查配置使用 `config --quiet`，避免完整展开后的凭证进入终端记录。

在 `.env` 或 `.env.docker` 中填写含 `$` 的密码时，用单引号包住整个值，例如 `FINANCE_DB_PASSWORD='自己的原始密码'`，防止 Compose 将其中的 `$` 当作变量展开。

| 配置 | 用途 |
|---|---|
| `FINANCE_DB_PASSWORD` | PostgreSQL 密码，必须设置；连接地址在容器入口安全构造，密码中的 `@`、`:` 等字符无需手动 URL 编码 |
| `FINANCE_HTTP_PORT` | 宿主机端口，默认 `8000`，仅绑定 `127.0.0.1` |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` | 主模型凭证、接口和模型 |
| `RAG_API_KEY`、`RAG_EMBED_BASE_URL`、`RAG_EMBED_MODEL` | 向量检索配置 |
| `RAG_RERANK_BASE_URL`、`RAG_RERANK_MODEL` | 重排配置；任一显式置空即可关闭重排 |
| `KNOWLEDGE_OCR_MODE` | `auto`、`always` 或 `off`，控制 PDF/PPTX 内图片的 OCR |
| `FINANCE_WEB_TOKEN` | 可选页面访问令牌，使用页面提供的带 token 入口 |

变更环境变量后用 `docker compose up -d` 重建相应容器；`restart` 不重新读取变量。修改数据库环境密码不会修改已有 PostgreSQL 数据卷内用户的密码，已有库应先完成数据库用户密码变更。模型凭证只通过运行环境传入，不复制到镜像中。

## Kafka 完整链路

```powershell
docker compose -f compose.yaml -f compose.kafka.yaml config --quiet
docker compose -f compose.yaml -f compose.kafka.yaml up --build -d
docker compose -f compose.yaml -f compose.kafka.yaml ps -a
docker compose -f compose.yaml -f compose.kafka.yaml logs --tail 100 kafka-publisher kafka-summary kafka-anomalies
```

broker 健康后显式建主题；历史补事件成功后发布器运行，两个消费者分别维护收支汇总和异常结果。`init-topics`、`kafka-backfill` 与 `migrate` 均为一次性任务，成功退出是正常状态。消费者处理不安全的事件时主动停止，修复后显式重启，避免跳过财务数据。交付语义、进度接口及故障处理见 [后端 Kafka 说明](BACKEND.md#kafka-财务事件处理)。

容器内连接 `kafka:29092`；宿主机客户端连接 `127.0.0.1:9092`。该编排为本地单 broker 演示环境。

## 数据、更新与停止

| 命名卷 | 保存内容 |
|---|---|
| `postgres_data` | 账单、会话、事件日志、文档任务与财务投影 |
| `backend_files` | 上传任务暂存及可重试输入 |
| `knowledge_files` | 内置知识、上传原件和检索索引，API/worker 共享 |
| `agent_memory` | Agent 长期记忆 |
| `kafka_data` | 可选 Kafka 消息日志及 broker 元数据 |

首次创建空知识卷时，镜像内置资料会填入卷中；已有卷保留原内容，重建镜像不会覆盖它。卷生命周期见 [Docker 持久卷说明](https://docs.docker.com/engine/storage/volumes/)。共享上传与知识目录的服务保持相同用户身份；自行改为宿主机目录挂载时，需要为容器用户提供读写权限。

如果曾运行早期 root 镜像，升级后旧卷可能报 `Permission denied`。仅这种情况需要停止使用共享目录的 API/worker，并一次性修复旧卷所有权；全新部署不需要执行：

```powershell
docker compose stop api worker
docker compose run --rm --no-deps --user 0 api chown -R 10001:10001 /app/data/backend_jobs /app/knowledge /app/memory
docker compose up -d
```

```powershell
# 更新代码和镜像，先完成迁移，再启动应用。
docker compose up --build -d

# 停止并移除容器，保留数据库、知识及记忆卷。
docker compose down

# 启用了 Kafka 时，停止也带相同的配置文件。
docker compose -f compose.yaml -f compose.kafka.yaml down
```

`down --volumes` 会删除对应项目的数据卷，日常停止不要添加该选项。数据库端口不发布到宿主机。保持单个 API 进程；当前确认等待与会话恢复机制不能直接改为多个 Uvicorn worker。

## 验证范围

本机未安装 Docker Engine，尚未在本机执行镜像构建或容器启动。基础与 Kafka 两套配置已通过官方 Compose v5.6.1 检查；数据库密码编码单元测试、后端回归及独立 HTTP/文档 worker 的重启冒烟已通过。CI 新增镜像与基础 Compose 冒烟验证，用无模型请求检查 API、账单导入、异步文档处理、非 root 身份及重启后的数据保留；工作流配置已加入，不代表远程 CI 已执行成功。Kafka 的真实 broker 集成测试与容器编排验证分别记录。
