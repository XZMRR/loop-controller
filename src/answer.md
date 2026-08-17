不需要先看代码。这 6 点都属于方案层决策，可以直接定案；其中 1-4 是 P1 L2 的开工边界，5 是 P0，6 是 P2，不应阻塞当前 P1。

## 1. Session 分配职责：归 Runtime，新增 SessionManager

**定案：新增 `SessionManager`，挂在 `Runtime` 上。**

推荐结构：

```python
task = runtime.create_task(
    user_id="...",
    agent_id="...",
    description="...",
)
```

原则：

- `session_id` 是治理状态，不应由 Agent、Planner 或普通调用方自行决定；
- `SessionManager` 负责：
  - `get_or_create_session(user_id, agent_id)`；
  - 30 分钟间隔判定；
  - `validate_and_touch(task)`；
  - session 过期与关闭；
- `Task` 数据结构暂时不变，仍然携带 `session_id`，避免大范围改动；
- `runtime.create_task(...)` 是正式入口；
- 为了兼容已有测试，`run_task(task, ...)` 可以继续存在，但必须校验：
  - `task.session_id` 存在；
  - session 仍活跃；
  - session 绑定的 `(user_id, agent_id)` 与 Task 一致；
  - 不一致则 fail-closed。

也就是说：**既新增 `runtime.create_task`，又保留底层 `run_task(task)`，但后者必须验证 session binding。**

现有 example 和 e2e 改成 `runtime.create_task(...)`，只是一行级别调整；单元测试仍可直接构造 `Task`，但要注入受控的 `SessionManager` fixture。

---

## 2. RiskStateManager：显式配置路径，P1 初始版本仍按单进程实现

**定案：新增 `risk_state_path` 配置，不写死隐藏路径。**

建议：

```yaml
risk_state_path: "./data/risk_state.jsonl"
```

对应：

```python
class AppConfig(BaseModel):
    risk_state_path: Path = Path("./data/risk_state.jsonl")
```

启动检查：

- 父目录不存在则创建或报错，行为与现有 `data/` 检查策略一致；
- 文件不可写则启动失败；
- 启动时重放 JSONL；
- 最后一行若是崩溃造成的不完整 JSON：
  - 忽略该行；
  - 记录 WARNING；
  - 不阻止启动；
  - 但不能静默吞掉。

持久化方式：

- P1 初版：单进程、单 writer；
- Runtime 内用 `asyncio.Lock` 串行化写入；
- 每次追加完整 JSONL 行；
- 写后 flush，必要时 fsync；
- 通过 `RiskStateStore` 接口隔离实现，例如：

```python
class RiskStateStore(Protocol):
    def append_event(...)
    def load_all(...)
```

关于多 writer：

- **L2 初版不要等多 worker 方案。**
- 当前仍沿用 L3 单进程 asyncio 假设；
- 但 P1 后续做“多 worker DecisionStore”时，必须把 `RiskStateStore` 一起迁移到同一套存储或锁机制；
- 不要现在单独为 RiskStateManager 搞一套文件锁方案，否则后面会出现两套并发语义。

换句话说：

> P1-L2 的代码合入可以基于单进程假设；但如果 v0.3.0 宣称支持多 worker，RiskStateManager 也必须包含在多 worker 原子性方案里。

---

## 3. `session_risk_threshold`：放 CapabilityProfile，传入 Rego input

**定案：per-profile 配置，默认 0.6。**

```python
class CapabilityProfile(BaseModel):
    session_risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
```

理由：

- 不同 Agent 的权限和信任等级不同；
- 高权限 Agent 可能需要更低阈值；
- 阈值属于能力档案的一部分，应该随 `profile_version` 一起进入审计。

`build_policy_input` 需要显式扩展：

```json
{
  "session_risk": {
    "score": 0.42,
    "threshold": 0.6,
    "recent_tags": ["deny", "critical"],
    "session_id": "..."
  }
}
```

Rego 规则只比较：

```rego
input.session_risk.score >= input.session_risk.threshold
```

注意三点：

1. 阈值不要写死在 Rego policy 里；
2. 不要把整个 `CapabilityProfile` 无筛选地塞进 Rego input，只传治理所需字段；
3. 必须补 Python ↔ Rego input contract test，防止再次出现字段不一致导致 default deny。

`session_risk_gate` 仍然只能产生 `require_approval`，不能覆盖硬 deny；优先级依旧是：

```text
deny > require_approval > modify > allow
```

---

## 4. `recent_tags`：固定最近 10 条，只记录风险证据，不随分数衰减

**定案：`recent_tags` 是 bounded FIFO，最多 10 条。**

维护规则：

- 只记录风险相关标签：
  - `deny`
  - `critical`
  - `require_approval`
  - `approval_denied`
  - `approval_granted`
- `allow`、`low_risk_success` 不进入 `recent_tags`；
- 低风险成功只影响 `cumulative_risk_score`，不清洗风险证据；
- 新风险事件到来时追加；
- 超过 10 条时淘汰最旧的一条；
- `recent_tags` 不做时间衰减；
- 分数衰减只作用于 `cumulative_risk_score`。

即：

```text
score 会恢复；
recent_tags 不会因为成功调用而被“洗掉”；
recent_tags 只通过 FIFO 自然淘汰。
```

Session 结束后：

- 活跃内存中的 RiskProfile 可以移除；
- JSONL 风险事件仍保留，用于重放和审计；
- 新 Session 从零开始；
- 跨 Session 信誉、长期信誉、Earned Authority 不属于 P1，放到后续阶段。

需要补的测试：

1. 连续 11 个风险事件后，只保留后 10 个；
2. 低风险成功会降低 score，但不会删除 `recent_tags`；
3. 新 session 的 score 和 tags 都从零开始；
4. 重启后通过 JSONL replay 能恢复相同状态。

---

## 5. P0 HMAC key：只能来自环境变量，单部署级 root key

**定案：P0 使用环境变量，不进配置文件，不接 KMS。**

建议配置里只存环境变量名：

```yaml
audit_hmac_key_env: "LOOP_CONTROLLER_AUDIT_HMAC_KEY"
```

实际 key 只从环境变量读取：

```bash
export LOOP_CONTROLLER_AUDIT_HMAC_KEY="..."
```

要求：

- key 至少 32 字节随机熵， hex 或 base64 编码；
- `hash_algo = hmac-sha256` 时，如果环境变量缺失或格式非法，启动 fail-closed；
- `sha256` 只作为兼容旧审计文件或开发模式保留；
- 新增 audit event 里应带 `key_id`，为未来轮换留口。

key 粒度：

- P0 不做 per-trace key；
- 不做 per-session key；
- 使用单个部署级 root key；
- key 轮换通过 `key_id` 支持，P2 再接 KMS。

事件链和 seal 可以共用同一个 root key，但必须做域分离：

```text
event key = HKDF(root_key, "lc:audit:event:v1")
seal key  = HKDF(root_key, "lc:audit:seal:v1")
```

如果不想引入 HKDF，也至少要用不同 label 做 HMAC domain separation，不能事件和 seal 直接混用同一段输入语义。

---

## 6. P2 Proxy：Checkpoint 需要服务化，HTTP 先行，身份由连接凭证决定

**定案：P2 的 Proxy 形态下，Checkpoint 应包装成独立决策服务。**

目标结构：

```text
外来 Agent
   │
   ▼
LC Proxy（MCP Server / PEP）
   │  HTTP JSON，mTLS 或服务令牌
   ▼
Checkpoint Service（PDP / R2）
   ├── OPA
   ├── DecisionStore
   ├── RiskStateManager
   └── AuditStore
   │
   ▼
真实 MCP Tool Server
```

P2 初版使用 HTTP JSON 即可：

- schema 与现有 `ActionProposal` / `Decision` 对齐；
- API 要版本化，例如 `/v1/...`；
- gRPC 是后续性能优化，不是 P2 必需项；
- Proxy 不直接访问 OPA；
- Proxy 不能自行 allow；
- Checkpoint 服务超时或不可达时，Proxy fail-closed。

角色边界要稍微调整表述：

- Checkpoint Service 是唯一决策权威；
- LC Proxy 是可信 PEP，负责执行已经下发的 Decision；
- Proxy 校验：
  - `decision_id`；
  - `expires_at`；
  - `max_uses = 1`；
  - `modified_params`；
- 这不会破坏“R2 唯一权威”，因为 Proxy 不能自己产生授权。

### 身份字段怎么来

Proxy 形态下必须坚持：

> Agent 自报的 `agent_id`、`user_id`、`task_id` 一律不能作为权威身份。

具体规则：

- `agent_id`：
  - 来自 MCP 连接凭证与 Proxy 配置的映射；
  - 可来自 API key、mTLS client cert、stdio 启动绑定等；
  - 不能来自工具参数或 prompt 字段；
- `user_id`：
  - 同样来自连接凭证映射；
  - 如果外来 Agent 没有用户概念，则映射到配置好的服务身份；
- `session_id`：
  - 由 Proxy 侧 SessionManager 创建；
  - 基于权威 `(user_id, agent_id)` 和连接上下文；
  - 仍沿用 30 分钟 gap 规则；
- `task_id`：
  - P2 初版由 Proxy 创建治理侧 synthetic task；
  - 外来 Agent 自报的 task 标识只能作为 `declared_task_id` 元数据进入审计；
- `call_id`：
  - 由 Proxy 或 Checkpoint 生成；
  - 仍必须全局唯一并进入 DecisionStore 防重放；
  - 不能由外来 Agent 提供。

自报信息可以继续遵循 v1.2 的铁律：

```text
只能收紧，不能放宽。
```

例如外来 Agent 自报“这是高风险操作”，Proxy 可以把它加入 declared context；但如果它自报“这是低风险”，不能直接降低治理等级。

---

## 开工顺序建议

前三点已经可以解锁 P1 L2：

1. `SessionManager` + `runtime.create_task`；
2. `risk_state_path` + JSONL replay；
3. `CapabilityProfile.session_risk_threshold`；
4. `build_policy_input` 扩展 + Rego contract test；
5. `recent_tags` FIFO 规则与评分测试。

优先级上：

- **如果只有一个代码 agent：先 P0 HMAC，再 P1 L2。**
- **如果可以并行：P0 HMAC 和 P1 L2 没有代码边界冲突，可以并行。**
- P2 Proxy 现在不要动，只把上述身份原则记录下来，避免提前过度设计。