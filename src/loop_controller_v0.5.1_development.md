# Loop Controller v0.5.1 开发指南：MCP Proxy 审批恢复与结构化响应

## 1. 背景与目标

v0.5.0 实现了 MCP Proxy 模式：外部 Agent 通过标准 MCP 协议接入 Loop Controller，每个 `tools/call` 都会经过 R2 Checkpoint 判定。但当前实现遇到 `require_approval` 时直接返回一段文本：

```text
BLOCKED: requires human approval (decision_id=xxx)
```

这段文本对人不友好，对 Agent 更难解析。更严重的问题是：**审批通过后，Agent 再次调用同一个 tool，Proxy 会重新生成一个新的 decision_id，旧审批失效**。

因此 v0.5.1 的核心目标是：

> **让 MCP Proxy 的 `require_approval` 可恢复、可重试，并返回结构化的响应。**

外部 Agent 收到 `require_approval` 后可以自由决定：
- 向用户展示审批请求；
- 尝试替代工具；
- 暂停任务；
- 审批后携带 `decision_id` 重试。

Loop Controller 不替 Agent 管理等待状态，只提供清晰的决策凭证。

## 2. 范围

### 2.1 In Scope

1. `proxy_server.py` 返回结构化的 `require_approval` 响应；
2. 支持通过 `x-loop-controller-decision-id` 头部重试已批准的请求；
3. 在 `ApprovalRequest` 中缓存原始 tool 参数，保证审批与调用参数绑定；
4. `ApprovalManager` / `JsonlApprovalStore` 暴露按 `request_id` 查询的能力；
5. 更新 `src/answer.md`、`src/development_log.md`、`docs/architecture/05_mvp_core_abstractions.md` 中 MCP 集成相关章节；
6. 新增/更新 Proxy 测试，覆盖 allow/deny/require_approval/重试/过期场景。

### 2.2 Out of Scope

- 长轮询或 SSE 推送审批结果；
- 在 Proxy 内部阻塞等待人工审批；
- 跨 Task 权限组合分析；
- `BudgetReservation` 状态机；
- `ActionProposal.intent_tag`（建议放入 v0.5.2）。

## 3. 关键设计决策

### 3.1 Agent 不阻塞，Proxy 立即返回

延续 v0.5.0 的原则：R0 审批是异步的人类流程，Proxy 的 MCP tool call 是同步的请求-响应。当 R2 返回 `require_approval` 时，Proxy 应该**立即返回一个结构化的“需要审批”结果**，而不是挂起连接。

这是为了：
- 不占用外部 Agent 的 HTTP/SSE 连接；
- 不依赖 Agent 实现长连接保持；
- 让 Agent 框架自己决定如何处理 pending 状态。

### 3.2 用 `decision_id` 作为审批凭证

当前代码中 `Decision` 对象已经使用 `decision_id`。v0.5.1 继续沿用这个术语，不再引入新的 `approval_id`，避免与 `ApprovalRequest.request_id` 混淆。

返回给 Agent 的结构：

```json
{
  "status": "require_approval",
  "decision_id": "dec_xxx",
  "request_id": "req_xxx",
  "tool_name": "send_email",
  "reason": "sending email to external address requires human approval",
  "expires_at": "2026-08-14T12:00:00Z",
  "retry_instruction": "Approve via `lc approvals approve <request_id>`, then retry with header x-loop-controller-decision-id: <decision_id>"
}
```

### 3.3 审批与调用参数绑定

为了防止 Agent 用一个已批准的 `decision_id` 去执行另一个参数不同的调用，Proxy 必须缓存原始调用参数，并在重试时校验。

具体做法：
- 在提交审批请求时，把 `tool_name` + `arguments` 存入 `ApprovalRequest.tool_arguments`；
- 重试时，比较当前调用参数与缓存参数；
- 不一致则返回 `deny`，说明参数已变更。

## 4. 详细设计

### 4.1 模型变更

#### `src/loop_controller/models.py`

在 `ApprovalRequest` 中新增字段：

```python
class ApprovalRequest(BaseModel):
    request_id: str
    decision_id: str
    task_id: str
    agent_id: str
    tool_name: str
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    created_at: datetime
    expires_at: datetime
```

> 注：`tool_arguments` 用 `dict[str, Any]`，避免 Pydantic 对复杂 MCP 参数的结构化限制。

#### `src/loop_controller/models.py` `Decision`

确认 `Decision` 已经包含 `decision_id`。当前已有，无需改动。

### 4.2 ApprovalManager 增强

#### `src/loop_controller/approval_manager.py`

新增方法：

```python
class AsyncApprovalManager:
    ...

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """按 request_id 查询原始审批请求。"""
        ...

    def get_request_by_decision(self, decision_id: str) -> ApprovalRequest | None:
        """按 decision_id 查询原始审批请求。"""
        ...
```

实现时直接在 `JsonlApprovalStore` 中扫描最新匹配记录即可。当前 store 是 append-only JSONL，数据量不大，线性扫描足够。

### 4.3 Proxy Server 改造

#### `src/loop_controller/proxy_server.py`

修改 `call_tool` 流程：

```python
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # 1. 解析 header / metadata 中的 decision_id
    retry_decision_id = self._extract_retry_decision_id()

    if retry_decision_id:
        # 2a. 重试路径
        decision = self.approval_manager.get_decision(retry_decision_id)
        if decision is None:
            return _error("decision_id not found")
        if decision.status != "approved":
            return _require_approval_response(decision, status=decision.status)

        request = self.approval_manager.get_request_by_decision(retry_decision_id)
        if request is None:
            return _error("approval request not found")
        if request.tool_name != name or request.tool_arguments != arguments:
            return _error("retry parameters mismatch original approved request")

        # 重建 proposal 并执行
        task = self.runtime.get_task(request.task_id)
        proposal = self._rebuild_proposal(request)
        result = await self.runtime.checkpoint.forward(proposal, decision, session_id=...)
        return [types.TextContent(type="text", text=result.content)]

    # 2b. 正常路径
    task, session = self.runtime.create_task(user_id=..., agent_id=...)
    agent = self.runtime.checkpoint._identity.get_agent(self.agent_id)
    proposal = self._build_proposal(task, name, arguments)
    decision = await self.runtime.checkpoint.evaluate(task, agent, proposal)

    if decision.verdict == "allow":
        result = await self.runtime.checkpoint.forward(proposal, decision, session_id=session.session_id)
        return [types.TextContent(type="text", text=result.content)]

    elif decision.verdict == "deny":
        return _error(f"denied: {decision.reason}")

    elif decision.verdict == "require_approval":
        # 提交审批请求时缓存 tool_arguments
        request = ApprovalRequest(
            request_id=decision.request_id,
            decision_id=decision.decision_id,
            task_id=task.task_id,
            agent_id=self.agent_id,
            tool_name=name,
            tool_arguments=arguments,
            reason=decision.reason,
            created_at=utc_now(),
            expires_at=utc_now() + self.approval_ttl,
        )
        await self.approval_manager.submit(request)
        return _require_approval_response(decision, request)
```

#### 结构化响应辅助函数

```python
def _require_approval_response(decision: Decision, request: ApprovalRequest, status: str = "require_approval") -> list[types.TextContent]:
    payload = {
        "status": status,
        "decision_id": decision.decision_id,
        "request_id": request.request_id,
        "tool_name": request.tool_name,
        "reason": decision.reason,
        "expires_at": request.expires_at.isoformat(),
        "retry_instruction": f"Approve via `lc approvals approve {request.request_id}`, then retry with header x-loop-controller-decision-id: {decision.decision_id}",
    }
    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
```

### 4.4 decision_id 传递方式

#### stdio 模式

MCP stdio 调用本身没有 HTTP header。v0.5.1 约定：如果 Agent 需要在 stdio 模式下重试，可以把 `decision_id` 作为参数传入：

```json
{
  "decision_id": "dec_xxx",
  "to": "user@example.com",
  "subject": "hello"
}
```

Proxy 优先读取 `arguments.get("_loop_controller_decision_id")` 或 `arguments.get("decision_id")`，然后把它从真实参数中剔除再转发。

#### SSE 模式

SSE 模式下 MCP 的 `POST /messages` 可以带 HTTP header，因此优先支持：

```http
x-loop-controller-decision-id: dec_xxx
```

### 4.5 Task 查找

重试时需要根据 `request.task_id` 找到原来的 Task。当前 `Runtime` 没有持久化的 `TaskStore`，所以：

- v0.5.1 先在内存中维护 `self._tasks: dict[str, Task]`；
- Proxy 进程重启后缓存丢失，重试会失败。这个限制写入 `KNOWN_LIMITATIONS.md`。

如果 Agent 在 Proxy 进程存活期间重试，则可以恢复。

## 5. CLI 与配置

无需新增 CLI 命令。`lc proxy` 现有参数保持不变：

```bash
lc proxy --agent-id research-agent --user-id alice --session-id sess_xxx
```

可选新增参数（如果需要）：

- `--approval-ttl`：审批有效期，默认 1 小时。

## 6. 验收标准

| 编号 | 验收项 | 验证方式 |
|---|---|---|
| P1 | Proxy allow 路径仍正常返回 tool 结果 | `test_proxy_allow.py` |
| P2 | Proxy deny 路径返回 deny 原因 | `test_proxy_deny.py` |
| P3 | `require_approval` 返回 JSON 结构，包含 decision_id/request_id/expires_at | `test_proxy_require_approval.py` |
| P4 | 审批通过后，携带 decision_id 重试能成功执行原 tool | `test_proxy_retry_approved.py` |
| P5 | 审批通过后，携带 decision_id 但参数不一致，返回 deny | `test_proxy_retry_param_mismatch.py` |
| P6 | 未审批/已拒绝/已过期 decision_id 重试返回对应状态 | `test_proxy_retry_not_ready.py` |
| P7 | `lc approvals approve <request_id>` 后重试可用 | 手动 E2E |
| P8 | 文档 `src/answer.md` / `docs/architecture/05_mvp_core_abstractions.md` 更新 MCP 集成章节 | 人工审查 |

## 7. 风险评估

| 风险 | 影响 | 缓解 |
|---|---|---|
| `tool_arguments` 存入 `ApprovalRequest` 导致参数序列化问题 | 中 | 使用 `dict[str, Any]`，拒绝不可 JSON 序列化的值 |
| Agent 用同一个 decision_id 多次重试 | 低 | `use_decision()` 已有次数校验 |
| Proxy 进程重启后无法恢复 pending Task | 低 | 写入 `KNOWN_LIMITATIONS.md`；v0.6.0 引入 TaskStore 再解决 |
| 现有 `ApprovalManager` 接口变更影响其他调用方 | 低 | `submit()` 签名不变，仅新增 `tool_arguments` 字段 |

## 8. 相关文档

- `src/loop_controller_v0.5.0_development.md`
- `docs/architecture/05_mvp_core_abstractions.md`
- `src/answer.md`
- `src/KNOWN_LIMITATIONS.md`

## 9. 版本号说明

v0.5.1 是 v0.5.0 的补丁增强，不引入新的架构层，只完善 Proxy 审批恢复路径。v0.5.2 建议关注 `ActionProposal.intent_tag` 或持久化 `BudgetLedger`。
