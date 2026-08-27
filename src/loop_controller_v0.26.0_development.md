# v0.26.0 开发文档：吊销与签名证据链

> **前提**：本版本基于 v0.25.0（已回退后的基线）构建。
>
> **目标收敛**：v0.26.0 只补两个企业级治理刚需——**全局吊销/Kill Switch** 和 **签名证据链接入审计**。不碰多租户隔离、远程证据存储、KMS 后端。

---

## 1. 目标

v0.25.0 完成后，Loop Controller 已经具备清晰的执行器架构：

```text
Loop Controller
  ├─ MCP 工具   → MCPExecutor
  ├─ HTTP 工具  → HTTPExecutor
  └─ Harness 工具 → HarnessExecutor → 外部 Harness
```

v0.26.0 只做两件事：

1. **全局吊销 + Kill Switch**：让管理员能在秒级吊销某个 agent、user、tool 或 secret 的调用权限，并能一键停止所有工具调用。
2. **签名证据链接入审计**：把审计事件从普通 JSONL 升级为带签名/哈希链的证据记录，支持本地不可篡改验证。

**v0.26.0 不做**：

- 多租户隔离（`tenant_id` 字段保留，但不强制隔离，v0.27+）；
- 远程证据存储（S3/GCS，v0.27+）；
- KMS/HSM 集成（v0.27+，本版本只支持 HMAC 和本地 Ed25519 签名）；
- UI 控制台、计费、配额、分布式控制平面。

---

## 2. 背景与动机

### 2.1 为什么需要吊销

当前凭证一旦签发就无法提前失效：

- JWT 在过期前一直可用；
- 静态 token / API key 泄露后只能整体轮换；
- 已审批的工具调用无法中途撤销。

企业需要能：

- 吊销某个 agent 的所有 token；
- 吊销某个 secret（如 SendGrid API key 泄露）；
- 吊销某个 tool（如发现某 MCP Server 有漏洞）；
- 一键 Kill Switch 停止所有高风险调用。

### 2.2 为什么需要签名证据链

当前审计是本地 JSONL + HMAC：

- 如果攻击者拿到主机权限，可以删除或篡改审计文件；
- 审计私钥和应用数据在同一台机器，不符合合规要求。

签名证据链要求：

- 每条审计事件带签名；
- 后一条记录绑定前一条哈希，形成链；
- 第三方拿到公钥后可以独立验证整条链。

---

## 3. 设计原则

1. **Fail-closed**：任何吊销/Kill Switch 配置错误时，默认拒绝调用，而不是放行。
2. **全路径检查**：吊销检查必须在身份认证之后、策略判定之前执行；审批恢复后和实际执行前必须再次检查。
3. **持久化 + 热更新**：吊销列表写入 `config/revocation.yaml`，admin API 修改后同步写回文件，`HotReloader` 能监控文件变化。
4. **审计必须先走证据链**：所有写入 `JsonlAuditStore` 的事件必须先经过 `EvidenceChain.append()`。
5. **向后兼容**：不配置 `revocation.yaml` / `evidence.yaml` 时，v0.25.0 行为不变。
6. **单租户优先**：不引入复杂多租户隔离逻辑，`tenant_id` 仅在数据模型中保留字段。

---

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/identity/revocation.py` | 重写 | `RevocationEntry`、`RevocationList`、`KillSwitchConfig` |
| `src/loop_controller/identity/models.py` | 修改 | `AgentIdentity` 预留 `tenant_id` 字段 |
| `src/loop_controller/controller.py` | 修改 | 在 `evaluate`、`resume_after_approval`、`execute_with_proposal` 中检查吊销 |
| `src/loop_controller/server.py` | 修改 | 新增 admin 端点：`/admin/revoke`、`/admin/revocation-list`、`/admin/kill-switch` |
| `src/loop_controller/grpc_server.py` | 修改 | 新增对应 gRPC admin 方法 |
| `proto/loop_controller/v1/governance.proto` | 修改 | 新增 `Revoke`、`SetKillSwitch`、`GetRevocationList` 方法及消息 |
| `src/loop_controller/audit/evidence.py` | 重写 | `EvidenceSigner`、`SignedEvidence`、`EvidenceChain` |
| `src/loop_controller/audit/evidence_backends.py` | 新增 | `LocalFileEvidenceBackend` |
| `src/loop_controller/infra/audit_store.py` | 修改 | `JsonlAuditStore.append` 先调用 `EvidenceChain.append` |
| `src/loop_controller/runtime.py` | 修改 | 初始化 `RevocationList`、`EvidenceChain` |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/revocation.yaml`、`config/evidence.yaml` |
| `src/loop_controller/infra/hot_reload.py` | 修改 | 监控 `revocation.yaml` 变化 |
| `config/revocation.yaml` | 新增 | 吊销列表示例 |
| `config/evidence.yaml` | 新增 | 证据链配置示例 |
| `tests/test_revocation.py` | 重写 | 吊销与 Kill Switch 测试 |
| `tests/test_evidence_chain.py` | 重写 | 证据链集成测试 |

---

## 5. 吊销模块设计

### 5.1 数据模型

```python
class RevocationType(str, Enum):
    AGENT = "agent"
    USER = "user"
    TOOL = "tool"
    SECRET = "secret"

class RevocationEntry(BaseModel):
    type: RevocationType
    id: str
    reason: str
    revoked_at: datetime
    expires_at: datetime | None = None
    tenant_id: str | None = None

class KillSwitchConfig(BaseModel):
    enabled: bool = False
    reason: str = ""
    except_tools: list[str] = []
    except_agents: list[str] = []
```

### 5.2 RevocationList 接口

```python
class RevocationList:
    def is_revoked(self, identity: AgentIdentity, tool_name: str, secret_refs: list[str]) -> tuple[bool, str | None]:
        """返回 (是否吊销, 原因)"""

    def check_kill_switch(self, identity: AgentIdentity, tool_name: str) -> tuple[bool, str | None]:
        """返回 (是否触发 Kill Switch, 原因)"""

    def add(self, entry: RevocationEntry) -> None
    def remove(self, type: RevocationType, id: str) -> None
    def set_kill_switch(self, config: KillSwitchConfig) -> None
```

### 5.3 匹配规则

| 吊销类型 | 匹配逻辑 | 说明 |
|---|---|---|
| `agent` | `entry.id == identity.agent_id` | 吊销整个 agent |
| `user` | `entry.id == identity.user_id` | 吊销某个用户 |
| `tool` | `entry.id == tool_name` | 吊销某个工具 |
| `secret` | `entry.id in secret_refs` | 吊销某个 secret |

### 5.4 检查位置

```text
身份认证
  ↓
RevocationList.is_revoked()    ← 新
  ↓
风险分类 / 策略判定
  ↓
require_approval
  ↓
resume_after_approval
  ↓
RevocationList.is_revoked()    ← 新：审批等待期间可能被吊销
  ↓
execute_with_proposal
  ↓
RevocationList.is_revoked()    ← 新：执行前最终检查
  ↓
执行器
```

### 5.5 Admin API

HTTP：

```http
POST /admin/revoke
Content-Type: application/json

{
  "type": "agent",
  "id": "agent_001",
  "reason": "suspected compromise"
}

# 返回 200
```

```http
DELETE /admin/revoke?type=agent&id=agent_001
```

```http
GET /admin/revocation-list
```

```http
POST /admin/kill-switch
Content-Type: application/json

{
  "enabled": true,
  "reason": "emergency maintenance",
  "except_tools": ["health_check"],
  "except_agents": ["admin-agent"]
}
```

gRPC：

```protobuf
rpc Revoke(RevokeRequest) returns (RevokeResponse);
rpc SetKillSwitch(SetKillSwitchRequest) returns (KillSwitchResponse);
rpc GetRevocationList(GetRevocationListRequest) returns (RevocationListResponse);
```

### 5.6 持久化与热更新

- `config/revocation.yaml` 是主存储；
- admin API 修改后**立即写回** `config/revocation.yaml`；
- `HotReloader` 监控 `revocation.yaml` 文件变化，从磁盘重新加载；
- 启动时从 `config/revocation.yaml` 加载初始状态。

---

## 6. 证据链模块设计

### 6.1 数据模型

```python
class SignedEvidence(BaseModel):
    seq: int
    timestamp: str
    tenant_id: str | None
    event: AuditEvent
    prev_hash: str
    current_hash: str
    algorithm: str       # "hmac-sha256" | "ed25519"
    key_id: str
    signature: str
```

### 6.2 EvidenceSigner 协议

```python
class EvidenceSigner(Protocol):
    @property
    def algorithm(self) -> str: ...
    @property
    def key_id(self) -> str: ...
    def sign(self, data: bytes) -> bytes: ...
    def verify(self, data: bytes, signature: bytes) -> bool: ...
```

本版本实现：

- `HMACEvidenceSigner`：HMAC-SHA256，用于测试/开发；
- `Ed25519EvidenceSigner`：从环境变量 `LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY` 读取 base64 私钥，用于本地生产场景。

### 6.3 哈希与签名内容

```python
current_hash = sha256(
    canonical_json({
        "seq": seq,
        "timestamp": timestamp,
        "tenant_id": tenant_id,
        "event": event,
        "prev_hash": prev_hash,
        "algorithm": algorithm,
        "key_id": key_id,
    })
)

signature = signer.sign(current_hash)
```

**注意**：`timestamp` 必须纳入 `current_hash`，防止修改时间戳。

### 6.4 EvidenceBackend 协议

```python
class EvidenceBackend(Protocol):
    async def append(self, tenant_id: str | None, signed_evidence: SignedEvidence) -> None: ...
    async def last_hash(self, tenant_id: str | None) -> str | None: ...
    async def iter_evidence(self, tenant_id: str | None) -> AsyncIterator[SignedEvidence]: ...
```

本版本只实现 `LocalFileEvidenceBackend`，按租户分目录：

```text
evidence/
  ├── default.jsonl
  └── {tenant_id}.jsonl
```

### 6.5 EvidenceChain 启动恢复

```python
class EvidenceChain:
    def __init__(self, backend: EvidenceBackend, signer: EvidenceSigner):
        self._seq = 0
        self._prev_hash = ""
        # 启动时从 backend 读取最后一条记录，恢复 seq 和 prev_hash
        last = await backend.last_hash(None)
        if last:
            self._seq = last.seq
            self._prev_hash = last.current_hash
```

### 6.6 审计接入

修改 `JsonlAuditStore.append()`：

```python
async def append(self, event: AuditEvent) -> None:
    # 1. 写入原来的 JSONL（向后兼容）
    await self._write_jsonl(event)
    # 2. 写入证据链
    await self._evidence_chain.append(event)
```

### 6.7 验证

- 启动时调用 `EvidenceChain.verify()`，逐条验证链完整性；
- 发现断链时记录 error 并发出 alert，但不阻塞启动（避免单点故障）。

---

## 7. 配置示例

### 7.1 `config/revocation.yaml`

```yaml
kill_switch:
  enabled: false
  reason: ""
  except_tools: ["health_check"]
  except_agents: ["admin-agent"]

revocations: []
```

### 7.2 `config/evidence.yaml`

```yaml
evidence:
  backend: local
  local:
    path: evidence/
    max_file_size_mb: 10
  signing:
    algorithm: ed25519  # hmac-sha256 | ed25519
    # ed25519 从 LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY 读取
```

---

## 8. 关键实现细节

### 8.1 吊销检查必须覆盖所有入口

| 入口 | 检查位置 |
|---|---|
| HTTP `/govern_tool_call` | `server.py` 调用 controller 前 |
| gRPC `EvaluateToolCall` | `grpc_server.py` 调用 controller 前 |
| MCP Proxy `tools/call` | `proxy_server.py` 调用 controller 前 |
| SDK `ToolGovernor.call` | `tool_governor.py` 调用 controller 前 |
| 审批恢复后 | `controller.resume_after_approval` |
| 执行前 | `controller.execute_with_proposal` |

### 8.2 证据链不能阻塞审计写入

- `EvidenceChain.append()` 如果失败，仍允许原 `JsonlAuditStore.append()` 成功，但记录 error alert；
- 不能因为签名失败就丢失审计记录。

### 8.3 吊销列表的并发安全

- `RevocationList` 内部使用 `asyncio.Lock` 保护 `_revocations` 字典；
- `is_revoked` 调用前获取锁，复制一份快照后遍历，避免遍历期间被修改。

### 8.4 Admin API 安全

- admin 端点使用现有 `x-api-key` / `Authorization: Bearer` 认证；
- 所有 revoke / kill-switch 操作写入审计日志；
- API key 比较使用 `hmac.compare_digest` 防侧信道。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 吊销列表被恶意清空 | admin API 写回文件，文件系统权限保护；操作本身写审计 |
| 证据链私钥泄露 | 生产环境推荐 Ed25519；v0.27 接入 KMS/HSM |
| 证据文件被删除 | 本地文件需独立备份；v0.27 实现远程不可变存储 |
| 启动验证失败阻塞服务 | verify 失败记 error，不阻塞启动 |
| 并发写证据链 | 单进程 asyncio lock；多 worker 场景需外部协调（v0.27+） |

---

## 10. 验收标准

- `pytest tests/test_revocation.py`：
  - agent/user/tool/secret 吊销测试通过；
  - Kill Switch 全阻断/例外测试通过；
  - 审批等待期间吊销测试通过；
  - admin API 增删改查测试通过；
  - gRPC admin 方法测试通过；
  - 吊销持久化 + 热更新测试通过。

- `pytest tests/test_evidence_chain.py`：
  - HMAC 签名链验证通过；
  - Ed25519 签名链验证通过；
  - 修改任一字段导致验证失败；
  - 重启后恢复链尾并继续追加；
  - timestamp 修改导致验证失败；
  - 审计写入时同步生成证据链。

- `pytest tests/`：整体无回归，至少保持 v0.25.0 的通过数量。
- `ruff check src tests examples`：通过。
- `mypy src`：通过。
- `KNOWN_LIMITATIONS.md` 和 `README.md` 更新。

---

## 11. 与 v0.27.0 的衔接

v0.26.0 完成后，v0.27.0 再补：

1. **KMS/HSM 集成**：把 `EvidenceSigner` 接入 Vault / AWS KMS；
2. **远程证据存储**：S3/GCS 后端 + WORM / 对象锁定；
3. **多租户隔离**：在 v0.26 的 `tenant_id` 字段基础上，实现真正的资源隔离。

---

## 12. 最终目标

v0.26.0 完成后，Loop Controller 具备：

```text
┌─────────────────────────────────────┐
│  治理层：身份 → 吊销 → 策略 → 审批  │
│  审计层：签名证据链（本地）           │
└─────────────────────────────────────┘
```

为 v0.27 的 KMS、远程证据存储、多租户隔离打下基础。
