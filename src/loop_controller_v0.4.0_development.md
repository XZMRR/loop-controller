# Loop Controller v0.4.0 开发文档：跨 Task Session 风险状态持久化

> **文档定位**：v0.3.0（develop 分支）的下一个迭代版本。核心目标是把治理视角从"单次任务独立执行"提升到"会话级持续风控"——同一个 Session 下的多个 Task 共享并持久化风险状态，连续越权行为会触发会话级熔断，防止攻击者通过频繁发起新任务绕过单任务内的调用限制。
>
> **版本**：v0.4.0
> **状态**：详细设计，可直接进入开发
> **最后更新**：2026-08-19

---

## 1. 版本核心跃迁

v0.3.0 的治理粒度是**单个 Task**：每个任务独立计算调用次数、独立累计风险，Session 不持久化，重启后 Session 丢失。v0.4.0 将治理粒度提升到**Session**，使以下场景成为可能：

| 场景 | v0.3.0 行为 | v0.4.0 行为 |
|---|---|---|
| 用户 A 在 1 分钟内连续发起 10 个 Task，每个 Task 尝试读取 `/etc/passwd` | 每个 Task 独立计数，前 3 次被 deny，第 4 次开始因为调用次数耗尽也被 deny，但第 11 个 Task 又重置计数 | 同一 Session 累计 deny 次数，达到阈值后**所有新 Task 在预算预留前即被阻断** |
| 服务重启后风险状态丢失 | 内存数据清空，攻击者可立即重试 | 风险状态持久化到 `risk_state.jsonl`，Session 本身持久化到 `sessions.jsonl`，重启后恢复 |

**核心设计原则**：Session 是用户与系统交互的"信任上下文"，不是任务的附属品。Task 进入 Session，接受 Session 的风险约束；Task 结束后，Session 继续存在，等待下一个 Task 或自然过期。

---

## 2. 与 v0.3.0 的差异总览

| # | 变更项 | v0.3.0 现状 | v0.4.0 决策 |
|---|---|---|---|
| C1 | `SessionManager` 持久化 | 内存实现，重启丢失 | 默认使用 `JsonlSessionStore`，重启恢复 |
| C2 | `Runtime.create_task` 签名 | `create_task(user_id, agent_id, description) -> Task` | `create_task(..., session_id=None) -> tuple[Task, Session]` |
| C3 | `RiskProfile` 字段 | `cumulative_risk_score`、`recent_tags`、`denied_count`、`approval_count` | 新增 `consecutive_deny_count` |
| C4 | 连续拒绝熔断 | 无 | `consecutive_deny_count >= threshold` 时直接 deny |
| C5 | Session 过期 | 30 分钟无新 Task 视为过期，内存中新建 | 持久化后按 TTL 判断，过期 Session 不可复用 |

---

## 3. 核心抽象

### 3.1 Session：会话上下文（已存在，v0.4.0 增强持久化）

当前 `loop_controller.session.Session` 定义不变：

```python
@dataclass
class Session:
    session_id: str
    user_id: str
    agent_id: str
    created_at: datetime
    last_task_at: datetime
    active: bool = True
```

**语义约定**：
- `session_id` 由入口层传入或 `SessionManager` 自动生成；
- 一个 Session 可包含多个 Task，但 MVP 建议**串行**执行；
- Session 有 TTL（默认 30 分钟，与 v0.3.0 一致），`last_task_at` 超过 TTL 后不可复用；
- Session 本身不携带权限信息，权限仍由 Task 关联的 Agent 和 Profile 决定。

### 3.2 Task：不变

`Task` 已包含 `session_id` 字段，v0.4.0 不修改 schema。

### 3.3 RiskProfile：扩展

```python
class RiskProfile(BaseModel):
    session_id: str
    cumulative_risk_score: float = 0.0
    recent_tags: list[str] = Field(default_factory=list)
    denied_count: int = 0
    approval_count: int = 0
    consecutive_deny_count: int = 0  # 新增
```

**语义约定**：
- `cumulative_risk_score` 沿用 v0.3.0 的乘性衰减（每次事件前乘以 0.9）；
- `consecutive_deny_count`：连续 deny 事件累加，中间出现一次 allow/modify/approval_granted 则归零；
- `consecutive_deny_count` 不衰减（安全起见，需显式成功动作才能归零）。

### 3.4 ActionProposal / Decision / ApprovalRecord / AuditEvent：不变

v0.4.0 不改动这些 schema。审计事件已包含 `session_id`。

---

## 4. 组件详细设计

### 4.1 SessionManager：增加 JSONL 持久化后端（升级）

当前 `SessionManager` 依赖 `SessionBackend` Protocol，默认 `InMemorySessionBackend`。v0.4.0 新增 `JsonlSessionBackend`。

```python
class JsonlSessionBackend(SessionBackend):
    def __init__(self, path: str | Path) -> None: ...
    def get_active(self, user_id: str, agent_id: str) -> Session | None: ...
    def get_by_id(self, session_id: str) -> Session | None: ...
    def put(self, session: Session) -> None: ...
```

**持久化格式**（`sessions.jsonl`）：

```json
{"session_id": "s-001", "user_id": "alice", "agent_id": "researcher_001", "created_at": "2026-08-19T10:00:00Z", "last_task_at": "2026-08-19T10:05:00Z", "active": true}
{"session_id": "s-001", "user_id": "alice", "agent_id": "researcher_001", "created_at": "2026-08-19T10:00:00Z", "last_task_at": "2026-08-19T10:30:00Z", "active": true}
```

**加载规则**：
- 启动时读取全部行；
- 按 `session_id` 分组，取 `last_task_at` 最新的一条；
- 只保留 `active=true` 且未过期的 Session；
- 非法 JSON 行：非末行抛 `SessionStoreError`（fail-closed），末行忽略并 WARNING（与 DecisionStore 对齐）。

**SessionManager 新增方法**：

```python
def get_session(self, session_id: str) -> Session | None: ...
```

### 4.2 RiskStateManager：增加 consecutive_deny_count（升级）

当前 `RiskStateManager._apply_in_memory` 已维护 `denied_count` 和 `approval_count`。v0.4.0 增加：

- `consecutive_deny_count`：
  - `deny` / `approval_denied` → +1
  - `allow` / `modify` 成功 / `approval_granted` / `low_risk_success` → 0
  - `require_approval` / `critical` → 不变

`RiskProfile` 模型需增加 `consecutive_deny_count` 字段（见 §3.3）。

### 4.3 Checkpoint：连续拒绝硬熔断（升级）

在 `Checkpoint.evaluate` 中，身份校验之后、重放检测之前，插入：

```python
session_risk = self._risk_manager.get_profile(task.session_id)
if session_risk.consecutive_deny_count >= profile.session_block_threshold:
    self._refund_for(proposal)
    return self._deny(
        proposal,
        f"session blocked: consecutive deny count {session_risk.consecutive_deny_count}",
        now,
        policy_version,
        policy_hits=["session_consecutive_deny_block"],
    )
```

`CapabilityProfile` 新增 `session_block_threshold: int = 5`（默认）。

**设计选择**：硬熔断放在 Checkpoint Python 代码中，而不是 Rego。原因：
- 这是全局安全策略（类似防重放），不随业务 Rego 变化；
- 避免 Rego 输入 schema 新增 `session_block_threshold` 字段；
- 失败早（在重放检测和预算预留之前），减少无效开销。

### 4.4 Runtime：create_task 支持 session_id 复用（升级）

```python
def create_task(
    self,
    user_id: str,
    agent_id: str,
    description: str,
    session_id: str | None = None,
) -> tuple[Task, Session]:
    if session_id is not None:
        session = self.session_manager.get_session(session_id)
        if session is None or self.session_manager.is_session_expired(session_id):
            raise ValueError(f"session {session_id} not found or expired")
        session = self.session_manager.touch_session(session_id)
    else:
        session = self.session_manager.get_or_create_session(user_id, agent_id)

    task = Task(
        task_id=uuid.uuid4().hex,
        session_id=session.session_id,
        user_id=user_id,
        agent_id=agent_id,
        description=description,
    )
    return task, session
```

**注意**：
- 复用 Session 时，仍校验 `task.user_id == session.user_id`；
- `task.agent_id` 可以与 `session.agent_id` 不同（用户可在同一 Session 内切换 Agent），但必须在 Checkpoint 身份校验中通过。

### 4.5 run_task / resume_task：不变

`run_task(task, agent, runtime)` 三参数签名保持不变。Session 通过 `task.session_id` 从 `runtime.session_manager` 获取。

### 4.6 build_runtime：默认使用 JsonlSessionBackend（升级）

```python
session_manager = SessionManager(
    backend=JsonlSessionBackend(config.session_path)  # 新增 AppConfig.session_path
)
```

`AppConfig` 新增 `session_path: str = "./data/sessions.jsonl"`。

---

## 5. Rego 策略

v0.4.0 **不新增** Rego 规则。连续拒绝硬熔断由 Checkpoint 处理，避免与现有 `session_risk_gate`（基于 `cumulative_risk_score` 的软熔断）重复。

若未来需要把硬熔断也交给业务策略，可在 v0.5.0 扩展 Rego input schema。

---

## 6. 持久化格式

### 6.1 sessions.jsonl

```json
{"session_id": "s-001", "user_id": "alice", "agent_id": "researcher_001", "created_at": "2026-08-19T10:00:00Z", "last_task_at": "2026-08-19T10:05:00Z", "active": true}
```

### 6.2 risk_state.jsonl

与 v0.3.0 相同，追加 `RiskEvent`：

```json
{"session_id": "s-001", "event_type": "deny", "score_delta": 0.2, "tag": "deny", "timestamp": "2026-08-19T10:05:00Z"}
```

`consecutive_deny_count` 通过重放事件计算，不单独存储。

---

## 7. 入口层使用方式

### 7.1 创建新 Session

```python
runtime = build_runtime(config)
task, session = runtime.create_task(
    user_id="alice",
    agent_id="researcher_001",
    description="调研 OpenAI 合规争议",
)
result = await run_task(task, agent, runtime)
```

### 7.2 复用现有 Session

```python
task, session = runtime.create_task(
    user_id="alice",
    agent_id="researcher_001",
    description="把刚才的摘要发给张经理",
    session_id="s-001",
)
result = await run_task(task, agent, runtime)
```

---

## 8. 目录结构变化

```text
src/loop_controller/
├── session.py              # 升级：新增 JsonlSessionBackend
├── risk_state.py           # 升级：新增 consecutive_deny_count
├── checkpoint.py           # 升级：步骤 0.5 连续拒绝硬熔断
├── runtime.py              # 升级：create_task 支持 session_id
├── models.py               # 升级：RiskProfile + CapabilityProfile 新增字段
├── infra/config_loader.py  # 升级：加载 session_path
```

---

## 9. 验收标准

| # | 验收项 | 通过条件 |
|---|---|---|
| S1 | Session 持久化 | 重启进程后，已存在的活跃 Session 可从 `sessions.jsonl` 恢复 |
| S2 | Session 复用 | 同一 `session_id` 可创建多个 Task；审计日志中多个 Task 共享同一 `session_id` |
| S3 | Session 过期 | `last_task_at` 超过 TTL 后，`create_task(session_id=...)` 抛错 |
| S4 | 跨 Task 风险累计 | Task 1 中 `read_file` 越权被 deny → Task 2 的 `RiskProfile.denied_count` 增加 |
| S5 | 连续拒绝熔断 | 同一 Session 连续 5 次 deny 后，Task 6 的首次 ActionProposal 被 deny，不消耗预算 |
| S6 | 风险恢复 | 连续 3 次 deny 后，1 次 allow → `consecutive_deny_count` 归零；后续 Task 正常执行 |
| S7 | 重启恢复风险状态 | 进程重启后，RiskStateManager 重放 `risk_state.jsonl`，`consecutive_deny_count` 恢复 |
| S8 | Session 损坏 fail-closed | 手动篡改 `sessions.jsonl` 中间行 → Runtime 启动抛 `SessionStoreError` |

---

## 10. 风险与约束声明

1. **Session 并发假设**：v0.4.0 建议同一 Session 的 Task 串行执行。并行执行可能产生竞态，当前单进程 asyncio 假设下不主动处理。
2. **持久化性能**：`sessions.jsonl` 和 `risk_state.jsonl` 采用追加写 + 启动全量加载，Session 数量超过 10 万时启动时间可能增加。post-v0.4.0 可迁移到 SQLite。
3. **Session 与 Agent 的关系**：Session 记录创建时的 `agent_id`，但允许后续 Task 指定不同 Agent。跨 Agent 的 Session 风险累计默认开启，业务上视为同一用户的信任上下文。

---

## 11. 参考文档

- `docs/architecture/00_r0r3_architecture.md`
- `src/history/Loop_Controller方案_v1.2增补.md`
- `reports/develop_mvp_review_for_team.md`
