# Loop Controller 前端所需后端接口缺口报告

> 版本：`v0.48.0`  
> 分支：`frontend/main`  
> 用途：为公司展示用前端控制台提供 API 支撑  
> 作者：前端开发规划  

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

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` / `/v1/health` | 健康检查 |
| GET | `/v1/identity` | 当前身份/Provider 信息 |
| GET | `/metrics` | Prometheus 指标 |
| POST | `/v1/govern/tool-call` | 工具调用治理 |
| POST | `/v1/govern/resume-after-approval` | 审批通过后恢复执行 |
| GET | `/v1/wait-for-approval` | 轮询等待审批结果 |
| GET | `/v1/wait-for-approval/sse` | SSE 实时等待审批结果 |
| GET | `/v1/admin/approvals/pending` | 待审批列表 |
| POST | `/v1/admin/approvals/{decision_id}/approve` | 通过审批 |
| POST | `/v1/admin/approvals/{decision_id}/deny` | 拒绝审批 |
| GET | `/v1/admin/harness/backends` | Harness 后端状态 |
| POST | `/v1/admin/harness/{name}/drain` | 排空 Harness 后端 |
| POST | `/v1/admin/harness/{name}/reset` | Reset Harness 后端 |
| GET/POST | `/v1/admin/evidence/anchor/*` | Evidence 锚点管理 |
| POST/DELETE | `/admin/revoke` | 吊销 |
| GET | `/admin/revocation-list` | 吊销列表 |
| POST | `/admin/kill-switch` | Kill Switch |
| GET | `/v1/admin/audit` | 审计事件查询 |

实现文件：`src/loop_controller/server.py`（路由注册于 `build_app()`，约 1198–1282 行）。

### 2.2 Go A2A Kernel（默认 `http://127.0.0.1:8080`）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/a2a/v1/agents` | 注册 Agent Card |
| GET | `/a2a/v1/agents` | 列出 Agent Cards |
| GET | `/a2a/v1/agents/{id}` | 查询 Agent Card |
| POST | `/a2a/v1/tasks` | 创建任务 |
| GET | `/a2a/v1/tasks/{id}` | 查询任务 |
| GET | `/a2a/v1/tasks/{id}/stream` | SSE 任务事件流 |
| POST | `/a2a/v1/messages` | 发送消息 |
| POST | `/a2a/v1/delegations` | 请求委托授权 |
| GET/POST | `/a2a/v1/delegation-approvals/*` | 委托审批 |
| POST | `/a2a/v1/tasks/{id}/cancel` | 取消任务 |
| POST/GET | `/a2a/v1/entrypoint/tasks/*` | Entrypoint 任务操作 |
| GET | `/health` | 健康检查 |

实现文件：`go/internal/api/handlers.go`（`RegisterRoutes()`，约 222–243 行）。

---

## 3. 接口缺口详单

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

## 6. 前端当前已实现的兼容方案

为不阻塞前端开发，第一期采用：

1. **Agent / 工具列表**：直接读取 `config/agents.yaml`、`config/profiles.yaml` 静态文件。
2. **配置展示**：只读展示 `entrypoints.yaml`、`identity.yaml`。
3. **审批操作**：使用现有 `/v1/admin/approvals/{id}/approve|deny`，已支持 `approver` + `comment`。
4. **审计**：使用现有 `/v1/admin/audit`。
5. **吊销/Kill Switch**：使用现有 `/admin/revoke`、`/admin/kill-switch`。

当后端补齐对应接口后，前端只需替换 `api/*.ts` 中的实现，视图层无需大改。

---

## 7. 后端最小可行接口集（MVP）

如果后端希望用最小成本支撑前端第一期上线，建议优先补齐以下 5 个接口：

1. `GET /v1/admin/agents`
2. `GET /v1/admin/profiles`
3. `GET /v1/admin/identity`（脱敏）
4. `GET /v1/admin/entrypoints`
5. `POST /v1/admin/govern/evaluate`（只读调试）

这样前端就可以完全脱离直接读取 YAML 文件，统一走后端 API。

### 实现状态（已完成）

以上 5 个接口已在 `src/loop_controller/server.py` 实现并注册路由，请求/响应模型见
`src/loop_controller/server_models.py`（`AdminAgentItem` / `AdminAgentsResponse` /
`AdminProfilesResponse` / `AdminGovernEvaluateRequest` / `AdminGovernEvaluateResponse`），
测试覆盖见 `tests/test_server.py` 末尾的管理接口测试段。

实现要点：

- **鉴权**：与既有 `/v1/admin/*` 一致，强制 `X-API-Key` / `Authorization: Bearer` 校验。
- **Agent 列表**：数据源为 `runtime.config.agents`，结合 `RevocationList` 计算 `revoked`
  字段（过期吊销条目不计入）。
- **Profile 列表**：数据源为 `runtime.profiles`，`CapabilityProfile` 原样序列化。
- **Identity / Entrypoints**：直接返回内存配置，经 `_mask_sensitive` 递归脱敏——键名命中
  `secret|token|password|private|credential|api_key`（忽略大小写）的字符串值替换为
  `******`（如 `static.allowed_tokens` 中的 token 字段）。
- **Govern 只读调试**：绕过 `LoopController.evaluate`（避免提交审批请求），直接调用
  `Checkpoint.evaluate`。已知可控副作用：防重放记录 call_id、按随机 task_id 预留预算
  （下次启动由 `recover_stale_reservations` 回收）、deny 时更新随机 session 风险状态
  （不累积）。所有合成 ID 带 `dryrun-` 前缀便于审计识别。
- **前端接入**：`frontend/src/api/config.ts` 的 4 个 loader 已改为 API 优先、YAML 回退，
  视图层无需感知数据来源。
