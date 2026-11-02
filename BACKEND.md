# 后端实现与验证

2026-10-27。当前面向个人使用：一个 API 进程、共享账单与知识目录，文档 worker 与可选 Kafka 财务事件 worker 可以独立运行。增加持久化和任务恢复，不等于已经支持多用户隔离或任意水平扩容。

## 启动

```powershell
python -m pip install -e ".[web,documents,dev,cache]"
# 全新数据库先做迁移；SQLite 默认写入 data/backend.sqlite3。
python -m alembic upgrade head
python -m web.server --port 8767
```

本机默认自动创建缺失表、后台启动两个文档 worker。原来的 `.env` 不需要重建，也不要提交。切换到 PostgreSQL 时，在启动进程前设置 `FINANCE_DATABASE_URL=postgresql+psycopg://用户名:已编码密码@主机:5432/数据库`，先迁移再启动。密码含特殊字符时需 URL 编码。迁移命令和 Web 进程都从现有配置加载环境，外部显式环境变量优先。

首次 Web 启动会按规则与已有分类缓存为示例 CSV 分类后导入数据库，之后相同输入跳过。CLI 仍保持 CSV/JSON 工作流；Web 的分类纠正以数据库为准，CLI 的缓存修改不会反向覆盖已有数据库账单。原始 CSV 保留，不会被数据库更新改写。`FINANCE_BOOTSTRAP_CSV` 可指定另一份初始 CSV，指定不存在路径则跳过自动导入。

已有开发数据库若由首版自动建表产生，需先确认其表结构与迁移 `20261026_01` 一致，才能用 `alembic stamp 20261026_01` 记录初始版本，再执行 `alembic upgrade head` 增加财务事件表。stamp 不会改变或验证表结构，不适用于未知或不匹配数据库；已经包含新表的未版本化数据库也不能直接套用这一步。后续结构变化应执行增量迁移，不能用 stamp 跳过实际变更。

独立启动文档进程：

```powershell
$env:FINANCE_DOCUMENT_WORKER='external'
python -m web.server --port 8767
# 另一个终端，使用相同的数据库 URL、暂存路径和知识库文件系统
python -m backend.worker --concurrency 2
```

`compose.yaml` 提供 PostgreSQL 17、迁移、API 与独立 worker；应用以非 root 用户运行，数据库、知识库、暂存和记忆使用持久卷。设置固定的 `FINANCE_DB_PASSWORD` 后执行 `docker compose up --build -d`，现有 `.env` 中模型/RAG 配置由 Compose 透传；容器入口安全构造数据库地址，密码无需手动 URL 编码。数据库端口不发布，API 默认发布到 `127.0.0.1:8000`，带独立健康检查。配置示例、可选 Kafka 与管理命令见 [Docker 部署](DOCKER.md)。本机未安装 Docker，镜像构建与启动检查已加入 CI，尚未在本机完成容器验证。

## 账单与分类

- 金额：PostgreSQL `NUMERIC(20,2)`；SQLite 存整数分，领域计算保持 `Decimal`，退款负号原样保存。
- 导入：文件 SHA256 与“账户＋交易编号”两层幂等。重复文件返回已有批次，重叠 CSV 跳过已有交易；同一编号内容变化拒绝并整批回滚。不同编号的同日同额交易会保留，交给异常检测判断双重扣款。
- 分类：商户归一化后使用版本号比较更新，确认期间被其他修改覆盖会拒绝旧版本。分类覆盖与前后值审计在同一事务提交，支持幂等请求键。Web 不再把分类缓存和 Markdown 记忆当成两个需要同步提交的事实来源。
- 数据一致性：Web 查询、异常检测、预测、子 Agent 和 MCP 账单子进程使用同一关系数据库。MCP 子进程只得到显式的数据库连接配置，不继承模型 API key；stdio 请求串行配对，有超时与输出大小上限。

HTTP `POST /api/ledger/import` 接收原始 UTF-8 CSV，20MB 上限，列为 `date,merchant,amount,account,txn_id`；重复导入返回批次结果，同源冲突返回 409。当前没有新增账单字段映射预览页面。

## Kafka 财务事件处理

Kafka 用于账单写入后的业务事件分发。原有文档任务队列、Agent 工具查询与 SSE 续播保持各自职责；API 默认不创建 Kafka 客户端，也不要求 broker 在线。只有显式启动事件发布器或消费者才连接 Kafka。

每笔真正新增的交易产生一条 `finance.ledger.v1` 事件，事件与账单在同一个数据库事务提交。重复文件与重叠记录不会重复生成事件；导入中途冲突会同时回滚账单和事件。消息以账户作为 key，金额使用十进制字符串。事件发布器从 Outbox 读取，等待 broker 确认后才标记已发布；失败保留记录并退避重试。发布成功后、数据库确认前退出仍可能重复投递，因此这是至少一次传递。

两个独立消费者组分别更新收支汇总和异常检测结果。消费者把事件去重记录与结果更新放在同一个数据库事务，数据库提交成功后才同步提交 Kafka 消费位点；位点提交失败允许重读，不重复累计财务金额。相同账户写入同一分区，Kafka 只保证分区内顺序；该项目没有跨数据库与 Kafka 的分布式原子事务，也不声称模型调用等外部副作用恰好执行一次。

金额沿用本项目“支出为正、收入或退款为负”的账单约定，不能把所有负数都解释成工资收入。HTTP 导入返回成功表示账单已保存，不表示后台投影已经更新；统计和异常结果是最终一致的派生数据，原有工具仍直接查询账单事实。

### 本地进程

Kafka 客户端是独立 extra，不加入基础或 `web` 默认依赖：

```powershell
python -m pip install -e ".[web,kafka]"
python -m alembic upgrade head

# 为升级前已经存入数据库的交易补缺失事件；可在 Kafka 启动前执行。
# 幂等执行，不重复创建已存在的交易事件。
python -m backend.kafka_worker backfill

# 先启动可访问的 broker；本机使用下方编排的外部监听地址。
$env:FINANCE_KAFKA_BOOTSTRAP_SERVERS='127.0.0.1:9092'
$env:FINANCE_KAFKA_TOPIC='finance.ledger.v1'
$env:FINANCE_KAFKA_GROUP_PREFIX='finance'
python -m backend.kafka_worker init-topics
python -m backend.kafka_worker publish
```

另开两个终端，使用相同数据库 URL、bootstrap、topic 与 group prefix，分别运行：

```powershell
python -m backend.kafka_worker consume --projection summary
python -m backend.kafka_worker consume --projection anomalies
```

`init-topics` 显式初始化主题；客户端不依赖自动建主题。`backfill` 是为数据库中缺失的历史交易事件补齐 Outbox，不会把所有已发布事件重新发送，也不会清空消费者去重状态。

### 可选容器编排

`compose.yaml` 保持原有 PostgreSQL、迁移、API 和文档 worker；增加第二个配置文件才启动 Kafka 链路。镜像安装 Kafka extra 本身不会连接 broker。先按启动章节设置 `FINANCE_DB_PASSWORD`，然后执行：

```powershell
# 完整本地链路：迁移、单 broker、主题初始化、历史补事件、发布器与两个消费者。
docker compose -f compose.yaml -f compose.kafka.yaml up --build -d
docker compose -f compose.yaml -f compose.kafka.yaml ps -a
docker compose -f compose.yaml -f compose.kafka.yaml logs --tail 100 kafka-publisher kafka-summary kafka-anomalies

# 只启动 broker，供本机 Python 进程使用。
docker compose -f compose.yaml -f compose.kafka.yaml up -d kafka
```

配置固定使用官方 `apache/kafka:4.1.2` JVM 镜像、KRaft 单节点和持久卷，broker 关闭自动建主题。容器客户端连接 `kafka:29092`，宿主机连接 `127.0.0.1:9092`；这两个地址由不同监听器通告，不能把容器地址直接用于宿主机 Python 进程。broker 默认保留消息 168 小时，主题自身配置可以覆盖它。镜像版本和监听配置依据 [Kafka 4.1 Docker 文档](https://kafka.apache.org/41/getting-started/docker/)及[官方单节点示例](https://github.com/apache/kafka/blob/4.1.2/docker/examples/docker-compose-files/single-node/plaintext/docker-compose.yml)。

`init-topics` 和 `kafka-backfill` 是执行一次后退出的服务；发布器等待主题初始化和补事件成功，再开始投递。消费者启动前等待迁移和主题初始化。不同消费者组独立追踪位点，某个投影停止不阻塞账单导入和另一个投影。该编排使用明文内网通信和单副本，只用于本地演示，不具备 broker 高可用或生产环境认证隔离。

### 进度与故障处理

以下接口沿用现有 Web token 校验，可通过 `?token=...` 查询：

- `GET /api/finance/summary`：读取后台收支汇总。
- `GET /api/finance/anomalies`：读取后台异常检测结果。
- `GET /api/finance/events/status`：检查数据库内事件及投影处理进度。

导入后应同时观察待发布事件和投影进度。Outbox 发布完成不代表消费者已经处理完成；消费者落后称为 lag。状态接口与处理日志用于定位积压，不能把 broker 在线、待发布为零或 HTTP 201 当作所有结果已同步的证明。

broker 暂时不可用时账单仍可导入，事件留在数据库中，发布器持续重试，指数退避上限60秒，恢复后继续投递。消费者遇到无法解析或无法安全处理的事件时采取 fail-stop：退出并保留该位点，修复事件协议或处理逻辑后显式重启，不跳过财务数据。当前没有自动死信队列、专门的延迟队列或毒消息自动修复；消费者不会靠无限重试掩盖数据错误。

重放受 Kafka 保留期约束，过期消息不能仅靠重置位点找回；数据库账单仍是事实来源。消费者数据库去重按业务投影保存，因此仅更换 group prefix 或重置 Kafka 位点不会自动重算已处理事件。需要变更计算规则或完整重建投影时，应先设计投影版本与受控重建流程；当前 `backfill` 只补缺失 Outbox，不是重建命令。详细交付语义见 [Kafka 设计文档](https://kafka.apache.org/41/design/design/)。

本机未安装 Docker，尚未进行该编排的容器启动验证；配置检查、离线故障测试与本地独立 broker 的集成测试分别报告，不能将替身测试描述为已验证 Kafka 集群运行。CI 已配置官方 `apache/kafka:4.1.2` service、Kafka 客户端 extra 和 `FINANCE_TEST_KAFKA_BOOTSTRAP_SERVERS`，使真实 broker 测试在 CI 中参与执行；配置存在不代表 GitHub 远程工作流已运行成功。

2026-10-27 本地验收：使用真实 Apache Kafka 4.1.2、PostgreSQL 17.11 与 SQLite，完整回归 **754 passed、5 skipped**，前端 **11 passed**。真实 broker 验证包含 Outbox 投递、相同事件重投、数据库提交后消费位点提交失败再重启，以及 broker 不可达时保留账单与待发事件。Windows 原生 Kafka 删除打开的日志分段时可能触发文件锁，集成测试在 Windows 保留独立测试主题，Linux CI 正常删除；推荐通过 Linux 容器运行 broker。验收使用隔离数据库与临时 broker，未修改现有业务账单，也未调用收费模型。

## 文档任务

上传 `POST /api/knowledge/files/{filename}` 返回 202；`GET /api/jobs/{id}` 查询，`POST /api/jobs/{id}/cancel` 取消，`POST /api/jobs/{id}/retry` 重试失败或取消任务。文件名、大小和访问 token 在入口校验，格式解析错误通过任务状态返回。

状态为 `queued → running → succeeded`；临时 I/O 失败进入 `retry_wait`，默认最多尝试三次，指数退避最高60秒。损坏/空白文档、缺少依赖、同名不同内容等确定性错误直接 `failed`。待执行任务可立即取消，运行中仅协作取消，不承诺撤回已经发布的原件。

任务先完整落盘并校验内容指纹，再发布数据库记录；CAS 领取、定期续租、租约令牌隔离过期 worker 的状态写入。每个知识库的 OS 文件锁串行保护原件发布与索引重建，进程退出时释放；相同原件已发布而任务状态尚未提交时，可核对内容并恢复索引后完成。任务状态与文件副作用不是跨资源原子事务，不能宣称所有操作恰好执行一次。

成功任务清理 `.upload` 暂存；失败/取消保留输入供重试。暂存目录默认 `data/backend_jobs`，可用 `FINANCE_JOB_STAGING_DIR` 修改。当前限制单文件20MB、每进程最多两个解析 worker，尚未设置整个队列的容量配额、过期清理或长期归档策略。原生 OCR 在线程中执行，停止服务会等待当前解析结束；未实现硬性终止 OCR 的子进程超时。

## 会话与 SSE

每个会话的事件通过数据库事务分配单调 `seq`，SSE 写入 `id:`；浏览器自动重连带 `Last-Event-ID`，服务按序返回其后的事件，前端再做序号去重。首次打开或刷新清空当前视图后从头回放；客户端不只保存一个游标而丢掉对应历史内容。

模型消息在完成一轮后与忙碌状态、`turn_end` 一起提交。重启恢复已完成轮上下文，未完成轮记录中断并解除 busy，不自动重试整个 Agent 或重新执行写工具。确认请求附独立 `confirmation_id`，Web 拒绝缺失或过期编号，历史已解决确认在回放中关闭。

分析执行使用固定线程池，默认两条运行线程＋八个等待名额；同会话 busy 比较更新防止重复执行，容量满返回429。关闭排队任务不会调用模型。该容量控制是并发保护，不是经过压测的 QPS 承诺。API 必须保持单进程，不能直接使用多个 uvicorn workers，否则启动恢复与内存确认对象可能互相干扰。

## 重试与熔断

主模型保留 SDK 层一次重试，没有给整个 Agent 外套重试。embedding 和 rerank 关闭/避免底层额外重试，由同一策略最多尝试两次；连接/传输失败、408、429、5xx 才重试，400/401/403/422、格式与配置错误不重试。短退避带抖动，较长 Retry-After 直接结束当前尝试，等待后续显式请求。

两个 RAG 依赖分别按服务、模型及凭证指纹维护进程内熔断器：连续三次逻辑请求耗尽后打开30秒，其间快速返回到已有的词法/融合降级链路；冷却后仅允许一个半开探测请求，成功关闭，失败重新打开。每次网络操作有15秒超时；这不是跨批次或整轮的总耗时上限。缓存命中不产生远程请求。

熔断保护可选检索服务，不能替代主模型。主模型无法连接时仍明确报错，不编造答案；分类纠正等写操作只按幂等键或用户明确请求执行，不能遇到未知错误就重跑。当前熔断状态不跨进程共享，尚未实现全链路总 deadline、分布式熔断或自动重试产生的费用核算。

## 验证

```powershell
python -m pytest -q
node --test tests/frontend_harness.cjs
# 可选真实 PostgreSQL 回归，每例独立 schema，测试后清理
$env:FINANCE_TEST_DATABASE_URL='postgresql+psycopg://用户名:已编码密码@127.0.0.1:5432/测试库'
python -m pytest tests/test_backend_ledger.py tests/test_document_jobs.py tests/test_persistent_sessions.py -q

# 可选真实 Kafka 测试：先安装 Kafka extra 并启动独立测试 broker。
# 使用测试实例，测试会创建隔离主题及消费组；不设置变量时跳过。
$env:FINANCE_TEST_KAFKA_BOOTSTRAP_SERVERS='127.0.0.1:9092'
python -m pytest -q
```

本次使用真实 PostgreSQL 17.11 和 SQLite 验证导入幂等、退款精度、整批回滚、乐观并发、审计原子性、数据库迁移、租约抢占、旧令牌隔离、两个独立 worker 的索引互斥、重启恢复和事件游标。模型、embedding、rerank 的异常测试均使用确定性替身，不消费真实模型额度。CI 配置 PostgreSQL 与 Kafka service，并安装 Kafka extra，执行数据库及真实 broker 集成测试；未声称 GitHub 远程 CI 已执行。

## 后续边界

多用户使用前仍需身份认证、数据资源归属与每用户费用配额。长对话事件/消息的自动归档、运行指标和模型 token 流可作为下一阶段；当前 SSE 推送工具事件及最终答案，尚未逐 token 输出。
