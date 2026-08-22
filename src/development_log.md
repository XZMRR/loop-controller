# Loop Controller MVP 开发记录

本文件记录 MVP 三次迭代中的关键决策、实现要点与踩坑经验，便于后续维护与开源时追溯。

## 迭代 1：MVP 核心（T1.1–T1.8）

目标：建立可运行的治理闭环，从零重写 `src/`。

### 完成内容

- **T1.1 统一 Schema**：所有核心结构收敛到 `models.py`，Pydantic v2 `frozen=True`，作为项目唯一 Schema 来源。
- **T1.2 MCP 网关**：`MCPGateway` 负责发现 MCP server、维护 `tool_name → (server, mcp_name)` 映射。
- **T1.3 PolicyEngine**：接入 OPA/Rego v1，fail-closed；统一 `build_policy_input` 的 input schema。
- **T1.4 PolicyStore**：文件策略版本管理，版本=内容哈希。
- **T1.5 Identity**：`ConfigIdentityProvider` 从 `agents.yaml/users.yaml` 加载。
- **T1.6 Checkpoint**：R2 判定流水线与执行前校验；DecisionStore/BudgetLedger/PermissionInteraction 先用占位实现，接口保持一致。
- **T1.7 Planner + Classifier**：`ScriptedPlanner` 按 YAML 脚本产出动作；`RuleBasedClassifier` 只输出风险信号，不做拦截决策。
- **T1.8 审计存储**：`JsonlAuditStore` 最小 append-only 实现，事件结构使用最终 `AuditEvent`。

### 关键决策

- **治理语义只许住在 Checkpoint**：forward 校验、审批组装、modify 复核不得下沉到 `MCPGateway`，也不得上浮到 `Planner`。
- **纯工具调用**：MVP 只治理 `tool_call`，不治理自然语言输出。
- **MCP SDK 2.x**：使用 snake_case 字段（`Tool.input_schema`、`CallToolResult.is_error`），handler 用构造函数式而非装饰器。

### 踩坑记录

- **mcp SDK anyio cancel scope 竞态**：Windows 下关闭子进程时抛异常，最终用 `shield=True` + 逐 server try/except 兜住，日志级别降为 WARNING。
- **PowerShell `2>&1` 被当 Pipe**：运行 pytest 时改用 `$env:PYTHONPATH="src"; .venv\Scripts\python.exe ...` 前缀。
- **ActionProposal 构造冲突**：显式参数与 `**overrides` 重复，改为 `kwargs.update`。

---

## 迭代 2：安全边界（T2.1–T2.5）

目标：把迭代 1 的占位全部替换为真实实现，加固边界。

### 完成内容

- **T2.1 DecisionStore 持久化**：`JsonlDecisionStore` 追加写 + 启动重放，跨重启防重放。
- **T2.2 R0-delegate**：`ConfigR0Delegate` 按 `approval.yaml` 规则返回 approve/deny；接口按 v1.1 改为 async，但实现立即返回。
- **T2.3 权限组合规则**：`ConfigPermissionInteractionAnalyzer` 实现“先读 kb 再发外部邮件 → deny”。
- **T2.4 预算**：`InMemoryBudgetLedger` reserve/commit/refund 三路径。
- **T2.5 调用次数上限**：per-task 工具成功执行次数封顶。

### 关键决策

- **R0Delegate async 签名**：IO 路径（OPA、MCP、审批）全链路 async；同步阻塞调用禁止出现在事件循环。
- **占位组件与真实实现同 Protocol**：替换时调用方零改动。

### 踩坑记录

- **`_handle_require_approval` 签名缺 profile**：批量补丁加参数。
- **Budget 会计断言错误**：重写为 reserve→commit→refund 逐步验证。
- **示例脚本首次运行 UnicodeDecodeError**：docstring 改为 raw string `r"""…"""`。

---

## 迭代 3：审计闭环（T3.1–T3.4）

目标：完成 R3 审计能力，让篡改可检测、参数可掩码、验收可自动化。

### 完成内容

- **T3.1 哈希链**：
  - 新建 `utils/canonical.py`：键排序、无空白、UTF-8、`ensure_ascii=False` 的 canonical JSON。
  - `JsonlAuditStore` 完整实现：append 分配 `seq`/`prev_hash`，`verify_chain` 校验 seq 连续与 prev_hash 链接，`query_by_trace` 按 trace_id 扫描。
  - 重启时读取文件末行恢复 `seq`/`prev_hash`，保证续写不断链。
- **T3.2 分级掩码**：
  - 新建 `masker.py`：`mask(arguments, level)` 支持 `audit_log`（全量规则）与 `approval_request`（仅凭证黑名单）。
  - 超长字段截断（>500 字符 → `{sha256, length, preview}`）。
  - 接入 `AuditStore.append`（audit_log 档）与 `Checkpoint.build_approval_request`（approval_request 档）。
- **T3.3 审计埋点核对**：`run_task` 在 `propose/evaluate/approve/deny/execute/task_start/task_end` 处写入审计事件，包含 `args_hash`、`args_mask`、`hash_algo`、`policy_version`、`profile_version`。
- **T3.4 全量验收自动化 + CI**：
  - 新建 `tests/test_e2e_research_agent.py`，覆盖 A5/A12/A13/A14。
  - 新建 `.github/workflows/ci.yml`，使用 uv 与 OPA linux binary。

### v1.0 → v1.1 对齐（迭代 3 前补齐）

在迭代 3 开工前，发现之前依据的是 v1.0 文档，用户指出后对齐了 12 处差异：

| v1.1 修订 | 落地 |
|---|---|
| #1 call_id 全局唯一 | `DecisionStore.is_call_id_seen(call_id)` 去掉 task_id 分区 |
| #2 单进程 asyncio 假设 | Checkpoint forward 加显式注释 |
| #3 cost_per_call 按工具计费 | `ToolMappingEntry.cost_per_call` + Checkpoint 按工具计费 |
| #4 R0Delegate async | Protocol/实现/调用处/测试全改 async |
| #5 宁宽勿漏 | masking_rules.yaml 保留并注释 |
| #6 裁决优先级总表 | Checkpoint evaluate 加显式优先级注释 |
| #7/#8 Planner 只输出草案 | `PlannedAction` 不含身份字段，run_task 统一生成 call_id |
| #9 启动校验 | 补 approver ≠ agent_id 校验 |
| 自审#1 分级掩码 | `MaskingRules.masking_applies_to` 改为 dict[视图, 规则列表] |
| 自审#3 finalize +5min | 保持审批通过时刻起算 |

### 关键决策

- **canonical JSON 唯一实现**：`args_hash`、哈希链、超长字段摘要共用 `utils/canonical.py`，避免算法漂移。
- **审计事件中的 args_hash 用原始参数**：`args_mask` 用 audit_log 档掩码；这样事后可用原始 hash 核对，日志中又不泄露原文。
- **CI 中下载 OPA**：`tools/opa.exe` 在 `.gitignore` 中，CI 通过环境变量 `OPA_PATH` 指向下载的 linux binary。

### 踩坑记录

- **Pydantic model_dump 含 datetime，json.dumps 失败**：`JsonlAuditStore.append` 使用 `model_dump(mode="json")` 先序列化。
- **哈希链只能检测中间篡改**：删除/修改最后一行无法被下一行检测。测试改为至少 3 行并篡改中间行。
- **真实 MCP server 在 CI/沙箱中不稳定**：e2e 测试用 `FakeGateway` 替换真实 MCP 调用，但保留真实 ConfigLoader、OPA、R0-delegate、审计链与掩码。

---

## 迭代 3.5：LLMPlanner（T3.5）

目标：用真实 LLM 替换脚本化规划器，展示"真实 Agent 被治理"的演示效果。

### 完成内容

- **配置层**：新增 `config/llm_planner.yaml`，`LLMPlannerConfig` dataclass；启用时校验 `api_key_env` 环境变量存在。
- **LLMPlanner 实现**：
  - `HttpxLLMClient` 用 httpx 调用 OpenAI 兼容 `chat/completions`；`trust_env=False`；不依赖 openai 库。
  - Prompt 五段结构：system + context + tools + history + ask。
  - 历史分层摘要：最近 1 步完整保留（content 截断 2000 字符），早期步骤一行摘要。
  - 响应解析：提取首个 JSON 对象（允许 markdown 包裹），Schema 校验 + 工具白名单预检。
  - Token 预算路径 A：调用前 `check_and_reserve` 预估上限，调用后按 usage commit 实际值并退还差额；超支时审计 `metadata.planner_budget_exceeded`。
  - 失败不重试：解析失败/预算耗尽/白名单失败 → 审计 `metadata.planner_error` 并返回 None。
  - 密钥纪律：API key 只从环境变量读取，不落盘、不进审计日志。
- **Runtime 集成**：`build_runtime` 在 `llm_planner.enabled=true` 时创建 `LLMPlanner`，否则继续使用 `ScriptedPlanner`；`Planner` 协议改为 async 以支持 `MCPGateway.list_tools`。
- **单测**：`tests/test_llm_planner.py` 使用 fake client 覆盖 JSON/markdown/缺字段/非 JSON/未授权工具/历史摘要/预算 commit-refund/密钥纪律。

### 关键决策

- Planner 协议 async 化：LLMPlanner 需要异步拉取工具列表并调用 LLM，同步 Protocol 会让实现非常复杂。
- 预算 ledger 与 Checkpoint 共用：`build_runtime` 把同一个 `InMemoryBudgetLedger` 注入 Checkpoint 与 LLMPlanner，per-task 额度真实共享。
- 工具白名单只是提前失败优化：真正的权限判定仍在 R2；白名单失败与 Schema 失败走同一审计路径。

### 踩坑记录

- `asyncio_mode=auto` 下现有同步测试可直接改为 `async def`，无需 `@pytest.mark.asyncio`。

### 真实 LLM 端到端验证

- **环境**：DeepSeek 官方 API（`https://api.deepseek.com/v1`，`deepseek-chat`）。
- **命令**：`$env:LLM_API_KEY="..."; $env:PYTHONPATH="src"; .venv\Scripts\python.exe examples/research_agent_example.py`
- **结果**：任务执行完成；LLM 自主规划了 web_search → web_search → send_email；send_email 因收件人不在 `*@company.com` 白名单被 R2 deny；审计链 11 个事件，`verify_chain()` 返回 True。
- **说明**：验证后 `config/llm_planner.yaml` 已恢复 `enabled: false`，API key 未写入任何文件。

---

## 验收状态（A1-A14）

| ID | 状态 | 自动化位置 |
|---|---|---|
| A1-A4 | 通过 | `tests/test_policy_engine.py` + `tests/test_checkpoint.py` |
| A5 | 通过 | `tests/test_e2e_research_agent.py` approve/deny 路径 |
| A6 | 通过 | `tests/test_checkpoint.py::test_build_approval_request_conflict` |
| A7 | 通过 | `tests/test_decision_store.py::test_persists_across_restarts` |
| A8 | 通过 | `tests/test_checkpoint.py::test_forward_expired_decision` |
| A9 | 通过 | `tests/test_permission_interaction.py::test_deny_short_circuit` |
| A10 | 通过 | `tests/test_checkpoint.py::test_evaluate_budget_cost_per_call` |
| A11 | 通过 | `tests/test_policy_engine.py::test_opa_down_fail_closed` |
| A12 | 通过 | `tests/test_audit_store.py::test_detects_*` + e2e |
| A13 | 通过 | `tests/test_masker.py` + e2e |
| A14 | 通过 | `tests/test_e2e_research_agent.py`（web_search 映射本地 mock） |

---

## P0：信任加固（HMAC 审计链）

目标：把安全局限 L1/L2 从"待办"变成"已有缓解"。

### 完成内容

- **`AuditEvent` 扩展**：新增 `key_id` 字段，为密钥轮换留口；新增 `seal` action。
- **`JsonlAuditStore` 支持 HMAC-SHA256**：
  - 通过环境变量 `LOOP_CONTROLLER_AUDIT_HASH_ALGO` 选择 `sha256`（默认，兼容）或 `hmac-sha256`；
  - `LOOP_CONTROLLER_AUDIT_HMAC_KEY` 从环境变量读取，支持 hex/base64，长度 ≥32 字节；
  - event key 与 seal key 通过 HMAC(root_key, label) 做域分离；
  - 追加事件时写入 `hash_algo` 与 `key_id`。
- **seal 记录**：`JsonlAuditStore.seal()` 固定当前链累积 HMAC；seal 记录本身进链，并带 `chain_hash` + `seal_signature`；篡改 seal 记录或删除 seal 前后事件都会使 `verify_chain()` 失败。
- **ConfigLoader 启动校验**：`hmac-sha256` 模式下环境变量缺失或格式非法时 fail-closed。
- **Runtime 集成**：`build_runtime` 按 `config.audit_hash_algo` 创建对应 `JsonlAuditStore`。

### 关键决策

- **key 只走环境变量，不进配置文件**：降低密钥泄露面。
- **域分离用 label HMAC 而非 HKDF**：P0 够用，避免引入额外依赖；若未来需要更标准派生，可无缝替换 `_derive_key`。
- **sha256 保持默认**：无敏感数据的开发/演示场景不需要 HMAC，避免强制配置增加启动门槛。

### 测试

- `tests/test_audit_store.py`：HMAC 链通过、错误 key 检测、seal 检测删除/篡改、sha256 兼容。
- `tests/test_config_loader.py`：HMAC key 缺失、hex/base64 合法、长度不足、编码非法四个反例。

---

---

## P1 L2：会话级风险判定（v0.3.0）

目标：实现 v1.2 §3 的 session 新约定、RiskStateManager 真用化、Rego `session_risk` 扩展。

### 完成内容

- **SessionManager**：新建 `src/loop_controller/session.py`；
  - `Session` dataclass 与 `SessionManager` 类；
  - `get_or_create_session(user_id, agent_id)` 按 30 分钟超时复用或新建 session（uuid hex）；
  - `validate_and_touch(task)` fail-closed：session 不存在/不活跃/(user_id, agent_id) 绑定不一致均抛 ValueError；
  - `is_session_active(session_id)` 与 `close_session(session_id)`。
- **RiskStateManager 真用化**：新建 `src/loop_controller/risk_state.py`；
  - `RiskEvent` dataclass 与 `RiskStateStore` Protocol；
  - `JsonlRiskStateStore`：父目录可写检查、JSONL 追加、启动重放、末行不完整忽略并 WARNING；
  - `RiskStateManager`：确定性算分（deny +0.20 / critical +0.30 / approval_denied +0.10 / approval_granted +0.05 / low_risk_success -0.05）、每条事件 ×0.9 衰减（0-1 封顶）、recent_tags bounded FIFO 最多 10 条；
  - `allow` / `low_risk_success` 不进入 `recent_tags`。
- **配置层**：
  - `CapabilityProfile` 增加 `session_risk_threshold: float = 0.6`；
  - `AppConfig` 增加 `risk_state_path`（默认 `./data/risk_state.jsonl`），`ConfigLoader.load()` 设置绝对路径并校验父目录可写。
- **Checkpoint 集成**：
  - `Checkpoint.__init__` 接收 `session_manager` 与 `risk_manager`；
  - `evaluate` 将 `session_risk` 结构传入 `build_policy_input`；
  - 返回 decision 后更新 risk：deny→deny、require_approval→require_approval、critical 信号→critical；
  - `forward` 成功后 allow + risk_level=low 时更新 `low_risk_success`。
- **Rego 策略**：`policies/default.rego` 新增 `session_risk_gate`，score >= threshold 且非 critical 时升级为 `require_approval`；兼容无 `session_risk` 的旧输入。
- **Runtime 集成**：
  - `Runtime` dataclass 增加 `session_manager` 与 `risk_manager`；
  - 新增 `Runtime.create_task(user_id, agent_id, description)` 作为推荐入口；
  - `build_runtime` 组装 `SessionManager`、`JsonlRiskStateStore`、`RiskStateManager` 并注入 Checkpoint；
  - `run_task` 开头 `validate_and_touch(task)`，审批结果记录后更新 risk，forward 传入 session_id。
- **测试**：
  - 新建 `tests/test_session.py`、`tests/test_risk_state.py`；
  - 更新 `tests/test_checkpoint.py` 断言 `session_risk` 进入 policy input 与 risk 更新；
  - 更新 `tests/test_policy_engine.py` 新增 Python ↔ Rego input contract 与 `session_risk_gate` 用例；
  - 更新 `tests/test_e2e_research_agent.py` 与 `examples/research_agent_example.py` 使用 `runtime.create_task`。

### 关键决策

- `Task.session_id == Task.task_id` 约定正式废除：模型校验器删除，session 由 `SessionManager` 权威分配。
- `build_policy_input` 中 `session_risk` 可选传入：旧测试不传时不影响 Rego 判定，传入时才加入 input_doc。
- `forward(session_id=...)` 显式传入 session_id，不扩展 `ActionProposal` schema，保持最小改动面。
- Session 结束后不主动 close（推荐保留），由 30 分钟 gap 自然过期；`close_session` 提供显式关闭能力。

### 验收状态更新

| ID | 状态 | 自动化位置 |
|---|---|---|
| A15（新增） | 通过 | `tests/test_session.py` + `tests/test_risk_state.py` |
| A16（新增） | 通过 | `tests/test_policy_engine.py::test_session_risk_*` |
| A17（新增） | 通过 | `tests/test_checkpoint.py::test_evaluate_includes_session_risk*` |

---

## P1/P0 Review 修正

目标：回应规划 agent 在 `src/answer.md` 中提出的两点核心问题。

### 1. 默认审计算法改为 hmac-sha256

- `ConfigLoader.load()` 默认 `audit_hash_algo="hmac-sha256"`（部署级默认安全）；
- `JsonlAuditStore` 构造函数仍保留 `sha256` 默认，用于底层兼容与旧文件验证；
- `tests/conftest.py` 增加 `autouse` fixture，为全部测试注入 32 字节测试 key；
- `.github/workflows/ci.yml` 注入 `LOOP_CONTROLLER_AUDIT_HMAC_KEY`；
- `src/KNOWN_LIMITATIONS.md` L1/L2 更新为"已有缓解"，并明确 HMAC 为默认。

### 2. HMAC 审计链篡改检测补全

新增 `tests/test_audit_store.py` 用例：
- `test_hmac_key_not_in_audit_log`：key 不出现在审计文件中；
- `test_truncated_file_fails_verification`：文件末尾被截断成不完整 JSON 行时校验失败；
- `test_forged_seal_metadata_fails`：伪造 seal 的 chain_hash 或 seal_signature 均失败；
- `test_reordering_events_fails_hmac`：HMAC 模式下交换事件顺序也失败；
- `test_hmac_refuses_mixed_algo_file`：hmac 模式打开已有 sha256 文件时拒绝启动；
- `test_sha256_can_verify_legacy_file`：sha256 store 仍可验证旧文件。

### 3. 旧 sha256 审计文件升级策略

- `JsonlAuditStore.__init__` 检测现有记录算法与当前不一致时抛 `ValueError`，拒绝启动；
- 运维需手动归档旧文件（如 `audit.jsonl.legacy`）后切换算法；
- 文档中说明：混合算法链无法安全校验，不静默切换。

### 4. session_risk 覆盖 modify 路径

- `checkpoint.py` 在 Rego 返回 `modify` 且 `session_risk.cumulative_risk_score >= profile.session_risk_threshold` 时，升级为 `require_approval`；
- reason 改写为 "session risk score above threshold; modify upgraded to approval"，policy_hits 追加 `session_risk_gate`；
- 新增 `tests/test_checkpoint.py::test_evaluate_modify_upgraded_when_session_risk_high` 与 `test_evaluate_deny_unchanged_when_session_risk_high`。

---

## Iteration 5：真实异步审批 CLI（v0.3.0）

目标：把 R0 审批从配置化同步打桩替换为真实异步人工审批，CLI 与 Runtime 共享持久化存储。

### 完成内容

- **DecisionStore 扩展**：
  - `Decision` 新增 `expires_at` / `max_uses` / `used_count`；
  - 原子化 `use_decision(decision_id, now)` 检查过期与次数，避免重复执行。
- **ApprovalStore 协议与实现**：
  - 新建 `src/loop_controller/infra/approval_store.py`；
  - `JsonlApprovalStore` 以 JSONL 持久化 `ApprovalRequest` / `ApprovalRecord`；
  - CLI 与 Runtime 通过同一文件共享状态。
- **AsyncApprovalManager**：
  - 新建 `src/loop_controller/approval_manager.py` 替代 `ConfigR0Delegate`；
  - `submit()` 把审批请求落盘；`check()` 查询审批结果。
- **Runtime 暂停态与恢复**：
  - `TaskRunResult` 新增 `needs_approval` 状态及 `decision_id` / `request_id` / `pending_decision` / `pending_proposal` 字段；
  - `run_task` 遇到 `require_approval` 时提交请求并返回 `needs_approval`；
  - `resume_task(..., pending=...)` 读取审批结果后继续执行已审批动作；
  - 修正 `ended` 标志，避免暂停态错误写入 `task_end`；
  - 审批通过后的 allow Decision 执行时写入 `approval_consumed`，超期时写入 `approval_expired`。
- **CLI 入口**：
  - 新建 `src/loop_controller/cli.py`，实现 `lc approvals list/approve/deny`；
  - 校验审批人存在于用户列表、审批人不能是请求者或执行 Agent、deny 必须带 reason；
  - `pyproject.toml` 注册 `lc` 脚本；
  - 支持 `LOOP_CONTROLLER_CONVERSATION_PATH` / `LOOP_CONTROLLER_APPROVAL_STORE_PATH` 环境变量覆盖。
- **配置与文档**：
  - `ApprovalRule` 移除 `behavior` 字段，`approval.yaml` 仅用于确定 `escalation_target`；
  - 更新 `src/README.md`、`src/KNOWN_LIMITATIONS.md`，标记 F1 已实现；
  - 更新 `examples/research_agent_example.py` 演示异步审批流程。

### 关键决策

- **审批行为不再由配置决定**：`approval.yaml` 只指定审批人；真实 approve/deny 必须由 CLI 写入，避免配置即终审权。
- **DecisionStore 与 ApprovalStore 分离**：前者记录 R2 判定元信息，后者记录人工审批流程；关注点清晰，便于未来接入真实消息通知。
- **resume_task 显式传入 pending**：调用方必须提供暂停时的 `TaskRunResult`，确保恢复的是同一决策，不依赖隐式全局状态。

### 测试

- `tests/test_r0_delegate.py` 改为测试 `AsyncApprovalManager`;
- `tests/test_cli.py` 覆盖 `lc approvals list/approve/deny`、审批人冲突校验、deny 必填 reason;
- `tests/test_e2e_research_agent.py` 覆盖 approve/deny 路径完整事件序列，以及审批超时 `approval_expired` 事件;
- `tests/test_audit_events.py` 更新事件序列以包含 `approval_consumed`;
- `tests/test_config_loader.py` 移除 `behavior` 字段;
- `tests/test_e2e_real_mcp.py` 新增真实 MCP 组件 E2E：使用 `build_runtime()` + 真实 `MCPGateway` + 本地 `email_mock`，验证邮件真实发出;
- 全量 205 用例通过。

### 踩坑记录

- **pytest-asyncio 未安装导致异步用例被跳过**：安装 `pytest-asyncio>=0.23` 后，异步用例才真正执行，暴露出 mcp SDK 版本兼容性问题。
- **mcp SDK 版本差异**：当前环境 mcp SDK 使用装饰器式 handler（`@server.list_tools()` / `@server.call_tool()`）与 camelCase 字段（`inputSchema` / `isError`）；`mcp_gateway.py` 与 `mocks/email_server.py` 已做兼容处理。
- **测试目录隔离**：`tmp_path_factory.mktemp("cli-config")` 会导致 `approvals.jsonl` 跨测试污染，改为 `tmp_path / "project"`。
- **`ended` 标志误写 task_end**：resume 后 `ScriptedPlanner` 再次返回 send_email，循环体中错误设置 `ended=True` 导致暂停态写入了 `task_end`，已修正。

### 审查阻塞项修复（reports/develop_mvp_review_for_team.md）

| 优先级 | 问题 | 修复方式 | 自动化位置 |
|---|---|---|---|
| P0 | 拒绝路径预算未返还 | `evaluate()` 所有 `deny` / `require_approval` 路径 `refund()`；`forward()` modify 复核失败 `refund()`；resume 时重新预留 | `tests/test_checkpoint.py::test_evaluate_refund_on_policy_deny` |
| P0 | 审批记录缺少强绑定验证 | `finalize_after_approval()` 校验 decision_id、request_id、approver_id、过期、重复应用；deny 必须带 reason | `tests/test_checkpoint.py::test_finalize_after_approval_binding_validation` |
| P1 | DecisionStore 损坏日志 fail-open | `JsonlDecisionStore._load()` 遇到非法 JSON / 非法 Decision 直接抛 `DecisionStoreError` 并报告行号 | `tests/test_decision_store.py::test_corrupt_log_fail_closed` |
| P1 | CI OPA 路径不一致 | `tests/conftest.py` 新增 `resolve_opa_bin()`：按 `OPA_PATH` -> `tools/opa` -> `tools/opa.exe` 解析 | 所有 OPA fixture 统一调用 |
| P1 | 工程质量门禁未闭合 | CI 新增 `lint` job：ruff + mypy；test job 增加 OPA 可用性校验；修复 5 处 mypy error | `.github/workflows/ci.yml` |
| P2 | E2E 仍用 FakeGateway | 新增 `tests/test_e2e_real_mcp.py`：使用 `build_runtime()` + 真实 `MCPGateway` + `email_mock` | `tests/test_e2e_real_mcp.py` |

### 验收状态更新

| ID | 状态 | 自动化位置 |
|---|---|---|
| A5 | 通过 | `tests/test_e2e_research_agent.py` approve/deny 路径 |
| F1 | 已实现 | `AsyncApprovalManager` + `JsonlApprovalStore` + `lc approvals` |

## v0.4.0：跨 Task Session 风险状态持久化

目标：把治理粒度从单次任务提升到会话级，连续越权行为触发会话级熔断，服务重启后 Session 和风险状态不丢失。

### 完成内容

- **`JsonlSessionBackend`**：新增 `src/loop_controller/session.py` 持久化后端，追加写 + 启动重放，中间行损坏 fail-closed；
- **`SessionManager` 扩展**：新增 `get_session()`、`touch_session()`、`is_session_active()`/`is_session_expired()`；
- **`RiskProfile` 扩展**：新增 `consecutive_deny_count`；
- **`RiskStateManager` 扩展**：`deny`/`approval_denied` 累加 `consecutive_deny_count`，`allow`/`approval_granted`/`low_risk_success` 归零；
- **`CapabilityProfile` 扩展**：新增 `session_block_threshold: int = 5`；
- **Checkpoint 硬熔断**：`evaluate()` 在 Profile 校验后检查 `consecutive_deny_count >= session_block_threshold`，满足则直接 deny；
- **`Runtime.create_task` 复用 Session**：签名改为 `create_task(..., session_id=None) -> tuple[Task, Session]`，支持显式复用已有 Session；
- **配置层**：`AppConfig` 新增 `session_path`，`ConfigLoader` 支持 `LOOP_CONTROLLER_SESSION_PATH` 环境变量；
- **测试**：新增 `tests/test_session_v040.py`，覆盖 S1-S8 验收标准。

### 关键决策

- Session 持久化与风险持久化分离：Session 写 `sessions.jsonl`，风险事件写 `risk_state.jsonl`；
- 连续拒绝熔断放在 Checkpoint Python 代码而非 Rego：这是全局安全策略，失败更早，且不扩展 Rego input schema；
- `create_task` 返回 `(Task, Session)` 元组：调用方需要知道本次 task 落在哪个 Session 上，便于日志、调试和后续复用。

### 验收状态

- `pytest tests/`：**212 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：**Success**

---

## v0.5.0：MCP Proxy / 外来 Agent 接入

目标：把 Loop Controller 同时暴露为一个 MCP Server，使未安装 Loop Controller SDK 的第三方 Agent 也能被 R2/R3 治理。

### 完成内容

- **`src/loop_controller/proxy_server.py`**：新增 `LoopControllerProxyServer`；
  - 低层 MCP Server 注册 `tools/list` 与 `tools/call` 处理器；
  - stdio 传输用于本地子进程/CLI 集成；
  - SSE 传输基于 Starlette + `SseServerTransport`，支持 HTTP header 身份覆盖；
  - 每次 tool call 映射为一个 `ActionProposal`，经 `Checkpoint.evaluate()` + `forward()` 治理后转发到真实 MCP Server；
  - `require_approval` 直接返回 `BLOCKED: requires human approval` 并附带 `decision_id`。
- **CLI 入口**：`lc proxy --agent-id <id> --user-id <id> [--transport sse --host ... --port ...]`；
- **Mock server 适配**：把 `email_server.py` 从旧版低层 `Server` 装饰器 API 迁移到 `mcp.server.mcpserver.MCPServer`，兼容当前 `mcp>=1.0`；
- **测试**：新增 `tests/test_proxy_server.py`，覆盖工具列表透传、allow 执行、deny 拒绝、审批阻塞、连续拒绝 Session 熔断。

### 关键决策

- **不使用 Planner**：外部 Agent 自行决定调用什么工具，Loop Controller 只负责单次 tool call 的治理；
- **同步调用限制**：MCP tool call 是请求-响应模式，v0.5.0 不实现长轮询等待人工审批；
- **身份映射**：stdio 使用 CLI 参数；SSE 可被 header 覆盖，但默认仍以 CLI 身份兜底；
- **Session 复用**：同一 SSE 连接内多次 tool call 共享 Session，v0.4.0 的 `consecutive_deny_count` 和 `cumulative_risk_score` 自然生效。

### 验收状态

- `pytest tests/`：**217 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：**Success**

### 设计文档

- `src/loop_controller_v0.5.0_development.md`

---

## v0.5.1：MCP Proxy 审批恢复与结构化响应

目标：让外部 Agent 收到 `require_approval` 后能够解析响应，并在人工审批通过后携带凭证重试，完成原 tool 调用。

### 完成内容

- **`ApprovalRequest` 模型扩展**：
  - 新增 `tool_arguments: dict[str, Any]`，保存原始未掩码参数；
  - 新增 `original_decision: Decision | None`，保存触发审批的原始 Decision，供重试时恢复。
- **`Checkpoint.build_approval_request()`**：填充 `tool_arguments` 与 `original_decision`。
- **`AsyncApprovalManager.get_decision()`**：v0.5.1 新增，按 `decision_id` 查询原始 Decision。
- **`JsonlApprovalStore` 序列化修复**：
  - `_serialize_request` / `_serialize_record` 改用 `model_dump(mode="json")`，解决嵌套 `Decision` 中的 `datetime` 无法 JSON 序列化的问题；
  - `_deserialize_request` / `_deserialize_record` 改用 `model_validate()`，自动解析 ISO datetime 和嵌套模型。
- **`LoopControllerProxyServer` 重写**（`src/loop_controller/proxy_server.py`）：
  - `require_approval` 返回结构化 JSON，包含 `status` / `decision_id` / `request_id` / `tool_name` / `reason` / `expires_at` / `retry_instruction`；
  - 支持通过 SSE header `x-loop-controller-decision-id` 或 stdio 保留参数 `_loop_controller_decision_id` 重试；
  - 重试时校验当前参数与 `ApprovalRequest.tool_arguments` 一致，防止 decision_id 被复用于不同调用；
  - 重试时调用 `Checkpoint.finalize_after_approval()` 将原始 `require_approval` Decision 转换为可执行的 `allow` Decision；
  - 重试时复用原始 `call_id`，保证 `Checkpoint.forward()` 的 call_id 一致性校验通过；
  - Proxy 进程内维护 `_tasks` 内存缓存，重试时恢复原始 Task。
- **测试更新**：
  - 更新 `test_proxy_require_approval_blocked` 断言结构化 JSON；
  - 新增 `test_proxy_retry_approved_executes`：审批通过后重试成功执行；
  - 新增 `test_proxy_retry_param_mismatch_denied`：参数不一致被拒绝；
  - 新增 `test_proxy_retry_not_approved_still_blocked`：未审批时重试仍被阻塞。

### 关键决策

- **Agent 不阻塞**：MCP tool call 保持同步请求-响应，Proxy 立即返回审批状态，由 Agent 决定如何向用户展示或何时重试；
- **审批凭证显式传递**：用 `decision_id` 作为重试凭证，不依赖隐式缓存或参数匹配，安全且可审计；
- **原始 Decision 持久化在 ApprovalRequest 中**：避免引入新的存储接口，复用现有 `JsonlApprovalStore`；
- **Proxy 进程重启后重试会失败**：Task 缓存丢失，写入错误响应；这是已知限制，v0.6.0 引入 `TaskStore` 后可解决。

### 验收状态

- `pytest tests/`：**220 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误（`planner.py`、`config_loader.py`），与本次改动无关。

### 设计文档

- `src/loop_controller_v0.5.1_development.md`

---

## v0.6.0：持久化基础设施（TaskStore + BudgetLedger）

目标：消除 v0.5.1 已知的 Proxy 重启后审批重试失败问题，并让预算状态在生产环境可持久化。

### 完成内容

- **`Task` 模型扩展**：新增 `status: Literal["created", "completed"]`（默认 `created`）和 `completed_at: datetime | None`，支持生命周期持久化；向后兼容，默认值不影响已有测试。
- **`JsonlTaskStore`**（`src/loop_controller/infra/task_store.py`）：
  - append-only JSONL，记录 `{"type": "task"}` 和 `{"type": "task_complete"}`；
  - `get(task_id)` 从尾部向前扫描返回最新状态；遇到 `task_complete` 返回 `None`；
  - 损坏文件 fail-closed，抛 `TaskStoreError`；
  - 提供 `InMemoryTaskStore` 作为测试默认实现。
- **`Runtime` 集成**：
  - 新增 `task_store: TaskStore = field(default_factory=InMemoryTaskStore)`；
  - `create_task()` 构造 Task 后立即 `task_store.save(task)`；
  - 新增 `get_task(task_id)` 从 Store 读取；
  - `run_task()` 任务结束时调用 `task_store.complete(task.task_id)`。
- **`JsonlBudgetLedger`**（`src/loop_controller/budget.py`）：
  - append-only JSONL，记录 `set_budget` / `reserve` / `commit` / `refund` 事件；
  - 启动时重放所有事件恢复内存状态；
  - 损坏文件 fail-closed，抛 `BudgetLedgerError`；
  - 保留 `InMemoryBudgetLedger` 供测试和旧代码使用。
- **`build_runtime()` 默认使用持久化实现**：
  - `JsonlBudgetLedger(config.budget_ledger_path)`；
  - `JsonlTaskStore(config.task_store_path)`；
- **配置扩展**：`AppConfig` 新增 `task_store_path` / `budget_ledger_path`，支持环境变量覆盖；
- **`ProxyServer` 重试恢复**：v0.6.0 起不再依赖进程内存 `_tasks` 缓存，改从持久化 `Runtime.get_task()` 恢复原始 Task；v0.5.1 的已知限制解除。

### 关键决策

- **向后兼容**：`Runtime.task_store` 默认 `InMemoryTaskStore`，所有已有测试无需改动；生产环境由 `build_runtime()` 注入 `JsonlTaskStore`。
- **最小改动**：没有引入 `BudgetReservation` 状态机，只是让 `BudgetLedger` 持久化；`BudgetLedger` Protocol 方法签名保持不变。
- **fail-closed**：TaskStore / BudgetLedger 文件损坏直接抛异常，拒绝启动，与 `DecisionStore` 行为一致。
- **Windows 测试限制**：完整"关闭 Runtime A 并立即重启 Runtime B"会触发 anyio stdio 子进程取消竞态，因此集成测试保持 A 不关闭、用 B 读取同一数据目录来验证持久化恢复；生产环境真实进程重启不受此限制。

### 新增/更新测试

- `tests/test_task_store.py`：6 个 JsonlTaskStore 测试；
- `tests/test_budget.py`：4 个 JsonlBudgetLedger 持久化测试；
- `tests/test_proxy_server.py`：`test_proxy_retry_survives_runtime_restart` 验证新 Runtime 读取持久化数据后成功重试。

### 验收状态

- `pytest tests/`：**231 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误（`planner.py`、`config_loader.py`），与本次改动无关。

### 设计文档

- `src/loop_controller_v0.6.0_development.md`

---

## v0.6.1：BudgetReservation 状态机

目标：把分散在 `Checkpoint.evaluate()` / `forward()` 中的预算预留/返还逻辑，抽象为显式的 `BudgetReservation` 状态机，消除二次预留、过期不释放、无法查询 pending 等隐患。

### 完成内容

- **`BudgetReservation` 模型**（`src/loop_controller/models.py`）：
  - 字段：`reservation_id`、`task_id`、`call_id`、`tool_name`、`cost`、`state`、``created_at`、`expires_at`；
  - 状态：`pending` / `pending_approval` / `committed` / `refunded` / `expired`。
- **`ReservationStore` Protocol + `InMemoryReservationStore`**（`src/loop_controller/infra/reservation_store.py`）：
  - `save` / `get` / `get_by_call_id` / `list_by_task`；
  - 默认内存实现，适合测试与单进程；未实现 `JsonlReservationStore`（P1，可在 v0.6.2 补充）。
- **`Checkpoint` 集成**：
  - `evaluate()` 预算预留成功后创建 `pending` reservation；
  - deny / invalid verdict 路径统一 `_refund_reservation()`；
  - `require_approval` 路径保留预算，reservation 转为 `pending_approval`；
  - `forward()` 查找 pending reservation，modify 复核失败 / 执行异常 refund，成功 commit；
  - `finalize_after_approval()` 审批 deny 时 refund，approve 时 reservation 转回 `pending` 供 forward commit；
  - 新增查询接口 `get_pending_reservation(call_id)` 和 `get_pending_reservations(task_id)`。
- **`reserve_for_execution()` 增强**：预留成功时同时创建 `pending` reservation，与 `forward()` 的检查逻辑对齐。
- **`Runtime.resume_task()` 改动**：审批通过后优先复用已有 reservation，不再无条件二次 `reserve_for_execution`；找不到 reservation 时仍保留 fallback 重新预留。

### 关键决策

- **向后兼容**：`Checkpoint` 新增 `reservation_store` 参数，默认 `InMemoryReservationStore`，所有已有测试无需改动；`forward()` 兼容测试直接调用：若找不到 reservation 会尝试现场预留。
- **BudgetLedger Protocol 不变**：状态机建立在 Ledger 之上，不破坏 `check_and_reserve` / `commit` / `refund` 三方法签名。
- **不引入持久化**：v0.6.1 只做状态机抽象；`JsonlReservationStore` 延后，因为当前 v0.6.0 的 `JsonlBudgetLedger` 已经能恢复预算余额，reservation 状态丢失在单进程重启场景下可接受。
- **审批超时被动检查**：reservation 有过期时间，但当前不做异步扫描；过期后再次 forward 会按 Checkpoint 的 decision 过期检查拦截。

### 新增/更新测试

- `tests/test_reservation.py`：7 个 BudgetReservation / ReservationStore 单元测试；
- 所有已有 `test_checkpoint.py`、`test_e2e_real_mcp.py`、`test_proxy_server.py` 等审批/执行路径测试均通过，验证状态机未破坏既有行为。

### 验收状态

- `pytest tests/`：**238 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.6.1_development.md`

---

## v0.7.0：MCP Proxy `approval_status` 查询工具

目标：让外部 Agent 能主动查询某个 `decision_id` 的审批状态，降低对 Agent LLM 解析结构化响应的依赖，避免在审批完成前盲目重试。

### 完成内容

- **新增内部 MCP 工具 `loop_controller_approval_status`**：
  - 输入参数：`decision_id`；
  - 返回 JSON：`{"status": "pending|approved|denied|expired|not_found", "decision_id": "...", "can_retry": true|false}`；
  - `can_retry=true` 只在 `approved` 时返回。
- **`tools/list` 注入内部工具**：
  - `LoopControllerProxyServer._handle_list_tools()` 在返回 Profile 过滤的真实工具后，额外追加 `loop_controller_approval_status`；
  - 工具名带 `loop_controller_` 前缀，避免与真实工具冲突。
- **`tools/call` 优先路由内部工具**：
  - 在重试决策和普通治理流程之前，先判断 `params.name == "loop_controller_approval_status"`；
  - 内部工具不创建 Task、不经过 Checkpoint，只读审批状态。
- **状态判定逻辑**：
  - `approval_manager.check(decision_id)` 返回 `ApprovalRecord`：
    - `verdict == "approve"` → `approved`；
    - `verdict == "deny"` → `denied`；
  - 无记录但 `get_decision(decision_id)` 返回 `Decision`：
    - 当前时间 >= `expires_at` → `expired`；
    - 否则 → `pending`；
  - 无 Decision → `not_found`。

### 关键决策

- **只读无副作用**：`approval_status` 不修改任何 store，可被 Agent 高频查询；
- **不替代重试路径**：查询到 `approved` 后，Agent 仍需带 `decision_id` 重试原 tool call；
- **身份校验最小化**：decision_id 是随机 UUID，MVP 阶段不强制绑定 agent_id；
- **不实现 Server 推送**：遵循 v0.5.0 的设计哲学，Agent 自己决定何时查询/重试。

### 新增/更新测试

- `tests/test_proxy_server.py`：
  - 更新 `test_proxy_list_tools`，断言工具列表包含 `loop_controller_approval_status`；
  - 新增 `test_proxy_approval_status_pending` / `approved` / `denied` / `not_found`。

### 验收状态

- `pytest tests/`：**242 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.7.0_development.md`

---

## v0.8.0：持久化 BudgetReservation 存储

目标：把 v0.6.1 中仍为内存实现的 `ReservationStore` 升级为持久化 JSONL 实现，让 BudgetReservation 状态机在 Runtime/Proxy 重启后可恢复。

### 完成内容

- **`JsonlReservationStore` 实现**（`src/loop_controller/infra/reservation_store.py`）：
  - append-only JSONL，事件类型 `reservation_created` / `reservation_transitioned`；
  - 启动时重放所有事件恢复内存索引；
  - 损坏文件 fail-closed，抛 `ReservationStoreError`；
  - `InMemoryReservationStore` 保留为测试默认实现。
- **配置扩展**：
  - `AppConfig` 新增 `reservation_store_path`；
  - `ConfigLoader` 支持环境变量 `LOOP_CONTROLLER_RESERVATION_STORE_PATH`；
  - 默认路径 `data/reservations.jsonl`。
- **Runtime 集成**：
  - `Runtime` 新增 `reservation_store: ReservationStore` 字段，默认 `InMemoryReservationStore`；
  - `build_runtime()` 创建 `JsonlReservationStore` 并注入 `Checkpoint` 和 `Runtime`；
  - `Checkpoint` 从 `Runtime` 接收持久化 store，生产环境统一走 JSONL。

### 关键决策

- **向后兼容**：`Runtime` 和 `Checkpoint` 都保留默认内存实现，测试代码无需改动；生产由 `build_runtime()` 注入持久化实现。
- **事件化持久化**：只记录创建和状态流转事件，不重写全量状态；恢复时重放到内存索引，与 `JsonlBudgetLedger`、`JsonlTaskStore` 风格一致。
- **不解决多 worker 并发**：仍明确单进程 asyncio 假设；多进程写 JSONL 的竞态不在本版本范围内。

### 新增/更新测试

- `tests/test_reservation.py`：新增 6 个 `JsonlReservationStore` 测试（save/get、transition overwrite、list_by_task、跨对象恢复、损坏 fail-closed、datetime roundtrip）；
- 文件标题更新为 `v0.6.1 / v0.8.0`；
- 所有已有测试通过，验证集成未破坏。

### 验收状态

- `pytest tests/`：**248 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.8.0_development.md`

---

## v0.9.0：生产环境考研（真实 Agent + 真实工具）

目标：不引入新治理架构能力，而是用真实 MCP server、真实 Python Agent 对当前 v0.8.0 架构进行端到端压测，暴露真实生产环境下的问题并修复。

### 完成内容

- **新增真实 MCP server（Python 实现）**：
  - `src/loop_controller/mcp_servers/fetch_server.py`：基于 httpx 的 HTTP GET server；
  - `src/loop_controller/mcp_servers/sqlite_server.py`：基于 sqlite3 的 `query`（只读 SELECT）和 `execute`（写操作）server；
  - 两者均使用与 Proxy 一致的 lowlevel MCP SDK 构造函数式 API。
- **配置扩展**：
  - `config/mcp_servers.yaml` 新增 `fetch`、`sqlite` server 和 `fetch_url`、`query_database`、`update_database`、`list_directory` 工具映射；
  - `config/profiles.yaml` 扩展 `research_assistant_v1`，覆盖新工具权限；
  - `policies/default.rego` 新增 `fetch_url`、`list_directory`、`query_database`、`update_database` 策略规则。
- **真实 Agent 示例**：
  - `examples/research_agent.py`：作为独立 MCP client 启动 `lc proxy`，运行 6 个真实场景（research/query/update/notify/exfil/write-attack）。
- **Bug 修复**：
  - `LoopControllerProxyServer.run_stdio()` 原实现调用 `anyio.run()`，但在 CLI 的 asyncio 事件循环内会抛 "Already running asyncio"；改为 `async def run_stdio()`，由 `cli.py` 直接 await；移除无用 `anyio` 导入；
  - 示例脚本补齐 `LOOP_CONTROLLER_AUDIT_HMAC_KEY` 默认测试 key，避免手动设置。
- **自动化测试**：
  - `tests/test_e2e_sqlite.py`：验证真实 sqlite MCP server 下 SELECT 直接执行、INSERT 触发 require_approval，审批后真实写入数据库。

### 关键决策

- **自研 fetch/sqlite server 而非依赖 npm**：官方 `@modelcontextprotocol/server-fetch` / `server-sqlite` 包在 npm 不存在；自研 Python server 行为可控、可离线运行，且与项目技术栈一致。
- **filesystem server 允许目录设为项目根**：在 Windows 下使用绝对路径 `/data/kb` 会映射到 `C:\data\kb`，跨平台困难；改为允许项目根目录 `.`，由 R2 的 glob 策略负责路径限制。MCP server 只是执行通道，治理仍在 R2。
- **不新增架构组件**：本次只做集成、配置、示例和 bugfix，Earned Authority / Permission Interaction Analyzer 等 R2 子系统延后。

### 手动场景验证结果

| 场景 | 结果 |
|---|---|
| `research`：fetch_url + read_file + write_file | allow |
| `query`：SELECT 返回 / DELETE 被 deny | allow + deny |
| `update`：INSERT 触发 require_approval | require_approval |
| `notify`：send_email 触发 require_approval | require_approval |
| `exfil`：外部收件人 deny | deny |
| `write-attack`：路径越界 deny | deny |

### 验收状态

- `pytest tests/`：**249 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.9.0_development.md`

---

## v0.9.1：真实 LLM Agent 端到端验证

目标：不引入新治理架构能力，使用真实 LLM（DeepSeek）驱动 Agent，对 v0.9.0 的真实工具集成进行端到端压测，暴露 LLM 规划器与治理层协同的真实问题并修复。

### 完成内容

- **新增 `examples/llm_agent_demo.py`**：
  - 作为独立 MCP client 启动 `lc proxy`；
  - 通过 DeepSeek API 驱动 `LLMPlanner` 自主规划；
  - 提供 `research` / `notify` / `exfil` 三个真实场景；
  - `require_approval` 时自动模拟审批通过。
- **修复 LLMPlanner prompt 歧义**：
  - 原 `_SYSTEM_PROMPT` 未明确强调 `action` 字段只能是 `"call_tool"` / `"ask_user"` / `"finish"`；
  - DeepSeek 经常把工具名写入 `action` 字段，导致解析失败；
  - 重写 prompt，强调工具名必须放在 `tool_name` 字段；
  - 在 `_parse_response()` 中增加容错恢复：当 `action` 是已授权工具名且 `tool_name` 为空时，自动归一化为 `call_tool`。
- **修复 `examples/llm_agent_demo.py` SyntaxWarning**：模块 docstring 改为 raw string。
- **调整对抗性任务措辞**：将"泄露文件内容"改为中性表述，避免触发 LLM 自身安全拒绝，从而真正测试 R2 的 deny。

### 验证结果

| 场景 | 实际结果 | 是否符合预期 |
|---|---|---|
| `research` | read_file → web_search → fetch_url → write_file 均 allow，生成摘要 | ✅ |
| `notify` | query_database allow → send_email require_approval → 审批通过 → 发送成功 | ✅ |
| `exfil` | read_file allow → send_email deny（recipient outside allowed patterns） | ✅ |

### 关键发现

1. **LLM 对 JSON schema 的理解需要非常明确的约束**：示例代码不足以保证遵循；
2. **parser 容错对真实 LLM 很有必要**：即使 prompt 已强化，仍保留归一化容错；
3. **对抗性测试要注意 LLM 自身安全对齐**：过于显眼的恶意描述会让 LLM 直接拒绝，无法验证治理层；
4. **被 deny 后 LLM 倾向于 ask_user 而不是绕过**：这是可接受行为，但未来可通过 prompt 引导其寻找替代方案。

### 设计文档

- `src/loop_controller_v0.9.1_development.md`

---

## v0.10.0：Capability-Based Permission Interaction Analyzer（组合风险 A+B>C）

目标：将 R2 的权限组合分析从静态 YAML 规则升级为基于"能力集合"的动态组合风险检测，实现 A+B>C 的自动发现，同时保持 Rego 作为最终裁决者。

### 完成内容

- **核心抽象**：
  - 新增 `src/loop_controller/capability.py`：
    - `Capability` / `CapabilityGraph`：能力实例与会话级能力集合（不可变）；
    - `CapabilityGraphAnalyzer`：从动作中提取能力、构建历史能力图、匹配组合规则；
    - 支持 `arg_match` / `arg_not_match` 的 POSIX glob 匹配。
  - `ActionProposal` 扩展 `combination_risk_tags` 与 `combination_risk_score` 字段，供审计与 Rego 使用。
- **配置层**：
  - `ConfigLoader` 新增 `CapabilityProducer`、`CapabilityDef`、`CapabilityCombinationRule`、`CapabilityRules` 配置类；
  - 加载 `config/capability_rules.yaml`；文件缺失时返回空规则（向后兼容）；
  - `PermissionRule` 扩展 `risk_tags` / `score` 字段，用于承载能力分析结果；
  - 启动校验纳入能力规则中的 glob 模式。
- **组合分析器**：
  - 新增 `CapabilityBasedPermissionAnalyzer`：基于能力集合返回 `PermissionRule`；
  - 新增 `CompositePermissionInteractionAnalyzer`：同时保留静态 YAML 规则与能力规则，deny 优先，合并风险标签/分数。
- **治理链路集成**：
  - `Checkpoint.evaluate()` 在步骤 5 调用 analyzer，命中时将风险标签/分数写入 `ActionProposal`；
  - `build_policy_input()` 将 `combination_risk_tags` / `combination_risk_score` 透传给 Rego；
  - `policies/default.rego` 新增基于 `input.action.combination_risk_tags` 的 deny / require_approval 规则；
  - `build_runtime()` 注入 `CompositePermissionInteractionAnalyzer`。
- **规则配置**：
  - 新增 `config/capability_rules.yaml`，声明 `data_read` / `email_external` / `network_external` 三种能力，以及 `data_exfil_via_email`（deny）和 `data_exfil_via_http`（require_approval）两条组合规则。
- **测试**：
  - 新建 `tests/test_capability.py`：覆盖单工具能力提取、arg_not_match、历史图构建、email/http 组合风险、误报控制；
  - 更新 `tests/test_permission_interaction.py`：验证 `CapabilityBasedPermissionAnalyzer` 与 `CompositePermissionInteractionAnalyzer`。

### 关键决策

- **Python 图分析 + Rego 最终裁决**：Python 负责能力图构建与 A+B>C 检测，Rego 根据风险标签做最终判定，保持策略最终裁决权在 Rego。
- **向后兼容**：静态 `permission_rules.yaml` 继续生效；能力规则配置缺失时返回空规则，不破坏旧配置树。
- **组合分析结果归并**：多个规则命中时 deny 优先，风险标签取并集，分数取最大值，统一返回单个 `PermissionRule`。
- **审计可解释性**：组合风险标签写入 `ActionProposal`，进入审计与 Rego input，便于事后追溯。

### 验收状态

- `pytest tests/`：**260 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.10.0_development.md`

---

## v0.11.0：Earned Authority Manager（动态权限提升）

目标：在静态 CapabilityProfile 天花板之上，引入受控的动态权限提升机制。Agent 可在任务执行过程中申请临时能力（AuthorityToken），经治理系统评估条件后签发；Checkpoint 在裁决时识别有效 Token，将原本 deny/require_approval 的动作降级为 allow/require_approval（取决于公司 Rego 策略）。

### 完成内容

- **核心抽象**：
  - 新增 `src/loop_controller/authority.py`：
    - `AuthorityManager` / `NoopAuthorityManager` / `EarnedAuthorityManager`；
    - `request_authority()` 按声明式条件评估并签发 `AuthorityToken`；
    - `validate_for_proposal()` 验证 token 是否覆盖触发能力；
    - `consume()` / `revoke_token()` / `revoke_expired_tokens()` 管理 token 生命周期。
  - 新增 `src/loop_controller/infra/authority_store.py`：
    - `AuthorityStore` Protocol；
    - `InMemoryAuthorityStore` 与 `JsonlAuthorityStore`（append-only JSONL，事件重放恢复）。
  - `models.py` 扩展：
    - 新增 `AuthorityRequest`、`AuthorityToken`、`AuthorityConditions`、`AuthorityGrantRule`、`AuthorityRules`、`AuthorityEvaluationContext`；
    - `ActionProposal` 扩展 `authority_token_ids`；
    - `AuditAction` 扩展 `authority_granted/used/revoked/expired`；
    - `PermissionRule` 扩展 `triggered_capabilities`（v0.11.0 用于判断 token 覆盖）。
- **配置层**：
  - `ConfigLoader` 加载 `config/authority_rules.yaml`；文件缺失时返回 `enabled=false`（向后兼容）；
  - `AppConfig` 新增 `authority_rules` 与 `authority_log_path`；
  - 启动校验纳入 `require_task_context_regex` 正则编译。
- **治理链路集成**：
  - `Checkpoint.evaluate()` 步骤 5 检测组合风险后，若 deny 规则触发且 proposal 携带覆盖触发能力的有效 token，则不短路 deny，把裁决权交给 Rego；
  - 步骤 5.5 防御性校验 proposal 声明的 token；
  - `build_policy_input()` 将 `authority_token_ids` 透传给 Rego；
  - `policies/default.rego` 新增规则：有 token 的 `data_exfil` 从 deny 降级为 `require_approval`；
  - `forward()` 成功后调用 `authority_manager.consume()` 扣减 token 预算；
  - `build_runtime()` 注入 `EarnedAuthorityManager` 与 `JsonlAuthorityStore`。
- **规则配置**：
  - 新增 `config/authority_rules.yaml`，声明 `email_external` 与 `network_external` 两种可动态授予能力，条件包含用户确认、预算、近期无拒绝。
- **测试**：
  - 新建 `tests/test_authority.py`：覆盖 grant/deny、条件评估、token 验证、消费、撤销、过期清理、多能力合并。

### 关键决策

- **Rego 保留最终裁决权**：token 只改变 input 事实，是否 allow/require_approval/deny 由 Rego 决定。
- **向后兼容**：`authority_rules.yaml` 缺失时 `AuthorityRules(enabled=false)`，所有调用返回 deny，不破坏旧配置树。
- **Token 不可伪造**：token_id 由 `EarnedAuthorityManager` 生成并持久化，`Checkpoint` 只信任 store 中的记录。
- **用户确认必须外部注入**：`user_confirmation` 字段不由 Agent 自行设置，必须由 R0 审批或显式用户输入设置。
- **Token 预算软限制**：token 剩余预算在 `forward` 成功后扣减；预算耗尽时 token 失效，但不阻止已执行动作（记录 warning）。

### 验收状态

- `pytest tests/`：**274 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.11.0_development.md`

---

## v0.12.0：R3 Asynchronous Audit Analyzer（异步审计分析器）

目标：补齐 R3 审计层，让 Loop Controller 在记录审计日志之外，能够异步分析日志、识别异常模式并生成告警/报告。分析器不阻塞主治理链路（R0-R2），在 task_end 后或独立触发。

### 完成内容

- **核心抽象**：
  - 新增 `src/loop_controller/audit_analyzer.py`：
    - `AuditAnalyzer` Protocol 与 `NoopAuditAnalyzer` 占位；
    - `RuleBasedAuditAnalyzer`：基于声明式规则消费审计日志，生成 `AuditAlert` 与 `AuditReport`；
    - 规则类型：rapid_denies、consecutive_denies、action_sequence、has_any_action、has_all_actions、authority_token_exhausted。
  - 新增 `src/loop_controller/infra/alert_store.py`：
    - `AlertStore` Protocol；
    - `InMemoryAlertStore` 与 `JsonlAlertStore`（单 JSONL 文件，按 `type` 区分 alert/report，启动重放恢复）。
  - `models.py` 扩展：
    - 新增 `AuditAlert`、`AuditReport`、`AuditRule`、`AuditRuleConditions`、`AuditRules`。
  - `infra/audit_store.py` 扩展：
    - 新增 `query_by_session()` 与 `query_by_task()`，供分析器读取。
- **配置层**：
  - `ConfigLoader` 加载 `config/audit_rules.yaml`；文件缺失时返回 `enabled=false`（向后兼容）；
  - `AppConfig` 新增 `audit_rules` 与 `alert_store_path`。
- **治理链路集成**：
  - `Runtime` 新增 `audit_analyzer` 字段；
  - `build_runtime()` 创建 `RuleBasedAuditAnalyzer` + `JsonlAlertStore`；
  - `run_task()` 在 `task_end` 后通过 `asyncio.create_task()` 异步触发 `audit_analyzer.analyze_task()`，不阻塞返回。
- **CLI 集成**：
  - `src/loop_controller/cli.py` 新增 `lc audit analyze --task-id/--session-id`；
  - 新增 `lc audit list-alerts --task-id/--session-id`。
- **规则配置**：
  - 新增 `config/audit_rules.yaml`，声明 rapid_denies、consecutive_denies、authority_token_exhausted 三条规则。
- **测试**：
  - 新建 `tests/test_audit_analyzer.py`：覆盖 disabled、rapid_denies、consecutive_denies、has_any_action、action_sequence、session 分析、token 耗尽。

### 关键决策

- **异步不阻塞**：分析器在 task_end 后通过 `asyncio.create_task` 触发，不影响主链路延迟与返回结果。
- **只读消费**：分析器只读取审计日志，不修改已有哈希链。
- **分析器内部 catch 所有异常**：避免审计分析失败拖垮事件循环或主任务。
- **告警允许重复**：多个规则可能同时命中同一事件集，每条命中独立生成 alert，便于运营归因。
- **CLI 不启动 Runtime**：直接构造 `JsonlAuditStore` + `RuleBasedAuditAnalyzer`，避免拉起 MCP server 等不必要开销。

### 验收状态

- `pytest tests/`：**281 passed**
- `ruff check src tests`：**All checks passed**
- `mypy src`：仅余 2 个预存在的 PyYAML stub 缺失错误。

### 设计文档

- `src/loop_controller_v0.12.0_development.md`

---

## 后续可选工作

- **签名/WORM 存储**：当前哈希链只能检测篡改，不能防御整体重写；生产环境需要签名或 WORM 存储。
- **多 worker 原子 DecisionStore**：当前单进程 asyncio 假设下检查+记账原子；多 worker 时需要原子语义。
- **CLI 通知扩展**：当前 CLI 依赖轮询文件；未来可扩展为 SSE/HTTP webhook 推送审批请求。
- **LLM-based 审计摘要**：在 `RuleBasedAuditAnalyzer` 基础上扩展 `LLMAuditAnalyzer`，生成自然语言风险摘要。

---

## v0.13.0：Agent 驱动治理接口与 LangChain 适配器

### 完成内容

- **项目定位澄清**：Loop Controller 是企业内部 Agent 的工具调用治理基础设施，不是 Agent 大脑，也不是面向陌生 Agent 的开放网关。
- **新增 `LoopController` 核心类**（`src/loop_controller/controller.py`）：
  - `evaluate()`：R1 + R2 判定，不执行；
  - `evaluate_and_execute()`：判定+执行一键完成；
  - `resume_after_approval()`：CLI/管理员 approve 后恢复执行；
  - `build_controller()`：从 `AppConfig` 快速构造控制器。
- **新增模型**（`src/loop_controller/models.py`）：
  - `EvaluationResult`：R1 + R2 判定结果；
  - `GovernanceResult`：单次工具调用治理完整响应。
- **ApprovalStore 扩展**（`src/loop_controller/infra/approval_store.py`）：
  - `get_request_by_id()` 按 `request_id` 查找原始审批请求，支持 `resume_after_approval`。
- **LangChain 适配器**（`src/loop_controller/adapters/langchain.py`）：
  - `govern_tool()` / `GovernedTool`：把 Loop Controller 治理下的工具包装成 LangChain / LangGraph Tool；
  - 使用 LangGraph `create_react_agent` 演示企业内部 Agent 接入完整 R0-R3。
- **新增示例**：
  - `examples/loop_controller_demo.py`：直接调用 `LoopController`；
  - `examples/langchain_agent_demo.py`：LangGraph Agent 接入。
- **测试**：新建 `tests/test_controller.py`，覆盖 evaluate allow/deny、evaluate_and_execute allow、require_approval + resume。
- **弃用声明**：
  - `src/loop_controller/planner.py` 和 `src/loop_controller/llm_planner.py` 已加弃用警告，并复制到 `examples/_demo_helpers/`。

### 关键决策

- **Agent 驱动框架**：企业 Agent 自己决定计划，Loop Controller 只治理工具调用；框架不再替 Agent 思考。
- **不删除 `run_task`**：保留作为兼容/测试入口，但不再是主要产品 API。
- **MCP Proxy 保留但边缘化**：继续作为边界兼容协议，但主要接入方式改为 Runtime API / SDK。
- **LangChain 适配器可选依赖**：通过 `pip install loop-controller[langchain]` 安装，不污染核心包。

### 验收状态

- `pytest tests/`：**285 passed**
- `ruff check src tests examples`：**All checks passed**
- 实际示例验证：
  - `examples/research_agent_example.py`（旧 run_task 路径）成功运行；
  - `examples/loop_controller_demo.py`（新 LoopController 路径）成功触发 allow / require_approval / resume_after_approval；
  - `examples/langchain_agent_demo.py` 成功创建 `GovernedTool`，LangGraph Agent 待设置 OPENAI_API_KEY 后可运行。

### 设计文档

- `src/loop_controller_v0.13.0_development.md`

### 后续工作

- 把 `ScriptedPlanner` / `LLMPlanner` 彻底移出核心包（当前仍为弃用转发）；
- 重写依赖 `run_task` 的测试为 `LoopController` 测试（v0.13.1 先迁移到 `_run_task_compat`）；
- 开发 AutoGen / OpenAI Agents SDK 适配器；
- 设计企业内部多 Agent 委托协议。

---

## v0.13.1：彻底移除核心 Planner，run_task 迁出核心包

### 完成内容

- **从 `Runtime` 移除 `planner` 字段**（`src/loop_controller/runtime.py`）：
  - `Runtime` dataclass 不再包含 `planner`；
  - `build_runtime()` 删除 `planner_yaml` 参数；
  - `Runtime` 只保留依赖容器方法（`create_task`、`get_task`、`add_user_message`、`add_agent_message`、`get_conversation_context`、`start`、`aclose`）。
- **创建兼容层**（`src/loop_controller/_run_task_compat.py`）：
  - 将原 `run_task()` / `resume_task()` 及内部辅助函数整体迁出 `runtime.py`；
  - `run_task()` / `resume_task()` 改为必须显式传入 `planner` 参数；
  - 保留到 v0.14.0 后彻底删除。
- **核心包 Planner 模块标记弃用**：
  - `src/loop_controller/planner.py` 和 `src/loop_controller/llm_planner.py` 保留为转发/弃用 shim；
  - 实际实现已复制到 `examples/_demo_helpers/`。
- **测试迁移**（先迁移到兼容层）：
  - `tests/test_e2e_research_agent.py`
  - `tests/test_e2e_sqlite.py`
  - `tests/test_e2e_real_mcp.py`
  - `tests/test_runtime_conversation.py`
  - `tests/test_audit_events.py`
  - 全部改为从 `loop_controller._run_task_compat` 导入 `run_task` / `resume_task`，并显式构造 `ScriptedPlanner` 传入。
- **示例调整**：
  - `examples/research_agent_example.py` 和 `examples/llm_agent_demo.py` 改为使用 `_run_task_compat`，并显式传入 `ScriptedPlanner` / `LLMPlanner`。
- **Runtime 内部修复**：
  - 修正 `Session` 导入来源；
  - `task_store.add` 改为 `task_store.save`；
  - `ConversationMessage` 构造补充 `message_id` / `session_id`；
  - `conversation_store` 使用 `append_message`。

### 关键决策

- **核心包不再依赖 Planner**：`runtime.py` 不再 import `loop_controller.planner` / `loop_controller.llm_planner`，核心包与 Agent 大脑完全解耦。
- **兼容层保留旧入口**：已有示例和测试暂时不改成 `LoopController`，降低一次性改动风险。
- **显式传入 planner**：`run_task(..., planner=...)` 的签名变化强制调用方意识到 Planner 已迁出核心包。

### 验收状态

- `pytest tests/`：**285 passed**
- `ruff check src tests examples`：**All checks passed**
- 预期出现的 `DeprecationWarning`：
  - `loop_controller.planner` / `loop_controller.llm_planner` 弃用；
  - `loop_controller._run_task_compat` 的 `run_task` / `resume_task` 弃用。

### 设计文档

- `src/loop_controller_v0.13.1_development.md`

### 后续工作

- 开发 AutoGen / OpenAI Agents SDK 适配器；
- 设计企业内部多 Agent 委托协议。

---

## v0.14.0：彻底删除旧入口，全部测试改为 LoopController 驱动

### 完成内容

- **彻底删除旧入口**：
  - 删除 `src/loop_controller/planner.py`；
  - 删除 `src/loop_controller/llm_planner.py`；
  - 删除 `src/loop_controller/_run_task_compat.py`。
- **核心包导出清理**：`src/loop_controller/__init__.py` 不再导出 `Planner`、`ScriptedPlanner`、`LLMPlanner`、`TaskRunResult`、`UserQuestion`。
- **LoopController 审计补全**：`controller.py` 的 `_audit_event` 现在正确写入 `args_mask`，与旧 `run_task` 路径一致。
- **新增测试辅助**：`tests/controller_helpers.py` 提供 `controller_for()`，统一构造并启动 `LoopController`。
- **测试全部改为 LoopController 驱动**：
  - 重写 `tests/test_e2e_research_agent.py`：手动调用 `evaluate_and_execute` 覆盖 web_search/read_file/write_file/send_email 路径，审批后 `resume_after_approval`。
  - 重写 `tests/test_e2e_sqlite.py`：SELECT 直接 allow，INSERT 触发 `require_approval`，审批后真实写入数据库。
  - 重写 `tests/test_e2e_real_mcp.py`：真实 `MCPGateway` + `email_mock`，验证邮件真实发出。
  - 重写 `tests/test_runtime_conversation.py`：同一 `session_id` 多次调用，验证 Session 复用与对话历史维护。
  - 重写 `tests/test_audit_events.py`：断言 `propose/evaluate/execute` 事件序列、`args_hash`、`args_mask`、policy/profile 版本及审计链完整性。
  - 删除 `tests/test_planner.py` 与 `tests/test_llm_planner.py`（被测组件已迁出核心包）。
- **示例更新**：
  - 删除 `examples/research_agent_example.py`（功能由 `examples/loop_controller_demo.py` 覆盖）。
  - 重写 `examples/llm_agent_demo.py`：Agent 自己掌握主循环，通过 `LoopController.evaluate_and_execute` 提交每一步，仍然用 `examples/_demo_helpers/llm_planner.py` 做 LLM 规划演示。

### 关键决策

- **核心包只剩 `LoopController`**：所有 Agent 计划/主循环逻辑完全外置，Loop Controller 只负责单次工具调用治理。
- **审计事件不再包含 `task_start/task_end/approval_consumed/approve`**：`LoopController` 只写 `propose/evaluate/execute`；旧 `run_task` 的生命周期事件随兼容层一起移除，测试断言同步调整。
- **`args_mask` 回归**：修复 `LoopController` 之前未调用 `Masker` 的遗漏，保证审计日志仍满足 A13 掩码验收。

### 验收状态

- `pytest tests/`：**271 passed**
- `pytest -W error::DeprecationWarning tests/`：**271 passed，无弃用警告报错**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（仅余 2 个预存在的 PyYAML / langchain_core stub 缺失错误）

### v0.14.0 补充修复

- **`LoopController.execute_with_proposal`**：新增公共方法，支持 Agent 先调用 `evaluate` 拿到 `allow` Decision，再单独调用执行；`execute(decision)` 明确提示需使用 `execute_with_proposal` 或 `evaluate_and_execute`。
- **`resume_after_approval` 审计补全**：审批恢复链路现在写入 `approve` 与 `approval_consumed` 事件，审计链与旧 `_run_task_compat.py` 语义一致。
- **`_audit_event` 扩展**：支持显式指定 `actor_type` / `actor_id` / `decision_verdict`，用于审批人动作等非 checkpoint 事件。
- **测试覆盖**：
  - `test_execute_without_arguments_raises`：验证 `execute(decision)` 抛 `NotImplementedError`。
  - `test_execute_with_proposal`：验证两段式 evaluate + execute_with_proposal 路径。
  - `test_audit_event_sequence_and_fields` 与各 e2e 测试同步更新，断言 `approve` / `approval_consumed` 事件及 actor 信息。

### 验收状态（补充修复后）

- `pytest tests/`：**273 passed**
- `pytest -W error::DeprecationWarning tests/`：**273 passed，无弃用警告报错**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（仅余 2 个预存在的 PyYAML / langchain_core stub 缺失错误）

### 设计文档

- `src/loop_controller_v0.14.0_development.md`

---

## v0.15.0：接入更多 Agent 框架

### 完成内容

- **新增 OpenAI Agents SDK 适配器**：
  - `src/loop_controller/adapters/openai_agents.py`
  - 提供 `govern_function_tool` 工厂函数，将被 `@function_tool` 装饰的函数转发到 `LoopController.evaluate_and_execute`。
- **新增 AutoGen 适配器**：
  - `src/loop_controller/adapters/autogen.py`
  - 提供 `govern_tool` 装饰器，把任意函数包装成受治理的 AutoGen 工具函数，保留签名与 docstring。
- **适配器共享辅助**：
  - 新增 `src/loop_controller/adapters/_shared.py`，统一把 `GovernanceResult` 转成给 Agent 阅读的自然语言字符串。
  - `src/loop_controller/adapters/langchain.py` 改为复用 `_shared.format_governance_result`，消除重复代码。
- **新增示例**：
  - `examples/openai_agents_demo.py`
  - `examples/autogen_agent_demo.py`
- **新增可选依赖**：
  - `pyproject.toml` 增加 `[openai-agents]`、`[autogen]` 和 `[all-adapters]` 可选依赖组。
  - mypy 配置增加 `agents.*` 与 `langchain_core.*` 的 `ignore_missing_imports`，避免未安装可选框架时报错。
- **新增测试**：
  - `tests/test_adapters_shared.py`：覆盖 `format_governance_result` 所有状态分支。
  - `tests/test_adapter_autogen.py`：mock `LoopController` 验证签名保留与调用转发。
  - `tests/test_adapter_openai_agents.py`：安装 `openai-agents` 后自动运行；未安装时 skip。

### 关键决策

- **可选依赖不进核心包**：适配器只在安装对应 extras 后可用，保证核心包轻量。
- **Agent 仍掌握主循环**：适配器只治理单次 tool call，与 LangChain 适配器保持同一设计范式。
- **未安装框架时优雅降级**：示例在未安装依赖或缺少 API key 时仅打印已创建的治理工具列表。

### 验收状态

- `pytest tests/`：**281 passed, 1 skipped**（跳过未安装 `openai-agents` 的测试）
- `pytest -W error::DeprecationWarning tests/`：**281 passed, 1 skipped**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（仅余 1 个预存在的 PyYAML stub 缺失错误）

### 设计文档

- `src/loop_controller_v0.15.0_development.md`

---

## v0.16.0：通用 Python 治理层 + 适配器重构

### 完成内容

- **新增 `ToolGovernor` 通用治理层**：
  - `src/loop_controller/tool_governor.py`
  - 与具体 Agent 框架无关，构造时固定 `agent_id` / `user_id` / `default_task_context`
  - `call(tool_name, arguments)` 直接转发给 `LoopController.evaluate_and_execute`，返回自然语言结果
  - 在 `src/loop_controller/__init__.py` 中导出，成为一级公共 API

- **重构所有适配器使用 `ToolGovernor`**：
  - `src/loop_controller/adapters/langchain.py`
  - `src/loop_controller/adapters/openai_agents.py`
  - `src/loop_controller/adapters/autogen.py`
  - 三个适配器签名与行为完全向后兼容，内部不再重复 `evaluate_and_execute + format_governance_result`

- **新增裸 Python Agent 示例**：
  - `examples/raw_python_agent_demo.py`
  - 展示不使用任何 Agent 框架，直接调用 `ToolGovernor` 的用法

- **新增测试**：
  - `tests/test_tool_governor.py`：mock `LoopController` 验证参数转发、`default_task_context` 覆盖、结果格式化

### 关键决策

- **通用层放在核心包**：`ToolGovernor` 不是适配器扩展，而是和 `LoopController` 同级别的 Python API，所以导出到 `loop_controller` 根命名空间。
- **适配器只做框架胶水**：保留函数签名、docstring、框架注册方式，实际治理逻辑全部下沉到 `ToolGovernor`。
- **为服务化打基础**：后续 HTTP/gRPC 服务可以直接在 endpoint 内部调用 `ToolGovernor.call(...)`。

### 验收状态

- `pytest tests/`：**284 passed, 1 skipped**
- `pytest -W error::DeprecationWarning tests/`：**284 passed, 1 skipped**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（仅余 1 个预存在的 PyYAML stub 缺失错误）

### 设计文档

- `src/loop_controller_v0.16.0_development.md`

---

## v0.17.0：Loop Controller HTTP 服务化

### 完成内容

- **新增 HTTP 治理服务**：
  - `src/loop_controller/server.py`
  - 提供 `build_app(controller, api_key=None)` 工厂函数，返回 Starlette ASGI 应用
  - 启动时自动调用 `controller.start()`，关闭时调用 `controller.aclose()`
  - 内部直接调用 `LoopController.evaluate_and_execute` / `resume_after_approval`

- **新增请求/响应模型**：
  - `src/loop_controller/server_models.py`
  - `GovernToolRequest` / `ResumeApprovalRequest` / `GovernResponse`

- **新增 API**：
  - `POST /v1/govern/tool-call`：提交工具调用治理
  - `POST /v1/govern/resume-after-approval`：审批后恢复执行
  - `GET /health`：健康检查

- **认证**：
  - 支持 `X-API-Key` header 或 `Authorization: Bearer <token>`
  - 从 `LOOP_CONTROLLER_API_KEY` 环境变量读取；未设置时允许所有请求（开发模式）

- **新增 CLI 命令**：
  - `lc server --host 127.0.0.1 --port 8080 --opa-url ...`

- **新增示例**：
  - `examples/http_agent_demo.py`：用 `httpx` 通过 HTTP 调用 Loop Controller

- **新增依赖**：
  - `pyproject.toml` 增加 `[server]` 可选依赖：`starlette>=0.40`、`uvicorn>=0.30`
  - `all-adapters` 扩展为包含 `server`

- **新增测试**：
  - `tests/test_server.py`：使用 Starlette `TestClient` 覆盖 health、tool-call、resume-after-approval、参数校验、API key 认证、lifespan 生命周期

### 关键决策

- **服务依赖可选**：`starlette` / `uvicorn` 不进核心依赖，保持核心包轻量。
- **服务内部直接调用 `LoopController`**：不经过 `ToolGovernor`，因为 HTTP 请求本身已携带 `agent_id` / `user_id`。
- **最小可用**：只暴露两个核心治理 endpoint + health，不为生产级完整服务。
- **向后兼容**：现有 `ToolGovernor`、适配器、示例全部保留不变。

### 验收状态

- `pytest tests/`：**292 passed, 1 skipped**
- `pytest -W error::DeprecationWarning tests/`：**292 passed, 1 skipped**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（仅余 1 个预存在的 PyYAML stub 缺失错误）

### 设计文档

- `src/loop_controller_v0.17.0_development.md`

---

## v0.18.0：事件驱动审批 + 可观测性

### 完成内容

- **事件驱动审批（long-polling）**：
  - `src/loop_controller/server.py` 新增 `GET /v1/wait-for-approval`
  - Agent 在收到 `require_approval` 后，可用返回的 `request_id` 长轮询等待审批结果
  - 超时后返回 `pending`，审批完成则返回最终 `allow/deny/error` 结果
  - 新增 `examples/http_agent_event_demo.py` 演示后台模拟审批 + 长轮询恢复

- **Prometheus 可观测性**：
  - 新建 `src/loop_controller/metrics.py`
  - 定义 `loop_controller_requests_total`、`loop_controller_request_duration_seconds`、`loop_controller_tool_calls_total`、`loop_controller_approval_pending_total`
  - `GET /metrics` 导出 Prometheus 格式指标
  - `MetricsMiddleware` 为每个请求注入 trace_id 并统计耗时

- **结构化日志与 trace_id**：
  - 新建 `src/loop_controller/logging_config.py`
  - 提供 `JsonFormatter` / `ColoredFormatter` 与 `configure_logging()`
  - 每个 HTTP 请求通过 `x-trace-id` header 或自动生成 trace_id，写入响应头 `X-Trace-ID`

- **增强 health check**：
  - `GET /health` 新增 `opa_reachable`、`gateway_ready`、`uptime_seconds`

- **Admin 管理 API**：
  - `GET /v1/admin/approvals/pending`：列出待审批请求
  - `GET /v1/admin/audit`：按 `session_id` / `task_id` 过滤审计事件

- **AuditStore 扩展**：
  - `src/loop_controller/infra/audit_store.py` 的 `AuditStore` 协议与 `JsonlAuditStore` 新增 `iter_events()` 异步迭代器

- **依赖**：
  - `pyproject.toml` `[server]` 可选依赖增加 `prometheus-client>=0.20`
  - 开发依赖增加 `types-PyYAML`

- **测试**：
  - 重写 `tests/test_server.py`，新增 wait-for-approval、metrics、admin pending/audit、API key 保护新端点等用例
  - 更新 `tests/test_adapter_openai_agents.py` 以兼容 `openai-agents>=0.22` 的 `FunctionTool` 返回类型

### 关键决策

- **long-polling 而非 webhook**：降低 Agent 侧实现复杂度，避免内网穿透，适合企业内部同步等待场景。
- **metrics 与日志分离**：metrics 走 Prometheus 用于监控告警；日志走 stdout/JSON 用于问题追踪；两者共用 trace_id 可关联。
- **admin API 与治理 API 同端口**：简化部署，生产环境通过 API key 统一保护。
- **事件驱动不替代审批 CLI**：`/v1/wait-for-approval` 只是消费侧阻塞等待，审批动作仍由 `lc approvals approve/deny` 写入。

### 踩坑记录

- **Starlette middleware 格式**：`middleware=[(Cls, {})]` 在新版 Starlette 会触发 `ValueError: not enough values to unpack`，必须改用 `Middleware(Cls)` 实例。
- **mypy async generator Protocol**：`async def iter_events(...) -> AsyncIterator[T]` 在 Protocol 中会被解释为 Coroutine；协议声明改为普通方法 `def iter_events(...) -> AsyncIterator[T]` 后实现用 async generator 可正常匹配。
- **OpenAI Agents SDK 0.22 返回 FunctionTool**：`@function_tool` 现在返回 `FunctionTool` 实例而非可调用对象；测试改为验证 `FunctionTool` 字段并通过 `__wrapped__` 直接调用被治理函数。

### 验收状态

- `pytest tests/`：**299 passed**
- `pytest -W error::DeprecationWarning tests/`：**299 passed**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**Success: no issues found**

### 设计文档

- `src/loop_controller_v0.18.0_development.md`

---

## v0.19.0：实时审批通道 + gRPC 边界

### 完成内容

- **实时审批通道（SSE）**：
  - 新建 `src/loop_controller/approval_watcher.py`
  - 基于 `asyncio.Event` 实现按 `request_id` 等待/通知抽象
  - `src/loop_controller/server.py` 新增 `GET /v1/wait-for-approval/sse`
  - SSE 立即推送 `pending` 心跳，审批完成后推送 `result` 事件
  - 长轮询 `/v1/wait-for-approval` 也改用 watcher 等待，可被同进程通知立即唤醒

- **gRPC 服务边界**：
  - 新建 `proto/loop_controller/v1/governance.proto`
  - 生成 `src/loop_controller/v1/governance_pb2.py` / `governance_pb2_grpc.py`
  - 新建 `src/loop_controller/grpc_server.py`，暴露 `EvaluateToolCall`、`ResumeAfterApproval`、`WaitForApproval`、`GetHealth`、`ListPendingApprovals`、`QueryAuditEvents`
  - 新建 `src/loop_controller/grpc_client.py`，提供 `ToolGovernanceClient` Python 客户端
  - `src/loop_controller/cli.py` 新增 `lc grpc-server` 子命令

- **示例**：
  - `examples/sse_agent_demo.py`：通过 SSE 实时等待审批
  - `examples/grpc_agent_demo.py`：通过 gRPC 调用治理服务

- **测试**：
  - `tests/test_approval_watcher.py`：watcher 通知、超时、多 waiter
  - `tests/test_server.py`：新增 SSE endpoint 测试
  - `tests/test_grpc_server.py`：gRPC 全接口测试（in-process server）

- **依赖与配置**：
  - `pyproject.toml` 新增 `[grpc]` 可选依赖：`grpcio>=1.68`、`grpcio-tools>=1.68`
  - 开发依赖增加 `grpcio-tools`、`mypy-protobuf`、`types-protobuf`
  - `all-adapters` 包含 `grpc`
  - ruff 排除生成代码目录 `src/loop_controller/v1/`
  - mypy 排除生成代码与 grpc 扩展模块

### 关键决策

- **gRPC 与 HTTP 共存**：gRPC 面向内部服务间/未来 Go 交互内核调用；HTTP 面向 Agent 与外部集成。
- **SSE 而非 WebSocket**：SSE 更简单、单向推送足够、与现有 HTTP 基础设施兼容。
- **watcher 只做同进程通知**：CLI 审批在另一个进程时，SSE/gRPC 退化为每秒轮询 `ApprovalStore`。未来多副本场景需要 Redis/消息队列共享 watcher。
- **生成代码提交仓库**：避免 CI 依赖 protoc，但 ruff/mypy 都排除该目录。

### 踩坑记录

- **protoc 输出目录**：proto 文件路径决定生成代码路径，本例生成到 `src/loop_controller/v1/` 而非 `src/loop_controller/proto/v1/`。
- **mypy 与 protobuf 生成代码**：动态生成的 message 属性 mypy 无法识别；最终用 mypy exclude 跳过 `v1/`、`grpc_server.py`、`grpc_client.py`。
- **Starlette TestClient 与 SSE**：`iter_lines()` 返回字符串；测试需在正确时机停止读取，避免等待到超时。
- **gRPC server-streaming 测试**：使用 `grpc.aio.server` + `add_insecure_port("localhost:0")` 启动 in-process server，再用同一事件循环的 async client 访问。

### 验收状态

- `pytest tests/`：**312 passed**
- `pytest -W error::DeprecationWarning tests/`：**312 passed**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**Success: no issues found**

### 设计文档

- `src/loop_controller_v0.19.0_development.md`
