# Loop Controller 方案 v1.2 增补：治理哲学、上下文模型与外来 Agent 接入

> **文档定位**：本文档是《Loop_Controller_MVP方案_纯工具调用_v1.1》的**增量修订**。
>
> - **未提及的章节，以 v1.1 为准**（v1.1 仍是已发布 v0.1.0 代码的基线依据）；
> - **与 v1.1 冲突的条款，以本文为准**（冲突点集中在 §3.1 session 约定、§3.9 RiskStateManager、§6.3 Rego input schema，本文 §3 逐一处理）；
> - 本文新增的"治理哲学"（第 1 章）与"三套上下文模型"（第 2 章）为全新章节，未来并回主文档。
>
> **修订动因**：MVP 验证的是"我们自己养的 Agent 被治理"；v1.2 回答两个被推迟的架构问题——① 复杂架构的真实 Agent（多层规划、harness 各异）如何被治理；② 外来 Agent 如何以最低成本接入我们的安全规范。
>
> **状态**：v1.2，待核心维护者评审；其中第 3 章进入 P1 开发，第 4 章进入 P2 开发（见 §5 路线图）
> **最后更新**：2026-08-17

---

## 0. 修订清单（TL;DR）

| # | 内容 | 类型 | 本文章节 |
|---|---|---|---|
| R1 | 治理哲学：三道防线映射、"阻止损害而非阻止欺骗"、三级接入成本、三个企业侧 | 新增（对 00 原则的提炼） | §1 |
| R2 | 三套上下文模型；自报信息单向使用原则（only tighten, never loosen） | 新增（00 无此内容） | §2 |
| R3 | session 概念修订：`session_id == task_id` 约定作废；RiskStateManager 真用化，会话级风险进入 Rego 判定 | **修订 v1.1**（§3.1/§3.9/§6.3） | §3 |
| R4 | PEP 独立化：MCP proxy 部署形态；外来 Agent 接入模型 | 新增（落地 00 原则 9 + 05 决策表演进项） | §4 |
| R5 | post-MVP 路线图重排：L2 会话级判定提前至 P1；PEP 独立化入 P2 | 替换 v1.1 §9.3 排序 | §5 |

**与 00 文档（R0-R3 架构）的兼容性**：本文不推翻 00 的任何决策；两处是 00 原则的部署落地（"R2 唯一授权出口" → proxy + 网络隔离），两处是新场景产生的新原则（单向使用、三套上下文）。逐条对照见 §6。

---

## 1. 治理哲学（新增章节）

### 1.1 三道防线：自家 Agent 与外来 Agent

00 文档开篇即声明 R0-R3 与企业内控三道防线对应。v1.2 把这条映射显式展开：

| 防线 | 角色 | 自家 Agent 场景 | 外来 Agent 场景 |
|---|---|---|---|
| 第一道（自我控制） | R1 自检：轻量分类器、主动申报 | 我们开发，可信任其存在 | **他人开发，存在性与质量均未知** |
| 第二道（独立风控） | R2 控制层：策略判定、授权出口 | 企业控制 | 企业控制 |
| 第三道（独立审计） | R3 审计层 | 企业控制 | 企业控制 |

**核心结论**：Agent 开发者按规范自建安全能力（prompt 注入检测、输出过滤等）属于第一道防线，**有价值但不可替代第二、三道**。企业的安全承诺必须由企业自己控制的 R2/R3 背书，因为：

- 外来 Agent 的第一道防线是否存在、是否被绕过，企业无法验证；
- 供应链投毒、模型被注入、开发者疏忽，都会让第一道防线失效；
- 因此 v1.1 的全部"不信任设计"（R1 不持有工具通道、申报信息自报属性、审计企业侧存储）在外来 Agent 场景下**从设计偏好升级为强制要求**。

### 1.2 核心理念：阻止损害，而非阻止欺骗

> **我们不阻止 Agent 被欺骗，我们阻止欺骗变成损害。**

Prompt 注入攻击的是 Agent 的"脑子"（推理过程），那是 Agent 框架与模型厂商的战场；Loop Controller 的战场是**副作用边界**：注入载荷要造成真实损害，必须经由工具调用（读敏感数据、外发、写文件、支付），而那个时刻必然撞在 R2 上。

这条理念是 00 原则 2（Runtime 强制优先于模型自律）的对外表述版本，建议作为演示与文档的统一口径。

### 1.3 接入成本三级（北极星：改一行配置）

外来 Agent 的接入成本决定生态采纳率。三级形态：

| 形态 | Agent 侧改动 | 定位 |
|---|---|---|
| **网关模式**：Agent 的 MCP client 从真实 server 地址改指向 Loop Controller proxy | **改一行配置，代码零改动** | **北极星目标**，外来 Agent 标准接入方式（见 §4） |
| **SDK 嵌入**：harness 增加 wrapper/middleware，内部调用 Checkpoint API | 数行代码 | 自研 harness 紧集成选项 |
| **框架收养**：按 Loop Controller 的 Planner/run_task 重写 Agent | 重写 Agent | **明确拒绝**——治理框架不做成 Agent 框架 |

类比：服务网格 sidecar——应用不改代码，流量被接管后获得鉴权、限流、审计。Loop Controller 是 Agent 工具调用流量的治理平面。

### 1.4 三个"企业侧"与直连隔离

外来 Agent 接入时，以下三项**必须部署在企业信任域内**，缺一不可：

1. **工具执行通道**：proxy/gateway 运行于企业基础设施；
2. **策略与判定**：Rego/Profile/组合规则由 R0 制定、R2 执行，Agent 无策略读取权；
3. **审计存储**：日志落盘于 Agent 无法触及的存储。

**配套强制要求（v1.2 新增，00 未覆盖）**：真实 MCP server 必须**网络层隔离直连路径**（localhost 绑定或内网 ACL），使 Agent 所在网络命名空间只能到达 proxy。否则"R2 是唯一授权出口"（00 原则 9）只是逻辑声明，可被直连绕过——本条把它变成物理强制。

---

## 2. 三套上下文模型（新增章节）

### 2.1 问题来源

复杂架构的 Agent（ReAct、plan-and-execute、多层 loop）内部必然有自己的上下文管理（思维链、状态机、对话历史）。治理框架与这些 harness 共存时，"上下文归谁管"必须先回答，否则会出现两种错误设计：要求 Agent 上交内部状态（不可信也不可行），或治理层放弃上下文（退化为静态 ACL）。

### 2.2 模型定义：三套上下文，各归各管

| 上下文 | 内容 | 管理者 | 治理层的立场 |
|---|---|---|---|
| **Agent 内部上下文** | 思维链、计划、记忆、中间推理 | Agent/harness 自己 | **不索取、不依赖**。理由：不可信（R1 可伪造）、不必要（判定依据是可观测行为）、不可行（各 harness 格式不一） |
| **治理上下文** | 动作历史、会话风险分、数据标签、拒绝计数 | **R2 自建自管**（唯一可信来源） | 判定的正式输入。R2 之所以能做到，是因为每个工具调用都由它亲自转发、亲自记账 |
| **申报上下文** | `reason`、`task_context`、plan 摘要等自报字段 | Agent 申报，R2 收下 | **单向使用**（见 §2.3） |

**归属原则**：治理上下文的存储与计算必须在 R2 控制层——判定的输入必须与判定者同域，否则等于再次信任外部状态。入口层只负责创建 Task 与分配 session（§3.1），不承载判定状态。

### 2.3 铁律：自报信息单向使用（only tighten, never loosen）

> 自报信息（reason、plan 摘要、自我风险声明等）**只能让判定更严格，绝不能更宽松**。

- **允许**：自报内容命中敏感特征 → 触发 require_approval 或加深审计采样；
- **禁止**：自报内容成为放行依据——"Agent 说这是正常工作需要"不能把 deny 变成 allow，也不能降低任何阈值；
- **用途**：供审批人阅读、供 R3 审计解释、供事后追溯。

**理由**：R1 可能恶意或被注入，其自我陈述是零信任成本的信息。内控语言：第一道防线的自我声明，必须经第二道防线独立验证后才产生效力——而验证的依据是可观测行为，不是更多陈述。

**工程落地**：`ActionProposal.reason` / `task_context` 进入 Rego input 时，策略编写规范强制约定——这些字段只允许出现在"升级为 require_approval/deny"的规则条件中，禁止出现在 allow 规则中。该约定通过策略评审与 `opa test` 用例强制执行。

### 2.4 对"多层架构 Agent"的回答

- 单层 ReAct、plan-and-execute、树搜索、多轮反思……**全部无需特殊处理**：中间思考不产生外部副作用，治理层不关心；思考落到动作的那一刻，以扁平的 `ActionProposal` 流撞在 R2 上；
- 治理锚点是 **tool call 这个 syscall 边界**（05 文档引用 Governed MCP 的结论），与 Agent 内部架构无关；
- 唯一例外：Agent 之间发生**委托/指令传递**（子 Agent、多 Agent 协作）——那属于 00 文档定义的"Agent 交互"治理，仍在 P3（§5），单 Agent 多层思考**不算** Agent 交互，此边界不得模糊。

---

## 3. Session 概念修订与 RiskStateManager 真用化（修订 v1.1）

### 3.1 Session 新约定（修订 v1.1 §3.1）

**v1.1 约定作废**：`session_id == task_id`（单任务单会话）——当时为简化 RiskStateManager 而设，但它使"跨任务的行为累积"无法表达。

**v1.2 新约定**：

- **session = 同一 `(user_id, agent_id)` 的连续任务流**。入口层创建 Task 时查询该 (user, agent) 是否存在活跃 session：存在则复用其 `session_id`；不存在或上一任务结束超过 **30 分钟**（可配置）则开新 session；
- `Task` 结构不变（`session_id` 字段已存在，仅语义与分配者变化）；分配职责在入口层，规则判定只消费 `session_id`；
- R3 审计的 `trace_id` 仍等于 `task_id`（不变），`session_id` 提供第二层聚合维度——审计可按 session 回放"这个会话里发生了什么"。

**工程落地补充**：新增 `SessionManager` 挂在 `Runtime` 上，对外暴露 `runtime.create_task(user_id, agent_id, description)`。`run_task(task, ...)` 保留，但进入时必须校验 `task.session_id` 存在、session 仍活跃、且 session 绑定的 `(user_id, agent_id)` 与 Task 一致——不一致则 fail-closed。

### 3.2 RiskStateManager 真用化（修订 v1.1 §3.9）

**从打桩升级为真实判定输入**。v1.1 中它"仅统计计数供审计引用，不参与判定"；v1.2 起，其输出进入 Rego input。

**算分规则（确定性，遵守 00 原则 3——不用大模型、轻量、断网可用）**：

| 事件 | 分值变动 |
|---|---|
| Decision = deny | +0.20 |
| 分类器信号 = critical | +0.30 |
| require_approval 后被审批人 deny | +0.10 |
| require_approval 后被 approve | +0.05（仍积累，获批不代表无风险） |
| 成功执行的 low 风险动作 | -0.05（下限 0） |
| 每条新事件写入时 | 全量分数 × 0.9 衰减（下限 0，上限 1.0） |

**持久化升级**：session 需跨任务存活，RiskStateManager 从"纯内存、任务结束即弃"升级为**本地文件持久化**（JSONL 追加 + 启动重放，与 DecisionStore 同构），遵守原则 7（断网可用）。

**`RiskProfile` 结构不变**（v1.1 §3.9 字段够用），仅 `cumulative_risk_score` 从装饰变为判定输入。

**工程落地补充**：
- 新增 `risk_state_path` 配置（如 `./data/risk_state.jsonl`），不写死隐藏路径；启动时检查父目录可写、文件可追加，并 JSONL 重放恢复状态；最后一行若因崩溃不完整，忽略并记 WARNING，但不阻止启动。
- 引入 `RiskStateStore` Protocol（`append_event`、`load_all`），P1 初版为单 writer，Runtime 内用 `asyncio.Lock` 串行化写入；多 worker 原子性留到与 DecisionStore 同一方案解决，不单独搞文件锁。
- `recent_tags` 为 bounded FIFO，最多保留 10 条风险证据（`deny` / `critical` / `require_approval` / `approval_denied` / `approval_granted`）；`allow` / `low_risk_success` 不进入；分数衰减只作用于 `cumulative_risk_score`，不影响 tags；session 结束后内存可释放，JSONL 事件保留用于重放与审计。

### 3.3 Rego input 扩展与默认策略新增（修订 v1.1 §6.3）

`input_doc` 新增顶层字段 `session_risk`：

```json
{
  "tool_name": "...", "arguments": {...}, "risk_level": "...", "risk_tags": [...],
  "task_context": "...", "agent": {...}, "profile": {...},
  "session_risk": {
    "score": 0.65,
    "threshold": 0.6,
    "denied_count": 3,
    "recent_tags": ["data_access"],
    "session_id": "..."
  }
}
```

`default.rego` 新增规则（置于通用门控区）：

```rego
# ---- 会话风险门控：异常累积 → 一律升级人工审批 ----
decision := {"verdict": "require_approval", "reason": "session risk score above threshold",
             "escalation_target": input.agent.owner_id, "policy_hits": ["session_risk_gate"]} if {
    input.session_risk.score >= input.session_risk.threshold
    input.risk_level != "critical"   # critical 门控更严，避免重复命中
}
```

**阈值 0.6 为默认值，进 `CapabilityProfile`**：

```python
class CapabilityProfile(BaseModel):
    session_risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
```

不同岗位可配置不同阈值；`build_policy_input` 必须显式把 `score`、`threshold`、`recent_tags`、`session_id` 传入 Rego，并补 Python ↔ Rego input contract test，防止字段不一致导致 default deny。阈值不写死在 Rego 里。

**与裁决优先级的兼容**：`session_risk_gate` 只产生 require_approval，服从 v1.1 的优先级总表（deny > require_approval > modify > allow）——它能把 allow 升级为审批，但不能救回任何 deny，与 §2.3 单向使用原则方向一致。

### 3.4 这套设计回答了什么

L2 上线后，"同一动作在不同上下文中不同判定"首次成立：连续被拒 5 次的 Agent 第 6 次申请时，会话分已超过阈值，原本 allow 的动作被升级为人工审批。**系统长出了记忆，且记忆的台账由 R2 自己记**——这就是对"治理上下文 R2 自建"（§2.2）的最小完整实现。更深的 L3（数据血缘 taint 传播、跨 session 行为基线）仍在 P2，见 §5。

---

## 4. PEP 独立化与外来 Agent 接入（新增演进章节）

### 4.1 两种部署形态

| | 内嵌形态（现状，v0.1.x） | Proxy 形态（P2 目标） |
|---|---|---|
| PEP 位置 | `Checkpoint.forward` 在 R2 进程内 | 独立进程/服务，对外暴露 MCP server 接口 |
| Agent 形态 | 我们养的 R1（run_task 循环） | 任意 harness（LangGraph / Agents SDK / 自研） |
| 接入成本 | 收养进框架 | **改一行 MCP endpoint 配置** |
| 判定位置 | 同进程 | **仍在 R2 域**——proxy 只做转发与封装，判定调用 Checkpoint 的判定接口 |

### 4.2 Proxy 形态架构

```
外来 Agent（任意 harness）
   │  MCP 协议（stdio 或 streamable HTTP）
   ▼
LC Proxy（PEP：独立 MCP server 进程）
   │  1. 接收 tools/list → 按该 Agent 的 Profile 过滤后返回
   │  2. 接收 tools/call → 封装为 ActionProposal 提交 Checkpoint.evaluate
   │     （task_context 由部署方模板生成；reason 允许 Agent 附带，按 §2.3 单向使用）
   ▼
Checkpoint（PDP，R2 域内） → Decision
   ▼
LC Proxy：allow/modify → 转发真实 MCP server；deny → 以 MCP 错误返回
   │
   └─ 全部事件写入企业侧 AuditStore
```

**关键约束**：

- **身份**：外来 Agent 的 `agent_id` 由企业预先分配（接入登记时签发），以 proxy 侧的连接凭证（token/mTLS）绑定——**Agent 自报的 agent_id 一律不信**（v1.1 §4.2 安全约束在 proxy 形态的延伸）；
- **不可绕过**：真实 MCP server 网络层只对 proxy 可达（§1.4 直连隔离）；
- **断网可用**：proxy 本地部署，判定链不依赖任何云服务（原则 7）；
- **治理对象不变**：proxy 只治理 `tool_call`；Agent 与用户的对话、内部推理不经过 proxy。

**工程落地补充**：P2 Proxy 形态下，Checkpoint 应包装为独立决策服务（HTTP JSON 先行，gRPC 后续按需），对外暴露 `/v1/...` 版本化 API，schema 与现有 `ActionProposal` / `Decision` 对齐。Proxy 不能自行 allow，超时或不可达时 fail-closed。

身份字段在 Proxy 形态下必须与企业连接凭证绑定，Agent 自报一律不信：
- `agent_id`、`user_id`：来自 MCP 连接凭证与 Proxy 配置映射（API key / mTLS / stdio 启动绑定）；
- `session_id`：由 Proxy 侧 `SessionManager` 基于权威 `(user_id, agent_id)` 创建，沿用 30 分钟 gap 规则；
- `task_id`：Proxy 创建治理侧 synthetic task；外来 Agent 自报的 task 标识只能作为 `declared_task_id` 元数据进入审计；
- `call_id`：由 Proxy 或 Checkpoint 生成，必须全局唯一并进入 DecisionStore 防重放，不能由外来 Agent 提供。

### 4.3 复杂 harness 接入矩阵

| Agent 架构 | 接入方式 | 需要的特殊处理 |
|---|---|---|
| 单层 ReAct | proxy 网关 | 无 |
| plan-and-execute | proxy 网关 | 无——plan 不产生副作用，执行期动作逐个申报 |
| 多轮反思 / 树搜索 | proxy 网关 | 无 |
| 带子 Agent 委托 | **暂不支持** | inter_agent 治理（P3）；当前应通过策略拒绝子 Agent 派生类工具 |

### 4.4 Open-Core 定位

**LC Proxy 属于开源工程层**（与 Checkpoint、AuditStore 同级），不是闭源的意图控制接口。理由：proxy 是通用工程能力，开源可审计是外来 Agent 场景的信任前提——"让别人的 Agent 接入你的黑盒"在技术上就不成立。闭源边界不变：意图控制接口服务与官方策略库/风险案例库内容（00 §2）。

---

## 5. 修正版 post-MVP 路线图（替换 v1.1 §9.3 的排序）

> 重排理由：① 治理层的上下文感知（L2）是框架完整性问题，不是优化项，提前到 P1；② 外来 Agent 接入（PEP 独立化）是生态成立的前提，入 P2；③ 其余按依赖与成本排序不变。

### P0：信任加固（约 1 周）→ v0.2.0

真实 token 计量收尾；HMAC 升级（`hash_algo` 切换）；审计链 seal 记录。**退出标准**：KNOWN_LIMITATIONS 安全局限从 5 条减至 2 条（L3、L4）。

**HMAC key 来源**：只从环境变量读取，不进配置文件，P0 不接 KMS。配置里只存环境变量名（如 `audit_hmac_key_env: "LOOP_CONTROLLER_AUDIT_HMAC_KEY"`）。key 至少 32 字节随机熵，hex 或 base64 编码；`hash_algo = hmac-sha256` 时环境变量缺失或格式非法则启动 fail-closed。P0 使用单个部署级 root key，事件链与 seal 记录通过 HKDF 或带 label 的 HMAC 做域分离。审计事件带 `key_id` 字段为轮换留口。

### P1：真实可用 + 上下文判定（约 3-4 周）→ v0.3.0

| 项 | 内容 |
|---|---|
| **审批异步化** | 真实通知（IM/邮件 webhook）+ 超时语义 + escalate 到 R0 打桩；async 接口已就位 |
| **L2 会话级风险判定** | 本文 §3 全部：session 新约定、RiskStateManager 真用化（规则算分 + 本地持久化）、Rego `session_risk` 扩展、阈值可配 |
| 低代码模板 | 3-5 个岗位模板 + 默认风险建议 |
| 多 worker DecisionStore | SQLite `INSERT OR FAIL`，摘掉 L3 |

**退出标准**：第一个真实用户跑真实任务；"同一动作在不同会话风险下判定不同"可演示。

### P2：合规与生态接入（约 1-2 月）→ v0.4.0

| 项 | 内容 |
|---|---|
| **PEP 独立化** | 本文 §4 全部：LC Proxy、身份凭证绑定、直连网络隔离方案 |
| **策略加密** | 先 KMS/密钥代理，HSM/TEE 按需——外来 Agent 接入后此需求从降级项转为刚需 |
| Earned Authority | 任务完成后临时提权，Fixed Ceiling 生效 |
| **L3 上下文深度** | 数据血缘（taint 传播）、跨 session 行为基线 |
| 财务支付预算 | 有支付场景才做 |

### P3：生态与商业闭环（持续）→ v1.0

多 Agent 委托治理（inter_agent）；用户脱敏上报 + 官方策略库；意图控制接口（Q4 备案）；沙箱按需接入。

---

## 6. 与 00 文档的对照（评审依据）

### 6.1 继承（完全遵循）

原则 2（Runtime 强制）、原则 3（R2 轻量确定性——session 算分为纯规则）、原则 5（人类只在 R0/R0-delegate 决策）、原则 6/7（本地部署、断网可用）、原则 9（R2 唯一授权出口）、Open-Core 边界、Agent 交互与工具调用分界。

### 6.2 落地（00 有原则，v1.2 给形态）

- 原则 9"唯一授权出口" → §4 proxy 形态 + §1.4 直连网络隔离（从逻辑声明升级为物理强制）；
- 决策表"R1 主动申报" → §2.2 申报上下文的定位；
- 05 决策表"PEP 可拆分为独立 MCP Client Proxy" → §4 正式立项。

### 6.3 扩展（00 没有的新原则）

- §1.2 "阻止损害，而非阻止欺骗"（原则 2 的对外表述）；
- §1.3 接入成本三级与北极星标准；
- §2 三套上下文模型；
- §2.3 自报信息单向使用原则。

### 6.4 冲突处理

仅一处实质冲突：v1.1 §3.1 的 `session_id == task_id` 约定 → 本文 §3.1 作废并给出新约定。v1.1 其余条款不变。

---

## 7. 触发条件（各阶段何时启动）

- **P0**：立即（发布后随时可做）；
- **P1**：v0.2.0 发布后；审批异步化与 L2 可并行两条线；
- **P2**：出现第一个外来 Agent 接入需求或真实部署涉及敏感策略资产时；
- **P3**：社区/客户出现多 Agent 协作场景，或 Q4 备案窗口临近时。
