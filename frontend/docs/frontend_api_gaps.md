# Loop Controller 前端所需后端接口缺口报告

> 版本：`v0.54.0`
> 分支：`develop`（v0.54 后端 + 前端全部工作；原 frontend/r15-port 已改名合并至此）
> 用途：为公司展示用前端控制台提供 API 支撑
> 作者：前端开发规划
>
> 相对 v0.48.0 报告的整体变化：
> - **认证模式变更**：`X-API-Key` 直连已移除，改为 `POST /v1/admin/session/login` 换取
>   Session Token（`Authorization: Bearer <session>`）；审批通过/拒绝要求**独立审批凭证**
>   （用后即焚，不落入 admin session）。
> - **v0.52~v0.54 新增能力已上线**：多租户 RBAC、policy candidates 全生命周期、
>   workload/Secret 引用/ExecutionReceipt、可靠调度（assignment/outbox/lease/fence、
>   retry/failover/dead-letter、有界 DAG、可恢复 SSE）、SQLite migration、Prometheus metrics。
> - 本文档 §3 缺口清单大部分已被 v0.54 后端兑现，剩余缺口见 §7.2。

---

## 1. 报告目的

本文档列出当前 Python Runtime 与 Go A2A Kernel 已暴露的 HTTP API，以及前端控制台（Dashboard / 审批台 / Agent 管理 / 工具策略 / 审计查询 / 系统配置 / A2A 治理）所需的接口缺口。对每一个缺口，给出：

- 前端使用场景
- 建议的 REST 路径、方法、请求/响应结构
- 后端实现位置建议
- 实现复杂度评估
- 优先级建议

后端配合度高，对于**不麻烦的缺口**可优先补齐；复杂接口可与前端分阶段迭代。

---

## 2. 当前已有接口速览

### 2.1 Python Runtime（默认 `http://127.0.0.1:8000`）

> v0.54 认证说明：除登录端点外，所有 `/v1/admin/*` 请求需带
> `Authorization: Bearer <session_token>`；审批通过/拒绝另需独立审批凭证
> （详见 `_handle_admin_decision_approve/deny` 的 approver 凭证解析与 RBAC 租户绑定校验）。

| 方法 | 路径 | 用途 | 认证 |
|---|---|---|---|
| GET | `/v1/health` | 健康检查 | 无 |
| GET | `/v1/identity` | 当前身份/Provider 信息 | 无 |
| GET | `/metrics` | Prometheus 指标 | 无 |
| POST | `/v1/govern/tool-call` | 工具调用治理 | 按 entrypoint 配置 |
| POST | `/v1/govern/resume-after-approval` | 审批通过后恢复执行 | 按 entrypoint 配置 |
| GET | `/v1/wait-for-approval` | 轮询等待审批结果 | 按 entrypoint 配置 |
| GET | `/v1/wait-for-approval/sse` | SSE 实时等待审批结果 | 按 entrypoint 配置 |
| POST | `/v1/admin/session/login` | 换取 Session Token | 无（API Key 仅用于换 Session） |
| POST | `/v1/admin/session/logout` | 注销 Session | Session |
| GET | `/v1/admin/approvals/pending` | 待审批列表 | Session |
| GET | `/v1/admin/approvals` | 审批历史（支持状态/时间过滤） | Session |
| POST | `/v1/admin/approvals/{id}/approve` | 通过审批 | Session + **独立审批凭证** |
| POST | `/v1/admin/approvals/{id}/deny` | 拒绝审批 | Session + **独立审批凭证** |
| GET | `/v1/admin/harness/backends` | Harness 后端状态 | Session |
| POST | `/v1/admin/harness/{name}/drain` | 排空 Harness 后端 | Session |
| POST | `/v1/admin/harness/{name}/reset` | Reset Harness 后端 | Session |
| GET/POST | `/v1/admin/evidence/anchor/*` | Evidence 锚点管理（anchor/verify/publish/bootstrap） | Session |
| GET | `/v1/admin/policy/candidates` | Policy 候选列表 / 创建 | Session |
| GET | `/v1/admin/policy/candidates/{id}` | Policy 候选详情 | Session |
| POST | `/v1/admin/policy/candidates/{id}/validate` | 候选校验 | Session |
| POST | `/v1/admin/policy/candidates/{id}/shadow` | 候选影子运行 | Session |
| POST | `/v1/admin/policy/candidates/{id}/publish` | 候选发布 | Session |
| POST | `/v1/admin/policy/rollback` | Policy 回滚 | Session |
| GET | `/v1/admin/policy/status` | Policy 状态 | Session |
| GET | `/v1/admin/policy/audit` | Policy 审计 | Session |
| GET/POST | `/v1/admin/rbac/bindings` | RBAC 绑定查询/创建（v0.52） | Session |
| POST | `/v1/admin/rbac/bindings/{id}/revoke` | 撤销绑定 | Session |
| GET/POST | `/v1/admin/rbac/grants` | RBAC 授权查询/创建 | Session |
| POST | `/v1/admin/rbac/grants/{id}/revoke` | 撤销授权 | Session |
| GET/HEAD | `/v1/opa/bundles/current` `/v1/opa/bundles/{rev}` | OPA Bundle 分发（v0.50） | 无 |
| POST | `/v1/opa/status` | OPA 状态上报 | 无 |
| GET | `/v1/admin/audit` | 审计事件查询（含时间过滤） | Session |
| GET | `/v1/admin/agents` | Agent 列表 | Session |
| GET | `/v1/admin/agents/{agent_id}` | Agent 详情 | Session |
| GET | `/v1/admin/profiles` | Profile 列表 | Session |
| POST | `/v1/admin/profiles/reload` | Profile 热重载 | Session |
| GET/PUT | `/v1/admin/profiles/{id}/tools` | Profile 工具策略读取/在线编辑（`de89059`） | Session |
| GET | `/v1/admin/identity` | Identity 配置（脱敏） | Session |
| GET | `/v1/admin/entrypoints` | Entrypoints 配置 | Session |
| GET | `/v1/admin/a2a/status` | A2A 治理总览 | Session |
| GET | `/v1/admin/a2a/agents` | A2A 已注册 Agent | Session |
| GET | `/v1/admin/a2a/tasks/{task_id}` | 单任务查询（经 Go 内核） | Session |
| POST | `/v1/admin/a2a/tasks/{task_id}/cancel` | 管理端取消任务 | Session |
| GET | `/v1/admin/a2a/tasks/{task_id}/stream` | 任务状态 SSE 转发 | Session |
| POST | `/v1/admin/a2a/delegations` | 发起委托 | Session |
| POST | `/v1/admin/govern/evaluate` | Govern 只读评估（dry-run） | Session |
| POST/DELETE | `/admin/revoke` | 吊销/移除吊销条目（旧版路径，未挂 `/v1`） | Session（经 `_check_api_key` 兼容） |
| GET | `/admin/revocation-list` | 吊销列表 + Kill Switch 状态（旧版路径） | Session（同上） |
| POST | `/admin/kill-switch` | Kill Switch（旧版路径） | Session（同上） |

实现文件：`src/loop_controller/server.py`（路由注册于 `build_app()`，约 2934–3081 行）。

### 2.2 Go A2A Kernel（默认 `http://127.0.0.1:8080`）

> v0.54 调度体系：Durable assignment/outbox、lease/attempt/fence、预算感知
> retry/failover/dead-letter、有界静态 DAG（task-graphs，特性开关控制）、
> 可恢复 SSE（cursor/Last-Event-ID）。dispatch 语义为 at-least-once。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/a2a/v1/agents` | 注册 Agent Card |
| GET | `/a2a/v1/agents` | 列出 Agent Cards |
| GET | `/a2a/v1/agents/{id}` | 查询 Agent Card |
| POST | `/a2a/v1/tasks` | 创建任务 |
| GET | `/a2a/v1/tasks/{id}` | 查询任务 |
| GET | `/a2a/v1/tasks/{id}/snapshot` | 任务快照（v0.54 新增） |
| GET | `/a2a/v1/tasks/{id}/stream` | SSE 任务事件流（可恢复） |
| POST | `/a2a/v1/messages` | 发送消息 |
| POST | `/a2a/v1/delegations` | 请求委托授权 |
| GET | `/a2a/v1/delegation-approvals/{id}` | 查询委托审批 |
| POST | `/a2a/v1/delegation-approvals/{id}/approve` | 委托审批通过 |
| POST | `/a2a/v1/delegation-approvals/{id}/reject` | 委托审批拒绝 |
| POST | `/a2a/v1/delegation-approvals/{id}/cancel` | 委托审批取消 |
| POST | `/a2a/v1/tasks/{id}/cancel` | 取消任务 |
| GET | `/a2a/v1/dead-letters` | Dead-letter 列表（v0.54 新增） |
| POST | `/a2a/v1/dead-letters/{id}/replay` | Dead-letter 重放（v0.54 新增） |
| POST | `/a2a/v1/task-graphs` | 创建有界 DAG（v0.54，特性开关） |
| GET | `/a2a/v1/task-graphs/{id}` | 查询 DAG |
| POST | `/a2a/v1/task-graphs/{id}/cancel` | 取消 DAG |
| POST/GET | `/a2a/v1/entrypoint/tasks/*` | Entrypoint 任务操作（create/accept/start/cancel/get/results） |
| GET | `/health` | 健康检查 |
| GET | `/ready` | Readiness（v0.54 新增） |
| GET | `/metrics` | Prometheus 指标（v0.54 新增） |

实现文件：`go/internal/api/handlers.go`（`RegisterRoutes()`，约 372–401 行）。

---

## 3. 接口缺口详单

> **v0.54 状态总览**：
> - ✅ 已兑现：1.1 Agent 列表、1.2 Agent 详情、2.1 Profile 列表、2.2 Profile 工具策略更新
>   （`GET/PUT /v1/admin/profiles/{id}/tools` + `POST /v1/admin/profiles/reload` 热重载）、
>   3.1 Identity 只读、4.1 Entrypoints 只读、7.1 审批历史、8.1 Govern 只读评估。
> - ⏳ 部分兑现：3.2 Identity 更新（需确认热重载范围）、3.6 配置热重载
>   （profiles 已支持，agents/identity/entrypoints 待确认）。
> - ❌ 仍未兑现：1.3 Agent CRUD、5.1/5.2 Secret 枚举与 CRUD、7.2 审批转交，
>   以及新增缺口 **A2A 任务树列表端点**（见 §7.2）。
> - 下文中标注「已实现」的缺口保留原始建议，仅供溯源。

### 3.1 Agent 管理（高优先级）

#### 缺口 1.1：Agent 列表查询

- **前端场景**：Agent 管理页展示已注册 Agent（ID、名称、Profile、Owner、状态）。
- **当前替代方案**：前端读取 `config/agents.yaml` 静态文件，无法反映运行态（在线/离线/吊销）。
- **建议接口**：

```http
GET /v1/admin/agents
X-API-Key: <key>
```

响应：

```json
{
  "agents": [
    {
      "agent_id": "researcher_001",
      "name": "Research Assistant",
      "profile_id": "research_assistant_v1",
      "owner_id": "zhang_manager",
      "status": "active",
      "revoked": false,
      "last_seen_at": null
    }
  ]
}
```

- **后端实现建议**：
  - 读取 `controller._runtime.checkpoint._identity._agents`（内存）或 `agents.yaml`。
  - 结合 `RevocationList` 判断 `revoked` 状态。
  - 实现位置：`src/loop_controller/server.py` 新增 `_handle_admin_agents`。
- **复杂度**：低。

#### 缺口 1.2：Agent 详情

```http
GET /v1/admin/agents/{agent_id}
```

返回 Agent 完整信息 + 当前 profile + 最近审计摘要。

- **复杂度**：低。

#### 缺口 1.3：Agent 增删改（可选，P1）

```http
POST   /v1/admin/agents
PUT    /v1/admin/agents/{agent_id}
DELETE /v1/admin/agents/{agent_id}
```

- **实现建议**：修改 `config/agents.yaml` 并调用热重载；或写入 `IdentityProvider` 的内存注册表。
- **复杂度**：中。需要持久化与备份策略。

---

### 3.2 工具策略 / Profile 管理（高优先级）

#### 缺口 2.1：Profile 列表查询

- **前端场景**：工具策略页展示每个 Profile 的工具权限表。
- **当前替代方案**：前端读取 `config/profiles.yaml`。
- **建议接口**：

```http
GET /v1/admin/profiles
X-API-Key: <key>
```

响应：

```json
{
  "profiles": [
    {
      "profile_id": "research_assistant_v1",
      "description": "研究助手岗位说明书",
      "max_budget_token": 100000,
      "tools": {
        "send_email": {
          "allowed": true,
          "require_approval": true,
          "max_calls_per_task": 1
        }
      }
    }
  ]
}
```

- **后端实现建议**：读取 `controller._runtime.checkpoint._profile` 或 `config/profiles.yaml`。
- **复杂度**：低。

#### 缺口 2.2：Profile 工具策略更新

```http
PUT /v1/admin/profiles/{profile_id}/tools/{tool_name}
Content-Type: application/json

{
  "allowed": true,
  "require_approval": true,
  "allowed_args": {"to": ["*@company.com"]},
  "max_calls_per_task": 1
}
```

- **实现建议**：
  - 修改 `config/profiles.yaml`。
  - 调用 `ConfigLoader` 重新加载 profile；或修改内存 `CapabilityProfile`。
  - **必须同步 OPA**：修改后应重新 `load_policy_into_opa()`，否则 R2 判定仍用旧策略。
- **复杂度**：中。涉及配置持久化与策略热重载。

---

### 3.3 Identity Provider 配置（中优先级）

#### 缺口 3.1：读取 Identity 配置

```http
GET /v1/admin/identity
```

返回 `config/identity.yaml` 内容（**注意脱敏**：static token 只返回 mask，不返回完整 token）。

#### 缺口 3.2：更新 Identity 配置

```http
PUT /v1/admin/identity
```

- **实现建议**：写入 `config/identity.yaml` 并重启 Runtime；或实现 `IdentityProvider` 热重载。
- **复杂度**：中。JWT/JWKS 变更涉及证书缓存刷新。

---

### 3.4 Entrypoints 配置（中优先级）

#### 缺口 4.1：读取 Entrypoints

```http
GET /v1/admin/entrypoints
```

返回 `config/entrypoints.yaml`。

#### 缺口 4.2：更新 Entrypoints

```http
PUT /v1/admin/entrypoints
```

- **实现建议**：写入 `config/entrypoints.yaml` 并重启；或动态更新 `entrypoints` 内存配置。
- **复杂度**：中。HTTP server 认证中间件在启动时已绑定，动态切换 `require_auth` 需重建 middleware 或路由守卫。

---

### 3.5 Secret 管理（中优先级）

#### 缺口 5.1：Secret 引用列表

- **前端场景**：系统配置页展示已配置 Secret 的引用名、作用域、后端类型，**不展示明文值**。
- **建议接口**：

```http
GET /v1/admin/secrets
X-API-Key: <key>
```

响应：

```json
{
  "secrets": [
    {
      "ref": "email_api_key",
      "tenant_id": null,
      "backend": "file",
      "has_value": true
    }
  ]
}
```

- **实现建议**：遍历 `SecretBroker` 已注册 backend，列出所有 `SecretRef`（需要 broker 支持枚举，当前 `broker.py` 未暴露枚举接口，需新增 `list_refs()`）。
- **复杂度**：低~中。

#### 缺口 5.2：Secret 创建/更新/删除

```http
POST   /v1/admin/secrets
PUT    /v1/admin/secrets/{ref}
DELETE /v1/admin/secrets/{ref}
```

- **实现建议**：
  - 写入 `secrets/` 目录下对应文件（file backend）或加密文件 backend。
  - 禁止在请求日志中记录明文值。
- **复杂度**：中。需要加密/文件权限管理。

---

### 3.6 配置热重载（高优先级）

- **前端场景**：修改 Agent/Profile/Identity/Entrypoints 后，点击"保存并生效"。
- **当前现状**：README 与 `KNOWN_LIMITATIONS.md` 明确说明，大部分配置修改后需重启服务。
- **建议接口**：

```http
POST /v1/admin/reload
Content-Type: application/json

{
  "target": "profiles"
}
```

或批量：

```http
POST /v1/admin/reload
{
  "targets": ["profiles", "identity", "agents"]
}
```

- **实现建议**：
  - 调用 `ConfigLoader.load(config_dir)` 重新加载对应配置。
  - 调用 `Runtime` 提供的 `_reload_*` 方法（若不存在需新增）。
  - 对 OPA policy，重新调用 `policy_engine.load_policy_into_opa()`。
  - 对 identity provider，重建 provider 实例。
  - 返回哪些配置已成功重载、哪些需要重启。
- **复杂度**：中~高。需要确保热重载期间不破坏正在进行的 task/approval。
- **替代方案**：前端先只做"保存到文件 + 提示用户重启"，后端后续再实现真正热重载。

---

### 3.7 审批历史（中优先级）

#### 缺口 7.1：已完成审批列表

- **前端场景**：审批台需要"已审批"、"已拒绝"历史，便于追溯。
- **当前现状**：只有 `/v1/admin/approvals/pending`。
- **建议接口**：

```http
GET /v1/admin/approvals?status=approved&limit=50
GET /v1/admin/approvals?status=denied&limit=50
GET /v1/admin/approvals?status=all&limit=50
```

响应：

```json
{
  "approvals": [
    {
      "request_id": "...",
      "decision_id": "...",
      "tool_name": "send_email",
      "requester_id": "alice",
      "approver_id": "zhang_manager",
      "verdict": "approved",
      "comment": "同意发送",
      "created_at": "...",
      "resolved_at": "..."
    }
  ]
}
```

- **实现建议**：
  - 当前 `ApprovalStore` 只保留 pending request 和已有 record。
  - 扩展 `ApprovalStore` 支持按状态、时间范围查询。
  - 或从审计日志 `/v1/admin/audit` 反查（前端可先这样实现，但缺少结构化字段）。
- **复杂度**：低~中。

#### 缺口 7.2：审批转交（可选，P2）

- **前端场景**：审批人繁忙时，将某条审批转交给他人。
- **当前现状**：`escalation_target` 在 Decision 创建时固定，审批人必须是该 target。
- **建议接口**：

```http
POST /v1/admin/approvals/{decision_id}/reassign
Content-Type: application/json

{
  "new_approver": "li_manager",
  "reason": "张经理出差，转交李经理"
}
```

- **实现建议**：
  - 修改 `ApprovalRequest.approver_id` 和 `Decision.escalation_target`。
  - 校验新 approver 存在且不等于 requester/agent。
  - 记录转交事件到审计日志。
- **复杂度**：中。

---

### 3.8 工具执行调试（中优先级）

#### 缺口 8.1：Govern 结果查询

- **前端场景**：技术用户在页面上模拟一次工具调用，查看 R2 判定结果（allow/deny/modify/require_approval）。
- **当前已有**：`POST /v1/govern/tool-call` 可直接使用。
- **注意**：该接口是真实执行入口，模拟调试时若结果为 `allow` 会真实调用工具。建议新增只读调试接口：

```http
POST /v1/admin/govern/evaluate
Content-Type: application/json

{
  "agent_id": "researcher_001",
  "user_id": "alice",
  "tool_name": "send_email",
  "arguments": {"to": "zhang@company.com"}
}
```

响应只返回 Decision，不执行。

- **实现建议**：调用 `Checkpoint.evaluate()` 但不调用 `Checkpoint.forward()`。
- **复杂度**：低。

---

### 3.9 A2A / Go Kernel 相关（P2）

Go Kernel 已提供 `/a2a/v1/agents`、`/a2a/v1/tasks`、`/a2a/v1/delegations` 等接口，前端可直接使用。需要补齐的是：

#### 缺口 9.1：Python 到 Go Kernel 的 control token 桥接

- **当前现状**：`GoKernelBridge` 调用 Go Kernel 时没有携带 control token（`src/loop_controller/go_kernel_bridge.py` 约 343–381 行）。
- **影响**：生产模式（`development: false`）下，Go Kernel 强制要求 control token，导致 Python 委托链无法工作。
- **建议修改**：
  - 在 `go_kernel.yaml` 中配置 `control_token`。
  - `GoKernelBridge` 发起请求时携带 `Authorization: Bearer <control_token>`。
  - 若启用 control auth，还需配置 `initiator_agent_id`。
- **复杂度**：低。

---

## 4. 可自行在现有系统中增改的缺口（不麻烦）

以下缺口不需要新增大型子系统，可在现有代码上小改实现：

| 缺口 | 修改位置 | 改动量 | 说明 |
|---|---|---|---|
| Agent 列表查询 | `server.py` | 小 | 读取现有 `IdentityProvider._agents` |
| Profile 列表查询 | `server.py` | 小 | 读取 `Runtime` 中已加载的 profile |
| Identity/Entrypoints 只读 | `server.py` + `ConfigLoader` | 小 | 返回当前已加载配置（注意脱敏） |
| 审批历史 | `approval_store.py` / `approval_manager.py` | 中 | 扩展查询方法 |
| Govern 调试接口 | `server.py` + `checkpoint.py` | 小 | 调用 `evaluate()` 不 `forward()` |
| Go Kernel control token | `go_kernel_bridge.py` + `go_kernel.yaml` | 小 | 增加 header |

---

## 5. 需要较大工作的缺口（后端评估后分阶段）

| 缺口 | 工作量 | 风险 |
|---|---|---|
| 配置热重载 | 中~高 | 需保证运行中 task/approval 不受影响 |
| Profile 策略更新并同步 OPA | 中 | 策略热重载一致性 |
| Secret CRUD | 中 | 加密、权限、审计 |
| 审批转交 | 中 | 状态机变更 |
| Agent CRUD 持久化 | 中 | 配置备份与并发写入 |

---

## 6. 前端当前实现状态（v0.54，develop 分支）

v0.54 前端已全面改为 API 直连，**YAML fallback 与 vite `/config/*` 中间件已移除**（Tools 页读取仍保留 YAML→在线 API 的单一回退）：

1. **认证**：`Login.vue` 通过 `POST /v1/admin/session/login` 换取 Session；
   `client.ts` 请求拦截器统一携带 `Authorization: Bearer <session>`，401 自动登出跳登录；
   store 启动时清除历史遗留的 `lc_api_key`/`lc_session_token`，token 不落 localStorage。
2. **Agent / Profile**：走 `/v1/admin/agents(+详情)`、`/v1/admin/profiles`、`profiles/{id}/tools`
   在线编辑 + `profiles/reload` 热重载。
3. **审批台**：待审批 + 审批历史（状态/时间过滤）均已接入；通过/拒绝使用**独立审批凭证**
   （credential 用后即焚，不随 admin session），`approver` 字段已从请求体移除；
   「内核对账」页签已随 v0.54 内核审批端点变更删除。
4. **审计**：`/v1/admin/audit`（含时间过滤）已接入。
5. **SSE 硬化**：cursor/Last-Event-ID、指数退避重连、generation 防串扰。
6. **A2A 任务树**：`TaskTree.vue` 通过 `A2ATaskDataSource` 接口消费数据，当前为
   Mock 实现（`api/a2a/mock.ts`），待后端提供任务树列表端点后切换 HTTP 实现，视图零改动。
7. **治理页面**（Mock 数据源先行）：死信队列、RBAC 绑定、Policy 生命周期三页均按
   `契约层 types.ts + Mock 数据源 + 视图 + vitest 用例` 模式交付，Mock 语义对齐
   后端源码；契约冻结后新增 Http 实现替换单例即可，视图与测试零改动。
8. **SSE 基础设施**：`api/sse.ts createEventStream`（游标续传/退避重连/401 处理）
   统一服务 A2A 任务流与审批推送；管理台审批推送端点未定，前端推送优先 +
   15s 轮询兜底。
9. **三态规范**：全站列表数据区统一 Loading/Empty/Error（共享组件
   `ErrorState.vue` 持久错误块 + 重试）；操作反馈仍用 ElMessage。
10. **测试资产**：vitest 55 用例 + Playwright E2E 15 用例（全部后端依赖
    `page.route` stub，不依赖 Python/Go 进程）。

---

## 7. 接口兑现状态

### 7.1 最小可行接口集（MVP）—— 全部已完成

v0.48.0 报告建议的 5 个接口全部实现，且远超预期：

1. ✅ `GET /v1/admin/agents`
2. ✅ `GET /v1/admin/profiles`
3. ✅ `GET /v1/admin/identity`（脱敏）
4. ✅ `GET /v1/admin/entrypoints`
5. ✅ `POST /v1/admin/govern/evaluate`（只读调试）

额外已兑现（v0.49~v0.54）：审批历史+时间过滤（`8383ea9`）、Profile 工具在线编辑+
热重载（`de89059`）、Agent 详情（`3c3e288`）、A2A 治理端点（status/agents/task/cancel/
stream/delegations，`b20a9b5`~`e1a6677`）、Session 认证（`1948248`）、RBAC/policy/evidence
端点面（v0.50~v0.52）。

### 7.2 剩余缺口（v0.54 待后端评估）

| 缺口 | 建议接口 | 前端场景 | 优先级 |
|---|---|---|---|
| A2A 任务树列表 | `GET /v1/admin/a2a/tasks?root_only=true` 或 `/a2a/v1/tasks?list=roots` | TaskTree 页需要列出多跳委托链路（当前仅有 `tasks/{id}` 单查，Mock 无法上线） | 高 |
| 审批详情字段 | 在 `GET /v1/admin/approvals` 响应中补充 escalation/租户/工具参数等全字段 | 审批详情抽屉完整展示 | 中 |
| Secret 引用枚举 | `GET /v1/admin/secrets`（只列 ref/backend/has_value，不明文） | 系统配置页 Secret 管理 | 中 |
| Secret CRUD | `POST/PUT/DELETE /v1/admin/secrets[/{ref}]` | Secret 管理写入 | 低 |
| Agent CRUD | `POST/PUT/DELETE /v1/admin/agents[/{id}]` | Agent 管理在线增改 | 低 |
| 审批转交 | `POST /v1/admin/approvals/{id}/reassign` | 审批人繁忙时转交 | 低 |
| Identity/Entrypoints 热更新 | `PUT /v1/admin/identity` `PUT /v1/admin/entrypoints`（或统一 reload 目标） | 配置在线生效范围确认 | 低 |
| 管理台审批 SSE 端点 | `GET /v1/admin/approvals/stream` 类（暂定形状） | 审批推送（前端 `streamAdminApprovals` 已就绪，推送优先+轮询兜底；`/v1/wait-for-approval/sse` 为 Agent 侧通道，管理台不可用——v0.54 代码核实） | 中 |
| 审计时间范围参数 | `GET /v1/admin/audit` 扩展 `start_time`/`end_time` | 审计查询服务端时间过滤（当前为前端本地过滤，数据量大时不可扩展） | 中 |
| 用户视图 | `GET /v1/admin/users` | Agents 页合并用户数据源（当前前端用 `users: []` 占位） | 低 |

### 7.3 已关闭的兼容方案（不再适用）

- ~~前端读取 `config/*.yaml` 静态文件~~：YAML fallback 已删除，配置只走 Admin API。
- ~~`X-API-Key` 直连与持久化~~：已移除，API Key 仅用于登录换 Session。
- ~~审批请求体 `approver` 字段~~：已由独立审批凭证替代。
- ~~「内核对账」页签~~：v0.54 内核审批端点已变更，该页签已删除。
- ~~Go Kernel control token 桥接缺口~~：v0.54 已由 entrypoint token 体系覆盖。
