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

## v0.5.0：MCP Proxy / 外来 Agent 接入（规划中）

目标：把 Loop Controller 同时暴露为一个 MCP Server，使未安装 Loop Controller SDK 的第三方 Agent 也能被 R2/R3 治理。

### 规划要点

- 新增 `src/loop_controller/proxy_server.py`，基于 `mcp.server.Server` 实现；
- 外部 Agent 作为 MCP Client 连接，每次 tool call 映射为一个 `ActionProposal`；
- 复用 `Runtime`、`Checkpoint`、`MCPGateway`，不使用 `Planner`；
- 支持 stdio 和 SSE 两种传输；
- CLI 入口 `lc proxy --agent-id xxx --user-id alice [--transport sse --port 8080]`；
- SSE 模式通过 HTTP header 透传 agent/user/session；
- `require_approval` 在 v0.5.0 直接 deny（MCP tool call 同步，无法暂停等待人工审批）；
- 同一连接内多次 tool call 共享 Session，v0.4.0 风险累计生效。

### 设计文档

- `src/loop_controller_v0.5.0_development.md`

---

## 后续可选工作

- **真实 LLM 端到端演示调通**：`config/llm_planner.yaml` 默认关闭；发布/演示前在有 API key 或本地 Ollama 的环境手动跑通，并更新本清单。
- **签名/WORM 存储**：当前哈希链只能检测篡改，不能防御整体重写；生产环境需要签名或 WORM 存储。
- **HMAC 升级**：`AuditEvent.hash_algo` 字段已预留，涉及真实 PII 时触发升级。
- **多 worker 原子 DecisionStore**：当前单进程 asyncio 假设下检查+记账原子；多 worker 时需要原子语义。
- **CLI 通知扩展**：当前 CLI 依赖轮询文件；未来可扩展为 SSE/HTTP webhook 推送审批请求。
- **BudgetReservation 状态机**：当前预算预留/返还逻辑分散在 Checkpoint 各分支，未来可抽象为显式状态机。
