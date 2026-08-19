# Loop Controller MVP 完备方案：纯工具调用版（v1.1）

> **文档定位**：本文档是 Loop Controller MVP 的**唯一权威实现依据**，取代 `05_mvp_core_abstractions.md`（草案 v0.3）。它在 `00_r0r3_architecture.md`（v0.3）的四层治理模型基础上，完成三件事：
>
> 1. **范围裁剪**：MVP 只治理 Agent 工具调用（`tool_call`），不含 Agent 间交互治理、不含多 Agent 委托；
> 2. **消除待定**：对原两份文档中的 40 个未确定问题逐条给出决策（见附录 A），本文正文即决策结果，不留"待确认"；
> 3. **补齐基础设施**：定义 Identity / Policy Store / Audit Store / Decision Store / ConfigLoader 五个基础设施组件的最小接口与配置格式。
>
> **MVP 场景**：制度化的研究助手（Research Assistant）——搜索公开资料、读取本地知识库、写摘要、发送研究报告邮件。
>
> **状态**：v1.1，所有待定项已关闭，可直接进入开发
> **最后更新**：2026-08-16
>
> **v1.1 修订记录**（外部评审 + 自审后修订，共 12 处）：
>
> | 来源 | 修订 | 章节 |
> |---|---|---|
> | 评审#1 | `call_id` 唯一性检测升级为全局（加固项；评审所述"复用 call_id 直接执行"场景不成立，授权防重放本就在 Decision 层全局生效） | §3.4、§4.5、§6.1 |
> | 评审#2 | 显式声明运行时假设：单进程 asyncio 事件循环，无并行 `forward` | §6.6 |
> | 评审#3 | token 估算从"固定计 1"改为按工具配置 `cost_per_call`；post-MVP 补登真实计量 | §3.8、§6.5、§9.2、§9.3 |
> | 评审#4 | `R0Delegate` 改 async 接口（实现仍立即返回），消除未来异步化的调用方重构 | §3.10、§5.2、§7.5 |
> | 评审#5 | 掩码正则标注"已知宽松、宁宽勿漏" | §7.4 |
> | 评审#6 | 显式声明裁决优先级总表 | §6.1 |
> | 评审#7/#8 | Planner 改为输出"动作草案"，`call_id` 由 run_task 框架统一生成；补 LLMPlanner 输出 JSON Schema | §3.4、§5.1、§5.2 |
> | 评审#9 | 启动校验补三条：glob 合法性、正则编译性、approver 存在性 | §4.1 |
> | 自审#1 | **审批视图与审计日志掩码分级**（修复"审批人看不到真实收件人导致审批形同虚设"） | §3.10、§7.4 |
> | 自审#2 | 审计超长字段截断规则（防 `write_file.content` 整体落盘） | §7.4 |
> | 自审#3 | 补 `finalize_after_approval` 新 Decision 有效期规则；启动试查询的预期结果说明 | §4.1、§6.1 |

---

## 1. MVP 范围

### 1.1 范围内（In Scope）

| 项 | 说明 |
|---|---|
| 单 Agent | 一个研究助手 Agent 完成全部步骤，无子任务委托 |
| 纯工具调用治理 | 治理对象只有 `tool_call`：申报 → 判定 → 转发 → 审计 |
| R1 执行层 | 执行循环 + 规则版轻量分类器（只输出风险信号） |
| R2 控制层 | Checkpoint（PDP+PEP 合一）、OPA/Rego 策略、YAML 权限组合规则、内存预算记账、per-task 动作历史 |
| R3 审计层 | 全量 JSONL 审计日志 + 哈希链 + 参数掩码 |
| R0-delegate | 打桩（async 接口 + 立即返回；配置化固定审批人，approve/deny 两态） |
| 基础设施 | ConfigLoader、IdentityProvider、PolicyStore、AuditStore、DecisionStore（全部为最小本地实现） |
| 工具集 | `web_search`（Mock 或 Brave）、`read_file`（真实 filesystem MCP server）、`write_file`（真实 filesystem，限目录）、`send_email`（Mock，只记录不真发） |

### 1.2 范围外（Out of Scope，明确移出 MVP）

| 项 | 移出原因 | 去向 |
|---|---|---|
| Agent 间交互治理（`inter_agent`） | 本期范围裁剪的核心 | post-MVP |
| 多 Agent 委托链治理 | 单 Agent 场景无需 | post-MVP |
| Earned Authority 动态权限提升 | 研究助手场景无此需求 | post-MVP，字段保留但固定为空 |
| 策略加密存储（HSM/TEE） | MVP 诚实降级：明文本地文件 + 文件系统权限 + 内容哈希版本号 | post-MVP，见 §9.3 |
| 审批异步通知 / 真实 UI | 打桩实现（立即返回）即可验证闭环；async 接口已就位 | post-MVP |
| 审批 escalate 到 R0 | R0 Governance 不在 MVP | post-MVP，`ApprovalRecord` 收敛为两态 |
| 审计采样 | MVP 全量记录，量小可控 | post-MVP |
| 财务支付预算与熔断 | 研究助手 `max_budget_payment=0` | post-MVP，字段保留 |
| 用户脱敏上报 / Open-Core 意图控制接口 | 商业层能力，与治理闭环验证无关 | post-MVP |
| 沙箱 | 架构上与 R1/R2/R3 解耦，MVP 不启用 | post-MVP |
| 低代码模板市场 / 更新机制 | MVP 仅附一份内置示例模板 | post-MVP |

### 1.3 继承的设计原则（来自 00 文档，本期全部保留）

1. 制度优先于审批；2. Runtime 强制优先于模型自律；3. **R1 自检与 R2 判定不用大模型**（规则 + Rego）；4. R3 审计异步、无指令下发权；5. 默认拒绝 + 最小可用权限；6. 核心控制流程断网可用（OPA sidecar 为本地进程，见 §6.4）；7. 沙箱解耦；8. **R1 不直接持有任何外部工具执行通道，R2 是唯一授权出口**。

> **关于 LLM 的精确定位**：LLM 在 MVP 中只允许出现在两处——R1 的**任务规划**（`LLMPlanner`，非治理动作，可选）和未来的 R3 审计分析。LLM 永远不参与 `Decision` 的生成。

---

## 2. 与 00 / 05 文档的偏离总表

本文档对前两份文档做出的全部修改，集中列示，便于评审：

| # | 原内容 | 本文决策 | 原因 |
|---|---|---|---|
| D1 | `ActionProposal.type` 预留 `inter_agent` | **删除 `type` 字段**，MVP 只有 tool_call | 预留字段造成两套理解；post-MVP 重新引入时再加 |
| D2 | `Task` 存在两版定义（3.1 节含 `agent_id` 无 `session_id`；10.2 节相反） | **统一为同时包含 `agent_id` 与 `session_id`**；MVP 中 `session_id == task_id` | 消除矛盾；单任务单会话简化 RiskStateManager |
| D3 | `ApprovalRecord.verdict` 三态（approve/deny/escalate） | **收敛为两态**（approve/deny） | R0 不在 MVP，escalate 无路由目标 |
| D4 | `ApprovalRecord` 存在两版字段（3.14 节 vs 10.2 节） | 统一为 §3.10 版本 | 消除矛盾 |
| D5 | Rego 策略的 input schema 与 Python 端 `input_doc` 不一致 | 统一为 §6.3 定义的 schema，Rego 与 Python 严格对齐 | 原方案会导致 OPA 全部默认拒绝 |
| D6 | `args_hash` 用 SHA-256 | MVP 保留 SHA-256，但 `AuditEvent` 增加 `hash_algo` 字段 | 遵循原 MVP 决策；`hash_algo` 使未来升级 HMAC 无需改 schema |
| D7 | 审计存储未选型 | **JSONL 追加写 + SHA-256 哈希链** | 零依赖、可 diff、可校验篡改；SQLite 列为替代 |
| D8 | Permission Interaction 命中后的动作不一致（YAML 示例 `require_approval` vs 10.2 代码 `deny`） | **按规则表中显式声明的 `action` 执行**；高危组合默认 `require_approval` | 规则表自描述，消除歧义 |
| D9 | 审批超时行为未定 | MVP 打桩立即返回，**不存在超时**；v1.1 起接口为 async 但语义同步 | 立即终局语义下无此问题，真实异步化时 post-MVP 再定义 |
| D10 | `RiskSignal.tags` 下游消费路径不明 | `ActionProposal` 增加 `risk_tags` 字段承载，`suggestion` 仅进审计 metadata | 明确消费路径，tags 可进 Rego input |
| D11 | `Decision` 防重放用内存 set | **DecisionStore 持久化**，重启后仍有效 | 内存 set 重启失效，不满足安全声明 |
| D12 | `history`（动作历史）维护者不明 | **Checkpoint 内存维护 per-task 历史**，任务结束即弃 | 唯一需要它的组件是 R2，就近维护 |
| D13 | `policy_hits` 生成责任不明 | **由 OPA 在 Rego 中输出命中规则 ID 列表**，Checkpoint 透传 | 策略引擎唯一知道命中了哪条规则 |
| D14 | 轻量分类器输出 `critical` 与策略 `allow` 的冲突未定义 | **R2 是唯一权威**；`risk_level`/`risk_tags` 作为输入进 Rego，由策略决定是否否决（默认策略含 critical→require_approval 规则） | 坚持"分类器只输出信号"原则，冲突规则由制度（Policy）表达 |
| D15 | OPA 故障策略 | **锁定 fail-closed**：OPA 不可用一律 deny，MVP 不提供缓存/降级开关 | 故障开放是安全禁区 |
| D16 | `datetime.utcnow()` | 全部改为 `datetime.now(timezone.utc)` | Python 3.12+ 弃用 naive UTC |
| D17 | MCP Server 发现机制未定 | **硬编码配置文件** `mcp_servers.yaml` | MVP 最简单可靠形态 |
| D18 | R1 规划方式未定义 | 定义 `Planner` 协议，默认 `ScriptedPlanner`，可选 `LLMPlanner` | 见 §5 |

---

## 3. 统一核心抽象（唯一权威 Schema）

> 本节定义是**全项目唯一版本**。任何其他文档、代码、测试中出现的同名结构，以本节为准。代码实现统一使用 Pydantic v2 做校验（下文用 dataclass 表达语义）。

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
```

### 3.1 Task：一次用户请求的上下文

```python
@dataclass(frozen=True)
class Task:
    task_id: str            # 全局唯一任务 ID，同时是 R3 审计的 trace_id
    session_id: str         # MVP 约定：session_id == task_id（单任务单会话）
    user_id: str            # 发起任务的人类用户（original_principal）
    agent_id: str           # 负责执行的 Agent
    description: str        # 用户原始请求原文
    created_at: datetime    # timezone-aware UTC
```

**决策说明**：

- Task 由**入口层**（CLI runner / 示例脚本）创建并直接传递给 R1 Agent；基础设施层不参与 Task 创建（关闭问题 #2）。
- `description` 原文**不进入 Rego input**（避免 prompt injection 借道策略引擎），仅用于 R1 规划与 R3 审计。

### 3.2 Agent：R1 的执行实体

```python
@dataclass(frozen=True)
class Agent:
    agent_id: str           # 全局唯一身份，预先分配，见 §4.2
    name: str
    profile_id: str         # MVP 一对一静态绑定，不支持运行时切换
    owner_id: str           # 所属人类用户/部门，用于审批路由
```

**决策说明**：Agent 与 CapabilityProfile 在 MVP 为**一对一静态绑定**，绑定关系写在 `agents.yaml` 中，运行期不可变（关闭问题 #4）。

### 3.3 CapabilityProfile：岗位说明书

```python
@dataclass(frozen=True)
class ToolPermission:
    tool_name: str
    allowed: bool = False
    require_approval: bool = False                     # 是否必须 R0-delegate 审批
    allowed_args: dict[str, list[str]] = field(default_factory=dict)  # 参数白名单，值支持 POSIX glob
    denied_args: dict[str, list[str]] = field(default_factory=dict)   # 参数黑名单
    max_calls_per_task: int | None = None              # 单任务内调用上限

@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    version: str                                       # 版本 = 文件内容 SHA-256 前 12 位，见 §4.3
    description: str
    tools: dict[str, ToolPermission]                   # 工具名 -> 权限；不在表中的工具默认拒绝
    max_budget_token: int = 1_000_000                  # Token 预算（单任务）
    max_budget_payment: float = 0.0                    # 财务预算：研究助手固定为 0，MVP 不启用
    fixed_ceiling: dict[str, Any] = field(default_factory=dict)  # MVP 固定为空；Earned Authority post-MVP
```

**语义约定**：

- **默认拒绝**：`tools` 中没有的工具名，R2 直接 deny，不进入 Rego 查询；
- `allowed_args` 的字符串值支持 POSIX glob（如 `*@company.com`、`/data/kb/**`）；
- 参数为对象/列表时只做浅层字符串匹配，复杂结构匹配 post-MVP；
- `max_calls_per_task` 由 Checkpoint 通过 per-task 历史计数强制执行。

### 3.4 ActionProposal：R1 向 R2 的动作申报

```python
@dataclass(frozen=True)
class ActionProposal:
    task_id: str
    call_id: str          # 由 run_task 框架统一生成的 UUID（Planner 不生成，见 §5.1）；R2 校验全局唯一
    agent_id: str
    tool_name: str        # 规范化工具名，见 §6.5
    arguments: dict[str, Any]
    task_context: str     # 由 Task.description 截断生成（前 200 字符），见 §5.3
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    risk_tags: list[str] = field(default_factory=list)  # 由轻量分类器写入，进入 Rego input
    reason: str = ""      # R1 认为需要此动作的理由，供审批人与审计阅读
```

**决策说明**：

- 已删除原草案的 `type` 字段（偏离 D1）；
- 新增 `risk_tags` 承载分类器标签，使其可进入 Rego input 被策略匹配（偏离 D10）；
- `risk_level` 与 `risk_tags` 均由 R1 分类器写入，**只是 R2 的输入信号，不构成任何判定效力**；
- `call_id` 为**全局唯一**：同一 `call_id` 出现在任何 task 下都会被拒绝（v1.1 加固）。注意 `call_id` 只是动作标识符，不是授权凭证——授权防重放由 Decision 层的 `decision_id`（R2 生成、全局唯一、单次使用）承担。

### 3.5 RiskSignal 与 LightweightClassifier

```python
@dataclass(frozen=True)
class RiskSignal:
    risk_level: Literal["low", "medium", "high", "critical"]
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    suggestion: str | None = None    # 仅供 R1 自省与审计，不进入 R2 判定

class LightweightClassifier(Protocol):
    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal: ...
```

**MVP 实现**：`RuleBasedClassifier`（规则版打桩）：

| 规则 | risk_level | tags |
|---|---|---|
| `tool_name == "send_email"` | high | `[external_communication]` |
| `tool_name == "read_file"` | medium | `[data_access]` |
| 参数值匹配敏感模式（邮箱、Bearer token、密码字段名） | high | 追加 `[pii_involved]` 或 `[credential_involved]` |
| 其他 | low | `[]` |

**消费路径**（关闭问题 #9、#10、#11）：

- `risk_level` 与 `tags` → 写入 `ActionProposal.risk_level` / `risk_tags` → 进入 Rego input；
- `suggestion` → 仅写入审计事件 metadata，不进入 R2 判定；
- **冲突规则**：分类器输出与策略判定无优先级关系——R2 是唯一权威。默认 Rego 策略包含一条规则：`risk_level == "critical"` → `require_approval`（见 §6.3），使 critical 信号经由制度通道生效，而非分类器越权。

### 3.6 Decision：R2 的判定结果

```python
@dataclass(frozen=True)
class Decision:
    decision_id: str        # R2 生成的 UUID
    call_id: str
    task_id: str
    verdict: Literal["allow", "deny", "modify", "require_approval"]
    reason: str
    modified_args: dict[str, Any] | None = None   # verdict == "modify" 时回写
    escalation_target: str | None = None          # verdict == "require_approval" 时指向审批人
    policy_hits: list[str] = field(default_factory=list)  # 由 OPA 返回，Checkpoint 透传
    policy_version: str = ""                      # 判定时生效的策略版本
    profile_version: str = ""                     # 判定时生效的 Profile 版本
    expires_at: datetime = field(...)             # allow/modify: +5min；require_approval: +15min；deny: 立即过期
    max_uses: int = 1                             # MVP 固定为 1（deny 为 0）
```

**决策说明**：

- 增加 `policy_version` / `profile_version`，使每个 Decision 自带"当时生效的是哪版制度"，审计可完整回放；
- `expires_at` 按 verdict 分档：allow/modify 5 分钟，require_approval 15 分钟（等待审批窗口），deny 立即过期且 `max_uses=0`。

### 3.7 Tool 与 ToolResult

```python
@dataclass(frozen=True)
class Tool:
    canonical_name: str     # Loop Controller 内部名，如 "read_file"
    mcp_name: str           # 真实 MCP server 工具名，如 "read_text_file"
    description: str
    input_schema: dict      # JSON Schema，与 MCP 协议对齐

@dataclass(frozen=True)
class ToolResult:
    call_id: str
    task_id: str
    tool_name: str          # canonical_name
    status: Literal["success", "error", "blocked"]  # blocked = 被治理链路拦截
    content: Any
    error_code: str | None = None
    elapsed_ms: int = 0
```

### 3.8 BudgetCost 与 BudgetLedger

```python
@dataclass(frozen=True)
class BudgetCost:
    token_count: int = 0
    payment_amount: float = 0.0   # MVP 恒为 0
    currency: str = "USD"

class BudgetLedger(Protocol):
    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, task_id: str, cost: BudgetCost) -> None: ...
```

**MVP 决策**（关闭问题 #20、#21）：

- 额度来源：`CapabilityProfile.max_budget_token`（静态配置，无运行时更新接口）；
- 实现：`InMemoryBudgetLedger`，per-task 计数，进程重启清零（MVP 可接受）；
- **估算口径（v1.1 修正）**：每次工具调用按 `mcp_servers.yaml` 中该工具配置的 `cost_per_call` 估算值计（如 `web_search=200`、`read_file=500`、`send_email=800`），不再固定计 1；R1 若使用 LLMPlanner，其 LLM 调用的 token 消耗由 Planner 按 usage 上报后按实际值计。**注意这仍是估算而非真实计量**：工具结果进入 LLM context 产生的 token 消耗未计入；真实计量（LLM usage 上报 + 工具结果长度折算）已登记 §9.3 post-MVP；
- 超支行为：`check_and_reserve` 返回 False → Checkpoint 直接 deny，**无熔断降级档位**；
- 财务预算：接口保留，`payment_amount` 恒为 0，不实现任何逻辑。

### 3.9 RiskStateManager（打桩）

```python
@dataclass(frozen=True)
class RiskProfile:
    session_id: str
    cumulative_risk_score: float = 0.0
    recent_tags: list[str] = field(default_factory=list)
    denied_count: int = 0
    approval_count: int = 0

class RiskStateManager(Protocol):
    def get_session_risk(self, session_id: str) -> RiskProfile: ...
    def update_after_decision(self, session_id: str, proposal: ActionProposal, decision: Decision) -> None: ...
```

**MVP 决策**（关闭问题 #7）：`InMemoryRiskStateManager`，`session_id == task_id`（§3.1 约定），纯内存、任务结束即弃；实现仅统计 `denied_count` / `approval_count` 供审计事件引用，不参与判定。

### 3.10 ApprovalRequest 与 ApprovalRecord

```python
@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    decision_id: str          # 强绑定触发审批的 Decision，不允许为空
    call_id: str
    task_id: str
    agent_id: str
    tool_name: str
    arguments_masked: dict    # 审批视图参数：仅掩码凭证类字段，决策必需字段（收件人/路径/正文）保持可见，见 §7.4
    reason: str               # R2 给出的升级理由
    requester_id: str         # 任务发起者 user_id
    approver_id: str          # 被指派的审批人
    created_at: datetime

@dataclass(frozen=True)
class ApprovalRecord:
    request_id: str
    decision_id: str          # 回指，强绑定
    verdict: Literal["approve", "deny"]   # 两态，escalate 移出 MVP
    approver_id: str
    comment: str
    decided_at: datetime

class R0Delegate(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> ApprovalRecord: ...
    # v1.1：async 接口 + 实现立即返回。语义仍是"返回即终局"，MVP 无超时概念；
    # post-MVP 异步化（通知→人工→回调）只改实现，调用方代码不变
```

**决策说明**（关闭问题 #22、#24、#25）：

- **接口 async、语义同步**（v1.1）：`request_approval` 签名是 async，但 MVP 实现在方法内立即构造并返回最终决定——返回即终局，因此 MVP 不存在"审批超时"；
- **冲突校验位置**：由 **Checkpoint 在组装 `ApprovalRequest` 时**校验 `approver_id != requester_id` 且 `approver_id != agent_id`，校验失败直接 deny（而不是把非法请求发给审批人）；
- **两态收敛**：无 escalate；需要升级到 R0 的场景在 MVP 中表现为 deny + 审计报告提示。

---

## 4. 基础设施层（05 草案缺失，本文补齐）

MVP 的基础设施层由五个组件构成，全部为**最小本地实现**：无外部服务依赖、断网可用、零云调用。

### 4.1 ConfigLoader：配置加载器

**职责**：进程启动时一次性加载全部静态配置，构造不可变的运行时配置对象。运行期不监听、不热更新（MVP 决策：改配置 = 重启进程，换取实现简单与行为确定）。

```python
class ConfigLoader(Protocol):
    def load(self, config_dir: str) -> AppConfig: ...

@dataclass(frozen=True)
class AppConfig:
    agents: dict[str, Agent]
    profiles: dict[str, CapabilityProfile]
    mcp_servers: dict[str, MCPServerConfig]
    permission_rules: list[PermissionRule]      # §6.2
    masking_rules: MaskingRules                 # §7.4
    policy_dir: str                             # Rego 策略文件目录
    audit_log_path: str
    decision_log_path: str
```

**启动校验**（任一失败则拒绝启动，fail-closed）：

1. 每个 Agent 引用的 `profile_id` 必须存在；
2. 每个 Profile 的 `tools` 中工具名必须能在 `mcp_servers` 的工具映射表中找到；
3. `policy_dir` 下必须存在 `default.rego` 且能被 OPA 成功加载——启动时做一次空 input 试查询，**通过标准是"返回结构合法的 decision 对象（含默认拒绝的 deny）"，而不是返回 allow**；
4. 审计/决策日志目录可写；
5. （v1.1）`profiles.yaml` / `permission_rules.yaml` 中所有 glob 模式语法合法（用 `glob.translate` 试编译）；
6. （v1.1）`masking_rules.yaml` 中所有正则可编译（`re.compile` 通过）；
7. （v1.1）`approval.yaml` 中引用的每个 `approver` 都存在于 `agents.yaml` 的 `users` 中，且不等于任何 Agent 的 `agent_id`。

### 4.2 IdentityProvider：身份管理

**职责**：回答"这个 agent_id 是谁"。MVP 中**可信配置 = 本地 `agents.yaml` 文件**，由部署者写入，进程启动时加载，`agent_id` 预先分配，不支持动态申领（关闭问题 #1、#31）。

```python
class IdentityProvider(Protocol):
    def get_agent(self, agent_id: str) -> Agent | None: ...
    def get_user(self, user_id: str) -> str | None: ...   # 返回显示名；MVP 仅校验存在性
```

**安全约束**：R2 判定身份时，**只信任 IdentityProvider 加载的 Agent 对象**；`ActionProposal.agent_id` 仅用于交叉校验（不一致即 deny），防止 R1 伪造身份。

`agents.yaml` 示例：

```yaml
agents:
  - agent_id: researcher_001
    name: Research Assistant
    profile_id: research_assistant_v1
    owner_id: zhang_manager
users:
  - user_id: alice
    display_name: Alice
  - user_id: zhang_manager
    display_name: 张经理
```

### 4.3 PolicyStore：策略存储

**职责**：Rego 策略文件的持久化与版本标识（关闭问题 #32）。

```python
class PolicyStore(Protocol):
    def policy_path(self, name: str) -> str: ...     # 返回 rego 文件路径
    def current_version(self) -> str: ...            # 全部策略文件内容连接后的 SHA-256 前 12 位
    def list_policies(self) -> list[str]: ...
```

**MVP 决策**：

- 存储形态：本地目录 `policies/`，文件名即策略名；
- **版本号 = 内容哈希**：`current_version()` 对目录内所有 `.rego` 文件按文件名排序后连接内容取 SHA-256，写入每个 Decision 与 AuditEvent；不实现显式版本号管理与回滚（post-MVP）；
- **加密降级声明**：MVP 策略为**明文文件**，仅依赖文件系统权限（建议 `chmod 600`）保护。00 文档要求的加密存储 + 受信解密属于 post-MVP；本降级已在 §1.2 明示，不得在对外材料中声称 MVP 具备策略加密能力。

### 4.4 AuditStore：审计存储

**职责**：append-only 写入审计事件，提供篡改检测（关闭问题 #27、#29、#33）。

```python
class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None: ...
    def verify_chain(self) -> bool: ...              # 重放全量日志校验哈希链
    def query_by_trace(self, trace_id: str) -> list[AuditEvent]: ...
```

**MVP 实现**：`JsonlAuditStore`

- 每个事件一行 JSON，追加写入 `audit.jsonl`；
- **哈希链**：每个事件含 `seq`（单调递增序号）与 `prev_hash`（上一行规范 JSON 的 SHA-256），首行 `prev_hash = "GENESIS"`；
- `verify_chain()` 重放全文件校验链完整性——这使"不可篡改"从信任假设升级为**可检测篡改**（检测删除/改写/插入，但不防御攻击者整体重写文件，该防御 post-MVP 引入签名/WORM 存储）；
- 查询用 `query_by_trace` 全文件扫描（MVP 数据量小，可接受）。

### 4.5 DecisionStore：判定存储

**职责**：持久化已签发的 Decision 使用记录，提供跨重启的防重放（关闭偏离 D11 的遗留风险）。

```python
class DecisionStore(Protocol):
    def is_call_id_seen(self, call_id: str) -> bool: ...        # v1.1：全局唯一性检测（不再按 task_id 分区）
    def is_decision_used(self, decision_id: str) -> bool: ...   # Decision 重放检测（全局）
    def record_proposal(self, task_id: str, call_id: str) -> None: ...   # task_id 仍记录，供审计关联查询
    def record_decision_use(self, decision_id: str) -> None: ...
```

**MVP 实现**：`JsonlDecisionStore`，追加写 `decisions.jsonl`，启动时全量加载进内存 set。

**v1.1 说明**：`call_id` 检测从 per-task 升级为全局。这是防御纵深加固——即使 `call_id` 仅为动作标识符（授权凭证是 `decision_id`，本就全局单次使用），全局唯一也让"动作标识"语义更严格、更易推理，且无合法用例受损。

### 4.6 配置文件总览

```
config/
├── agents.yaml              # Agent 与用户身份（§4.2）
├── profiles.yaml            # CapabilityProfile（§3.3）
├── mcp_servers.yaml         # MCP server 连接与工具映射（§6.5）
├── permission_rules.yaml    # 权限组合静态规则（§6.2）
├── masking_rules.yaml       # 参数掩码规则（§7.4）
└── approval.yaml            # R0-delegate 打桩配置（§7.5）
policies/
└── default.rego             # 主策略（§6.3）
data/
├── audit.jsonl              # 审计日志（运行时生成）
└── decisions.jsonl          # 判定记录（运行时生成）
```

`profiles.yaml` 示例（研究助手）：

```yaml
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
      read_file:
        allowed: true
        allowed_args:
          path: ["/data/kb/**"]
        max_calls_per_task: 20
      write_file:
        allowed: true
        allowed_args:
          path: ["/data/output/**"]
        max_calls_per_task: 5
      send_email:
        allowed: true
        require_approval: true
        allowed_args:
          to: ["*@company.com"]
        max_calls_per_task: 1
```

---

## 5. R1 执行循环（05 草案缺失，本文补齐）

### 5.1 Planner 协议：动作如何被规划出来

05 草案只定义了 R1 的职责，没有定义 R1 如何运转。本文补齐：**R1 = 执行循环 + Planner + 轻量分类器**。其中"下一步做什么"由 Planner 决定：

```python
@dataclass(frozen=True)
class PlannedAction:
    """Planner 输出的动作草案。v1.1：不含 call_id/task_id/agent_id——
    这些身份字段由 run_task 框架在组装 ActionProposal 时统一生成/填充，
    Planner（尤其是 LLMPlanner）无权自定身份标识。"""
    tool_name: str
    arguments: dict[str, Any]
    reason: str = ""

class Planner(Protocol):
    """决定 R1 的下一个动作。这是非治理组件，不参与任何判定。"""
    def next_action(
        self,
        task: Task,
        agent: Agent,
        observations: list[ToolResult],   # 已完成动作的结果，供规划下一步
    ) -> PlannedAction | None: ...         # 返回 None 表示任务完成
```

**MVP 提供两个实现**：

| 实现 | 说明 | 用途 |
|---|---|---|
| `ScriptedPlanner`（默认） | 从 YAML 脚本读取预定义动作序列，逐一发出 | 演示与测试：行为完全确定、可复现，治理链路的每个分支都可精确触发 |
| `LLMPlanner`（可选） | 调用 LLM，根据任务描述 + observations 生成下一步动作；输出经 JSON Schema 强校验，非法输出视为任务结束 | 演示真实 Agent 被治理的效果 |

`LLMPlanner` 的输出契约（v1.1 补齐，评审#7）——LLM 被指示输出且仅输出如下 JSON，经 Schema 校验后映射为 `PlannedAction`：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["action", "tool_name", "arguments"],
  "properties": {
    "action":    {"enum": ["call_tool", "finish"]},
    "tool_name": {"type": "string"},
    "arguments": {"type": "object"},
    "reason":    {"type": "string", "maxLength": 500}
  },
  "additionalProperties": false
}
```

映射规则：`action="finish"` → 返回 `None`（任务结束）；`action="call_tool"` → `PlannedAction(tool_name, arguments, reason)`；任何校验失败 → 返回 `None` 并记审计（`metadata.planner_error`），**不重试、不纠错**（避免 LLM 输出整形逻辑成为攻击面）。

**决策说明**：

- **治理正确性不依赖 Planner 的智能程度**：无论 Planner 输出什么，都必须经过 R2 判定。ScriptedPlanner 是默认值，因为 MVP 要验证的是治理链路，不是 Agent 的智能；
- `LLMPlanner` 的 token 消耗计入 BudgetLedger（见 §3.8）；
- Planner **只能看到掩码前的工具结果**——注意：工具结果内容本身可能含敏感信息，这是 R1 的合法工作输入；敏感信息的管控点是"哪些结果能被 `send_email` 带出去"，由策略与权限组合规则负责，而不是蒙蔽 R1。

`scripted_plan.yaml` 示例：

```yaml
steps:
  - tool_name: web_search
    arguments: {query: "OpenAI 企业合规 争议"}
    reason: "调研公开资料"
  - tool_name: read_file
    arguments: {path: "/data/kb/ai_compliance_checklist.md"}
    reason: "读取内部合规清单"
  - tool_name: write_file
    arguments: {path: "/data/output/summary.md", content: "..."}
    reason: "写出摘要"
  - tool_name: send_email
    arguments: {to: "zhang@company.com", subject: "AI 合规调研摘要", body: "..."}
    reason: "发送报告给张经理"
```

### 5.2 执行循环定义（关闭问题 #8）

```python
async def run_task(task: Task, agent: Agent, runtime: Runtime) -> None:
    audit = runtime.audit_store
    profile = runtime.profiles[agent.profile_id]  # CapabilityProfile 对象
    observations: list[ToolResult] = []

    audit.append(event(task, action="task_start"))
    try:
        while True:
            # 1. 规划下一步动作：Planner 只产出草案，框架组装 ActionProposal 并统一生成 call_id
            planned = runtime.planner.next_action(task, agent, observations)
            if planned is None:
                break
            proposal = ActionProposal(
                task_id=task.task_id,
                call_id=str(uuid4()),        # 框架统一生成，Planner 无权自定（v1.1，评审#8）
                agent_id=agent.agent_id,
                tool_name=planned.tool_name,
                arguments=planned.arguments,
                task_context=task.description[:200],
                reason=planned.reason,
            )

            # 2. R1 自检：轻量分类器，只产出信号
            signal = runtime.classifier.classify(task, agent, proposal, profile)
            proposal = replace(proposal,
                               risk_level=signal.risk_level,
                               risk_tags=signal.tags)
            audit.append(event(task, proposal, action="propose", signal=signal))

            # 3. R2 判定
            decision = await runtime.checkpoint.evaluate(task, agent, proposal)
            audit.append(event(task, proposal, action="evaluate", decision=decision))

            # 4. 需要审批 → 请求 R0-delegate（async 接口，MVP 实现立即返回）
            if decision.verdict == "require_approval":
                record = await runtime.r0_delegate.request_approval(
                    runtime.checkpoint.build_approval_request(decision, proposal, task)
                )
                audit.append(event(task, proposal, action="approve" if record.verdict == "approve" else "deny",
                                   record=record))
                decision = runtime.checkpoint.finalize_after_approval(decision, record)

            # 5. 执行或被拦截，结果都进入 observations 供下一步规划
            if decision.verdict in ("allow", "modify"):
                result = await runtime.checkpoint.forward(proposal, decision)
            else:
                result = blocked_result(proposal, decision)
            observations.append(result)
            audit.append(event(task, proposal, action="execute", result=result))
    finally:
        audit.append(event(task, action="task_end"))
```

**关键不变量**：

1. R1 代码路径中**不存在任何对 MCPGateway / MCP client 的直接引用**——`forward` 是 Checkpoint 的方法；
2. 被 deny 的动作也会产生一条 `blocked` 状态的 ToolResult 进入 observations，让 Planner 能感知失败并调整（ScriptedPlanner 忽略，LLMPlanner 可利用）；
3. `task_end` 在 `finally` 中，保证异常时审计链闭合。

### 5.3 task_context 的生成规则（关闭问题 #5）

`ActionProposal.task_context` = `Task.description` 的**纯截断**（前 200 字符，不做摘要、不做改写）。理由：任何摘要/重构都引入一个需要被信任的转换组件，截断是无损可验证的。`task_context` 进入 Rego input 供策略阅读（例如"上下文中包含'密码'则升级"类规则），而 `description` 原文不进 Rego。

---

## 6. R2 Checkpoint：完整判定算法

### 6.1 evaluate() 判定流水线（步骤顺序固定，任一失败即短路）

```
输入：Task, Agent, ActionProposal
输出：Decision

步骤 0  身份交叉校验
        proposal.agent_id == agent.agent_id，且 agent 来自 IdentityProvider
        失败 → deny("identity mismatch")

步骤 1  重放检测
        DecisionStore.is_call_id_seen(call_id)        # v1.1：全局唯一性检测
        已见过 → deny("duplicate call_id")
        否则 record_proposal(task_id, call_id)

步骤 2  Profile 与工具存在性
        profile = profiles[agent.profile_id]；tool_name 在 profile.tools 中且 allowed == true
        失败 → deny（默认拒绝原则，不进入 Rego）

步骤 3  调用次数上限
        per-task 历史中该工具的成功执行次数 >= max_calls_per_task → deny("call limit exceeded")

步骤 4  预算
        BudgetLedger.check_and_reserve(task_id, cost)
        失败 → deny("budget exceeded")

步骤 5  权限组合分析（Permission Interaction）
        PermissionInteractionAnalyzer.check(current=proposal, history=本任务已成功执行的动作)
        命中且 action=deny → 立即 deny（短路）
        命中且 action=require_approval → 记录 pending_approval 标记，继续步骤 6（不短路）

步骤 6  主策略查询（OPA/Rego）
        按 §6.3 的 input schema 构造 input_doc，查询 PolicyEngine
        OPA 不可用 → deny("policy engine unavailable")（fail-closed，锁定，见偏离 D15）

步骤 7  汇总输出 Decision

        裁决优先级（显式声明，v1.1 评审#6）：
            deny > require_approval > modify > allow
        即：任一来源（组合规则 / Rego / 前置检查）产出更严格的裁决时，覆盖更宽松的裁决；
        组合规则可以否决或升级 Rego 的 allow，反之不行。

        Rego 判定为 deny → deny
        否则 pending_approval 或 Rego 判定 require_approval → require_approval
        否则按 Rego 判定（allow / modify）
        写入 policy_hits、policy_version（PolicyStore.current_version()）、profile_version、
        expires_at（按 §3.6 分档）、max_uses=1；require_approval 时填 escalation_target
```

**决策说明**：

- 步骤 2 的"默认拒绝"前置在 Rego 之前：未在 Profile 中声明的工具连策略查询都不发起，减少攻击面与审计噪音；
- 步骤 5 在步骤 6 之前，但只有 deny 短路：组合规则命中 require_approval 时仍须继续走 Rego，保证 Rego 的硬拒绝（如外部收件人 deny）不会被审批绕过——**deny 永远优先于 require_approval**；
- `require_approval` 的后续：`R1 执行循环`（§5.2 步骤 4）调用 `build_approval_request`（此时做 §3.10 的冲突校验）→ await 拿到 `ApprovalRecord` → `finalize_after_approval` 把 approve 转成新 Decision（verdict=allow，继承原 modified_args 与 policy_hits，追加 `policy_hits += ["approval:granted"]`），deny 转成 verdict=deny 的 Decision。**新 Decision 的 `expires_at` 按 allow 档从审批通过时刻重新起算 5 分钟**（v1.1 自审#3：审批耗时不得占用执行授权窗口，否则异步化后审批完成时授权可能已过期）。

### 6.2 PermissionInteractionAnalyzer：静态规则表

```python
class PermissionInteractionAnalyzer(Protocol):
    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],   # 本任务内已成功执行的动作（Checkpoint 内存维护，见偏离 D12）
    ) -> PermissionRule | None: ...       # 返回命中的规则；无命中返回 None
```

`permission_rules.yaml` 格式（关闭问题 #18、#19；命中后动作按规则显式声明执行，见偏离 D8）：

```yaml
rules:
  - id: contact_plus_external_email
    description: "读取联系人/知识库后向外部邮箱发信 = 数据外泄风险"
    when_all:
      - history_tool: read_file
        history_arg_match: {path: "**/*contact*"}
      - current_tool: send_email
        current_arg_not_match: {to: "*@company.com"}
    action: require_approval        # 可选值：deny | require_approval
    reason: "检测到 读取内部资料→外发邮件 组合"
  - id: kb_read_plus_external_email
    description: "读取知识库后向外部邮箱发信"
    when_all:
      - history_tool: read_file
        history_arg_match: {path: "/data/kb/**"}
      - current_tool: send_email
        current_arg_not_match: {to: "*@company.com"}
    action: deny
    reason: "内部知识库内容禁止外发"
```

**决策说明**：MVP 用 YAML 独立配置层（不并入 Rego），因为组合规则需要引用"历史动作"这一 Rego input 之外的上下文，由 Python 侧评估更直接；匹配语义与 `allowed_args` 一致（POSIX glob，浅层匹配）。

### 6.3 主策略：Rego input schema 与 default.rego（修正偏离 D5）

**Python 与 Rego 的契约**——`input_doc` 的 schema 固定为：

```json
{
  "tool_name": "send_email",
  "arguments": {"to": "zhang@company.com"},
  "risk_level": "high",
  "risk_tags": ["external_communication"],
  "task_context": "截断后的任务描述",
  "agent": {"agent_id": "researcher_001", "owner_id": "zhang_manager"},
  "profile": {"tools": {"send_email": {"require_approval": true, "allowed_args": {"to": ["*@company.com"]}}}}
}
```

`policies/default.rego`（OPA 1.x / Rego v1 语法）：

```rego
package loop_controller.tool_permission

import rego.v1

# 默认拒绝
default decision := {"verdict": "deny", "reason": "no policy allows this action", "policy_hits": ["default_deny"]}

# ---- 通用规则：critical 风险信号必须人工审批（分类器信号经由制度生效）----
decision := {"verdict": "require_approval", "reason": "critical risk signal requires approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["critical_signal_gate"]} if {
    input.risk_level == "critical"
}

# ---- web_search ----
decision := {"verdict": "allow", "reason": "web search allowed", "policy_hits": ["web_search_allow"]} if {
    input.tool_name == "web_search"
    input.risk_level != "critical"
}

# ---- read_file：限目录 ----
decision := {"verdict": "allow", "reason": "read within allowed directories", "policy_hits": ["read_file_allow"]} if {
    input.tool_name == "read_file"
    input.risk_level != "critical"
    some pattern in input.profile.tools.read_file.allowed_args.path
    glob.match(pattern, ["/"], input.arguments.path)
}

# ---- write_file：限目录 ----
decision := {"verdict": "allow", "reason": "write within allowed directories", "policy_hits": ["write_file_allow"]} if {
    input.tool_name == "write_file"
    input.risk_level != "critical"
    some pattern in input.profile.tools.write_file.allowed_args.path
    glob.match(pattern, ["/"], input.arguments.path)
}

# ---- send_email：白名单内收件人 → 按 Profile 决定是否审批；白名单外 → deny ----
decision := {"verdict": "require_approval", "reason": "send_email requires human approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["send_email_approval"]} if {
    input.tool_name == "send_email"
    input.risk_level != "critical"
    input.profile.tools.send_email.require_approval == true
    recipient_allowed
}

decision := {"verdict": "allow", "reason": "internal email allowed", "policy_hits": ["send_email_allow"]} if {
    input.tool_name == "send_email"
    input.risk_level != "critical"
    input.profile.tools.send_email.require_approval == false
    recipient_allowed
}

decision := {"verdict": "deny", "reason": "recipient outside allowed patterns", "policy_hits": ["send_email_deny_external"]} if {
    input.tool_name == "send_email"
    not recipient_allowed
}

recipient_allowed if {
    some pattern in input.profile.tools.send_email.allowed_args.to
    glob.match(pattern, [], lower(input.arguments.to))
}
```

**决策说明**：

- `policy_hits` 由 Rego 显式输出（偏离 D13），Checkpoint 原样透传进 Decision；
- 工具是否 `allowed`、是否超 `max_calls_per_task` 已在 §6.1 步骤 2/3 由 Python 前置处理，Rego 不再重复检查；Rego 专注于**参数级策略**（目录、收件人、风险门控）；
- Rego v1 语法（`if` 关键字、`import rego.v1`）兼容 OPA 1.x。

### 6.4 PolicyEngine：OPA HTTP sidecar

```python
class PolicyEngine(Protocol):
    def evaluate(self, package: str, input_doc: dict) -> dict: ...   # 返回 decision 对象
```

**MVP 实现**：`OPAPolicyEngine`，本地 `opa run --server --addr localhost:8181 policies/`，HTTP 查询，超时 2s。**任何异常（连接失败、超时、非 2xx、返回体缺 verdict）一律 deny**（fail-closed，无缓存、无降级开关，锁定）。断网可用性：OPA 为本地进程，满足原则 6。

### 6.5 MCPGateway：工具映射与授权转发

```python
class MCPGateway(Protocol):
    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]: ...   # 按 Profile 过滤
    async def call_tool(self, tool_name: str, arguments: dict, call_id: str) -> ToolResult: ...
```

`mcp_servers.yaml`（关闭问题 #17/#34，硬编码发现）：

```yaml
servers:
  filesystem:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data/kb", "/data/output"]
    transport: stdio
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio
tool_mapping:
  read_file:   {server: filesystem, mcp_name: read_text_file, cost_per_call: 500}
  write_file:  {server: filesystem, mcp_name: write_file, cost_per_call: 300}
  web_search:  {server: brave,      mcp_name: brave_web_search, cost_per_call: 200}   # MVP 可用 Mock
  send_email:  {server: email_mock, mcp_name: send_email, cost_per_call: 800}
```

`cost_per_call`（v1.1，评审#3）：该工具每次调用的 token 成本估算值，供 BudgetLedger 计费（见 §3.8）；为估算占位，非真实计量。

**决策说明**：`list_tools` 返回的是**按 Profile 过滤后**的工具列表——这同时解决 MCP 默认暴露全部工具的问题和 LLMPlanner 的 context 占用问题；规范化工具名到真实工具名的映射只存在于本组件。

### 6.6 forward() 执行前校验与 modify 复核（关闭问题 #13）

```
输入：ActionProposal, Decision

1. decision.call_id == proposal.call_id，否则抛异常
2. decision.verdict ∈ {allow, modify}，否则抛异常
3. now < decision.expires_at，否则抛异常（过期授权作废）
4. DecisionStore.is_decision_used(decision_id) == False，否则抛异常（防重放）
5. record_decision_use()                                    ← 先记账再执行，防并发复用
6. 若 verdict == modify：
   a. modified_args 必须存在且只改动了"参数值"（工具名不可改）
   b. 轻量复核：对 modified_args 重新执行 Profile 参数白/黑名单匹配
      复核失败 → 不执行，返回 ToolResult(status="blocked", error_code="modify_recheck_failed") + 审计
7. MCPGateway.call_tool(tool_name, effective_args, call_id)
8. BudgetLedger.commit（执行异常则 refund）
```

**决策说明**：

- 复核放在 **Checkpoint.forward**（PEP 职责），而非 MCPGateway——MCPGateway 是哑代理，所有治理语义集中在 Checkpoint；
- **运行时假设（v1.1 显式声明，评审#2）**：MVP 运行模型为**单进程 asyncio 事件循环，同一时刻不存在并行的 `forward` 调用**。步骤 4-5（检查 + 记账）之间无 await 点，在该假设下是原子的。**若未来引入多 worker / 多进程部署，DecisionStore 必须升级为原子语义**（如 SQLite `INSERT OR FAIL` 或分布式锁），该升级是部署形态变更的前置条件，已列入 §9.3；
- `record_decision_use` 先于执行记账：即使 MCP 调用失败，该 Decision 也视为已消耗（防"失败后重试同一授权"变成重放通道）。

---

## 7. R3 审计与 R0-delegate

### 7.1 AuditEvent：最小日志单元（统一定义）

```python
@dataclass(frozen=True)
class AuditEvent:
    schema_version: str = "1.0"
    event_id: str             # UUID
    seq: int                  # 全局单调递增序号，由 AuditStore 分配
    prev_hash: str            # 上一条事件规范 JSON 的 SHA-256；首条为 "GENESIS"
    trace_id: str             # == Task.task_id
    session_id: str           # == Task.session_id
    call_id: str | None       # 动作级 ID；task_start/task_end 为空
    timestamp: datetime       # timezone-aware UTC
    actor_type: Literal["agent", "user", "r0_delegate", "system", "checkpoint"]
    actor_id: str
    action: Literal["task_start", "propose", "classify", "evaluate",
                    "approve", "deny", "execute", "task_end"]
    target: str | None        # tool_name 或 "checkpoint"
    decision: Literal["allow", "deny", "modify", "require_approval"] | None
    args_hash: str | None     # 规范 JSON 的 SHA-256
    hash_algo: str = "sha256" # 升级 HMAC 时改此字段，schema 不变（偏离 D6）
    args_mask: dict | None    # 掩码后的结构化参数
    reason: str | None
    policy_version: str | None
    profile_version: str | None
    metadata: dict = field(default_factory=dict)   # 分类器 suggestion、审批 comment 等
```

### 7.2 采样策略（关闭问题 #26）

**MVP 决策：全量记录，不采样**。研究助手场景单任务事件量 < 100 条，采样纯属多余复杂度。`AuditEvent` 不预留采样字段；post-MVP 引入 Audit Hook Controller 时在写入侧加过滤器即可，schema 不受影响。

### 7.3 args_hash 计算规则

`args_hash = SHA-256(canonical_json(arguments))`，其中 canonical_json = 键排序、无空白、UTF-8、`ensure_ascii=False`。**已知局限**（沿用 05 草案的安全说明）：SHA-256 对低熵值可被字典攻击；生产环境必须升级为 HMAC，升级时改 `hash_algo` 字段并轮换计算逻辑，历史日志可识别、可双写校验。升级触发条件：任何涉及真实 PII 的部署即触发（关闭问题 #30）。

### 7.4 参数掩码规则（关闭问题 #28）

`masking_rules.yaml`：

```yaml
field_name_blacklist:        # 字段名匹配（不区分大小写，含子串匹配）
  - password
  - passwd
  - secret
  - token
  - api_key
  - authorization
  - credential
value_patterns:              # 值模式匹配（正则）
  - name: bearer_token
    pattern: "Bearer\\s+\\S+"
  - name: email_address
    pattern: "[\\w.+-]+@[\\w-]+\\.[\\w.]+"
    replacement: "***@***"   # 缺省替换为 "***"
masking_applies_to:          # v1.1：分级掩码（自审#1）
  audit_log:                 # 落盘审计日志：两类规则全部应用
    - field_name_blacklist
    - value_patterns
  approval_request:          # 审批视图：只应用凭证类黑名单
    - field_name_blacklist   # 决策必需字段（收件人、路径、正文）必须对审批人可见，否则审批形同虚设
```

**决策说明**：

- 掩码发生在**写入审计/发出审批请求之前**，由 Checkpoint/R1 调用统一的 `Masker` 工具函数完成；原始参数只存在于进程内存与 MCP 调用中，永不落盘；
- **分级掩码（v1.1 自审#1）**：审计日志应用全部规则；审批请求只应用 `field_name_blacklist`（审批人无需看到密码/token 原文），**不应用 `value_patterns`**——审批人必须能看到真实收件人、文件路径和邮件正文才能做出审批判断。审批人属 R0 授权角色，其视图权限高于审计落盘；
- **超长字段截断（v1.1 自审#2）**：审计落盘时，单个字段值超过 500 字符（如 `write_file.content`、邮件正文）只存 `{sha256, length, 前 100 字符预览}`，防止审计日志膨胀与产出物被整体抄写；
- **已知宽松声明（v1.1 评审#5）**：`value_patterns` 的正则 MVP 阶段偏宽松（可能误伤含 `@` 的非邮箱字符串），方向是**宁宽勿漏**——过度掩码只损失审计可读性，漏掩码则是 PII 落盘事故；生产环境再收紧。

### 7.5 R0-delegate 打桩（关闭问题 #23）

`approval.yaml`：

```yaml
approvers:
  default: zhang_manager
rules:
  - tool_name: send_email
    approver: zhang_manager
    behavior: approve        # approve | deny；演示用固定行为
```

```python
class ConfigR0Delegate:
    """打桩实现：async 接口，方法内立即按配置返回（v1.1）。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalRecord:
        behavior = self._lookup_behavior(request.tool_name)   # 配置驱动
        return ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,     # 强绑定，不允许为空
            verdict="approve" if behavior == "approve" else "deny",
            approver_id=request.approver_id,
            comment=f"MVP stub: configured behavior={behavior}",
            decided_at=datetime.now(timezone.utc),
        )
```

**决策说明**：MVP 形态 = **R2 内的打桩 hook**（对应 00 文档方案 A）；v1.1 起接口为 async，未来替换为云端 SaaS 或私有化授权服务时只需改实现（加入通知/轮询/回调与超时语义），`run_task` 等调用方代码不变（post-MVP）。

---

## 8. 端到端流程（纯工具调用版）

```
User（入口层 CLI）
  │ 创建 Task(task_id=t1, session_id=t1, user_id=alice, agent_id=researcher_001)
  ▼
R1 run_task loop
  │ Planner.next_action → ActionProposal(call_id=c1, tool=web_search, ...)
  │ RuleBasedClassifier → RiskSignal(low) → 写回 proposal.risk_level/risk_tags
  ▼
R2 Checkpoint.evaluate
  │ 身份校验 → 重放检测 → Profile 检查 → 次数上限 → 预算 → 组合规则 → OPA
  │ → Decision(allow, policy_hits=["web_search_allow"], expires_at=+5min)
  ▼
R2 Checkpoint.forward → 五重校验 → MCPGateway.call_tool → MCP Server
  ▼
ToolResult(success) → observations → Planner 规划下一步 …
  │
  │ ActionProposal(call_id=c4, tool=send_email, args={to: "zhang@company.com"})
  ▼
R2 evaluate → Rego: require_approval（send_email_approval）
  ▼
R1 → ConfigR0Delegate.request_approval（async 接口，立即返回）→ ApprovalRecord(approve)
  ▼
finalize_after_approval → Decision(allow) → forward → Mock 邮件 server 记录
  ▼
R3 AuditStore：全量 JSONL + 哈希链（task_start → propose → evaluate → approve → execute → task_end）
```

---

## 9. 工程落地

### 9.1 目录结构

```
loop-controller/
├── config/
│   ├── agents.yaml
│   ├── profiles.yaml
│   ├── mcp_servers.yaml
│   ├── permission_rules.yaml
│   ├── masking_rules.yaml
│   ├── approval.yaml
│   └── scripted_plan.yaml
├── policies/
│   └── default.rego
├── src/loop_controller/
│   ├── __init__.py
│   ├── models.py                # §3 全部抽象（唯一 Schema 来源）
│   ├── planner.py               # Planner / ScriptedPlanner / LLMPlanner
│   ├── classifier.py            # RuleBasedClassifier
│   ├── checkpoint.py            # evaluate / forward / build_approval_request / finalize_after_approval
│   ├── policy_engine.py         # OPAPolicyEngine
│   ├── permission_interaction.py
│   ├── risk_state.py
│   ├── budget.py
│   ├── r0_delegate.py           # ConfigR0Delegate
│   ├── masker.py                # 参数掩码
│   ├── mcp_gateway.py
│   ├── runtime.py               # Runtime 组装 + run_task 执行循环
│   ├── infra/
│   │   ├── config_loader.py
│   │   ├── identity.py
│   │   ├── policy_store.py
│   │   ├── audit_store.py       # JsonlAuditStore + 哈希链
│   │   └── decision_store.py    # JsonlDecisionStore
│   └── mocks/
│       └── email_server.py      # Mock send_email MCP server
├── examples/
│   └── research_agent_example.py
├── tests/
│   ├── test_policy_engine.py    # Rego 策略用例
│   ├── test_checkpoint.py       # 判定流水线各分支
│   ├── test_audit_store.py      # 哈希链 + 篡改检测
│   ├── test_masker.py
│   ├── test_r0_delegate.py
│   └── test_e2e_research_agent.py
└── pyproject.toml
```

### 9.2 验收标准（MVP 完成的定义）

| # | 验收项 | 通过条件 |
|---|---|---|
| A1 | 正常链路 | 示例任务端到端跑通：search → read → write → email（审批 approve），每步有审计事件 |
| A2 | 默认拒绝 | 申报一个 Profile 中不存在的工具 → deny，且不发起 OPA 查询 |
| A3 | 目录越权 | `read_file` 路径在 `/data/kb/**` 之外 → deny（`read_file` 无 allow 规则命中） |
| A4 | 外部收件人 | `send_email` 收件人非 `*@company.com` → deny（`send_email_deny_external`） |
| A5 | 审批链路 | 内部收件人 `send_email` → require_approval → ConfigR0Delegate 按配置 approve/deny，行为可切换 |
| A6 | 审批冲突 | 构造 `approver_id == requester_id` 的请求 → 组装阶段直接 deny，审计记录原因 |
| A7 | 重放防护 | 同一 `call_id` 二次申报 → deny；同一 `decision_id` 二次 forward → 抛异常；**重启进程后仍有效** |
| A8 | 授权过期 | 人为构造过期 Decision → forward 抛异常 |
| A9 | 组合风险 | 先读 `/data/kb/**` 再发外部邮件 → 权限组合规则命中 deny |
| A10 | 预算熔断 | 按各工具 `cost_per_call` 估算累计，超出 `max_budget_token` → deny("budget exceeded") |
| A11 | OPA 故障 | 杀掉 OPA 进程后申报任何动作 → 一律 deny("policy engine unavailable") |
| A12 | 审计完整性 | 篡改 `audit.jsonl` 任意一行 → `verify_chain()` 返回 False |
| A13 | 掩码 | 参数含密码字段/邮箱 → 审计日志中只有掩码值，全文检索不到原文；审批请求中凭证类字段被掩码、但收件人与正文对审批人可见 |
| A14 | 断网运行 | 断开外网（web_search 用 Mock）→ 全流程可跑通 |

### 9.3 post-MVP 路线图（不在本期承诺内）

1. `inter_agent` 治理与多 Agent 委托链；2. Earned Authority（Fixed Ceiling 生效）；3. 策略加密存储（HSM/TEE/密钥代理）；4. 审批异步化：真实 UI/IM 通知 + 超时语义 + escalate 到 R0；5. 审计采样与 Audit Hook Controller；6. `hash_algo` 升级 HMAC；7. 财务支付预算与熔断；8. 沙箱接入；9. 用户脱敏上报与官方策略库；10. Open-Core 意图控制接口；11. 低代码模板与更新机制；12. 风险画像（STM/LTM）参与判定；13.（v1.1 补登）**真实 token 计量**：LLMPlanner usage 上报 + 工具结果长度折算，取代 `cost_per_call` 估算；14.（v1.1 补登）**多 worker/多进程部署**：DecisionStore 升级原子语义（SQLite `INSERT OR FAIL` 或分布式锁）为前置条件。

---

## 附录 A：40 个未确定问题的逐条决策表

> 状态说明：**【定稿】** 本文已给出完整定义，可直接开发；**【打桩】** MVP 给出简化实现，语义明确；**【移出】** 明确不在 MVP，已列入 §9.3。

| # | 问题 | 决策 | 状态 |
|---|---|---|---|
| 1 | Agent `agent_id` 分配与加载 | 预先分配于 `agents.yaml`，ConfigLoader 启动时加载，运行期不可变（§4.2） | 【定稿】 |
| 2 | Task 创建者与传递路径 | 入口层（CLI runner/示例脚本）创建，直接传给 R1；基础设施不参与（§3.1） | 【定稿】 |
| 3 | Task 定义两版矛盾 | 统一为含 `agent_id` + `session_id`，MVP 约定 `session_id == task_id`（§3.1，偏离 D2） | 【定稿】 |
| 4 | Agent-Profile 绑定 | 一对一静态绑定，写于 `agents.yaml`，运行期不可切换（§3.2） | 【定稿】 |
| 5 | description → task_context 转换 | 纯截断前 200 字符；description 不进 Rego input（§5.3） | 【定稿】 |
| 6 | history 维护与传递 | Checkpoint 内存维护 per-task 历史，仅含已成功执行的动作，任务结束即弃（§6.1 步骤 5） | 【定稿】 |
| 7 | RiskStateManager 持久化 | 纯内存打桩，仅统计计数供审计引用，不参与判定（§3.9） | 【打桩】 |
| 8 | R1 执行循环缺失 | §5.2 定义完整循环；Planner 协议 + ScriptedPlanner 默认（§5.1） | 【定稿】 |
| 9 | tags/suggestion 下游消费 | tags → `ActionProposal.risk_tags` → Rego input；suggestion → 仅审计 metadata（§3.5） | 【定稿】 |
| 10 | risk_level "参考"的具体机制 | 进入 Rego input，策略可匹配；默认策略 critical→require_approval（§3.5、§6.3） | 【定稿】 |
| 11 | 分类器 critical vs 策略 allow 冲突 | R2 唯一权威；critical 经 Rego 制度通道生效，分类器无否决权（§3.5） | 【定稿】 |
| 12 | policy_hits 生成责任 | OPA 在 Rego 中显式输出，Checkpoint 透传（§6.3，偏离 D13） | 【定稿】 |
| 13 | modify 复核位置与回退 | Checkpoint.forward 内复核；失败 → blocked + 审计，不执行（§6.6） | 【定稿】 |
| 14 | OPA 故障策略 | 锁定 fail-closed，任何异常一律 deny，无缓存/降级开关（§6.4） | 【定稿】 |
| 15 | Policy Compiler 表达形式 | Rego DSL（OPA），遵循 00 文档 MVP 决策记录（§6.3） | 【定稿】 |
| 16 | 策略加密 MVP 实现 | 降级：明文文件 + `chmod 600` + 内容哈希版本；已明示为降级项（§4.3） | 【打桩】 |
| 17 | Earned Authority | 不实现，`fixed_ceiling` 保留但固定为空（§3.3） | 【移出】 |
| 18 | Permission Interaction 形式化 | YAML 静态规则表 + glob 匹配（§6.2），遵循 00 文档 MVP 决策记录 | 【定稿】 |
| 19 | 组合规则 YAML vs Rego | YAML 独立配置层，由 Python 评估（需访问历史上下文）；不并入 Rego（§6.2） | 【定稿】 |
| 20 | Budget 额度配置管理 | 额度静态来自 CapabilityProfile；消耗按工具 `cost_per_call` 估算（v1.1），内存计数（§3.8、§6.5） | 【打桩】 |
| 21 | 双预算熔断差异 | 支付预算恒 0 不实现；token 超支直接 deny，无降级档位（§3.8） | 【打桩】 |
| 22 | 审批超时行为 | async 接口 + 立即返回，语义同步、不存在超时；真实异步化属 post-MVP（§3.10、§7.5） | 【定稿】 |
| 23 | R0-delegate 技术形态 | R2 内 hook（00 文档方案 A）；v1.1 起接口为 async，实现可平滑替换（§7.5） | 【定稿】 |
| 24 | 审批人冲突校验位置 | Checkpoint 组装 ApprovalRequest 时校验，失败即 deny（§3.10） | 【定稿】 |
| 25 | escalate 流程 | 收敛为 approve/deny 两态，escalate 移出（§3.10，偏离 D3） | 【移出】 |
| 26 | 审计采样策略 | 全量记录，不采样；post-MVP 在写入侧加过滤（§7.2） | 【定稿】 |
| 27 | 审计存储形态 | JSONL 追加写（§4.4） | 【定稿】 |
| 28 | 参数掩码规则 | 字段名黑名单 + 值模式正则，配置化（§7.4） | 【定稿】 |
| 29 | 审计不可篡改实现 | SHA-256 哈希链 + `verify_chain()`，可检测篡改；整体重写防御 post-MVP（§4.4） | 【定稿】 |
| 30 | args_hash 升级触发条件 | 任何涉及真实 PII 的部署即触发 HMAC 升级；`hash_algo` 字段已预留（§7.3） | 【定稿】 |
| 31 | Identity 接口 | `IdentityProvider.get_agent/get_user`，静态配置（§4.2） | 【定稿】 |
| 32 | Policy Store 接口 | `policy_path / current_version / list_policies`，版本=内容哈希（§4.3） | 【定稿】 |
| 33 | Audit Store 接口 | `append / verify_chain / query_by_trace`（§4.4） | 【定稿】 |
| 34 | MCP Server 发现机制 | 硬编码 `mcp_servers.yaml` + 显式工具映射（§6.5） | 【定稿】 |
| 35 | 沙箱交互边界 | MVP 不启用沙箱，不定义接口（§1.2） | 【移出】 |
| 36 | MVP 范围确认 | 确认：单 Agent + 纯工具调用 + R1/R2/R3 + R0-delegate 打桩（§1.1） | 【定稿】 |
| 37 | Open-Core 意图接口形态 | 移出 MVP；R0Delegate 协议预留可替换性（§7.5） | 【移出】 |
| 38 | 用户上报数据流 | MVP 仅本地沉淀，无上报（§1.2） | 【移出】 |
| 39 | 多 Agent 委托预留 | 删除 `type` 字段，不做预留（§3.4，偏离 D1） | 【移出】 |
| 40 | 低代码模板 | MVP 附 `profiles.yaml` + `default.rego` 作为内置示例模板，无更新机制（§4.6） | 【打桩】 |

---

## 附录 B：缩略语与引用

- 本文档上位架构：`00_r0r3_architecture.md` v0.3（R0-R3 四层治理模型）
- 本文档取代：`05_mvp_core_abstractions.md` 草案 v0.3
- OPA / Rego v1：https://www.openpolicyagent.org/
- MCP 规范：https://modelcontextprotocol.io/specification/
