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

## 后续可选工作

- **T3.5 LLMPlanner**：按方案 §5.1 JSON Schema 契约实现，优先级最低。
- **签名/WORM 存储**：当前哈希链只能检测篡改，不能防御整体重写；生产环境需要签名或 WORM 存储。
- **HMAC 升级**：`AuditEvent.hash_algo` 字段已预留，涉及真实 PII 时触发升级。
- **多 worker 原子 DecisionStore**：当前单进程 asyncio 假设下检查+记账原子；多 worker 时需要原子语义。
