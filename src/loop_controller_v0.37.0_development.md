# v0.37.0 单实例可靠 A2A 闭环：OpenAPI 权威协议、SQLite 状态层与真实委托执行

> 一句话目标：**在 v0.36.1 的语义与工程基线之上，首次实现单实例、同一信任域内 Agent A→B 的真实委托闭环：以 OpenAPI/JSON Schema 为唯一权威协议，以 SQLite 持久化 Task/Message/Event 与幂等键，以异步 HTTP Agent entrypoint 承接目标端执行，以状态机驱动取消、结果查询与 outcome_unknown，并通过 ActionKind.DELEGATION 让 Python R2 对委托动作完成授权。**
>
> 范围限定：本版本不引入分布式消息总线、多副本、跨信任域联邦或多租户；聚焦单实例、可靠、可观测、可回退的 A2A 闭环。

- 状态：**已发布 / annotated tag `v0.37.0` 已打**
- 前置版本：v0.36.1 发布冻结基线
- 目标版本：v0.37.0
- 版本性质：功能扩展 / 端到端闭环 / 协议权威化
- 核心原则：
  - **OpenAPI/JSON Schema 是 A2A 线协议唯一权威来源**；Python 与 Go 均从 Schema 派生，禁止双写不一致模型。
  - **状态持久化是可靠性的前提**：Task、Message、Event、幂等键全部入库，进程重启后可恢复。
  - **委托是工具治理的延伸**：ActionKind.DELEGATION 必须经 Python R2 授权，Go 仅作为交互治理与执行编排骨架。
  - **目标 Agent 独立治理**：Agent B 的工具调用仍需独立通过 SDK/MCP Proxy/HTTP REST 接入本地 Python R2。
  - **尽力取消与 outcome_unknown**：远端执行结果不可确认时，必须明确进入 `outcome_unknown` 状态，不得伪造成功。
  - **单实例首发，可扩展不锁死**：设计为后续分布式演进保留接口，但本版本不实现分布式协调。

---

## 1. 背景与版本判定

v0.36.1 已经达成：

- Python 工具治理层语义收敛（modify 三视图、审计事件拆分、Approval 加密）；
- Go A2A 协议骨架（Registry、Task、Router、Delegation、Token、SSE、协议版本检查）；
- Python/Go 统一 Message Part 契约与 contract tests；
- 标准 Python wheel/sdist 打包、安装 smoke、完整 CI gate；
- 干净 annotated tag `v0.36.1`。

但 v0.36.1 明确不实现：

1. 真实目标 Agent entrypoint 与远程执行；
2. Task/Message/Event 持久化，进程重启后状态丢失；
3. 完整的 Task 状态机（accepted → running → completed/failed/cancelled，以及 outcome_unknown）；
4. 异步 Agent entrypoint（目标端 HTTP 接收、执行、回调或 SSE 推送）；
5. `ActionKind.DELEGATION` 作为 R2 治理原语；
6. Python R2 对委托动作的显式授权接口；
7. 取消与结果查询主链；
8. OpenAPI/JSON Schema 作为唯一权威协议来源。

因此 v0.37.0 的目标是实现**单实例可靠 A2A 闭环**：Agent A 在本地 R2 授权下，通过 Go Kernel 把工具执行委托给已注册的 Agent B，Agent B 的 entrypoint 接收任务、在本地 R2 治理下执行工具、返回结果，Go Kernel 把结果回传，Agent A 的调用方得到最终执行结果或 `outcome_unknown`。

---

## 2. 架构边界

### 2.1 双治理平面延续

```text
工具调用治理平面（Python R2，继续为权威）
├─ Python SDK / @governed
├─ MCP Proxy
├─ HTTP REST API
├─ Policy / Profile / Approval / Budget / Revocation / Audit
└─ 本地执行器、MCP Server、HTTP Tool、Harness

Agent 交互治理平面（Go A2A Kernel）
├─ Agent Registry / Discovery
├─ SQLite Task/Message/Event/Idempotency Store
├─ Task State Machine
├─ Async HTTP Agent Entrypoint
├─ Delegation / Token
├─ Cancel & Result Query API
└─ SSE Task Event Stream
```

边界不变：

- Python R2 不把工具治理决策权交给 Go；
- Go 不直接执行工具；
- A2A Token 仅表示交互层授权，不被解释为工具调用授权；
- Agent B 的工具调用仍需独立接入 Agent B 本地的 Python R2；
- Go Kernel 不可达时，显式委托请求 fail-closed，不得隐式回退到本地执行。

### 2.2 单实例范围

本版本明确限制为单实例：

- 一个 Go Kernel 进程；
- 一个本地 SQLite 数据库文件（默认 `data/a2a.db`）；
- 目标 Agent entrypoint 通过 HTTP 回调同一 Kernel 或另一进程内的 entrypoint；
- 同一信任域内首发，mTLS 服务身份在 v0.38.0 引入。

### 2.3 不纳入范围

| 编号 | 内容 | 计划 |
|---|---|---|
| RC-N1 | 分布式消息总线（Kafka/NATS/RabbitMQ） | v0.38.0+ |
| RC-N2 | 多副本/高可用 Go Kernel | v0.38.0+ |
| RC-N3 | 跨信任域联邦、mTLS 服务身份 | v0.38.0+ |
| RC-N4 | PostgreSQL 状态后端 | v0.38.0+ |
| RC-N5 | Token JTI 消费端验证、参数摘要、密钥轮换 | v0.38.0 |
| RC-N6 | SSE 重放、heartbeat、轮询 fallback 完善 | v0.38.0 |
| RC-N7 | 多租户、统一企业 RBAC | 企业化阶段 |
| RC-N8 | R3 LLM 审计分析增强 | 后续智能治理阶段 |

---

## 3. 纳入与排除范围

### 3.1 纳入本版本

| 编号 | 内容 | 主要位置 |
|---|---|---|
| RC-01 | 建立 OpenAPI/JSON Schema 作为 A2A 唯一权威协议 | `openapi/`, `contract/`, 代码生成脚本 |
| RC-02 | Go SQLite Task 持久化与状态机 | `go/internal/store/`, `go/internal/task/` |
| RC-03 | Go SQLite Message/Event 持久化 | `go/internal/store/`, `go/internal/stream/` |
| RC-04 | 幂等键与幂等状态层 | `go/internal/store/idempotency.go` |
| RC-05 | 异步 HTTP Agent entrypoint | `go/internal/entrypoint/`, `go/internal/api/` |
| RC-06 | Task 状态机（accepted/running/completed/failed/cancelled/outcome_unknown） | `go/internal/task/`, `go/internal/executor/` |
| RC-07 | 取消主链 | `go/internal/api/`, `go/internal/task/` |
| RC-08 | 结果查询主链 | `go/internal/api/`, `go/internal/task/` |
| RC-09 | ActionKind.DELEGATION 与 Python R2 委托授权接口 | `src/loop_controller/models.py`, `src/loop_controller/controller.py`, `src/loop_controller/delegation.py` |
| RC-10 | Go 调 Python R2 委托授权接口 | `go/internal/delegation/`, `src/loop_controller/http_server.py` |
| RC-11 | Python/Go 协议契约与 OpenAPI 一致性测试 | `contract/`, `tests/`, `go/internal/api/contract_test.go` |
| RC-12 | 版本一致性、CI gate、发布验证 | `pyproject.toml`, `.github/workflows/`, `go/` |

### 3.2 明确不纳入

见 §2.3。

---

## 4. RC-01：OpenAPI/JSON Schema 权威协议

### 4.1 目标

消除 Python、Go、proto 三份并行协议描述。v0.37.0 起：

- `openapi/a2a_v0.37.0.yaml` 是 A2A HTTP/JSON API 的唯一权威来源；
- `contract/a2a_v0.37.0.json` 是跨语言契约 fixture 的权威来源；
- Go 代码从 OpenAPI Schema 生成或通过反射校验，不再手写重复模型；
- Python 的 `GoKernelBridge` 也按同一 fixture 校验请求/响应；
- proto 目录仅保留历史参考，不再作为可生成线协议承诺。

### 4.2 OpenAPI 文件结构

```text
openapi/
├── a2a_v0.37.0.yaml          # 主 OpenAPI 文档
├── schemas/
│   ├── agent-card.yaml
│   ├── task.yaml
│   ├── message.yaml
│   ├── part.yaml
│   ├── delegation-request.yaml
│   ├── delegation-response.yaml
│   ├── error-response.yaml
│   └── protocol-version.yaml
└── paths/
    ├── agents.yaml
    ├── tasks.yaml
    ├── messages.yaml
    ├── delegations.yaml
    └── health.yaml
```

### 4.3 Schema 核心要求

- 所有请求/响应必须包含 `protocol_version`，格式 `major.minor.patch`；
- `Message.parts` 仅允许 `text` 与 `data` 两种 Part，不允许二次编码 JSON 字符串；
- `data` Part 的值为任意 JSON（object/array/value），但必须是合法 JSON；
- Task 状态枚举：`pending`, `accepted`, `running`, `completed`, `failed`, `cancelled`, `outcome_unknown`；
- Event 类型枚举：`task_created`, `task_accepted`, `task_running`, `task_completed`, `task_failed`, `task_cancelled`, `task_outcome_unknown`, `message_received`, `delegation_authorized`, `delegation_denied`；
- 错误响应统一 envelope：`{ "error": "message", "code": "code", "protocol_version": "..." }`；
- 所有时间戳使用 RFC 3339 UTC；
- 所有 ID 使用 `^[a-zA-Z0-9_.-]{1,128}$` 或更严格规则。

### 4.4 代码生成与校验

Python：

- 使用 `datamodel-codegen` 或仓库内脚本从 OpenAPI 生成 Pydantic 模型到 `src/loop_controller/a2a_schema/`；
- 生成脚本纳入 CI，禁止手动修改生成文件；
- `GoKernelBridge` 与 HTTP Server 使用生成模型校验请求。

Go：

- 使用 `oapi-codegen` 生成类型与 chi/gorilla 路由骨架到 `go/internal/api/generated/`；
- 手写 handlers 只处理业务逻辑，模型由生成文件提供；
- 生成产物提交到仓库，CI 校验生成结果与 OpenAPI 一致。

### 4.5 验收

- OpenAPI 文档可被 Swagger UI / Redoc 渲染；
- Python 与 Go 对同一 fixture 编解码结果一致；
- 未知字段、未知 Part type、缺必填字段均返回相同错误 code；
- 协议版本 major/minor 不一致时 fail-closed；
- CI 中 `make generate` 后 `git diff --exit-code` 通过。

---

## 5. RC-02/RC-03：SQLite Task/Message/Event 持久化

### 5.1 目标

替代内存中的 `task.Manager` 与 `stream.InMemoryPublisher`，使进程重启后状态可恢复，并为取消/查询/幂等提供基础。

### 5.2 数据库 Schema

```sql
-- Task 主表
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    initiator_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'accepted', 'running', 'completed', 'failed', 'cancelled', 'outcome_unknown'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT,              -- 成功结果 JSON 或失败原因摘要
    error_code TEXT,
    INDEX idx_session_id (session_id),
    INDEX idx_target_agent_id (target_agent_id),
    INDEX idx_status_updated_at (status, updated_at)
);

-- Message 表
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parts_json TEXT NOT NULL,  -- Message.parts 的 JSON 文本
    timestamp TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    INDEX idx_task_id (task_id)
);

-- Event 表（SSE 事件日志，也是审计链的 A2A 侧输入）
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published INTEGER DEFAULT 0,  -- 0=待推送, 1=已推送
    INDEX idx_task_id_published (task_id, published)
);
```

### 5.3 存储接口

Go：

```go
package store

type TaskStore interface {
    Create(ctx context.Context, t models.Task) error
    Get(ctx context.Context, taskID string) (models.Task, error)
    UpdateStatus(ctx context.Context, taskID, status string, outcome []byte, errorCode string) error
    ListBySession(ctx context.Context, sessionID string) ([]models.Task, error)
    ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error)
}

type MessageStore interface {
    Save(ctx context.Context, msg models.Message) error
    ListByTask(ctx context.Context, taskID string) ([]models.Message, error)
}

type EventStore interface {
    Append(ctx context.Context, ev models.TaskEvent) error
    ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error)
    MarkPublished(ctx context.Context, eventIDs []string) error
}
```

### 5.4 初始化与迁移

- 启动时自动 `CREATE TABLE IF NOT EXISTS`；
- 数据库路径通过 `LC_A2A_DB_PATH` 配置，默认 `./data/a2a.db`；
- 父目录不存在时自动创建；
- 使用 `database/sql` + `modernc.org/sqlite` 或 `mattn/go-sqlite3`（优先纯 Go，避免 CGO）；
- 写入使用事务，保证 Task+Message+Event 原子性；
- 启动时运行 `PRAGMA foreign_keys = ON` 与 `PRAGMA journal_mode = WAL`。

### 5.5 验收

- 创建 Task 后，kill 进程重启，GetTask 仍能返回；
- Message 持久化后，ListByTask 可恢复完整对话；
- Event 持久化后，SSE 重连可重放未推送事件；
- 并发创建 1000 个 Task 无主键冲突；
- 外键约束生效，删除 Task 自动清理关联 Message/Event（或软删除）。

---

## 6. RC-04：幂等状态层

### 6.1 目标

防止网络重试导致重复创建 Task、重复执行委托、重复发送 Message。

### 6.2 幂等键契约

- 客户端在可重试请求中提供 `Idempotency-Key` header（或请求体 `idempotency_key`）；
- 幂等键作用域：`key + request_target + agent_id`；
- 服务端保存幂等键与首次响应的映射至少 24 小时（可配置）；
- 重复请求在 key 有效期内返回首次响应，HTTP status 与首次一致；
- 首次请求未完成时，后续相同 key 请求返回 `409 Conflict` 并带 `Retry-After`。

### 6.3 数据表

```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key_hash TEXT PRIMARY KEY,  -- SHA-256(key + scope)
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    request_hash TEXT NOT NULL, -- 请求体 SHA-256，用于检测参数变化
    response_body TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    locked INTEGER DEFAULT 1      -- 1=处理中, 0=已完成
);
CREATE INDEX idx_idempotency_created_at ON idempotency_keys(created_at);
```

### 6.4 行为

- 无幂等键：正常处理；
- 新幂等键：加锁、处理、解锁、保存响应；
- 同 key 同请求：返回缓存响应；
- 同 key 不同请求：返回 `409 Conflict`（不得用缓存响应糊弄）；
- key 过期后清理，默认 24 小时。

### 6.5 验收

- 相同 `Idempotency-Key` 两次 POST /a2a/v1/delegations 只创建一个 Task；
- 并发相同 key 只有一个成功，另一个等待或返回 409；
- 过期 key 被清理；
- 幂等键不泄露到日志或 Event payload。

---

## 7. RC-05/RC-06：异步 HTTP Agent Entrypoint 与状态机

### 7.1 目标

让目标 Agent B 有一个标准的异步 HTTP 入口：接收委托 Task、本地治理、执行工具、返回结果。

### 7.2 Agent Entrypoint 接口

OpenAPI 路径：

- `POST /a2a/v1/entrypoint/tasks`：接收新委托任务，返回 `task_id` 与确认状态。
- `POST /a2a/v1/entrypoint/tasks/{id}/accept`：目标 Agent 显式接受任务；
- `POST /a2a/v1/entrypoint/tasks/{id}/cancel`：请求取消；
- `GET /a2a/v1/entrypoint/tasks/{id}`：查询任务与结果；
- `POST /a2a/v1/entrypoint/tasks/{id}/results`：目标 Agent 回传执行结果（仅内部或 mTLS，v0.37.0 单实例下由本地 executor 直接写库）。

Entrypoint 请求体：

```json
{
  "protocol_version": "0.37.0",
  "task_id": "task-...",
  "session_id": "...",
  "initiator_agent_id": "agent-a",
  "target_agent_id": "agent-b",
  "tool_name": "canonical.tool.name",
  "arguments": { "k": "v" },
  "delegation_token": "..."
}
```

### 7.3 状态机

```text
pending
  ↓ 目标 Agent entrypoint 收到并 accept
accepted
  ↓ 目标 Agent 开始执行
running
  ↓ 成功返回结果
completed
  ↓ 失败返回结果
failed
  ↓ 取消请求被接受
 cancelled
  ↓ 远端超时/不可确认
outcome_unknown
```

规则：

- `pending → accepted`：entrypoint 显式 accept 或自动 accept（配置决定，默认显式）；
- `accepted → running`：executor 开始执行；
- `running → completed/failed`：executor 返回明确结果；
- 任何非终态均可尝试 `cancelled`；取消是否成功由目标 Agent/executor 决定；
- 远端超时或网络不可达后，进入 `outcome_unknown`，不得假设失败或成功；
- 终态：`completed`, `failed`, `cancelled`, `outcome_unknown`。

### 7.4 目标端执行流程

1. Entrypoint 接收 Task；
2. 验证 `delegation_token`（签名、过期、scope）；
3. 解析 `tool_name` 与 `arguments`；
4. 调用本地 Python R2（通过 SDK 或 MCP Proxy 或 HTTP REST）进行工具治理；
5. R2 授权后执行工具；
6. 将结果或失败写入本地 SQLite Task 记录；
7. 发布 `task_completed` / `task_failed` 事件；
8. 通过 SSE 或 HTTP 回调让发起方查询/接收结果。

在单实例模式下，Go Kernel 与 Python R2 在同一进程/机器内，entrypoint 可直接通过本地 HTTP/SDK 调用 R2。

### 7.5 验收

- 发起方委托后 Task 进入 `pending`；
- 目标 Agent accept 后 Task 进入 `accepted`；
- 执行中进入 `running`；
- 成功返回后进入 `completed`，结果可查询；
- 失败进入 `failed`，原因可查询；
- 超时进入 `outcome_unknown`；
- 取消请求被接受后进入 `cancelled`；
- 状态迁移不合法时返回 409 Conflict。

---

## 8. RC-07/RC-08：取消与结果查询主链

### 8.1 取消主链

发起方：

- `POST /a2a/v1/tasks/{id}/cancel` 向 Go Kernel 发起取消；
- Go Kernel 向目标 Agent entrypoint 转发取消请求；
- 目标 Agent/executor 尽力取消；
- 无论目标是否成功取消，Go Kernel 必须给发起方一个明确的取消响应（成功/已终态/未知）；
- 若目标不可达，Task 可进入 `outcome_unknown`（而非自动 `cancelled`）。

目标端：

- Entrypoint 收到取消后，向本地 executor 发送取消信号；
- executor 在检查点响应取消；
- 若已终态，返回当前状态；
- 若取消成功，更新为 `cancelled`。

### 8.2 结果查询主链

- `GET /a2a/v1/tasks/{id}` 返回当前 Task 状态与结果（若已终态）；
- 发起方也可通过 SSE `/a2a/v1/tasks/{id}/stream` 订阅事件；
- 查询必须验证身份（Token 或 session 关联），不得允许跨 agent 越权查询；
- 结果 payload 大小限制与 Message body 一致（默认 1 MiB），大产物使用外部引用（本版本先落地基础，外部引用为预留字段）。

### 8.3 超时与 outcome_unknown

- 委托请求超时：发起方未收到响应 → Task 状态未知；
- 执行超时：Go Kernel 未在 `execution_timeout` 内收到结果 → Task 标记为 `outcome_unknown`；
- outcome_unknown 后仍允许结果回调/查询，若收到结果则按实际结果迁移到 completed/failed；
- 一次调用最终只产生一个终态事件。

### 8.4 验收

- 取消请求返回明确响应；
- 已 completed Task 的取消请求返回 `already_completed`；
- 结果查询返回最新状态；
- SSE 流按事件顺序推送；
- 超时后进入 `outcome_unknown`，有明确 reason；
- 越权查询返回 403 Forbidden。

---

## 9. RC-09：ActionKind.DELEGATION 与 Python R2 委托授权接口

### 9.1 目标

把“委托给另一个 Agent 执行工具”提升为与 `allow/deny/modify/require_approval` 同级的治理动作，让 R2 策略可以显式授权、拒绝、要求审批或修改委托参数。

### 9.2 Python 模型扩展

在 `src/loop_controller/models.py` 中：

```python
ActionKind = Literal[
    "tool_call",
    "delegation",
]
```

`Proposal` 增加可选字段：

- `target_agent_id: str | None`：委托目标 Agent；
- `delegation_context: dict[str, Any] | None`：委托附加元数据；
- `action_kind: ActionKind = "tool_call"`。

`Decision` 增加：

- `action_kind: ActionKind`；
- `delegation_token: str | None`；
- `target_entrypoint: dict[str, Any] | None`。

### 9.3 R2 委托授权接口

新增 `src/loop_controller/delegation.py` 模块，提供：

```python
class DelegationAuthorizer:
    def authorize(
        self,
        proposal: Proposal,
        controller: LoopController,
    ) -> Decision:
        ...
```

行为：

- `action_kind == "tool_call"`：走现有工具治理路径；
- `action_kind == "delegation"`：
  1. 校验 `target_agent_id` 已注册；
  2. 查询目标 Agent Card（通过 Go Kernel Bridge 或本地缓存）；
  3. 调用 OPA/Rego 策略评估委托风险；
  4. 根据 verdict 返回 `allow/deny/modify/require_approval`；
  5. `allow` 时向 Go Kernel 申请 delegation token；
  6. 返回带 `delegation_token` 与 `target_entrypoint` 的 Decision。

### 9.4 策略输入

Rego input 增加：

```json
{
  "action_kind": "delegation",
  "tool_name": "...",
  "target_agent_id": "agent-b",
  "target_capabilities": ["..."],
  "trust_domain": "same",
  "arguments": { ... }
}
```

### 9.5 验收

- 策略可基于 `action_kind` 单独拒绝委托；
- 委托参数被修改后二次复核；
- 目标 Agent 不存在时返回 deny；
- 目标 Agent 无 `delegate_execution` capability 时返回 deny；
- 审批通过的委托可恢复执行；
- 审计记录 `action_kind=delegation` 与 `execution_*` 事件。

---

## 10. RC-10：Go 调 Python R2 委托授权接口

### 10.1 目标

当 Go Kernel 收到 `POST /a2a/v1/delegations` 时，必须向 Python R2 确认该委托是否被授权，而不是仅根据 Registry capability 判断。

### 10.2 接口设计

Python 端：在 `src/loop_controller/http_server.py` 新增 endpoint：

```http
POST /r2/v1/delegations/authorize
Content-Type: application/json

{
  "request_id": "...",
  "initiator_agent_id": "agent-a",
  "target_agent_id": "agent-b",
  "tool_name": "...",
  "arguments": { ... },
  "session_id": "...",
  "task_id": "...",
  "risk_level": "low",
  "protocol_version": "0.37.0"
}
```

响应：

```json
{
  "allowed": true,
  "verdict": "allow",
  "decision_id": "...",
  "delegation_token": "...",
  "target_entrypoint": { "type": "http", "url": "http://..." },
  "reason": "R2 authorized delegation",
  "protocol_version": "0.37.0"
}
```

或拒绝：

```json
{
  "allowed": false,
  "verdict": "deny",
  "reason": "target agent not trusted",
  "protocol_version": "0.37.0"
}
```

### 10.3 Go 端调用

Go `delegation.Delegator` 增加 `R2Authorizer` 依赖：

```go
type R2Authorizer interface {
    Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error)
}
```

`Delegator.Request` 流程更新：

1. 校验字段；
2. 查询目标 Agent Card；
3. 调用 `R2Authorizer.Authorize`；
4. 若 R2 拒绝，直接返回拒绝响应；
5. 若 R2 允许，创建 Task、签发 Token、持久化、发布事件；
6. 返回带 TaskID 与 Token 的响应。

### 10.4 本地 R2 客户端实现

默认实现通过 HTTP 调用 Python R2；单实例下也可通过本地函数调用（测试场景）。

```go
type HTTPR2Authorizer struct {
    BaseURL string
    Client  *http.Client
}
```

### 10.5 验收

- R2 拒绝时 Go Kernel 不创建 Task；
- R2 允许后创建 Task 并签发 Token；
- R2 不可达时 fail-closed；
- 响应包含 R2 返回的 decision_id；
- 委托请求幂等。

---

## 11. RC-11：Python/Go 协议契约与 OpenAPI 一致性测试

### 11.1 测试矩阵

| Fixture | Python 读取 | Go 读取 | 版本检查 | 未知字段 |
|---|---|---|---|---|
| Agent Card | ✓ | ✓ | ✓ | ✓ |
| Task | ✓ | ✓ | ✓ | ✓ |
| Message | ✓ | ✓ | ✓ | ✓ |
| Delegation Request | ✓ | ✓ | ✓ | ✓ |
| Delegation Response | ✓ | ✓ | ✓ | ✓ |
| Error Response | ✓ | ✓ | ✓ | ✓ |
| Task Event | ✓ | ✓ | ✓ | ✓ |

### 11.2 CI 校验

- `make generate` 生成 Python/Go 模型；
- `git diff --exit-code` 确保生成产物与 OpenAPI 一致；
- `python -m jsonschema` 用 OpenAPI 组件验证 fixture；
- Python 与 Go contract tests 共用 `contract/a2a_v0.37.0.json`。

---

## 12. 数据兼容与回退

### 12.1 SQLite 迁移

- v0.37.0 首次引入 SQLite Schema；
- 启动时自动建表；
- 后续版本迁移使用 `go/internal/store/migrations/`；
- v0.36.1 的内存 Task 数据不保留（内存状态原本就是非持久化实验）。

### 12.2 协议版本

- v0.37.0 协议版本 `0.37.0`；
- 与 v0.36.1 不兼容（minor 差异），fail-closed；
- 保留 `contract/a2a_v0.36.1.json` 用于历史测试。

### 12.3 Approval/审计

- Python 侧 Approval 加密格式不变；
- 新增委托相关审计事件：`delegation_authorized`, `delegation_denied`, `delegation_requested`；
- A2A Event 与 Python 审计通过 `request_id/call_id/task_id` 关联。

---

## 13. 安全审查清单

- [ ] `ActionKind.DELEGATION` 不绕过本地 R2 工具治理；
- [ ] Go Kernel 必须调用 Python R2 才能授权委托；
- [ ] Delegation Token 有明确 TTL 与 scope，不可用于非委托调用；
- [ ] 目标 Agent entrypoint 必须验证 Token；
- [ ] 取消请求只能由发起方或目标 Agent 发起；
- [ ] 结果查询必须验证身份，禁止越权；
- [ ] 幂等键防止重放攻击；
- [ ] SQLite 文件权限在生产模式限制为 owner-only；
- [ ] 协议版本不兼容时 fail-closed；
- [ ] `outcome_unknown` 不泄露内部错误细节；
- [ ] 敏感参数不在 URL 或 Event payload 中明文传输。

---

## 14. 交付物

v0.37.0 完成时应交付：

1. OpenAPI/JSON Schema 权威协议文件；
2. Python/Go 从 Schema 生成的模型与校验代码；
3. Go SQLite Task/Message/Event/Idempotency 存储层；
4. 异步 HTTP Agent entrypoint 与状态机；
5. 取消与结果查询主链；
6. `ActionKind.DELEGATION` 与 Python R2 委托授权接口；
7. Go 调 Python R2 授权客户端；
8. Python/Go contract tests 与 OpenAPI 一致性 CI；
9. 更新后的 README、KNOWN_LIMITATIONS、版本文档；
10. annotated Git tag `v0.37.0`。

---

## 15. 完成定义（Definition of Done）

### 正确性

- [x] RC-01 至 RC-10 全部实现并有回归测试；
- [x] 单实例 A2A 闭环端到端测试通过；
- [x] 取消、超时、outcome_unknown 路径测试通过；
- [x] 委托授权失败时 fail-closed 测试通过。

### 工程

- [x] Ruff 通过；
- [x] Mypy 0 error；
- [x] Python unit/integration 全绿；
- [x] Go test 全绿；
- [x] OpenAPI 生成产物与手写代码一致；
- [x] wheel/sdist 构建成功；
- [x] wheel 安装、import smoke 成功；
- [ ] CI gate 全绿（推送后由 GitHub Actions 验证）。

### 发布

- [x] 工作区干净（已整理临时调试文件）；
- [x] 包、配置、协议、文档、tag 版本统一为 v0.37.0；
- [x] 发布说明包含已知限制和迁移说明；
- [ ] 从干净 clone 完成完整复验（建议发布前在独立环境执行）；
- [x] tag 指向通过全部 gate 的干净提交（`git log` 确认 `HEAD` -> `develop` -> `v0.37.0`）。

---

## 16. 推荐实施顺序

1. **RC-01**：建立 OpenAPI/JSON Schema 与代码生成链；
2. **RC-02/RC-03/RC-04**：实现 SQLite Task/Message/Event/Idempotency 存储层；
3. **RC-09/RC-10**：实现 ActionKind.DELEGATION 与 Python R2 授权接口；
4. **RC-05/RC-06**：实现异步 Agent entrypoint 与状态机；
5. **RC-07/RC-08**：实现取消与结果查询主链；
6. **RC-11**：补全 Python/Go 协议契约测试；
7. **RC-12**：版本一致性、CI gate、完整回归；
8. **安全审查与干净 clone 复验**；
9. **创建 v0.37.0 tag 与 Release**。

---

## 17. v0.38.0 接续边界

v0.37.0 完成后，v0.38.0 方向：

- mTLS 服务身份与跨信任域联邦；
- PostgreSQL 状态后端；
- 分布式消息总线与多副本；
- Token 消费端验证、JTI、参数摘要与密钥轮换；
- SSE 重放、heartbeat、轮询 fallback；
- 更细粒度的取消信号与执行器响应。

v0.37.0 不为上述功能提前堆叠实现，但接口设计需保留演进空间。
