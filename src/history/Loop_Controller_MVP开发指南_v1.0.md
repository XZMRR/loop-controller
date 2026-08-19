# Loop Controller MVP 开发指南（v1.0）

> **文档定位**：本文档是《Loop Controller MVP 完备方案：纯工具调用版 v1.1》（下称"方案"）的配套执行文档。方案回答"做什么、为什么"，本指南回答"**按什么顺序做、每一步做什么、怎么算做完**"。
>
> **适用读者**：执行 MVP 开发的工程师。
>
> **使用方式**：按迭代顺序执行；每个任务卡标注了产出物、前置依赖、对应验收标准（§9.2 的 A1-A14）和预估工作量；每个迭代结束做一次里程碑演示。
>
> **最后更新**：2026-08-16

---

## 0. 总览

### 0.1 三迭代计划

| 迭代 | 目标 | 对应验收 | 预估工作量 |
|---|---|---|---|
| **迭代 0：环境地基** | 工具链可用、骨架可 import | —（技术就绪） | 0.5-1 人日 |
| **迭代 1：骨架跑通** | 端到端最小闭环：申报 → Rego 判定 → MCP 转发 → 返回 | A1、A2、A3、A4、A14 | 5-7 人日 |
| **迭代 2：安全边界** | 审批、防重放、组合规则、预算、fail-closed | A5、A6、A7、A8、A9、A10、A11 | 4-5 人日 |
| **迭代 3：审计闭环** | 哈希链、掩码、全量验收自动化 | A12、A13 + 全量回归 | 3-4 人日 |

**合计约 13-17 人日**（单人全栈口径；多人并行时的拆分见 §5.4）。

### 0.2 三条全局纪律（先于一切任务）

1. **`models.py` 是唯一 Schema 来源**。方案 §3 的每个 dataclass 在 `models.py` 中且仅在此处定义一次；任何模块需要数据结构改动，先改 models.py 并检查全文引用，禁止在业务模块里私自加字段。
2. **所有 deny 必须带 reason**。`Decision.reason` 不允许为空字符串——这是审批可读性和审计可解释性的底线，Code Review 时一票否决。
3. **测试与实现同 PR 提交**。每个任务卡的"完成定义"包含对应测试通过；不允许"先实现后补测试"。

---

## 1. 迭代 0：环境地基（0.5-1 人日）

### T0.1 工具链安装

| 工具 | 版本要求 | 用途 | 验证命令 |
|---|---|---|---|
| Python | ≥ 3.12 | 主语言 | `python --version` |
| OPA | ≥ 1.0 | Rego 策略引擎（本地 sidecar） | `opa version` |
| Node.js / npx | ≥ 20 | 运行官方 filesystem MCP server | `npx --version` |
| Git | 任意 | 版本管理 | — |

OPA 安装（Linux/macOS）：

```bash
curl -L -o opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x opa && sudo mv opa /usr/local/bin/
```

### T0.2 项目初始化

```bash
mkdir loop-controller && cd loop-controller
git init
# 按方案 §9.1 建立目录骨架
mkdir -p config policies data src/loop_controller/{infra,mocks} examples tests
```

`pyproject.toml` 最小依赖：

```toml
[project]
name = "loop-controller"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.0",        # Schema 校验
    "httpx>=0.27",          # OPA HTTP 查询（async，不用 requests，见 §6 坑 #3）
    "pyyaml>=6.0",          # 配置加载
    "mcp>=1.0",             # MCP client（stdio）
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

### T0.3 冒烟验证

```bash
# 1. OPA 能起服务
opa run --server --addr localhost:8181 &
curl -s localhost:8181/health   # 期望 200

# 2. filesystem MCP server 能起
mkdir -p /data/kb /data/output
echo "# AI 合规 checklist 测试内容" > /data/kb/ai_compliance_checklist.md
npx -y @modelcontextprotocol/server-filesystem /data/kb /data/output &
# 起得来即可，Ctrl+C 关掉；正式接入由 MCPGateway 管理生命周期

# 3. Python 依赖可装
pip install -e ".[dev]"   # 或 uv sync
```

**迭代 0 完成定义**：三条冒烟验证全部通过；目录骨架与方案 §9.1 一致；空 `models.py` 可被 import。

---

## 2. 迭代 1：骨架跑通（5-7 人日）

> **里程碑演示**： ScriptedPlanner 驱动示例任务端到端跑通；越权读取、外部收件人被 Rego 拒绝；全程断外网可运行（A1/A2/A3/A4/A14）。

### T1.1 `models.py`：全量 Schema（1 人日）

**内容**：将方案 §3 的全部抽象落成 Pydantic v2 模型：`Task`、`Agent`、`ToolPermission`、`CapabilityProfile`、`ActionProposal`、`RiskSignal`、`Decision`、`Tool`、`ToolResult`、`BudgetCost`、`RiskProfile`、`ApprovalRequest`、`ApprovalRecord`、`AuditEvent`、`PlannedAction`。

**实现要点**：

```python
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime, timezone

class Task(BaseModel):
    model_config = ConfigDict(frozen=True)   # 对应方案的 frozen 语义

    task_id: str
    session_id: str
    user_id: str
    agent_id: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

- 不可变修改用 `model_copy(update={...})`（Pydantic 等价于 `dataclasses.replace`），§5.2 循环里写回 `risk_level/risk_tags` 时用到；
- `Literal` 枚举值照抄方案，不要自创取值；
- `Decision.expires_at` 的分档逻辑（allow/modify 5min、require_approval 15min、deny 立即过期）写在 Checkpoint 的工厂方法里，不写在模型里。

**测试**：`tests/test_models.py`——每个模型的必填/默认值、frozen 语义（赋值抛错）、枚举非法值拒绝。

**前置**：T0。 **验收映射**：无直接验收项，但阻塞一切。

### T1.2 `infra/config_loader.py` + 全部配置文件（1 人日）

**内容**：实现 `ConfigLoader.load(config_dir)` + `AppConfig`；按方案 §4.6 编写六份配置文件 + `scripted_plan.yaml` 的完整可用样例。

**实现要点**：

- **7 条启动校验逐条实现**（方案 §4.1），每条一个独立函数、一条独立测试：
  1. `profile_id` 存在性；2. 工具名映射存在性；3. `default.rego` 存在且 OPA 试查询返回**结构合法的 deny**（注意：通过标准不是 allow，方案 §4.1 已强调）；4. 日志目录可写；5. glob 试编译；6. 正则试编译；7. approver 存在性；
- 校验 3 依赖 OPA 已启动——ConfigLoader 需要接收 OPA 地址参数，或由调用方保证启动顺序（推荐：启动脚本先起 OPA 再起主程序，见 T1.9）；
- 配置加载后构造的 `AppConfig` 必须不可变。

**测试**：`tests/test_config_loader.py`——每条校验一个正例 + 一个反例（共 14 个用例）。

**前置**：T1.1。 **验收映射**：间接受益全部。

### T1.3 `infra/identity.py` + `infra/policy_store.py`（0.5 人日）

**内容**：两个静态配置组件，接口照抄方案 §4.2 / §4.3。

**实现要点**：

- `PolicyStore.current_version()`：目录内 `.rego` 文件**按文件名排序**后连接内容取 SHA-256 前 12 位——排序不可省，否则版本号不稳定；
- `IdentityProvider` 只读，不提供任何写接口。

**测试**：版本号对内容变化敏感；`get_agent` 未知 ID 返回 None。

**前置**：T1.2。

### T1.4 `policy_engine.py` + `policies/default.rego`（1 人日，本迭代的技术高点）

**内容**：`OPAPolicyEngine`（方案 §6.4）+ 方案 §6.3 的 `default.rego` 全文落地。

**实现要点**：

- HTTP 客户端用 **httpx async**，超时 2s；**所有异常路径（连接失败、超时、非 2xx、返回缺 `verdict`）统一返回 deny**——fail-closed 逻辑写在一个地方，不要散落在 try/except 各处；
- `default.rego` 直接采用方案 §6.3 全文，注意 `import rego.v1` 与 `glob.match` 的分隔符参数（路径用 `["/"]`，邮箱用 `[]`）；
- **input_doc 构造函数单独成函数** `build_policy_input(proposal, agent, profile) -> dict`，按方案 §6.3 的 JSON schema 逐字段构造，这是 Python↔Rego 的唯一契约点，改 schema 只改这里。

**测试**：`tests/test_policy_engine.py`——对照方案 §6.3 每条规则一个用例（共 8 个：web_search allow、read_file 内/外、write_file 内/外、send_email 白名单审批/白名单放行/外部拒绝、critical 门控）；再加 OPA 未启动时返回 deny 的用例。

**前置**：T1.2。 **验收映射**：A2、A3、A4 的策略侧。

### T1.5 `mcp_gateway.py` + `mocks/email_server.py`（1 人日）

**内容**：`MCPGateway`（方案 §6.5）：stdio 拉起 MCP server 子进程、`tool_mapping` 双向映射、`list_tools` 按 Profile 过滤、`call_tool` 转发。`email_server.py`：一个 10 行级的 mock MCP server，`send_email` 只把参数写到 `data/sent_emails.jsonl`，返回成功。

**实现要点**：

- MCP stdio 子进程的**生命周期由 MCPGateway 独占管理**：启动时拉起、注册 `atexit` 清理；禁止业务代码触碰子进程句柄；
- `call_tool` 入参是规范化工具名，内部查 `tool_mapping` 翻译为 `mcp_name` 再发——映射表查不到直接抛错（配置校验 T1.2 已保证不会发生，这里是防御层）；
- mock email server 用 `mcp` 包的 server SDK 写，返回固定 `{"status": "queued"}`。

**测试**：真实拉起 filesystem server 读写 `/data/kb`；mock email 调用后 `sent_emails.jsonl` 有记录；`list_tools` 对研究助手 Profile 只返回 4 个工具。

**前置**：T1.2。 **验收映射**：A1 的执行侧、A14。

### T1.6 `checkpoint.py`：evaluate + forward 主干（1-1.5 人日）

**内容**：实现方案 §6.1 的步骤 0/1/2/6/7 与 §6.6 的 forward 校验 1/2/3/7/8。**迭代 1 暂缺**：步骤 1 的 DecisionStore（先用内存 set 占位，接口就位）、步骤 3/4（次数/预算，接口就位返回通过）、步骤 5（组合规则，接口就位返回 None）、审批分支（`require_approval` 暂时直接按 deny 处理并打日志——迭代 2 接 ConfigR0Delegate）。

**实现要点**：

- **占位组件必须实现真实 Protocol**（方案 §3 的接口），不允许 if/else 跳过——迭代 2 替换实现时调用方零改动；
- forward 的步骤 5（`record_decision_use` 先记账）在迭代 1 就按最终语义写好，只是存储是内存版；
- Decision 工厂方法集中处理 expires_at 分档（T1.1 提过）。

**测试**：`tests/test_checkpoint.py`——allow 全链路、deny（Profile 未声明工具）、modify 复核失败、call_id 不匹配、过期 Decision、decision 二次使用。

**前置**：T1.1、T1.3、T1.4、T1.5。 **验收映射**：A2、A3、A4 的判定侧。

### T1.7 `classifier.py` + `planner.py`（0.5 人日）

**内容**：`RuleBasedClassifier`（方案 §3.5 四条规则）；`Planner` 协议 + `ScriptedPlanner`（读 `scripted_plan.yaml` 逐条产出 `PlannedAction`）。`LLMPlanner` **迭代 3 再做**（降级优先级，见 §5.3）。

**测试**：四条分类规则；ScriptedPlanner 序列耗尽返回 None。

**前置**：T1.1。

### T1.8 `runtime.py` + `examples/research_agent_example.py`（0.5-1 人日）

**内容**：`Runtime` 组装所有组件；`run_task` 执行循环按方案 §5.2 全文实现（含 call_id 框架生成、task_context 截断、finally 里的 task_end）；示例脚本串起完整任务。**审计**：迭代 1 先用 `JsonlAuditStore` 的最小形态（只 append、无哈希链无掩码），事件结构用最终 `AuditEvent`——迭代 3 只加能力不改结构。

**实现要点**：启动脚本顺序 = 起 OPA → ConfigLoader.load（含试查询校验）→ 组装 Runtime → run_task。

**前置**：T1.6、T1.7。 **验收映射**：A1、A14。

### 迭代 1 出口检查

- [ ] A1：示例任务 search→read→write 全通（email 因审批未接，预期被临时 deny 并打日志，属已知行为）
- [ ] A2/A3/A4 通过
- [ ] A14：拔网线重跑 A1 全通
- [ ] 全量测试绿；里程碑演示完成

---

## 3. 迭代 2：安全边界（4-5 人日）

> **里程碑演示**：内部邮件走审批链路（approve/deny 可通过配置切换演示两种结局）；同一 call_id/decision_id 重放被拦且**重启进程后仍被拦**；过期授权被拦；读过知识库再发外部邮件被组合规则拦；预算耗尽被拦；杀掉 OPA 后全链路 fail-closed（A5-A11）。

### T2.1 `infra/decision_store.py`（0.5 人日）

**内容**：`JsonlDecisionStore`（方案 §4.5）替换迭代 1 的内存占位。

**实现要点**：

- 启动时全量加载进两个内存 set（`call_ids`、`used_decision_ids`），运行期"查内存 + 追加落盘"；
- `is_call_id_seen(call_id)` 是**全局**检测（v1.1 决策），`record_proposal` 落盘时带上 `task_id` 供审计关联；
- 落盘格式一行一 JSON：`{"type": "proposal", "task_id": ..., "call_id": ..., "ts": ...}` / `{"type": "decision_use", "decision_id": ..., "ts": ...}`。

**测试**：A7 专项——同一 call_id 二次申报 deny；同一 decision_id 二次 forward 抛异常；**kill 进程重启后重放仍被拦**（这是本任务的核心用例，不要漏）。

**前置**：T1.6。 **验收映射**：A7、A8。

### T2.2 `r0_delegate.py` + Checkpoint 审批分支（1 人日）

**内容**：`ConfigR0Delegate`（方案 §7.5）、`build_approval_request`、 `finalize_after_approval`，接通迭代 1 留下的审批断点。

**实现要点**：

- `build_approval_request` 内做**冲突校验**（`approver_id != requester_id` 且 `!= agent_id`），失败直接返回 deny Decision 且不发起审批（方案 §3.10）；
- `finalize_after_approval`：approve → 新 Decision（verdict=allow，继承 modified_args/policy_hits，追加 `policy_hits += ["approval:granted"]`，**expires_at 从审批通过时刻重新起算 5 分钟**，v1.1 自审#3）；deny → verdict=deny 的 Decision；
- `arguments_masked` 此时可先放原参数（掩码迭代 3 接入），但**字段结构按最终形态**；
- 演示配置：`approval.yaml` 里 `send_email` 的 `behavior` 支持 approve/deny 切换，里程碑演示各跑一次。

**测试**：approve→执行、deny→blocked、审批人=发起人→直接 deny 且 audit 有原因、escalation_target 路由到 `approval.yaml` 指定审批人。

**前置**：T1.6。 **验收映射**：A5、A6。

### T2.3 `permission_interaction.py`（0.5-1 人日）

**内容**：静态规则表引擎（方案 §6.2）：加载 `permission_rules.yaml`，`check(current, history)` 返回命中规则；接入 Checkpoint 步骤 5（含 pending_approval 不短路语义）与步骤 7 的 deny 优先汇总。

**实现要点**：

- `when_all` 是条件列表，**全部满足**才命中；`history_tool` 条件对 history 做存在性匹配（任一历史动作满足即可）；
- glob 匹配复用 `Masker` 之外的独立工具函数（与 `allowed_args` 同一套 POSIX glob 语义，抽成 `utils/globmatch.py` 共用）；
- 命中后的动作语义严格按方案 §6.1：deny 短路、require_approval 挂标记继续走 Rego——**写一条专项测试证明"组合规则 require_approval + Rego deny → 最终 deny"**（deny 优先原则，防审批绕过）。

**测试**：A9 专项 + deny 优先专项 + 无命中返回 None。

**前置**：T1.6。 **验收映射**：A9。

### T2.4 `budget.py` + `cost_per_call` 接入（0.5 人日）

**内容**：`InMemoryBudgetLedger`（check_and_reserve / commit / refund）+ 从 `tool_mapping` 读 `cost_per_call`。

**实现要点**：

- reserve 在 evaluate 步骤 4，commit 在 forward 成功返回后，refund 在 forward 抛异常路径——**三条路径写全**，漏 refund 会导致预算"只进不出"的假超支；
- 每工具成本从 `mcp_servers.yaml` 的 `cost_per_call` 读，查不到默认 100。

**测试**：A10 专项——构造小预算 Profile，连续调用直到 deny("budget exceeded")；refund 路径（mock 一次 MCP 抛异常）。

**前置**：T1.6。 **验收映射**：A10。

### T2.5 调用次数上限 + fail-closed 专项（0.5 人日）

**内容**：接通 Checkpoint 步骤 3（per-task 成功调用计数 vs `max_calls_per_task`）；A11 专项测试。

**实现要点**：步骤 3 的计数源是"本任务已成功执行的动作历史"（与组合规则共用同一份 history，方案 §6.1 口径）——不要在两处各维护一份。

**测试**：`send_email` 第二次调用被 deny("call limit exceeded")；杀 OPA 进程后任意申报 deny("policy engine unavailable")（A11）。

**前置**：T1.6、T2.3。 **验收映射**：A11、隐含的调用上限行为。

### 迭代 2 出口检查

- [ ] A5-A11 全绿（A7 含重启用例）
- [ ] 里程碑演示：审批 approve/deny 两种结局各演示一次
- [ ] 回归迭代 1 全部测试

---

## 4. 迭代 3：审计闭环（3-4 人日）

> **里程碑演示**：篡改 `audit.jsonl` 任意一行后 `verify_chain()` 检出失败；含密码/邮箱的参数在审计日志中检索不到原文，但审批视图中收件人与正文可见（A12/A13 + 全量回归）。

### T3.1 `infra/audit_store.py`：哈希链（1 人日）

**内容**：`JsonlAuditStore` 完整版——`append` 时分配 `seq`、计算 `prev_hash`、写入；`verify_chain()` 全量重放校验；`query_by_trace` 按 trace_id 过滤。

**实现要点**：

- 哈希输入必须是**规范 JSON**（键排序、无空白、UTF-8、`ensure_ascii=False`）——与 `args_hash` 共用同一个 canonical 序列化函数（放 `utils/canonical.py`），两处各写一份必然漂移；
- `seq` 从启动时读取的文件末行 +1 继续，保证重启后连续；
- `verify_chain` 校验三件事：`seq` 连续递增、`prev_hash` 链接正确、每行自身可解析。

**测试**：A12 专项——正常链通过；删一行 / 改一字 / 插一行 / 换顺序，四种篡改全部检出；重启后续写不断链。

**前置**：T1.8（最小 append 版就位）。 **验收映射**：A12。

### T3.2 `masker.py`：分级掩码 + 超长截断（1 人日）

**内容**：`Masker`（方案 §7.4）：字段名黑名单 + 值模式正则；**两套应用档位**（`audit_log` 全量、`approval_request` 仅凭证类）；超长字段截断（>500 字符 → `{sha256, length, 前100字符预览}`）。

**实现要点**：

- 掩码函数签名：`mask(arguments: dict, level: Literal["audit_log", "approval_request"]) -> dict`；
- 接入点：AuditStore append 前（audit_log 档）、`build_approval_request` 组装 `arguments_masked` 时（approval_request 档）；
- 截断的 sha256 用 `utils/canonical.py` 的规范序列化，与 `args_hash` 对齐。

**测试**：A13 专项——密码字段/邮箱在日志中不可检索、审批视图收件人可见、`write_file` 大 content 不落盘全文。

**前置**：T3.1。 **验收映射**：A13。

### T3.3 审计埋点核对（0.5 人日）

**内容**：对照方案 §5.2 与 §7.1，核对 `task_start / propose / classify / evaluate / approve / deny / execute / task_end` 八种事件全部埋点、字段齐全（`policy_version`、`profile_version`、`hash_algo`、metadata 里的分类器 suggestion）。

**测试**：跑一次示例任务，断言事件序列与字段完整性（快照式测试）。

**前置**：T3.1、T3.2。

### T3.4 全量验收自动化 + CI（0.5-1 人日）

**内容**：把方案 §9.2 的 A1-A14 全部落成 `tests/test_e2e_research_agent.py` 的自动化用例；接入 CI（GitHub Actions 或等价物），OPA 作为 service 或测试前置步骤启动。

**前置**：T3.3。 **验收映射**：全部。

### T3.5（可选，最低优先级）`LLMPlanner`

**内容**：按方案 §5.1 的 JSON Schema 契约实现；**放到最后做**，因为它是演示增强而非治理能力——没有它 MVP 验收一条不少。

**前置**：全部。 **验收映射**：无（演示增强）。

### 迭代 3 出口检查

- [ ] A1-A14 全绿且自动化
- [ ] 里程碑演示：篡改检出 + 分级掩码
- [ ] README 更新：启动顺序、配置说明、演示命令

---

## 5. 开发规范与协作

### 5.1 代码纪律

1. **Schema 单一来源**：方案 §3 的所有结构只在 `models.py` 定义；改动 Schema 必须同步检查 `build_policy_input`、审计埋点、测试夹具三处；
2. **deny 必带 reason**（§0.2 纪律 2）；
3. **占位组件实现真实 Protocol**：迭代 1 的内存占位与迭代 2/3 的真实实现实现同一接口，替换时调用方零改动——这是本计划能平滑演进的关键，Code Review 重点检查；
4. **治理语义只许住在 Checkpoint**：`forward` 的校验、审批组装、复核逻辑不得下沉到 MCPGateway 或上浮到 R1（方案 §6.6）；
5. **async 一致性**：IO 路径（OPA 查询、MCP 调用、审批）全链路 async；同步阻塞调用（如 `requests`）禁止出现在事件循环里。

### 5.2 测试纪律

- 每个任务卡自带测试清单，与实现同 PR；
- Rego 策略用例（T1.4）与 Python 判定用例（T1.6）分开：前者测策略逻辑，后者测流水线组装——混在一起出错时无法定位层；
- 安全类用例（重放、过期、篡改、fail-closed）**必须有失败注入**，不允许只测"正常通过"；
- e2e 用例可重复运行：测试前清理 `data/` 目录或由 fixture 提供临时目录。

### 5.3 范围护栏（防止开发中范围蔓延）

开发中如果冒出"要不要顺手做了 X"的念头，先查方案 §1.2 范围外表——以下都已明确**不做**：inter_agent、Earned Authority、策略加密、审批 UI、审计采样、财务预算、LLMPlanner（迭代 3 可选除外）。确实需要变更范围的，回方案文档提修订，不在代码里私自带入。

### 5.4 多人并行拆分（如适用）

| 角色 | 迭代 1 | 迭代 2 | 迭代 3 |
|---|---|---|---|
| 工程师 A（治理核心） | T1.4、T1.6 | T2.2、T2.3 | T3.3、T3.4 |
| 工程师 B（基础设施） | T1.1、T1.2、T1.3、T1.5 | T2.1、T2.4、T2.5 | T3.1、T3.2 |

接口对齐点：迭代 1 开始的第 1 天，两人先共同冻结 `models.py` 与各 Protocol 签名，之后并行。

---

## 6. 踩坑清单（按历史经验预登记）

| # | 坑 | 规避方法 |
|---|---|---|
| 1 | Rego v1 语法：忘记 `import rego.v1` 或 `if` 关键字，OPA 1.x 下解析失败 | 策略文件头部固定两行；T1.4 测试先行 |
| 2 | `glob.match` 分隔符参数：路径模式必须传 `["/"]`，否则 `**` 不跨目录 | 方案 §6.3 已给出正确写法，照抄 |
| 3 | 用 `requests` 查 OPA 会阻塞 asyncio 事件循环，并发下全链路卡死 | 一律 httpx async（§0 依赖已锁） |
| 4 | `datetime.utcnow()` 在 3.12+ 弃用且 naive，审计时区歧义 | 统一 `datetime.now(timezone.utc)`（方案偏离 D16） |
| 5 | PolicyStore 版本哈希不排序文件名 → 版本号不稳定 | T1.3 实现要点已注明 |
| 6 | 哈希链与 args_hash 各写一份 canonical JSON → 算法漂移 | 共用 `utils/canonical.py`（T3.1） |
| 7 | BudgetLedger 漏 refund → 预算假超支 | T2.4 三条路径写全 + 专项测试 |
| 8 | MCP stdio 子进程泄漏（主程序退出后 server 残留） | MCPGateway 独占生命周期 + atexit（T1.5） |
| 9 | 组合规则的 history 与调用次数计数各维护一份 → 口径漂移 | 共用同一份 per-task 历史（T2.5） |
| 10 | OPA 未启动时 ConfigLoader 校验 3 卡死 | 启动脚本固定顺序：先 OPA 后主程序（T1.8） |
| 11 | 把"审批通过"误解为"绕过 Rego" | deny 优先原则（方案 §6.1 步骤 7）；T2.3 专项测试兜底 |
| 12 | JSONL 落盘不 flush → 崩溃丢审计 | append 后 `flush()`；MVP 不要求 fsync |

---

## 7. 进度跟踪表

> 用法：每周更新；某条验收变绿 = 该能力可对利益相关方演示。

| 迭代 | 任务 | 预估 | 状态 | 验收 |
|---|---|---|---|---|
| 0 | T0.1-T0.3 环境地基 | 0.5-1 d | ⬜ | — |
| 1 | T1.1 models.py | 1 d | ⬜ | — |
| 1 | T1.2 ConfigLoader + 配置 | 1 d | ⬜ | — |
| 1 | T1.3 Identity + PolicyStore | 0.5 d | ⬜ | — |
| 1 | T1.4 PolicyEngine + Rego | 1 d | ⬜ | A2/A3/A4 策略侧 |
| 1 | T1.5 MCPGateway + mock email | 1 d | ⬜ | A1/A14 执行侧 |
| 1 | T1.6 Checkpoint 主干 | 1-1.5 d | ⬜ | A2/A3/A4 判定侧 |
| 1 | T1.7 Classifier + Planner | 0.5 d | ⬜ | — |
| 1 | T1.8 Runtime + 示例 | 0.5-1 d | ⬜ | A1/A14 |
| 2 | T2.1 DecisionStore | 0.5 d | ⬜ | A7/A8 |
| 2 | T2.2 审批链路 | 1 d | ⬜ | A5/A6 |
| 2 | T2.3 组合规则 | 0.5-1 d | ⬜ | A9 |
| 2 | T2.4 预算 | 0.5 d | ⬜ | A10 |
| 2 | T2.5 次数上限 + fail-closed | 0.5 d | ⬜ | A11 |
| 3 | T3.1 哈希链 | 1 d | ⬜ | A12 |
| 3 | T3.2 Masker | 1 d | ⬜ | A13 |
| 3 | T3.3 埋点核对 | 0.5 d | ⬜ | — |
| 3 | T3.4 全量验收 + CI | 0.5-1 d | ⬜ | A1-A14 |
| 3 | T3.5 LLMPlanner（可选） | 1 d | ⬜ | 演示增强 |

---

## 附录：与方案文档的对应关系

| 本指南章节 | 方案文档章节 |
|---|---|
| T1.1 | §3 统一核心抽象 |
| T1.2 / T1.3 | §4.1-4.3、§4.6 基础设施与配置 |
| T1.4 | §6.3、§6.4 Rego 与 OPA |
| T1.5 | §6.5 MCPGateway |
| T1.6 / T2.x | §6.1、§6.2、§6.6 判定流水线 |
| T1.7 / T1.8 | §5 R1 执行循环 |
| T2.2 | §3.10、§7.5 审批 |
| T3.1-T3.3 | §7.1-7.4 审计 |
| 迭代出口检查 | §9.2 验收标准 |
