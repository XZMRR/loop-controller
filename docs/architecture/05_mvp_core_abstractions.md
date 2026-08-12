# Loop Controller MVP 核心抽象与接口设计（草案 v0.3）

> **文档定位**：在 [00\_r0r3\_architecture.md](./00_r0r3_architecture.md) 四层治理模型基础上，定义 MVP（最小可行产品）阶段的核心抽象、接口形态和调用流程。本文面向开发者和贡献者，用于统一 R0-R3 的代码语义。
>
> **MVP 场景**：制度化的研究助手（Research Assistant），可搜索公开资料、读取本地知识库、写摘要、发送研究报告邮件。
>
> **状态**：草案 v0.2，已吸收外部 AI 审阅意见，待评审\
> **最后更新**：2026-08-12

***

## 1. 为什么先做抽象，而不是直接写代码

在写第一行业务代码前，必须先回答一个问题：**不同组件之间交换的“最小信息单元”是什么？**

如果这个问题不统一，组员会各自理解：

- R1 给 R2 的到底是一个函数名 + 参数，还是要带上风险等级和理由？
- R2 返回的 `allow` 是一次性授权，还是针对某个 `call_id` 的授权？
- R3 审计日志里到底记录原始参数、哈希，还是脱敏后的版本？

本文先定义这些最小信息单元（抽象），再讨论它们如何组合。代码实现时，每个抽象对应一个 Python dataclass / Pydantic model 或一个函数接口。

***

## 2. MVP 场景：研究助手

### 2.1 场景描述

用户向研究助手下达任务：

> “调研一下 OpenAI 最新模型在企业合规方面的争议，读取我们内部知识库里的《AI 合规 checklist》，写一份 500 字摘要，发邮件给张经理。”

这个场景能同时暴露四类风险，正好用来验证 R0-R3：

| 动作                 | 风险           | 治理点                                  |
| ------------------ | ------------ | ------------------------------------ |
| `web_search`       | 访问不可信来源、信息外泄 | R2 限制搜索来源（如只允许特定 provider）+ Token 预算 |
| `read_file`（内部知识库） | 越权读取敏感文件     | R2 CapabilityProfile 限定可读目录          |
| `write_file`       | 覆盖、篡改本地文件    | R2 限定写入目录 + 默认拒绝                     |
| `send_email`       | 外发敏感信息、钓鱼风险  | R2 必须人工审批，收件人白名单                     |

### 2.2 MVP 工具集

Loop Controller 使用**规范化工具名**，由 `MCPGateway` 映射到真实 MCP server 的工具名。

| 规范化工具名       | 真实 MCP 工具名示例                   | 作用     | MVP 实现                          |
| ------------ | ------------------------------ | ------ | ------------------------------- |
| `web_search` | `brave_web_search`             | 搜索公开资料 | Mock 或 Brave Search API（可选）     |
| `read_file`  | `read_text_file` / `read_file` | 读取本地文件 | 真实 `filesystem` MCP server      |
| `write_file` | `write_file`                   | 写摘要/报告 | 真实 `filesystem` MCP server，限制目录 |
| `send_email` | `send_email`                   | 发送邮件   | Mock MCP server，只记录不真发          |

**贴近真实的原因**：用真实 MCP server 可以验证“R2 作为 MCP Client Policy Gateway”是否真的能卡住工具调用；用 mock `send_email` 可以避免真实邮件外泄风险。

***

## 3. 核心抽象

下面每个抽象都给出：**业务含义**、**字段设计**、**与其他抽象的关系**。

代码示例统一使用 `from __future__ import annotations` 来避免前向引用问题；生产实现建议用 Pydantic 做校验。

### 3.1 `Task`：一次用户请求的上下文

**业务含义**：用户向 Agent 提出的一个完整任务。它贯穿 R0-R3，是审计的顶层追踪单元。

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Task:
    task_id: str              # 全局唯一任务 ID，也是 R3 审计的 trace_id
    user_id: str              # 发起任务的人类用户
    agent_id: str             # 负责执行的 Agent
    description: str          # 用户原始请求
    created_at: datetime      # UTC
```

**设计理由**：

- 把“谁让 Agent 干活”与“Agent 自己是什么”分开；
- `task_id` 作为 trace root，所有子事件都挂在这个 ID 下。

### 3.2 `Agent`：R1 的执行实体

**业务含义**：一个具有身份、角色和能力边界的 Agent。它是企业员工的数字化映射。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Agent:
    agent_id: str           # 全局唯一身份，如 "researcher_001"
    name: str               # 可读名称，如 "Research Assistant"
    profile_id: str         # 关联的 CapabilityProfile ID
    owner_id: str           # 所属人类用户/部门，用于 R0 审批路由
```

**设计理由**：

- `agent_id` 是审计主键；
- `profile_id` 把“谁能做什么”与“谁在执行”解耦，一个岗位可以有多个 Agent 实例。

**安全说明**：Agent 对象由 Loop Controller 运行时从可信配置加载，不是由 R1 自己构造。R1 提交的 `ActionProposal` 里虽然也有 `agent_id`，但 R2 必须以运行时加载的 `Agent` 对象为准，防止伪造。

### 3.3 `CapabilityProfile`：Agent 的岗位说明书

**业务含义**：定义一个 Agent 被允许使用的工具集、参数限制、预算上限和固定权限天花板（Fixed Ceiling）。对应企业内控里的“岗位职责”。

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True)
class ToolPermission:
    tool_name: str
    allowed: bool = False
    require_approval: bool = False          # 是否需要 R0-delegate 审批
    allowed_args: dict = field(default_factory=dict)   # 参数白名单，如 {"to": ["*@company.com"]}
    denied_args: dict = field(default_factory=dict)    # 参数黑名单
    daily_limit: int | None = None            # 每日调用上限

@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    version: str                            # 策略版本，用于审计追溯
    description: str
    tools: dict[str, ToolPermission]        # 工具名 -> 权限配置
    max_budget_token: int = 1_000_000       # Token 级运行预算
    max_budget_payment: float = 0.0         # 财务支付预算，研究助手为 0
    fixed_ceiling: dict = field(default_factory=dict)  # Earned Authority 固定上限
```

**设计理由**：

- 默认拒绝：如果 `tools` 里没有某个工具，Agent 默认无权使用；
- `require_approval` 区分“自动允许”和“必须审批”，如 `send_email` 默认要求审批；
- `allowed_args`/`denied_args` 支持参数级控制，如只允许发给公司邮箱；
- `version` 字段让审计能回答“当时生效的是哪一版策略”。

**MVP 语义约定**：

- `allowed_args` 中的字符串值支持 `*` 通配符，匹配规则为 POSIX glob（如 `*@company.com`）；
- 若参数是对象/列表，MVP 只做浅层字符串匹配，复杂结构匹配留待未来。

### 3.4 `ActionProposal`：R1 向 R2 的动作申报

**业务含义**：R1 规划出一个动作后，不是直接执行，而是把它封装成申报单提交给 R2。这是 Agent 交互与工具调用的分界线。

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class ActionProposal:
    task_id: str
    call_id: str              # R1 生成的候选调用 ID，R2 会校验其唯一性
    agent_id: str
    tool_name: str
    arguments: dict
    task_context: str         # 当前任务的简短描述
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    reason: str = ""          # Agent 为什么认为需要这个动作
```

**设计理由**：

- `task_id` 把动作挂到一次完整任务；
- `call_id` 用于把 R2 的判定、R3 的审计、最终工具执行结果串成一条链；
- `risk_level` 是 R1 自检的参考，不是最终判定；
- `reason` 给 R0-delegate 审批时看，也用于 R3 审计解释性。

**安全说明**：`call_id` 由 R1 生成 UUID，R2 在判定前检查该 `call_id` 是否已处理过，防止重放。真正的权威 Decision 由 R2 签发，R1 不能伪造。

### 3.5 `Decision`：R2 的判定结果

**业务含义**：R2 综合 Policy、CapabilityProfile、权限连锁分析后给出的最终裁决。

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "deny", "modify", "require_approval"]
    decision_id: str          # R2 生成的判定唯一 ID
    call_id: str              # 关联的 ActionProposal.call_id
    task_id: str
    reason: str
    modified_args: dict | None = None       # verdict == "modify" 时回写
    escalation_target: str | None = None     # verdict == "require_approval" 时指向 R0-delegate
    policy_hits: list[str] = field(default_factory=list)  # 命中的规则名，用于审计
    expires_at: datetime | None = None        # 授权有效期，MVP 默认 5 分钟
    max_uses: int = 1                         # 授权可用次数，MVP 固定为 1
```

**设计理由**：

- 四种裁决直接对应架构里的 R2 输出；
- `decision_id` 和 `expires_at` 防止 Decision 被重放或长期滥用；
- `policy_hits` 让审计可解释；
- `modified_args` 支持 R2 对参数做安全改写（如把外部邮箱改为内部邮箱）。

**`modify`** **流程说明**：

- `modify` 表示“允许执行，但必须使用 R2 改写后的参数”；
- `Checkpoint.forward` 必须基于 `modified_args` 执行，并在执行前做一次轻量复核（如参数类型、目标范围）；
- 如果改写后的参数涉及新的风险维度，应返回 `require_approval` 而非 `modify`。

### 3.6 `Checkpoint`：R2 的统一入口

**业务含义**：R2 对外的门面。接收 `ActionProposal`，返回 `Decision`，对 `allow`/`modify` 的动作代理转发到 MCP Gateway。

```python
from typing import Protocol

class Checkpoint(Protocol):
    async def evaluate(self, proposal: ActionProposal, agent: Agent, task: Task) -> Decision: ...
    async def forward(self, proposal: ActionProposal, decision: Decision) -> ToolResult: ...
```

**关键设计**：

- `evaluate` 只做判定，不产生副作用；
- `forward` 只在 `Decision.verdict in ("allow", "modify")` 时被调用，由 R2 自己把调用发到 MCP Gateway；
- R1 **不直接持有** MCP client。

**安全说明**：`forward` 在执行前会校验：

1. `decision.expires_at` 未过期；
2. `decision.call_id` 与 `proposal.call_id` 一致；
3. `decision.max_uses` 未超限；
4. 如 `verdict == "modify"`，则使用 `decision.modified_args` 而非 `proposal.arguments`。

### 3.7 `PolicyEngine`：OPA 的封装层

**业务含义**：把 Rego 策略的加载、查询、版本管理封装起来，让 `Checkpoint` 只关心业务输入输出。

```python
from typing import Protocol

class PolicyEngine(Protocol):
    def load_policy(self, package: str, rego_file: str) -> None: ...
    def evaluate(self, package: str, input_doc: dict) -> dict: ...
```

**MVP 实现方式：OPA HTTP sidecar**

```python
import requests

class OPAPolicyEngine:
    def __init__(self, base_url: str = "http://localhost:8181"):
        self.base_url = base_url

    def evaluate(self, package: str, input_doc: dict) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}/v1/data/{package}",
                json={"input": input_doc},
                timeout=2.0,
            )
            response.raise_for_status()
            return response.json().get("result", {})
        except requests.RequestException:
            # 安全原则：OPA 不可用时默认拒绝，避免故障开放
            return {"verdict": "deny", "reason": "policy engine unavailable"}
```

**选择 OPA HTTP sidecar 的理由与未来改进**：

| 维度   | 当前选择                      | 未来可能                              |
| ---- | ------------------------- | --------------------------------- |
| 集成方式 | OPA 作为本地守护进程，通过 HTTP 查询   | 可替换为 OPA Go SDK、WASM 嵌入，或自研字节码 VM |
| 原因   | 标准、调试方便、Python 无成熟 Rego 库 | 当性能或部署形态（如无 sidecar 场景）成为瓶颈时      |
| 部署   | 开发时 `opa run --server`    | 生产可内嵌、sidecar、远端服务                |
| 故障策略 | 默认拒绝（fail-closed）         | 未来可配置缓存/降级策略                      |

### 3.8 `PermissionInteractionAnalyzer`：权限连锁分析

**业务含义**：检测多个独立权限/工具组合后产生的新能力（A + B > C）。MVP 先用静态规则表，但接口要预留扩展。

```python
from typing import Protocol

class PermissionInteractionAnalyzer(Protocol):
    def check(self, history: list[ActionProposal], current: ActionProposal) -> list[str]: ...
```

**MVP 静态规则表示例**（YAML）：

```yaml
rules:
  - id: read_contact + send_email = phishing_risk
    tools: ["read_file", "send_email"]
    condition: "read_file.target contains 'contact' and send_email.to is external"
    risk: high
    action: require_approval
```

**设计理由**：

- 先定义接口，让 R2 能调用；
- MVP 实现走简单规则匹配；
- 未来可替换为图分析或能力集合代数。

### 3.9 `MCPGateway`：R2 的 MCP Client 代理

**业务含义**：R2 不直接暴露原始 MCP client 给 R1，而是通过 `MCPGateway` 做两件事：

1. `list_tools`：按 CapabilityProfile 过滤后返回工具列表，解决 MCP “默认暴露全部工具” 的问题；
2. `call_tool`：只转发已被 R2 授权的调用。

```python
from typing import Protocol

class MCPGateway(Protocol):
    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]: ...
    async def call_tool(self, tool_name: str, arguments: dict, call_id: str) -> ToolResult: ...
```

**设计理由**：

- MCP 协议本身没有工具级 ACL，`MCPGateway` 是 Loop Controller 补充的治理层；
- 过滤 `tools/list` 还能减少 LLM context window 占用（context rot 问题）；
- 规范化工具名到真实 MCP 工具名的映射，屏蔽不同 server 的实现差异。

### 3.10 `Tool` 与 `ToolResult`

```python
from dataclasses import dataclass
from typing import Any, Literal

@dataclass(frozen=True)
class Tool:
    canonical_name: str     # Loop Controller 内部使用的名字，如 "read_file"
    mcp_name: str           # 真实 MCP server 工具名，如 "read_text_file"
    description: str
    input_schema: dict      # JSON Schema，与 MCP 协议对齐

@dataclass(frozen=True)
class ToolResult:
    call_id: str
    task_id: str
    tool_name: str          # canonical_name
    status: Literal["success", "error", "blocked"]
    content: Any            # 成功时返回的结构化内容；MVP 用 str/dict
    error_code: str | None = None
    elapsed_ms: int = 0
```

### 3.11 `BudgetLedger`：双类预算记账

**业务含义**：分别跟踪 Token 级运行预算和现实财务支付预算。MVP 主要实现 Token 预算，但接口要预留支付预算。

```python
from typing import Protocol

class BudgetLedger(Protocol):
    def check_and_reserve(self, agent_id: str, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, agent_id: str, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, agent_id: str, task_id: str, cost: BudgetCost) -> None: ...

@dataclass(frozen=True)
class BudgetCost:
    token_count: int = 0
    payment_amount: float = 0.0
    currency: str = "USD"
```

**设计理由**：

- 把预算检查与 R2 判定解耦：R2 在判定前咨询 BudgetLedger；
- `check_and_reserve` 先冻结预算，执行成功后再 `commit`，失败则 `refund`。

### 3.12 `R0Delegate` 与 `ApprovalRecord`

**业务含义**：R0-delegate 是 R0 授权的人类审批人。MVP 不打真实 UI，只保留最小接口和配置化实现。

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    decision_id: str
    call_id: str
    task_id: str
    agent_id: str
    tool_name: str
    arguments: dict          # 审批人看到的应是脱敏/部分可见参数
    reason: str                # R2 给出的升级理由
    requester_id: str          # 任务发起者 user_id
    approver_id: str           # 被指派的审批人
    created_at: datetime

@dataclass(frozen=True)
class ApprovalRecord:
    request_id: str
    decision_id: str
    verdict: Literal["approve", "deny", "escalate"]
    approver_id: str
    comment: str
    decided_at: datetime

class R0Delegate(Protocol):
    async def request_approval(self, req: ApprovalRequest) -> None: ...
    async def get_decision(self, request_id: str) -> ApprovalRecord | None: ...
```

**MVP 打桩实现**：

```python
class ConfigR0Delegate:
    """从配置文件读取固定审批人，自动 approve（演示用）或 deny（根据配置）。"""
    def __init__(self, approver_id: str, auto_approve: bool = False):
        self.approver_id = approver_id
        self.auto_approve = auto_approve

    async def request_approval(self, req: ApprovalRequest) -> None:
        # MVP 阶段：不实现真实通知/等待，直接返回预设决策
        pass

    async def get_decision(self, request_id: str) -> ApprovalRecord | None:
        verdict = "approve" if self.auto_approve else "deny"
        return ApprovalRecord(
            request_id=request_id,
            decision_id="",
            verdict=verdict,
            approver_id=self.approver_id,
            comment="MVP stub decision",
            decided_at=datetime.utcnow(),
        )
```

**安全说明**：审批人不能与任务发起者为同一人，也不能是被审批动作的执行 Agent。MVP 至少检查 `approver_id != requester_id`。

### 3.13 `AuditEvent`：R3 的最小日志单元

**业务含义**：R3 异步、只读地采集 R1/R2/R0-delegate 的行为记录。注意 R3 不记录原始敏感参数，而是记录哈希或掩码后的版本。

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass(frozen=True)
class AuditEvent:
    schema_version: str = "1.0"
    event_id: str
    trace_id: str            # 对应 Task.task_id
    parent_event_id: str | None = None
    call_id: str | None      # 动作级 ID，Agent 交互类事件可为空
    timestamp: datetime      # UTC，带时区
    actor_type: Literal["agent", "user", "delegate", "system"]
    actor_id: str
    action: Literal["task_start", "propose", "evaluate", "approve", "deny", "execute", "task_end"]
    target: str | None       # tool_name 或 "checkpoint" 等
    decision: Literal["allow", "deny", "modify", "require_approval"] | None
    args_hash: str | None    # 参数 SHA-256（MVP 用）；未来应改为 HMAC/加盐哈希
    args_mask: dict | None   # 脱敏后的结构化参数，如 {"to": "***@company.com"}
    reason: str | None       # 判定或审批理由
    policy_version: str | None = None
    profile_version: str | None = None
    metadata: dict = field(default_factory=dict)
```

**安全说明**：

- MVP 使用 SHA-256 对参数做摘要，便于事后比对；但 SHA-256 对低熵值（如已知邮箱、常见文件名）可被字典攻击，**未来必须替换为 HMAC 或加盐哈希**；
- `args_mask` 是结构化掩码，保留字段名，隐藏字段值；
- 所有时间戳使用 UTC，避免时区歧义。

***

## 4. 一次任务的生命周期

```
User
  │ "调研...并发邮件给张经理"
  ▼
Task(task_id=t1, user_id=u1, agent_id=a1)
  ▼
R1 Agent (ResearchAssistant)
  │ 1. 解析任务，规划动作序列
  ▼
ActionProposal(call_id=c1, task_id=t1, tool=web_search, args={"query":"OpenAI 合规争议"})
  │
  ▼
R2 Checkpoint.evaluate(proposal, agent, task)
  │ 2. 查询 PolicyEngine（OPA/Rego）
  │ 3. 检查 CapabilityProfile
  │ 4. 咨询 BudgetLedger
  │ 5. 检查 Permission Interaction（MVP 静态规则表）
  │ 6. 返回 Decision(allow, decision_id=d1)
  ▼
R2 Checkpoint.forward(proposal, decision)
  │ 7. 校验 decision_id、call_id、有效期、使用次数
  │ 8. 调用 MCPGateway.call_tool(...)
  ▼
MCP Server (brave_web_search)
  │ 9. 返回结果
  ▼
R1 Agent 继续规划下一个动作 ...
  │
ActionProposal(call_id=c4, task_id=t1, tool=send_email, args={"to":"zhang@company.com", ...})
  │
  ▼
R2 Checkpoint.evaluate(...)
  │ 10. Policy 判定：require_approval（外部/敏感动作）
  ▼
R0-delegate 审批（MVP 用 config 中的固定审批人打桩）
  │ 11. 返回 ApprovalRecord(approve/deny)
  ▼
R2 Checkpoint.forward(...) 或拒绝
  │ 12. forward 前再次校验 decision 仍有效
  ▼
R3 Audit：异步采集全流程 AuditEvent（task_start → propose → evaluate → execute/approve → task_end）
```

**流程中的边界说明**：

- `R1 Agent → R2 Checkpoint` 的箭头是 **动作申报**，属于 Agent 交互。
- `R2 Checkpoint → MCP Server` 的箭头是 **工具调用**，是真正产生外部副作用的动作。
- R1 与 User/其他 Agent/R0-delegate 之间的往返属于 Agent 交互，不经过工具调用通道。

***

## 5. MVP 阶段 Rego 策略示例

下面是一个兼容 OPA v0.60+ 和 OPA 1.0（兼容模式）的 Rego 策略，演示如何判断 `send_email`、`read_file`、`write_file`。

```rego
package loop_controller.tool_permission

import rego.v1

# 默认拒绝
default decision := {"verdict": "deny", "reason": "no policy allows this action"}

# 工具级规则入口
decision := result if {
    input.tool_name == "send_email"
    input.capability.tools.send_email.allowed == true
    result := evaluate_send_email(input)
}

decision := result if {
    input.tool_name == "read_file"
    input.capability.tools.read_file.allowed == true
    result := evaluate_read_file(input)
}

decision := result if {
    input.tool_name == "write_file"
    input.capability.tools.write_file.allowed == true
    result := evaluate_write_file(input)
}

decision := result if {
    input.tool_name == "web_search"
    input.capability.tools.web_search.allowed == true
    result := {"verdict": "allow", "reason": "web search from allowed provider"}
}

# send_email 细分逻辑
evaluate_send_email(req) := {"verdict": "deny", "reason": "external recipient not allowed"} if {
    not is_internal_email(req.arguments.to)
}

evaluate_send_email(req) := {"verdict": "require_approval", "reason": "send_email requires human approval", "escalation_target": req.agent.owner_id} if {
    is_internal_email(req.arguments.to)
    req.capability.tools.send_email.require_approval == true
}

evaluate_send_email(req) := {"verdict": "allow", "reason": "internal email allowed"} if {
    is_internal_email(req.arguments.to)
    req.capability.tools.send_email.require_approval == false
}

is_internal_email(email) if endswith(lower(email), "@company.com")

# read_file 细分逻辑：限制可读目录
evaluate_read_file(req) := {"verdict": "deny", "reason": "path outside allowed read directories"} if {
    not is_under_allowed_dirs(req.arguments.path, req.capability.tools.read_file.allowed_args.directories)
}

evaluate_read_file(req) := {"verdict": "allow", "reason": "read within allowed directories"} if {
    is_under_allowed_dirs(req.arguments.path, req.capability.tools.read_file.allowed_args.directories)
}

# write_file 细分逻辑：默认 deny，仅在允许目录下且 require_approval=false 时 allow
evaluate_write_file(req) := {"verdict": "deny", "reason": "write_file requires explicit approval by default"} if {
    req.capability.tools.write_file.require_approval == true
}

evaluate_write_file(req) := {"verdict": "allow", "reason": "write within allowed output directory"} if {
    req.capability.tools.write_file.require_approval == false
    is_under_allowed_dirs(req.arguments.path, req.capability.tools.write_file.allowed_args.directories)
}

is_under_allowed_dirs(path, dirs) if {
    some d in dirs
    startswith(path, d)
}
```

**说明**：

- `input` 由 Python 端把 `ActionProposal` + `CapabilityProfile` + `Agent` 序列化而来；
- `import rego.v1` 同时兼容 OPA v0.60+（显式导入未来语法）和 OPA 1.0（兼容模式）；
- `default decision` 实现“未明确允许即拒绝”；
- 函数参数命名为 `req`，避免与全局 `input` 文档冲突；
- `is_internal_email` 使用 `lower()` 做大小写归一化。

***

## 6. MVP 代码目录结构（建议）

```
loop-controller/
├── src/loop_controller/
│   ├── __init__.py
│   ├── task.py                   # Task 上下文
│   ├── agent.py                  # Agent 抽象与 R1 执行循环
│   ├── capability_profile.py     # CapabilityProfile / ToolPermission
│   ├── checkpoint.py             # R2 Checkpoint：evaluate + forward
│   ├── policy_engine.py          # PolicyEngine / OPAPolicyEngine
│   ├── permission_interaction.py # PermissionInteractionAnalyzer 接口与静态实现
│   ├── mcp_gateway.py            # MCPGateway：工具列表过滤 + 授权转发
│   ├── budget.py                 # BudgetCost / BudgetLedger
│   ├── r0_delegate.py            # R0-delegate 接口与打桩实现
│   ├── audit.py                  # AuditEvent / AuditLogger
│   └── utils.py                  # 参数哈希、掩码、canonicalization
├── policies/
│   └── default.rego              # MVP 默认 Rego 策略
├── examples/
│   └── research_agent_example.py # 研究助手端到端示例
├── tests/
│   ├── test_checkpoint.py        # Checkpoint + PolicyEngine 单元测试
│   └── test_policy_engine.py     # Rego 策略测试
└── pyproject.toml
```

***

## 7. 关键设计决策与原因

| 决策                           | 结论                                                                     | 原因                         | 未来可能的改进                              |
| ---------------------------- | ---------------------------------------------------------------------- | -------------------------- | ------------------------------------ |
| R1 不直接调用工具                   | R1 只生成 `ActionProposal`，R2 转发                                          | 防止 Agent 绕过策略；与 MCP 网关模式一致 | 未来可在沙箱内让 R1 执行只读工具，但仍需 R2 授权         |
| OPA HTTP sidecar             | MVP 用本地 OPA 进程 + HTTP 查询                                               | 标准、调试方便、Python 无成熟 Rego 库  | 未来可替换为 Go SDK、WASM 或自研字节码 VM         |
| CapabilityProfile 与 Agent 分离 | Agent 通过 `profile_id` 关联 Profile                                       | 一个岗位多个 Agent 实例；策略独立演进     | 未来支持多 Profile 动态切换                   |
| Decision 四态 + 有效期 + 单次使用     | allow/deny/modify/require\_approval，带 `expires_at` 和 `max_uses=1`      | 防止重放和长期滥用                  | 未来可增加 `defer`（异步等待外部条件）和 token 签名    |
| R0-delegate 打桩               | MVP 用 config 文件指定固定审批人                                                 | 先跑通 R0-R3 闭环，不阻塞在 UI/通知    | 未来接入真实审批 UI、IM、邮件通知                  |
| MCP Gateway 在 R2 内部          | R2 同时是 PDP 和 PEP                                                       | MVP 简化部署；避免组件过多            | 未来可把 PEP 拆分为独立 MCP Client Proxy      |
| Audit 只记录哈希+掩码               | 平衡可追溯与隐私                                                               | 合规要求；防止审计日志本身成为泄露源         | 未来支持分级审计，R0 可查看完整日志；SHA-256 升级为 HMAC |
| 规范化工具名                       | Loop Controller 内部用 `read_file`/`write_file`/`web_search`/`send_email` | 屏蔽不同 MCP server 的实现差异      | 未来通过 Tool Registry 做更灵活的映射           |
| Permission Interaction 静态规则表 | MVP 用 YAML 规则 + 简单匹配                                                   | 先覆盖常见高危组合                  | 未来替换为图分析或能力集合代数                      |
| Budget 独立 Ledger             | R2 判定前咨询 BudgetLedger                                                  | 解耦预算逻辑与策略逻辑                | 未来支持多币种、按任务/按 Agent 多维度预算            |

***

## 8. 仍待确认的问题

以下问题不影响 MVP 骨架，但需要在实现过程中逐步收敛：

1. **R0-delegate 审批超时**：如果审批人不在线，R1 是挂起、降级执行，还是直接拒绝？
2. **Permission Interaction 静态规则表格式**：当前方案是 YAML，是否需要统一到 Rego？
3. **Earned Authority 的 Fixed Ceiling 默认值**：研究助手是否需要临时提升权限？MVP 是否先不实现？
4. **MCP Server 发现机制**：是硬编码配置，还是运行时扫描目录？
5. **R3 审计存储**：MVP 用本地 SQLite/JSONL，还是直接写文件？
6. **参数掩码规则**：哪些字段必须掩码（PII、密码、token）、哪些可以保留？

***

## 9. 参考文档

- [00\_r0r3\_architecture.md](./00_r0r3_architecture.md)：R0-R3 四层治理模型
- [overview.md](./overview.md)：架构概览与核心抽象候选
- [../research/内控最小岗位结构抽象\_v0.1.md](../research/内控最小岗位结构抽象_v0.1.md)：企业内控角色抽象的理论来源
- [../research/03\_runtime\_governance\_landscape.md](../research/03_runtime_governance_landscape.md)：Zenity/Palo Alto/OPA 竞对调研
- [Open Policy Agent 集成文档](https://www.openpolicyagent.org/docs/latest/integration/)：OPA REST API / sidecar / SDK 模式
- [OPA 1.0 / Rego v1 语法](https://www.openpolicyagent.org/docs/policy-reference/keywords/import/)：`import rego.v1` 与 future keywords
- [MCP 规范 - 架构](https://modelcontextprotocol.io/specification/)：Host-Client-Server 模型
- [MCP Filesystem Server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)：官方文件系统工具名
- [OpenAI Agents SDK - Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents)：Agent loop 设计参考

