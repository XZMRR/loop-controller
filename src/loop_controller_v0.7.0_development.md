# Loop Controller v0.7.0 开发指南：MCP Proxy `approval_status` 查询工具

> **状态**：已完成（242 tests passed，ruff 干净，mypy 仅余 2 个预存在 PyYAML stub 错误）。
> **完成提交**：`feat(v0.7.0): MCP Proxy approval_status tool`。
>
> 本文件保留为设计记录，具体实现细节以 `src/development_log.md` v0.7.0 章节为准。
>
> **目标**：让外部 Agent 能主动查询某个 `decision_id` 的审批状态，降低对 Agent LLM 解析结构化响应的依赖。

---

## 1. 背景与目标

v0.5.1 开始，MCP Proxy 对 `require_approval` 返回结构化 JSON，包含 `decision_id` 和 `request_id`；v0.6.0 让审批后的重试能跨 Runtime 恢复；v0.6.1 优化了预算预留状态机。

但外部 Agent 在收到 `require_approval` 后，仍然面临一个问题：
> “我怎么知道人类审批人是否已经批准了？”

当前唯一方式是：
1. Agent 记住 `decision_id`；
2. 人去 CLI 审批后，Agent 重试同一个 tool call；
3. Proxy 返回成功或继续 BLOCKED。

这个流程对 Agent 不够友好：每次重试都可能触发一次新的 `require_approval` 判定（如果审批未完成），消耗资源且日志混乱。

v0.7.0 的目标：给 MCP Proxy 增加一个专用查询工具 `loop_controller_approval_status`，让 Agent 能主动查询审批状态，只在确定已批准后再重试。

---

## 2. 范围与边界

### 2.1 纳入 v0.7.0

| # | 功能 | 优先级 |
|---|---|---|
| 1 | `loop_controller_approval_status` MCP 工具注册 | P0 |
| 2 | SSE 模式下通过 `x-loop-controller-decision-id` 查询（保持与重试一致） | P0 |
| 3 | stdio 模式下通过参数 `_loop_controller_decision_id` 查询 | P0 |
| 4 | 返回状态：`pending` / `approved` / `denied` / `expired` / `not_found` | P0 |
| 5 | 不暴露审批人、审批意见等敏感信息 | P0 |
| 6 | 测试覆盖 | P0 |

### 2.2 明确不纳入

- 不实现 MCP 的 `sampling` 协议；
- 不实现 Server 主动向 Client 推送审批结果；
- 不修改 `require_approval` 的返回结构；
- 不修改现有重试路径。

---

## 3. 设计

### 3.1 工具 schema

```json
{
  "name": "loop_controller_approval_status",
  "description": "查询 Loop Controller 审批状态。返回 pending / approved / denied / expired / not_found。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "decision_id": {
        "type": "string",
        "description": "require_approval 响应中的 decision_id"
      }
    },
    "required": ["decision_id"]
  }
}
```

### 3.2 响应格式

```json
{
  "status": "pending",
  "decision_id": "dec_xxx",
  "can_retry": false
}
```

状态定义：

- `pending`：尚未审批，继续等待；
- `approved`：已批准，可以重试；
- `denied`：已被拒绝，不需要再重试；
- `expired`：Decision 已过期且未被审批，不需要再重试；
- `not_found`：decision_id 不存在；

`can_retry` 字段方便 LLM 快速判断是否需要重试。

### 3.3 ProxyServer 改动

在 `LoopControllerProxyServer` 中：

1. `list_tools()` 除了返回真实工具外，额外注入 `loop_controller_approval_status`；
2. `call_tool()` 检测到 `loop_controller_approval_status` 时走专用分支；
3. 分支内调用 `runtime.approval_manager.check(decision_id)` 和 `runtime.approval_manager.get_decision(decision_id)` 判断状态；
4. 返回标准化 JSON。

### 3.4 身份校验

`approval_status` 工具不需要 agent 身份与原始 task 一致，因为审批状态本身不是敏感操作，且 decision_id 是随机 UUID，不容易被猜测。但仍需校验 agent 存在。

为了安全，可以要求 Agent 提供与原始请求相同的 `agent_id`，但实际实现中 decision_id 已经 acts as a capability。MVP 阶段不做额外绑定。

### 3.5 与 v0.5.1 重试路径的关系

`approval_status` 是**可选**工具。Agent 可以直接重试，也可以通过查询工具确认后再重试。

查询到 `approved` 后，Agent 应使用原参数 + `x-loop-controller-decision-id`（SSE）或 `_loop_controller_decision_id`（stdio）重试。

---

## 4. 接口变更

### 4.1 新增

- `loop_controller_approval_status` MCP tool

### 4.2 修改

- `src/loop_controller/proxy_server.py`：
  - `list_tools()` 注入内部工具；
  - `call_tool()` / `_handle_call_tool()` 增加对内部工具的分支。

### 4.3 不修改

- `Checkpoint` / `AsyncApprovalManager` 接口；
- `require_approval` 返回格式；
- CLI 行为。

---

## 5. 实现顺序

1. 在 `proxy_server.py` 定义 `loop_controller_approval_status` schema；
2. 实现 `_handle_approval_status()`；
3. 在 `list_tools()` 中注入该工具；
4. 在 `_handle_call_tool()` 中优先路由内部工具名；
5. 测试：pending / approved / denied / expired / not_found；
6. 更新 `development_log.md`。

---

## 6. 测试策略

### 6.1 ProxyServer 单元测试

- `test_approval_status_pending`：未审批返回 pending；
- `test_approval_status_approved`：审批后返回 approved；
- `test_approval_status_denied`：审批拒绝后返回 denied；
- `test_approval_status_not_found`：不存在的 decision_id 返回 not_found；
- `test_approval_status_expired`：Decision 过期且未审批返回 expired。

### 6.2 集成测试

- 在 `test_proxy_server.py` 中：触发 require_approval，查询 pending，审批后查询 approved，重试成功。

---

## 7. 验收标准

- `pytest tests/` 全部通过（至少 238 个）；
- `ruff check src tests` 干净；
- `mypy src` 无新增错误；
- `loop_controller_approval_status` 工具返回结构化 JSON；
- 审批后 `can_retry=true`，拒绝/过期/不存在 `can_retry=false`。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| Agent 误用该工具频繁查询 | 查询只读，无副作用；必要时可限流 |
| decision_id 泄露导致任意 Agent 可查 | decision_id 是随机 UUID，MVP 可接受；未来可绑定 agent_id |
| 工具名与真实工具冲突 | 使用 `loop_controller_` 前缀，避免冲突 |
