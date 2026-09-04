# v0.41.0：分布式可靠性

**Status**: 完成  
**Target Version**: `0.41.0`  
**Goal**: 在 v0.40.0 单实例执行生命周期之上，消除内核的单实例假设，使多个 kernel 实例可在共享状态下可靠运行，并具备故障转移与任务恢复能力。先在单设备上以多进程 + 共享 SQLite + 故障注入方式验证，为未来多服务器部署打基础。

---

## 1. 版本定位

v0.40.0 完成的是单实例下的执行正确性：CAS 状态机、Task/Event 同事务、SSE 重放、真实派发、取消传播、task-scoped token、lifecycle outbox。但这些实现都隐含“只有一个 kernel 进程”这一假设：

| 单实例假设 | 所在位置 | 多实例后果 |
| --- | --- | --- |
| SQLite 本地文件路径 + 无 busy_timeout | `store/db.go` | 多进程同时读写时报 `database is locked` |
| outbox 无领取/租约机制 | `store/lifecycle_outbox.go` | 多个实例重复投递同一审计事件 |
| SSE 仅进程内内存 fan-out | `stream/stream.go` | 客户端连 A，事件由 B 提交时收不到 |
| `executions` 为进程本地 map | `api/handlers.go` | 实例宕机后 `running` 任务无人接管 |
| 任务/事件 ID 用本地时间 + 计数器 | `task/task.go`、`store/task.go`、`stream/stream.go` | 跨实例可能 ID 冲突 |
| registry / router / discovery 纯内存 | `registry/`、`router/`、`discovery/` | 跨实例注册信息不可见 |

v0.41.0 在**不引入 PostgreSQL 或外部消息队列**的前提下，通过共享 SQLite（WAL）与显式租约/恢复机制，达成单设备多实例下的可靠性。多服务器部署只需将 SQLite 文件换成可共享的网络存储，或后续替换为共享数据库驱动。

---

## 2. 核心原则

1. **共享状态为事实源**：SQLite 文件是唯一事实源，进程内缓存仅是投影。
2. **显式实例身份**：每个 kernel 实例拥有唯一 instance ID，用于租约、ID 生成与竞争消解。
3. **单次投递**：outbox 与执行任务都必须通过原子领取，保证同一单元最多被一个实例处理。
4. **不确定即恢复**：无法确认的 `running` 任务超时后进入 `outcome_unknown`，不伪报结果。
5. **可重放投影**：SSE 是持久化事件的投影，跨实例通过存储轮询 + `Last-Event-ID` 补发。
6. **默认拒绝**：任何租约、状态、身份或持久化异常均 fail-closed。

---

## 3. 实施阶段

### P41-01 共享存储与实例身份

- `store.Open` 增加 `PRAGMA busy_timeout`，使多进程共享同一 SQLite 文件时可等待锁而非立即失败。
- 新增实例身份 `instance.ID`（进程唯一，用于租约 owner 与 ID 前缀）。
- 使 `api.NewServer` 与 kernel 入口可注入 instance ID；未注入时自动生成。

### P41-02 outbox 竞争消解（单次投递）

- `lifecycle_outbox` 增加 `claimed_by`、`claim_expires_at` 两列。
- 新增原子领取 `ClaimDue`，领取条件是 `delivered_at IS NULL AND next_attempt_at <= now AND (claimed_by = '' OR claim_expires_at < now)`。
- `MarkDelivered` / `MarkFailed` 增加 owner 校验，仅领取者可确认或失败。
- Dispatcher 改为 claim → deliver → ack/fail；失败时释放租约并指数退避。

### P41-03 SSE 跨实例

- `stream` 增加存储轮询能力：订阅者除本地 fan-out 外，周期性 `ListAfter(taskID, lastEventID)` 拉取跨实例提交的事件。
- 以 `lastEventID` 去重，保证本地 fan-out 与轮询不重复推送。
- 断线重连仍依赖 `Last-Event-ID` 重放。

### P41-04 故障转移与任务恢复

- `tasks` 增加 `exec_owner`、`exec_lease_expires_at` 两列。
- 任务进入 `running` 时记录执行租约（owner = 当前 instance ID，过期时间可配）。
- 新增恢复扫描：定期将 `running` 且租约过期的任务 CAS 至 `outcome_unknown`（结果不确定，等待迟到结果回补或人工/策略判定）。

### P41-05 幂等与 ID 分布式安全

- 任务/事件 ID 生成加入 instance 前缀，保证共享库内全局唯一。
- 幂等存储依赖 SQLite 事务，配合 busy_timeout 在共享文件下保持跨进程安全。

---

## 4. 明确不做

- PostgreSQL 或其他共享数据库驱动（留待后续版本）。
- 外部消息队列 / Kafka / NATS。
- 分布式共识（Raft/Paxos）。
- 跨信任域 Agent 联邦或 OAuth2 动态注册。
- 自动补偿业务副作用。
- registry / router / discovery 的持久化（本次仅记录为已知单实例假设，暂不改造）。

---

## 5. 完成定义

v0.41.0 只有同时满足以下条件才能完成：

1. 两个 kernel 实例共享同一 SQLite 文件可并发读写而不报 `database is locked`。
2. 多个实例的 outbox dispatcher 并发运行时，每条 outbox 记录恰好被投递一次（无重复、无丢失）。
3. 客户端连接到实例 A，仍能收到由实例 B 提交的任务事件。
4. 实例宕机后，其遗留的 `running` 任务在租约过期后被标记为 `outcome_unknown`，不永久卡死。
5. 跨实例生成的 task/event ID 无冲突。
6. Python、Go、OpenAPI、contract fixture 版本统一为 `0.41.0`。
7. Go 全量与 race 测试、`go build`、`go vet`、`git diff --check` 全部通过。

---

## 6. 验证记录

- `go build ./...`：通过。
- `go test ./...`：全部包通过（`task`、`store`、`stream`、`api` 等均 ok）。
- `go vet ./...`：通过。
- `git diff --check`：通过。
- `uv run pytest -q`：`870 passed, 4 skipped`。
- `go test -race ./...`：全部包通过（已安装 mingw-w64 gcc 并设置 `CGO_ENABLED=1`）。
