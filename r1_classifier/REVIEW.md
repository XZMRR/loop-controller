# R1 轻量分类器（r1_classifier）实现审查与设计说明

> **用途**：交同事审查。本文档对照架构文档（`docs/architecture/00_r0r3_architecture.md`、`docs/architecture/05_mvp_core_abstractions.md`）逐项说明：哪些是文档已有定义且实现满足的；**哪些是文档未具体描写、我们自主设计（含用户拍板决策）的**；以及已知缺口与待评审问题。
>
> **分支**：`develop_r1`　**实现版本**：MVP v0.1（规则版打桩）
>
> **日期**：2026-08-14

***

## 1. 目录结构

```
r1_classifier/
├── __init__.py          # 包导出
├── models.py            # RiskLevel / RiskSignal / Task / Agent / ToolPermission / CapabilityProfile / ActionProposal
├── classifier.py        # LightweightClassifier(Protocol) + RuleBasedClassifier
├── rules.yaml           # 规则配置表（YAML）
├── agent.py             # ResearchAgent 编排层 + PlannedAction + Decision + mock_r2_checkpoint
├── demo.py              # 完整调用链演示脚本（对应文档 2.1 场景）
└── tests/
    ├── test_classifier.py   # 分类器 12 个用例
    └── test_agent.py        # Agent 编排链 4 个用例
```

运行方式：`.venv\Scripts\python.exe -m r1_classifier.demo`

***

## 2. 文档已定义、实现满足的部分

| # | 文档定义 | 文档依据 | 实现位置 | 结论 |
|---|---------|---------|---------|------|
| 1 | `RiskSignal` 四字段：risk_level / tags / reason / suggestion | 05 §3.5 L191-196 | [models.py](models.py) `RiskSignal` | ✅ 满足 |
| 2 | 分类器接口签名 `classify(task, agent, proposal, profile) -> RiskSignal` | 05 §3.5 L198-205 | [classifier.py](classifier.py) `LightweightClassifier` | ✅ 满足 |
| 3 | `ActionProposal` 字段（task_id/call_id/agent_id/type/tool_name/arguments/task_context/risk_level/reason） | 05 §3.4 L156-167 | [models.py](models.py) `ActionProposal` | ✅ 满足 |
| 4 | 分类器归属 R1 执行层，只输出信号、不做最终判定 | 00 L119-120；05 §3.5 L185 | `classify()` 返回 `RiskSignal` | ✅ 满足 |
| 5 | 触发时机：Agent 规划动作后、提交 R2 前 | 05 §4 时序图 L573-577 | [agent.py](agent.py) `run()` 第 76-83 行 | ✅ 满足 |
| 6 | 结果流向：分类器 → Agent → 二次封装（写 risk_level）→ R2 | 05 §4 L575-580、L173 | [agent.py](agent.py) L77-80 `dataclasses.replace` | ✅ 满足 |
| 7 | 工具级打桩规则：send_email→high、read_file→medium、默认→low | 05 §3.5 L229-253 | [rules.yaml](rules.yaml) 已覆盖且扩展 | ✅ 满足（含扩展，见 §3） |
| 8 | R3 异步只读采集，R1 不主动发 R3 | 00 L155/L163；05 §3.13、L608 | [agent.py](agent.py) docstring；demo 末尾标注 | ✅ 满足 |

***

## 3. 文档未具体描写、我们自主设计的部分（重点审查）

> 以下内容在 `00_r0r3_architecture.md` / `05_mvp_core_abstractions.md` 中**没有具体定义**，为本次开发自主设计，其中带 ★ 的为用户明确拍板决策。

### 3.1 规则粒度：工具级 + 参数级 ★

- 文档只定义了**工具级**打桩规则（不看参数）。
- 我们扩展为**参数级**：在工具默认等级基础上，对 `proposal.arguments` 逐条应用参数规则，命中则升级（★ 用户决策"工具级 + 参数级"）。
- 例：`send_email` 默认 high；若 `to` 不是 `@company.com` 结尾 → critical。

### 3.2 完整规则条目（文档仅 2 条工具级，我们补齐到 4 工具 + 6 参数规则）

| 工具 | 默认等级 | 参数规则（自主设计） | 等级 |
|------|---------|---------------------|------|
| `web_search` | low | query 含 `内部/机密/internal/secret/token/password` | medium |
| `read_file` | medium | path 含 `secret/credential/password/.env` | high |
| `write_file` | high | path 含 `secret/credential/password/.env` | critical |
| `send_email` | high | to 非 `@company.com` 结尾 | critical |

- `web_search`、`write_file` 在文档 §3.5 中**无任何规则**，规则与关键词库均为自主设计。
- 敏感词库（中文+英文）需业务侧评审覆盖度（见 §5-6）。

### 3.3 四档判定标准（文档只有字段，没有判定依据）

| 等级 | 判定依据（自主设计） |
|------|---------------------|
| low | 只读/公开检索，profile 明确授权，参数无敏感信息 |
| medium | 内部数据访问，参数正常 |
| high | 对外通信、写操作，或参数含 PII/内部敏感词 |
| critical | 不可逆操作、敏感信息外发、明显越权（写入敏感路径 / 外部收件人） |

### 3.4 规则组织：YAML 配置表 ★

- 文档为**硬编码 if-else**（`RuleBasedClassifier` 类内直接写死）。
- 我们改为 **YAML 配置表**（★ 用户决策"配置表"），由 [rules.yaml](rules.yaml) 声明规则，新增/修改规则不改代码，可审计。
- 加载优先级：显式 `rules` dict > `rules_path` > 包内默认 `rules.yaml`；加载时校验顶层 `classifier` 键。

### 3.5 匹配器：仅 regex + 忽略大小写 ★

- 文档未定义匹配器类型。我们 MVP 仅支持 `regex`（★ 用户决策"仅 regex"），`re.search(..., IGNORECASE)`。
- 其他匹配器类型（精确/后缀等）未来可扩展。

### 3.6 多规则命中取最高 ★

- 参数命中多条规则时，等级取最高（★ 用户决策"取最高"，符合内控从严）。

### 3.7 CapabilityProfile 参与判定：未授权工具 → high 短路 ★

- 文档 `classify` 签名含 `profile`，但**未定义分类器如何使用它**。
- 我们设计：工具未在 `profile.tools` 授权（或 allowed=False）→ 直接返回 high（含 `unauthorized_tool` tag 和 suggestion），不查后续规则（★ 用户决策"参与：未授权→high"）。
- `unauthorized_tool_level` 可在 YAML 顶层配置（当前为 `high`）。

### 3.8 数据模型实现细节（文档为伪代码/Pydantic 建议，我们选型）

- `RiskLevel` 用 **`StrEnum` + 优先级比较**（low<medium<high<critical）：文档用 `Literal[...]` 无比较语义；参数级升级需要大小比较，StrEnum 默认继承 `str.__gt__` 会按字典序比较（"high"<"medium"），故重写 `__lt__/__gt__/__le__/__ge__` 为优先级语义。
- 模型用 **`dataclass(frozen=True)`** 实现：文档建议"生产用 Pydantic"，MVP 选 dataclass 以减少依赖（仅 pyyaml 一个运行依赖）。
- **精简字段**：`Task` 去掉 `created_at`、`Agent` 保持 4 字段、`CapabilityProfile` 只保留 `is_tool_authorized()` 所需最小集（文档 §3.1-3.3 字段更全）。

### 3.9 Agent 编排层（文档只有时序图语义，无代码抽象）

- 文档时序图描述了调用链，但**没有定义 Agent 的代码形态**。我们自主设计：
  - `ResearchAgent(Agent)`：`plan()` 抽象（MVP 子类打桩，未来换 LLM 规划）+ `run()` 编排链（构造申报单 → classify → 二次封装 → 提交 R2）。
  - `PlannedAction`：规划动作的最小单元。
  - `Decision`（verdict/reason/decision_id）与 `mock_r2_checkpoint`：**R2 打桩**，校验授权 + critical→require_approval；文档定义 R2 为 `allow/deny/modify/require_approval` 四态 + 有效期，本打桩仅覆盖 allow/deny/require_approval 三态，无 modify/有效期/防重放校验（属未来 R2 实现范围）。

### 3.10 工程配置（自主）

- `pyproject.toml`：新增运行依赖 `pyyaml`、dev 依赖 `types-pyyaml`；新增 `[tool.pytest.ini_options]`（`pythonpath=["."]`、`testpaths=["r1_classifier/tests"]`）。
- 测试命令：`.venv\Scripts\python.exe -m pytest`（注意：`pytest.exe` 入口在本机缺 DLL，须用 `python -m pytest`）。

***

## 4. 验证结果

| 检查 | 命令 | 结果 |
|------|------|------|
| 单元测试 | `.venv\Scripts\python.exe -m pytest` | 16 passed（分类器 12 + Agent 4） |
| 静态检查 | `.venv\Scripts\python.exe -m ruff check r1_classifier` | All checks passed |
| 类型检查 | `.venv\Scripts\python.exe -m mypy r1_classifier` | Success（7 个源文件） |
| 演示 | `.venv\Scripts\python.exe -m r1_classifier.demo` | 4 个动作完整走通预检→封装→R2 |

测试覆盖：未授权→high、web_search/read_file/write_file/send_email 的正常与敏感参数分支、多条规则取最高（send_email:to）、配置表无规则→low、非字符串参数跳过、自定义规则 dict 注入、Agent 二次封装写 risk_level、申报元数据、mock R2 判定。

***

## 5. 已知缺口 / 待评审问题

> 每项按「现状 → 为什么是缺口 → 影响 → 可选方向」展开。建议同事评审时优先聚焦 **4（risk_level 消费方式）、5（AuditEvent）** 这两个结构性缺口，以及 **1、2、3（tags/suggestion）** 这三个输出语义完整性缺口。

### 5.1 tags 词汇表未标准化

- **现状**：`RiskSignal.tags` 只在两处产生——未授权时写死 `["unauthorized_tool"]`（[classifier.py](classifier.py) L85），参数级规则命中时自动生成 `f"{tool_name}:{arg_rule['key']}"` 格式（如 `send_email:to`、`read_file:path`，[classifier.py](classifier.py) L108）。
- **为什么是缺口**：文档示例 tag 是语义化的 `external_communication`、`pii_involved`、`data_access`（05 §3.5 L194），并注明 tags 的作用是"**便于 R2 命中规则**"（05 §3.5 L222）。当前 `{tool}:{key}` 是机械拼接，不是受控词汇，R2 无法按统一 tag 编写策略。
- **影响**：R1 输出的 tag 集合不可枚举、不可校验；R2 策略若引用 tag 会随规则表变化而漂移；审计报表按 tag 聚合不稳定。且未授权分支的 `unauthorized_tool` 是词汇风格、参数分支是拼接风格，两者不一致，恰说明需要统一。
- **可选方向**：定义受控词汇表（如 `external_communication / pii_involved / data_access / path_sensitive / unauthorized`），在 `rules.yaml` 每条规则上显式声明 `tag:` 字段（引擎已支持 `arg_rule.get("tag", ...)`，当前只是默认值兜底为 `{tool}:{key}`）。

### 5.2 tags / suggestion 到 R2 的传递通道未定义

- **现状**：`RiskSignal` 有 `tags`、`suggestion` 字段（[models.py](models.py) L58-61），但 `ActionProposal` 只有 `risk_level` 和 `reason`（[models.py](models.py) L106-118）。Agent 二次封装只把 `risk_level` 写入申报单（[agent.py](agent.py) L80），tags/suggestion 留在 R1 内部。
- **为什么是缺口**：文档时序图明确 `RiskSignal` 生成后，Agent 拿到的只是带 `risk_level` 的 `ActionProposal`；tags 设计用途是"R2 命中规则"，但**没有任何字段承载它到 R2**。R2 只能看到 low/medium/high/critical 四个等级，看不到风险类别。
- **影响**：R2 想按风险类型差异化处理（如 tag=external_communication 才审批、data_access 只记日志）做不到；审计时 classify 事件里的 tags 与最终 Decision 无法在同一链条上对齐（tag 在 RiskSignal，Decision 在 R2）。
- **可选方向**：方案 A（轻）在 `ActionProposal` 加 `tags: list[str]`，Agent 二次封装时同步写入；方案 B（重）R2 直接消费 `RiskSignal` 而非仅 `risk_level`（需改申报/决策协议）；至少应在文档层面明确"tags 是否跨 R1/R2 边界"，当前文档未定义。

### 5.3 suggestion 覆盖不完整

- **现状**：`RiskSignal.suggestion` 只在**未授权分支**给出（[classifier.py](classifier.py) L87）；参数级规则命中时返回的 `RiskSignal.suggestion=None`（[classifier.py](classifier.py) L111-112）。
- **为什么是缺口**：文档定义的 `RiskSignal` 是四字段完整语义（05 §3.5 L191-196），且文档规则示例 `send_email` 明确给了 `suggestion="请确认收件人白名单"`（05 §3.5 L236-239）。当前实现丢掉了"文档示例语义 → 实现"这一环。
- **影响**：缓解建议是 R1 自检的重要输出（R1 依据 CapabilityProfile 自检），`suggestion=None` 意味着命中高风险参数时 agent 拿不到"如何降低风险"的提示，自检质量打折。
- **可选方向**：在 `rules.yaml` 每条参数规则上加可选 `suggestion:` 字段，命中时写入 `RiskSignal.suggestion`；至少 `send_email` 外部收件人这条按文档补上"请确认收件人白名单"。

### 5.4 R2 如何消费 risk_level 未定

- **现状**：文档只写了 `risk_level` 是"R1 自检的参考，不是最终判定"（05 §3.4 L173）。当前 mock 打桩：`critical → require_approval`、未授权 → deny、其他 → allow（[agent.py](agent.py) L88-102）。
- **为什么是缺口**：真实 R2 是"Policy 编译后规则 + CapabilityProfile + 权限连锁分析"的复杂策略引擎（00 §R2 控制层），`risk_level` 在其中扮演什么角色**完全没有定义**：是硬性映射（high 必审批）？还是仅作 Policy 的一个输入因子？四档分别对应什么治理动作？
- **影响**：R1/R2 若各自实现会出现双标准——R1 认为 medium 的动作 R2 却要求审批，或 R1 标 critical 的动作 R2 直接放行，治理一致性被破坏。
- **可选方向**：定义"等级 → 治理动作"映射表（如 low 直接放行 / medium 记录并放行 / high 需审批 / critical 必须 R0-delegate 审批），并入 R2 Policy 文档；`mock_r2_checkpoint` 目前只是占位。

### 5.5 classify 审计事件（AuditEvent）未实现

- **现状**：文档 §3.13 定义了 `AuditEvent`（05 §3.13 L527+），时序图审计链含 `classify` 环节（05 §4 L608）；但本次范围**未实现 audit 模块**（无 `AuditLogger`/`AuditEvent` 代码），`classify` 结果目前无任何持久化。
- **为什么是缺口**：R3 的价值来自"独立、只读、可追溯"，若 classify 事件不上报，审计链 `propose → classify → evaluate` 中间缺一环，无法还原"R1 当初判断了什么风险、为什么"。
- **影响**：出问题时无法回溯 R1 自检过程；`call_id`/`task_id` 串链的审计能力（05 §3.4 L171-172）没有实际载体。
- **可选方向**：下一步实现 `audit.py`（`AuditEvent` + `AuditLogger`，MVP 落 JSONL/SQLite），在 Agent 二次封装后发一条 `classify` 事件；注意按文档做**脱敏**（记录哈希/掩码而非原始参数，05 §3.13 L529）。

### 5.6 敏感词库需业务评审

- **现状**：`rules.yaml` 的敏感词为自主设计的中英文混合正则：`(内部|机密|internal|secret|token|password)`、`(secret|credential|password|\.env)`，外部收件人判定 `@(?!company\.com$)`。
- **为什么是缺口**：词库是开发假设，未经业务/安全侧确认。具体风险：**漏报**——路径分隔符（Windows `\` vs `/`）、变体（`pwd`、`passwd`、`.env.local`）未覆盖；**误报**——如 `C:/kb/credentials_analysis.md`（分析文档而非凭据）会误标 high，`secret` 出现在正常文件名也误报；**域名假设**——`company.com` 是文档示例，真实收件人白名单需接配置。
- **影响**：分类器是第一道防线，词库偏差直接决定误报（干扰 agent 效率）或漏报（风险信号失真）。
- **可选方向**：词库条目化（每类敏感词分组 + 注释来源），交业务/安全评审；域名/目录白名单改为配置化；必要时引入"负向排除"规则。

### 5.7 inter_agent 类型未纳入分类

- **现状**：`ActionProposal.type` 预留了 `inter_agent`（[models.py](models.py) L113），但 `classify()` 完全按 `tool_name` 判定，不区分 type。
- **为什么是缺口**：严格说不算缺陷——文档明确 MVP 阶段 `inter_agent`（子任务委托）由 R1 内部处理、不进入 R2（05 §3.4 L177-179），且单 Agent MVP 不会产生该类动作。列为缺口是提示：**未来引入多 Agent 委托时**，分类器需为 `inter_agent` 定义独立风险规则（委托给哪个 agent、跨域委托等），当前无此能力。
- **影响**：仅对未来多 Agent 扩展有影响，MVP 内无实际影响。
- **可选方向**：暂不处理；扩展多 Agent 时给 `type=inter_agent` 单独的分类分支。

### 5.8 模型为精简版，与文档完整字段未对齐

- **现状**：`Task` 去掉 `created_at`（文档 §3.1 L73-80 有）、`CapabilityProfile` 只保留 `tools` 授权判断所需（文档 §3.3 还有 `max_budget_token`、`fixed_ceiling` 等）；用 `dataclass` 而非文档建议的 Pydantic（05 §3 引言）。
- **为什么是缺口**：分类器当前只用了字段的**最小子集**，足够自运行。但 R2/R3 要消费同一份 `Task`/`Agent`/`CapabilityProfile` 时，若各自实现精简版，字段漂移会造成集成成本。
- **影响**：不阻塞当前 MVP 闭环；阻塞后续 R2/R3 集成时的模型统一。
- **可选方向**：后续建统一模型包（`loop_controller/models.py`，Pydantic）替换本地模型，分类器只依赖接口字段；或在评审结论中记录"模型统一"为 R2/R3 开发的第一个前置任务。

***

## 6. 结论

- 文档已定义的核心语义（字段、接口签名、触发时机、结果流向、R3 旁路）**均已满足**。
- 本次自主设计集中在：**参数级规则、YAML 配置表、profile 参与判定、匹配器与取最高策略、四档判定标准、Agent 编排层与 R2 打桩**，详见 §3。
- 建议重点评审 §3（自主设计）与 §5（缺口），特别是敏感词库、tags 词汇表、risk_level 的 R2 消费方式。
