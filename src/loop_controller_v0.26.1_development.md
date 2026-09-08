# v0.26.1 开发文档：吊销与证据链可靠性修复

> **基线**：本版本基于 v0.26.0 提交 `e2ee490`。
>
> **版本定位**：v0.26.1 是缺陷修复版本，只修复 v0.26.0 吊销、Kill Switch 和签名证据链的安全边界与一致性问题，不增加新的治理能力。

---

## 1. 目标

v0.26.0 已实现：

- Agent、User、Tool、Secret 吊销；
- 全局 Kill Switch；
- HTTP/gRPC 管理接口；
- 吊销持久化和热更新；
- HMAC-SHA256、Ed25519 签名证据链；
- 审计存储与证据链集成。

代码审查确认主体方向正确，但仍存在以下问题：

1. Secret 吊销检查读取调用参数，而不是真实执行器配置；
2. MCP Proxy 在审批恢复和最终执行前未重新检查吊销；
3. gRPC Admin RPC 只有身份认证，没有管理员授权；
4. 无时区 `expires_at` 会导致吊销检查异常；
5. 异步治理路径写证据时会阻塞事件循环；
6. 删除证据链完整尾部记录无法被当前验证发现；
7. 吊销阻断后可能残留预算预留；
8. 实际命中吊销/Kill Switch 的调用缺少审计事件。

v0.26.1 的目标是修复以上问题，使 v0.26.0 已承诺的能力在真实执行路径上成立。

### 1.1 本版本不做

- 不实现完整多租户隔离；
- 不接入 KMS/HSM；
- 不实现 S3/GCS/WORM 证据后端；
- 不增加新的 Shell、SQL、Browser 内置执行器；
- 不重构现有策略、审批或执行器体系；
- 不承诺在攻击者可同时删除审计文件、证据文件和本地 checkpoint 时检测删除；该能力仍需 v0.27+ 的外部可信锚点。

---

## 2. 修复原则

1. **检查可信执行事实**：Secret 吊销必须基于执行器实际使用的 Secret，而不是调用者可伪造的参数。
2. **最终执行边界统一检查**：所有入口都必须在真实执行器调用前再次检查吊销和 Kill Switch。
3. **认证不等于授权**：通过 mTLS 验证只代表身份可信，不代表拥有管理权限。
4. **时间统一为 UTC-aware**：所有进入吊销模型的时间必须带时区并规范化为 UTC。
5. **异步路径不得同步等待线程**：HTTP、gRPC、MCP 等异步入口不得通过 `thread.join()` 阻塞事件循环。
6. **本地完整性保证必须明确边界**：v0.26.1 检测审计与证据之间的单边丢失和尾部回退，但不虚假承诺能抵抗删除全部本地状态的主机级攻击者。
7. **阻断也必须收尾和审计**：吊销阻断后必须释放预算预留，并留下可验证的安全审计记录。

---

## 3. 修复后的关键流程

```text
身份认证
  ↓
解析 Agent / User / Tool
  ↓
从可信 Tool Spec / Executor 获取实际 secret_refs
  ↓
吊销与 Kill Switch 初检
  ↓
风险分类 / 策略判定
  ↓
审批（如需要）
  ↓
恢复审批
  ↓
统一最终执行边界重检
  ├─ 已吊销：释放 reservation → 写 revoked 审计 → 返回 blocked
  └─ 未吊销：调用 Executor
  ↓
异步写审计与签名证据
  ↓
审计—证据一致性校验
```

最终检查应尽量下沉到所有执行路径必经的公共边界，避免 Controller、MCP Proxy、SDK 或未来入口分别实现后再次遗漏。

---

## 4. 修改文件范围

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/identity/revocation.py` | 修改 | UTC 时间校验和规范化 |
| `src/loop_controller/controller.py` | 修改 | 使用可信 Secret 引用；阻断后的预算释放和审计 |
| `src/loop_controller/checkpoint.py` | 修改 | 增加统一执行前吊销检查，或提供唯一公共执行边界 |
| `src/loop_controller/proxy_server.py` | 修改 | 普通调用和审批重试执行前重检 |
| `src/loop_controller/executors/base.py` 或现有执行器协议文件 | 修改 | 暴露工具实际依赖的 Secret 引用 |
| `src/loop_controller/executors/http_executor.py` | 修改 | 从 `HTTPToolSpec.auth.secret_ref` 返回实际 Secret 引用 |
| `src/loop_controller/grpc_server.py` | 修改 | Admin RPC 增加显式授权 |
| `src/loop_controller/infra/audit_store.py` | 修改 | 提供异步审计写入路径，移除每条事件创建线程并 `join()` 的桥接 |
| `src/loop_controller/audit/evidence.py` | 修改 | 支持与审计尾状态联合验证 |
| `src/loop_controller/audit/evidence_backends.py` | 修改 | 提供尾状态/记录摘要读取能力 |
| `src/loop_controller/runtime.py` | 修改 | 启动时执行审计—证据一致性检查 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 gRPC 管理员授权配置并校验 |
| `config/identity.yaml` | 修改 | 增加 gRPC 管理身份 allowlist 示例 |
| `tests/test_revocation.py` | 修改 | 增加真实 Secret、Proxy、UTC、预算和审计测试 |
| `tests/test_evidence_chain.py` | 修改 | 增加异步并发和尾部删除测试 |

如现有执行器协议文件名称不同，应修改现有文件，不为单个方法额外创建无必要模块。

---

## 5. 修复一：Secret 吊销使用可信来源

### 5.1 当前问题

当前实现递归扫描 `ActionProposal.arguments` 中名为 `secret_ref` 的字段：

```python
secret_refs = self._secret_refs(proposal.arguments)
```

但 HTTP 工具真正使用的 Secret 来自：

```python
HTTPToolSpec.auth.secret_ref
```

正常调用参数通常不包含该字段。因此，吊销配置中的 Secret 后，HTTPExecutor 仍可能解析并使用该 Secret。

调用参数属于不可信输入，也不能作为安全检查的唯一依据。调用者可以删除、伪造或添加 `secret_ref` 字段影响检查结果。

### 5.2 目标设计

执行器注册体系必须能根据工具名返回该工具实际依赖的 Secret 引用。

建议在现有 `ToolExecutor` 协议中增加：

```python
class ToolExecutor(Protocol):
    def secret_refs_for(self, tool_name: str) -> list[str]: ...
```

规则：

- `HTTPExecutor`：读取当前热更新快照中的 `HTTPToolSpec.auth.secret_ref.name`；
- `MCPExecutor`：默认返回空列表，除非 Loop Controller 自身为该 MCP 后端注入 Secret；
- `HarnessExecutor`：返回 Harness 工具配置中由 Loop Controller 注入的 Secret 引用；
- `LocalFunctionExecutor`：默认返回空列表；
- 调用参数中的 `secret_ref` 只能作为补充，不能覆盖或替代可信配置来源；
- 最终列表去重后传给 `RevocationList.is_revoked()`。

建议统一入口：

```python
def resolve_secret_refs(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    executor = executor_registry.get(tool_name)
    trusted_refs = executor.secret_refs_for(tool_name)
    declared_refs = extract_declared_secret_refs(arguments)
    return sorted(set(trusted_refs) | set(declared_refs))
```

### 5.3 热更新要求

HTTP 工具配置热更新后，Secret 吊销检查必须读取 `HTTPExecutor` 当前生效的 Tool Spec，不得读取启动时复制出的旧配置。

### 5.4 验收

- HTTP Tool Spec 配置 `auth.secret_ref.name=sendgrid-key`；
- 调用参数不包含 `secret_ref`；
- 吊销 `sendgrid-key` 后调用必须返回 `blocked/revoked`；
- 移除吊销后恢复执行；
- 调用参数伪造另一个 `secret_ref` 不得让真实配置 Secret 绕过检查；
- HTTP Tool Spec 热更新 Secret 名称后，吊销检查立即使用新名称。

---

## 6. 修复二：统一最终执行前吊销检查

### 6.1 当前问题

Controller 路径已在审批恢复和执行前重检，但 MCP Proxy 仍直接调用：

```python
checkpoint.forward(...)
```

MCP Proxy 只在请求入口检查一次。若调用等待审批期间发生吊销或启用 Kill Switch，审批重试仍可能执行。

### 6.2 目标设计

吊销检查必须覆盖：

| 路径 | 初检 | 最终执行前重检 |
|---|---:|---:|
| HTTP Controller | 必须 | 必须 |
| gRPC Controller | 必须 | 必须 |
| MCP Proxy 普通调用 | 必须 | 必须 |
| MCP Proxy 审批重试 | 必须 | 必须 |
| SDK / ToolGovernor | 必须 | 必须 |
| `execute_with_proposal` | 必须 | 必须 |

首选方案是把最终检查下沉到唯一执行边界，例如 `Checkpoint.forward()` 的执行器调用之前。该边界需要获得：

- 当前 `AgentIdentity`；
- `tool_name`；
- 可信来源的 `secret_refs`；
- 当前共享的 `RevocationList`。

如果为避免 `Checkpoint` 依赖 Runtime 而暂时不能下沉，则必须建立一个公共 `execute_governed()` 方法，由 Controller 和 MCP Proxy 共同调用，禁止入口直接调用 `Checkpoint.forward()`。

不得只在 `_handle_retry()` 临时增加一行检查而保留其他可绕过路径。

### 6.3 错误语义

- 吊销命中：`status="blocked"`、`error_code="revoked"`；
- Kill Switch 命中：可继续使用 `error_code="revoked"`，但审计 metadata 必须包含 `revocation_source="kill_switch"`；
- 不得把吊销错误转换成 `execution_failed`。

### 6.4 验收

- MCP Proxy 首次检查后、`forward()` 前吊销 Tool，执行器不得被调用；
- MCP Proxy 等待审批期间吊销 Agent，审批通过后重试不得执行；
- 等待审批期间启用 Kill Switch，重试不得执行；
- `except_tools` 和 `except_agents` 在最终检查时仍正确生效；
- 通过测试 Spy/Mock 明确断言 Executor 调用次数为 0。

---

## 7. 修复三：gRPC Admin RPC 增加授权

### 7.1 当前问题

当前 `_require_admin_identity()` 只验证 mTLS 身份是否有效。任何被 IdentityProvider 接受的普通 Agent 都可调用：

- `Revoke`；
- `SetKillSwitch`；
- `GetRevocationList`。

身份认证只能证明“是谁”，不能证明“是否有管理权限”。

### 7.2 目标设计

v0.26.1 使用简单、明确的管理员身份 allowlist，不引入完整 RBAC。

建议配置：

```yaml
entrypoints:
  grpc:
    require_auth: true
    admin_agent_ids:
      - admin-agent
```

也可放入现有 identity 配置，但必须满足：

- 配置字段含义明确；
- 默认空列表；
- 未配置管理员时，Admin RPC 全部拒绝；
- 不能通过 agent 名称前缀、profile 名称字符串或 `admin-agent` 硬编码判断；
- HTTP Admin API 继续使用现有 API key，不在本版本统一两套认证体系。

授权函数：

```python
async def _require_admin_identity(context) -> AgentIdentity | None:
    identity = await self._verify_identity(context)
    if identity is None:
        set UNAUTHENTICATED
        return None
    if identity.agent_id not in self._admin_agent_ids:
        set PERMISSION_DENIED
        return None
    return identity
```

### 7.3 验收

- 无有效证书：`UNAUTHENTICATED`；
- 普通已认证 Agent：`PERMISSION_DENIED`；
- allowlist 内管理员：三个 Admin RPC 均可调用；
- 未配置 `admin_agent_ids`：即使身份有效也拒绝；
- 被吊销的管理员身份不得继续调用 Admin RPC；
- 成功和失败的管理操作均保留必要审计，不记录证书或 token 原文。

---

## 8. 修复四：吊销时间统一为 UTC-aware

### 8.1 当前问题

以下值会被解析成无时区 datetime：

```text
2026-08-28T12:00:00
```

之后执行：

```python
entry.expires_at <= datetime.now(UTC)
```

会抛出 `TypeError`，使吊销检查不可用。

### 8.2 目标设计

在 `RevocationEntry` 模型边界统一处理 `revoked_at` 和 `expires_at`，而不是只修复某个 HTTP/gRPC handler。

本版本采用严格策略：**拒绝无时区时间**。

```python
@field_validator("revoked_at", "expires_at")
def require_timezone(cls, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone")
    return value.astimezone(UTC)
```

要求：

- HTTP、gRPC、YAML 都经过同一个模型校验；
- 所有合法时间写盘前转换为 UTC；
- HTTP 非法输入返回 422；
- gRPC 非法输入返回 `INVALID_ARGUMENT`；
- 热更新遇到无时区时间时保留旧内存快照，不清空现有保护。

### 8.3 验收

- `2026-08-28T12:00:00` 被拒绝；
- `2026-08-28T12:00:00Z` 被接受；
- `2026-08-28T15:00:00+03:00` 被转换为 UTC；
- 无时区 YAML 热更新失败时保留旧吊销列表；
- 单条过期记录不会导致其他工具调用异常。

---

## 9. 修复五：异步审计路径不得阻塞事件循环

### 9.1 当前问题

`JsonlAuditStore._append_evidence()` 在检测到已有事件循环后：

```python
thread = threading.Thread(target=run)
thread.start()
thread.join()
```

这会为每条审计创建线程和事件循环，并通过 `join()` 阻塞 HTTP、gRPC 或 MCP 主事件循环。

### 9.2 目标设计

为 AuditStore 增加真正的异步写入接口：

```python
class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None: ...
    async def append_async(self, event: AuditEvent) -> None: ...
```

使用规则：

- HTTP、gRPC、MCP Proxy、Controller 等异步调用链使用 `await append_async()`；
- CLI、启动脚本和纯同步调用方可继续使用 `append()`；
- `append_async()` 不得调用 `thread.join()`；
- 本地同步文件 I/O 可使用 `asyncio.to_thread()` 放到受控线程池；
- 不得为每条事件创建独立线程和独立事件循环；
- 同一 AuditStore 的 seq、哈希链状态和 EvidenceChain 状态必须由共享锁保护；
- 多个并发 append 的最终落盘顺序必须与 seq 顺序一致。

不建议在本版本引入复杂消息队列。使用单实例异步锁加 `asyncio.to_thread()` 即可满足当前本地后端需求。

### 9.3 写入语义

保持 v0.26.0 原则：

- 证据写入失败时，原审计仍必须写入；
- 产生 critical alert；
- 不得静默吞掉失败；
- 同一进程内并发写入不能产生重复 seq、乱序 `prev_hash` 或证据链分叉。

### 9.4 验收

- 删除 `_append_evidence()` 中每条事件创建线程并 `join()` 的实现；
- 在运行中的 asyncio loop 连续写入多条审计，无跨事件循环异常；
- `asyncio.gather()` 并发写入至少 50 条，审计和证据 seq 连续、哈希链验证通过；
- 使用慢 EvidenceBackend 时，事件循环中的独立 heartbeat 协程仍能运行；
- 同步 `append()` 的既有调用和测试保持兼容。

---

## 10. 修复六：检测证据尾部回退和单边丢失

### 10.1 安全边界

仅依靠单个本地哈希链，无法检测攻击者删除完整合法后缀：剩余部分仍是一条密码学上有效的短链。

v0.26.1 不实现远程可信锚点，因此目标限定为：

1. 审计文件仍存在时，检测证据文件尾部删除或整体删除；
2. 证据文件仍存在时，检测审计文件尾部删除；
3. 检测启动后相对于上次本地 checkpoint 的序号回退；
4. 明确记录“攻击者同时删除全部本地副本和 checkpoint”仍不可检测。

### 10.2 审计—证据交叉校验

每条 SignedEvidence 已包含完整 `AuditEvent`。启动验证时增加一致性检查：

- 对参与证据链的审计事件，比较 `event_id`、审计 `seq` 和规范化事件摘要；
- 审计存在但证据缺失：验证失败并告警；
- 证据存在但审计缺失：验证失败并告警；
- 两边最后一个共同事件之后任一侧还有记录：验证失败并告警；
- 空证据文件但审计中已有应签名事件：验证失败；
- 两边都为空：视为新部署，可通过。

如果 seal 等内部审计事件有特殊处理，必须显式定义哪些 action 进入 EvidenceChain，不得依赖隐式跳过。

### 10.3 本地尾状态 checkpoint

可增加本地 checkpoint，例如：

```json
{
  "audit_seq": 120,
  "audit_hash": "...",
  "evidence_seq": 120,
  "evidence_hash": "...",
  "updated_at": "...",
  "signature": "...",
  "algorithm": "ed25519",
  "key_id": "evidence-1"
}
```

要求：

- 临时文件写入后原子替换；
- checkpoint 载荷使用 EvidenceSigner 签名；
- 启动时比较当前文件尾部与 checkpoint；
- 当前 seq 小于 checkpoint 时必须告警；
- checkpoint 缺失但数据文件非空时必须告警，不得静默重建并覆盖证据；
- checkpoint 与数据文件一起被删除的场景仍属于已知限制。

checkpoint 不是远程可信锚点，不得在 README 中宣传为 WORM 或主机级不可删除证明。

### 10.4 启动失败策略

保持 v0.26.0 的可用性原则：验证失败时告警但不阻塞服务。不过必须：

- 将 EvidenceChain 标记为 `degraded`；
- 后续告警包含原验证失败原因；
- 健康检查暴露 `evidence_status=healthy|degraded|disabled`；
- 不得返回“证据链健康”。

### 10.5 验收

- 删除最后一条完整证据记录，启动验证失败；
- 删除多条完整证据记录，启动验证失败；
- 删除整个证据文件但保留审计，启动验证失败；
- 删除审计尾部但保留证据，启动验证失败；
- checkpoint seq 大于当前文件 seq，启动验证失败；
- 新部署中审计和证据均为空，验证通过；
- 同时删除全部本地文件仍作为已知限制写入文档，不编写无法成立的“可检测”测试。

---

## 11. 修复七：吊销阻断后释放预算预留

### 11.1 当前问题

策略判定可能已经创建 reservation，审批路径还会将其转为 `pending_approval`。如果随后因吊销直接返回，reservation 可能没有退款，持续占用任务预算。

### 11.2 目标设计

所有在“已预留预算、尚未真实执行”阶段发生的吊销都必须执行统一收尾：

```text
发现吊销
  ↓
查找 decision / reservation
  ↓
若 reservation 尚未 commit/refund，则 refund(reason="revoked")
  ↓
写吊销审计
  ↓
返回 blocked
```

要求：

- refund 必须幂等；
- 不得退款已经成功 commit 的 reservation；
- 审批等待期间吊销必须释放 `pending_approval` reservation；
- Controller 和 MCP Proxy 使用同一个收尾函数；
- Kill Switch 阻断使用相同收尾逻辑。

### 11.3 验收

- 审批等待期间吊销 Agent，reservation 状态变为 refunded；
- allow 后、forward 前吊销 Tool，reservation 被释放；
- 重复调用恢复接口不会重复退款；
- Executor 已成功执行并 commit 后新增吊销，不修改历史 reservation。

---

## 12. 修复八：记录吊销命中审计

### 12.1 当前问题

Admin API 会记录“谁增加或删除了吊销项”，但实际工具调用命中吊销时可能直接返回，没有留下安全事件。

这导致无法回答：

- 哪个 Agent 在吊销后继续尝试调用；
- 哪个 Tool 被 Kill Switch 拦截；
- 哪个 Secret 吊销实际命中了调用；
- 审批恢复后是否因为吊销而未执行。

### 12.2 审计事件设计

建议使用统一 action：

```python
AuditEvent(
    action="revocation_blocked",
    actor_type="agent",
    actor_id=identity.agent_id,
    target=tool_name,
    decision="blocked",
    reason=reason,
    metadata={
        "revocation_type": "agent|user|tool|secret|kill_switch",
        "revocation_id": matched_id,
        "stage": "initial|approval_resume|pre_execute",
    },
)
```

为支持该事件，`RevocationList.is_revoked()` 最好返回结构化匹配结果，而不是只能返回字符串。可以新增不可变模型：

```python
class RevocationMatch(BaseModel):
    revoked: bool
    reason: str | None = None
    type: RevocationType | Literal["kill_switch"] | None = None
    id: str | None = None
```

如不修改返回类型，也必须由调用层可靠获得 `type/id/stage`，不得通过解析 reason 字符串推断。

### 12.3 数据保护

- Secret 类型审计只记录 Secret 引用名称，不记录 Secret 值；
- 不记录 token、证书、Authorization header；
- arguments 继续使用现有 Masker；
- 审计失败不得让已吊销调用继续执行。

### 12.4 验收

- 初次检查命中 Agent/User/Tool/Secret 均产生一条 `revocation_blocked`；
- Kill Switch 命中产生对应事件；
- 审批恢复阶段命中时 `stage=approval_resume`；
- 最终执行前命中时 `stage=pre_execute`；
- 审计中不出现 Secret 值；
- 同一阶段不得重复记录相同阻断事件。

---

## 13. Admin 持久化错误处理

本次修复同时统一 HTTP/gRPC 管理接口的持久化失败语义：

- HTTP `RevocationList.add/remove/set_kill_switch` 写盘失败：返回 500 或 503，不返回成功；
- gRPC 写盘失败：返回 `INTERNAL` 或 `UNAVAILABLE`，不泄漏本地绝对路径和异常堆栈；
- 写盘失败时保持当前内存状态不变；
- 只有状态真正持久化成功后才记录 `revocation_added`、`revocation_removed` 或 `kill_switch_updated` 成功审计；
- 失败操作记录 `admin_operation_failed`，但不得包含密钥信息。

---

## 14. 测试计划

### 14.1 `tests/test_revocation.py`

新增或补齐：

1. HTTP Tool Spec 中的真实 Secret 吊销；
2. 调用参数伪造 Secret 引用不能绕过；
3. MCP Proxy 普通调用最终执行前吊销；
4. MCP Proxy 审批等待期间吊销；
5. MCP Proxy 审批等待期间启用 Kill Switch；
6. gRPC 普通 Agent 调用 Admin RPC 返回 `PERMISSION_DENIED`；
7. 未配置 admin allowlist 时拒绝 Admin RPC；
8. 无时区 HTTP/gRPC/YAML 时间被拒绝；
9. 带偏移时间转换为 UTC；
10. 吊销后 reservation 被退款；
11. 吊销命中产生审计；
12. admin 持久化失败返回受控错误。

### 14.2 `tests/test_evidence_chain.py`

新增或补齐：

1. 运行中事件循环连续写入；
2. 并发写入至少 50 条；
3. 慢后端不阻塞 heartbeat；
4. 删除最后一条完整证据记录；
5. 删除多条完整证据记录；
6. 删除整个证据文件但保留审计；
7. 删除审计尾部但保留证据；
8. checkpoint 回退；
9. checkpoint 缺失但数据非空；
10. 新部署空文件状态；
11. degraded 状态进入健康检查。

### 14.3 回归检查

```powershell
uv run python -m pytest tests/ -q
uv run ruff check src tests examples
uv run python -m mypy src
```

不得只运行新增测试。必须保证 v0.26.0 原有吊销、审批、HTTP、gRPC、MCP Proxy、审计和证据链测试全部通过。

---

## 15. 验收标准

v0.26.1 完成必须同时满足：

### 吊销

- [ ] Agent、User、Tool、Secret 和 Kill Switch 在所有入口有效；
- [ ] Secret 检查使用执行器当前真实配置；
- [ ] MCP Proxy 审批恢复和最终执行前重检；
- [ ] 所有真实执行路径共享统一最终检查边界；
- [ ] 无时区 datetime 无法进入吊销快照；
- [ ] 吊销阻断释放未提交 reservation；
- [ ] 吊销命中写入结构化审计事件。

### Admin 安全

- [ ] gRPC Admin RPC 同时具备认证和管理员授权；
- [ ] 普通 Agent 返回 `PERMISSION_DENIED`；
- [ ] 未配置管理员时默认拒绝；
- [ ] 持久化失败不会更新内存或返回成功；
- [ ] Admin 成功和失败操作均有适当审计。

### 证据链

- [ ] 异步入口不再使用每条事件创建线程并 `join()`；
- [ ] 并发写入不产生重复 seq、断链或分叉；
- [ ] 审计和证据单边尾部丢失可被启动校验发现；
- [ ] 本地 checkpoint 回退可被发现；
- [ ] 验证失败后健康状态为 degraded；
- [ ] 文档明确本地 checkpoint 不能抵抗全部本地状态同时被删除。

### 工程质量

- [ ] 全量 pytest 通过；
- [ ] ruff 通过；
- [ ] mypy 通过；
- [ ] 不引入多租户、KMS、远程存储等超出 v0.26.1 的功能；
- [ ] 不新增 Shell、SQL、Browser 核心执行器；
- [ ] 版本号更新为 `0.26.1`。

---

## 16. 实施顺序

建议代码 Agent 按以下顺序开发：

1. 修复 datetime 模型校验；
2. 增加 gRPC Admin allowlist 授权；
3. 为执行器增加可信 Secret 引用查询；
4. 建立统一最终执行前吊销检查；
5. 增加阻断后的 reservation 退款和审计；
6. 改造 AuditStore 异步写入路径；
7. 增加审计—证据交叉校验和本地 checkpoint；
8. 补齐测试并运行全量检查。

前五项先保证治理正确性，后三项再修复证据链可靠性，避免同时大范围修改执行路径和审计路径。

---

## 17. 完成后的边界

v0.26.1 完成后，Loop Controller 应达到：

```text
治理正确性
  ├─ 可信身份
  ├─ 可信 Secret 依赖解析
  ├─ 全入口吊销初检
  ├─ 统一最终执行前重检
  ├─ Admin 认证 + 授权
  └─ 阻断后的预算与审计收尾

本地证据可靠性
  ├─ 异步安全写入
  ├─ 单进程并发有序
  ├─ 审计—证据交叉校验
  ├─ 本地尾状态 checkpoint
  └─ 明确的 degraded 健康状态
```

仍然不承诺：

- Agent 在不受控运行环境中无法绕过 Loop Controller；
- 多进程/多 worker 对同一 JSONL 文件安全并发写入；
- 攻击者取得主机权限并删除全部本地审计、证据和 checkpoint 后仍能检测；
- 本地 Ed25519 私钥达到 KMS/HSM 的密钥保护等级。

这些能力继续留给 v0.27+ 的凭证隔离、外部可信锚点、远程不可变存储和分布式协调。