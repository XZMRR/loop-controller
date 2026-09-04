# v0.42.0：registry / router / discovery 持久化

**Status**: 进行中
**Target Version**: `0.42.0`
**Goal**: 消除 v0.41.0 遗留的最后一个单实例假设——`registry` / `router` / `discovery` 的纯内存状态，使 Agent 注册信息与路由消息跨实例可见。继续沿用 v0.41.0 的约束：**不引入 PostgreSQL、外部消息队列或分布式共识**，以共享 SQLite（WAL）作为唯一事实源。

---

## 1. 版本定位

v0.41.0 已完成任务执行生命周期、outbox、SSE、故障转移与 ID 分布式安全，但仍有一处单实例假设未处理：

| 单实例假设 | 所在位置 | 多实例后果 |
| --- | --- | --- |
| Agent 注册信息存进程内存 map | `registry/agent.go` | 实例 A 注册的 Agent，实例 B 的 delegation / 路由不可见 |
| 路由消息存进程内存切片 | `router/router.go` | 实例 A 路由的消息，实例 B 无法查询 |
| discovery 的 `known` 为进程内存 map | `discovery/discovery.go` | 每次实例启动都要重新全量 sync，且删除/更新依赖本地快照 |

v0.42.0 将这三者从内存投影迁移到共享 SQLite，使注册信息与路由消息成为跨实例可共享、可查询的事实源。

---

## 2. 核心原则

1. **共享状态为事实源**：SQLite 是唯一事实源；`registry` 与 `router` 的内存路径仅作为测试与无存储场景的兼容实现保留。
2. **最小侵入兼容**：保留现有 `registry.New()`、`Register/Get/List/Delete`、`Router.Route/MessagesFor` 等无 context 方法签名，避免大范围改动测试与装配代码。
3. **依赖倒置**：`registry` / `router` 通过本包定义的窄接口消费存储，不直接 import `store`，保持分层清晰（与 `delegation.AgentQuerier` 同风格）。
4. **语义不回归**：`Register` 仍为幂等 upsert；`Get`/`Delete` 对缺失项仍返回 `registry.ErrAgentNotFound`；`discovery` 仍复用 `RegistryStore` 三方法。
5. **路由消息与任务消息解耦**：路由消息不强制携带 `task_id`，因此不复用带外键的 `messages` 表，而是新增独立的 `routed_messages` 表。

---

## 3. 设计决策

### 3.1 新增 `agents` 表与 `store.AgentStore`

在 `store/db.go` 的 `schema` 中新增：

```sql
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    entrypoint_type TEXT NOT NULL DEFAULT '',
    entrypoint_url TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    trust_domain TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT ''
);
```

- `AgentCard.Capabilities` 以 JSON 数组序列化入 `capabilities_json`。
- `entrypoint` 拆为 `entrypoint_type` 与 `entrypoint_url` 两列，避免二次 JSON 解析。

`store.AgentStore` 接口（全部带 `context.Context`）：

```go
type AgentStore interface {
    Upsert(ctx context.Context, card models.AgentCard) error
    Get(ctx context.Context, agentID string) (models.AgentCard, error)
    Delete(ctx context.Context, agentID string) error
    List(ctx context.Context) ([]models.AgentCard, error)
}
```

- `Upsert` 用 `INSERT ... ON CONFLICT(agent_id) DO UPDATE` 实现幂等覆盖。
- `Delete` 对未命中返回 `sql.ErrNoRows`（`RowsAffected == 0`），由上层映射为 `ErrAgentNotFound`。
- `List` 按 `agent_id ASC` 排序，保证确定性。
- `db.AgentStore()` accessor 与现有 `TaskStore()` 等并列。

### 3.2 `registry` 改造为 store-backed（保留内存兼容）

`registry` 包内新增窄接口：

```go
type AgentStore interface {
    Upsert(ctx context.Context, card models.AgentCard) error
    Get(ctx context.Context, agentID string) (models.AgentCard, error)
    Delete(ctx context.Context, agentID string) error
    List(ctx context.Context) ([]models.AgentCard, error)
}
```

`Registry` 结构体增加 `store AgentStore` 字段：

- `New()` 保持为纯内存实现（供既有测试与无存储场景使用）。
- 新增 `NewStore(s AgentStore) *Registry` 返回 store-backed 实现。
- 四个方法（`Register/Get/List/Delete`）在 `store != nil` 时委托给 store，否则走原内存逻辑。
- store-backed 路径内部使用 `context.Background()`，保持无 context 的公开方法签名不变。
- `Get`/`Delete` 将 `sql.ErrNoRows` 映射回 `ErrAgentNotFound`，保证既有错误语义不变。
- `Register` 的空 `agent_id` 校验保持在 `registry` 层，对两条路径都生效。

**兼容性结论**：所有 `registry.New()` 调用点（discovery 测试、delegation 测试、router 测试）无需修改；生产装配改用 `registry.NewStore(db.AgentStore())`。

### 3.3 新增 `routed_messages` 表与 `store.RoutedMessageStore`

`messages` 表带 `task_id TEXT NOT NULL REFERENCES tasks(task_id)`，而 `/a2a/v1/messages` 路由消息通常不携带 `task_id`，直接复用会触发外键失败。因此新增独立表，不引入 `task_id`：

```sql
CREATE TABLE IF NOT EXISTS routed_messages (
    message_id TEXT PRIMARY KEY,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parts_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    protocol_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routed_messages_to ON routed_messages(to_agent_id);
CREATE INDEX IF NOT EXISTS idx_routed_messages_from ON routed_messages(from_agent_id);
```

`store.RoutedMessageStore` 接口：

```go
type RoutedMessageStore interface {
    Save(ctx context.Context, msg models.Message) error
    ListByAgent(ctx context.Context, agentID string) ([]models.Message, error)
}
```

- `ListByAgent` 返回 `from_agent_id = ? OR to_agent_id = ?` 的结果，按 `timestamp ASC`。
- `db.RoutedMessageStore()` accessor 新增。

### 3.4 `router` 改造为 store-backed

`router` 包内新增窄接口：

```go
type RoutedMessageStore interface {
    Save(ctx context.Context, msg models.Message) error
    ListByAgent(ctx context.Context, agentID string) ([]models.Message, error)
}
```

- `Router` 结构体：`registry *registry.Registry` + `messages RoutedMessageStore`。
- 构造函数改为 `New(reg *registry.Registry, store RoutedMessageStore) *Router`，显式注入存储，避免生产路径隐藏内存默认实现。
- `Route`：保留校验（`to_agent_id` 非空、目标已注册），随后 `messages.Save(context.Background(), msg)`。
- `MessagesFor`：委托 `messages.ListByAgent(context.Background(), agentID)`。
- 内部使用 `context.Background()` 以保持无 context 的公开签名。

### 3.5 `discovery` 接线（不改接口）

`discovery.RegistryStore` 接口（`Register/Get/Delete`，无 context）由 `*registry.Registry` 继续满足，因此 `discovery.Manager` 无需改动：

- `Manager.known` 仍是每次 `Sync` 的本地快照，仅用于计算“消失的卡”，其读写结果落地到 store-backed registry。
- 生产装配 `NewManager(reg, providers...)` 中 `reg` 换成 store-backed registry 即可。

### 3.6 `api.NewServer` 装配变化

```go
reg := registry.NewStore(db.AgentStore())
r := router.New(reg, db.RoutedMessageStore())
mgr := discovery.NewManager(reg, providers...)
```

其余装配不变。`Server.registry` 字段仍为具体类型 `*registry.Registry`，`handleRegisterAgent/List/Get` 与 `TestTaskStreamSSE` 的 `srv.registry.Register(...)` 无需改动。

### 3.7 context 取舍

`registry` / `router` 公开方法保持无 context，store 调用内部使用 `context.Background()`。理由：

- 这些操作是单行/小范围 SQLite 读写，WAL + `busy_timeout` 下时延可控。
- 保持既有接口签名，将测试与装配改动降到最低。
- 真正的长事务（任务状态机、outbox、幂等）已经在 v0.41.0 使用显式 `r.Context()` 或 `context.Background()` 并配套租约，本版本不重复引入。

---

## 4. 实施阶段

### P42-01 存储层：`agents` 表、`AgentStore`、`routed_messages` 表、`RoutedMessageStore`

- `store/db.go` schema 增加 `agents`、`routed_messages` 两表及索引。
- 新增 `store/agent.go`（`AgentStore` + `agentStore`）。
- 新增 `store/routed_message.go`（`RoutedMessageStore` + `routedMessageStore`）。
- `store/db.go` 新增 `AgentStore()`、`RoutedMessageStore()` accessor。

### P42-02 registry store-backed

- `registry/agent.go` 增加 `AgentStore` 窄接口、`NewStore`、`store` 字段及委托逻辑。
- 保留 `New()` 内存实现。

### P42-03 router store-backed

- `router/router.go` 增加 `RoutedMessageStore` 窄接口，构造函数注入 store，`Route`/`MessagesFor` 走 store。

### P42-04 api 接线

- `api/handlers.go` `NewServer` 改为 store-backed registry 与 router。

### P42-05 测试适配与新增

- `router/router_test.go`：适配 `New(reg, store)`，用本地内存 fake 实现 `RoutedMessageStore`。
- 新增 `store/agent_test.go`：`AgentStore` 的 upsert/get/list/delete 行为。
- 新增 `store/routed_message_test.go`：`RoutedMessageStore` 的 save/list-by-agent。
- `registry/agent_test.go`：新增 store-backed 路径测试（用本地 fake `AgentStore`），验证 `sql.ErrNoRows` → `ErrAgentNotFound` 映射。
- `discovery` / `delegation` / `api` 既有测试保持通过（内存 `registry.New()` 仍兼容）。

### P42-06 版本统一 0.42.0

- Go：`models.CurrentProtocolVersion`、`contract_test.go`、`entrypoint_test.go` 中硬编码版本。
- contract fixture：`contract/a2a_v0.41.0.json` → `contract/a2a_v0.42.0.json`，并更新 `contract_test.go` 的 `loadFixture` 路径。
- OpenAPI：`openapi/a2a_v0.41.0.yaml` → `openapi/a2a_v0.42.0.yaml`，同步内部版本引用。
- Python / config / pyproject：`0.41.0` → `0.42.0`。

---

## 5. 明确不做

- PostgreSQL 或其他共享数据库驱动。
- 外部消息队列 / Kafka / NATS。
- 分布式共识（Raft/Paxos）。
- 路由消息的跨实例实时推送（SSE 化）：本版本只保证可查询、可持久化，不新增消息订阅语义。
- Agent 动态注销的分布式广播。
- `registry` / `router` 公开方法的 context 化（保持兼容优先）。

---

## 6. 完成定义

v0.42.0 只有同时满足以下条件才能完成：

1. 两个 kernel 实例共享同一 SQLite 文件时，实例 A 注册的 Agent 可被实例 B 通过 `GET /a2a/v1/agents/{id}` 与 delegation 查询到。
2. 路由消息写入 `routed_messages`，`MessagesFor` 可跨实例查询到由其他实例路由的消息。
3. `discovery.Sync` 的注册/删除结果落地到共享 `agents` 表，重启后无需依赖本地 `known` 快照即可查询。
4. 既有内存路径（`registry.New()`）语义不回归，discovery / delegation / router 既有测试全部通过。
5. 版本统一为 `0.42.0`（Go / Python / OpenAPI / contract fixture / config）。
6. Go 全量与 race 测试、`go build`、`go vet`、`git diff --check`、`pytest` 全部通过。

---

## 7. 验证记录

- `go build ./...`：通过（exit 0）。
- `go vet ./...`：通过（exit 0）。
- `go test ./internal/...`：全部通过（api / delegation / discovery / execution / registry / router / store / stream / task / token）。
- `go test -race ./internal/...`：未执行——当前 Windows 主机缺少 `gcc`（`-race` 需 CGO），属环境限制而非代码缺陷。
- `pytest -q`：`870 passed, 4 skipped`。
- `git diff --check`：通过（无空白错误）。
- 版本统一 `0.42.0`：Go `CurrentProtocolVersion`、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock`、OpenAPI（含 `schemas/protocol-version.yaml` 与 `paths/tasks.yaml` 示例）、contract fixture、config 与 Python 测试硬编码均已同步；`contract/a2a_v0.42.0.json` 与 `openapi/a2a_v0.42.0.yaml` 已重命名生成，旧 `v0.41.0` 文件已删除。
