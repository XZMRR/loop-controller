# v0.36.1 发布冻结：治理语义收敛、协议一致性与工程发布基线

> 一句话目标：**不扩展新的产品能力，集中修复 v0.36.0 已暴露的治理语义、A2A 协议和敏感数据持久化问题，补齐类型检查、Python 打包、安装验证、版本一致性与完整 CI 门禁，形成可复验、可安装、可发布的 v0.36 稳定基线。**
>
> 范围限定：本版本是 v0.36.0 的稳定化补丁版本；不实现真实远程 Agent 执行，不引入分布式状态、多租户、跨信任域联邦或新的工具接入方式。

- 状态：**待开发**
- 前置版本：v0.36.0 A2A 自动发现、流式任务与 Runtime 委托集成
- 目标版本：v0.36.1
- 版本性质：发布冻结 / 正确性修复 / 工程基线收敛
- 核心原则：
  - 工具调用治理与 Agent 交互治理保持两个正交平面；
  - Python R2 继续作为工具调用治理权威；
  - Go A2A 内核继续作为交互治理骨架，不在本版本承担真实目标执行；
  - SDK、MCP Proxy、HTTP REST 三种工具接入方式全部保留；
  - 只修复已经存在的语义、协议、安全和发布问题，不为 v0.37.0 提前堆叠新功能。

---

## 1. 背景与版本判定

v0.36.0 已完成以下骨架能力：

- Python 工具治理层的身份、Profile、预算、审批、吊销、执行和审计主链；
- Go A2A Registry、Task、Router、Delegation、Discovery、SSE 与 Token 骨架；
- Python `GoKernelBridge`、Runtime 生命周期和 LoopController 委托门控；
- Durable JSONL、部分 SQLite 状态后端、持久化指标和 Health durability；
- Python/Go 单元与集成测试。

但当前工作区和审计结果表明，v0.36.0 尚不适合作为正式发布基线：

1. `modify` verdict 的参数改写语义与执行前校验冲突；
2. A2A 返回的“委托成功”不能区分授权、Task 创建、消息路由和目标接收；
3. Python、Go 与 proto 中的 Message 数据结构和 HTTP envelope 存在漂移；
4. 一次工具执行可能产生两条同名 `execute` 审计事件；
5. Approval Store 为跨进程恢复保存原始参数，但缺少加密保护；
6. Mypy 当前不能通过，CI lint gate 不完整；
7. Python 项目缺少可靠 build-system，声明的 `lc` entrypoint 不能通过标准安装流程稳定生成；
8. 包版本、Git tag、README、子目录文档、配置和发布说明尚未形成单一事实基线；
9. 工作区仍包含未整理提交的审计修复，不应直接从 dirty workspace 发布。

因此，本版本编号确定为 **v0.36.1**，而不是继续覆盖 v0.36.0。v0.37.0 专用于后续“单实例可靠 A2A 真实委托闭环”。

---

## 2. 架构边界（冻结后必须保持）

### 2.1 双治理平面

```text
工具调用治理平面（Python R2）
├─ Python SDK / @governed
├─ MCP Proxy
├─ HTTP REST API
├─ Policy / Approval / Budget / Revocation
└─ MCP / HTTP / Local / Harness 执行与 R3 审计

Agent 交互治理平面（Go A2A）
├─ Agent Registry / Discovery
├─ Task / Message
├─ Delegation / Token
└─ SSE Task Event
```

v0.36.1 不改变上述职责：

- Python R2 不把工具治理决策权交给 Go；
- Go 不直接执行工具；
- A2A Token 不得被解释为工具调用授权；
- Agent B 的工具调用仍应独立通过 SDK、MCP Proxy 或 HTTP REST 接入 Python R2；
- Go Kernel 未启用时，普通本地工具路径保持原行为；
- 调用显式声明 `__target_agent_id` 且 Go Kernel 已启用但不可达时，保持 fail-closed，并统一代码、测试和文档描述。

### 2.2 本版本不引入 `ActionKind.DELEGATION`

`ActionKind.DELEGATION` 已确定为 v0.37.0 的领域模型方向，但 v0.36.1 不提前修改核心 Proposal 模型。当前 `__target_agent_id` 保留为实验性兼容入口，只修正返回语义和协议一致性。

### 2.3 协议权威来源的过渡

后续 A2A 将以 OpenAPI/JSON Schema 为唯一权威协议来源。v0.36.1 完成过渡准备：

- 明确当前 HTTP/JSON/SSE 是实际线协议；
- 修复 Python/Go 当前模型不一致；
- 增加显式协议版本和跨语言契约测试；
- proto 标记为历史/概念规范，不再作为当前可生成线协议承诺；
- 完整 OpenAPI/JSON Schema 生成链可在 v0.37.0 建立，但本版本不得继续新增手写不一致模型。

---

## 3. 纳入与排除范围

### 3.1 纳入本版本

| 编号 | 内容 | 主要位置 |
|---|---|---|
| FZ-01 | 修正 `modify` 参数重写与执行前复查语义 | `models.py`、`policy_engine.py`、`checkpoint.py`、策略测试 |
| FZ-02 | 拆分 A2A 委托授权、Task 创建、消息路由和目标接收状态 | `go_kernel_bridge.py`、`controller.py`、Go API 模型与测试 |
| FZ-03 | 统一 Python/Go Message Part 和 HTTP envelope | `go/internal/models/`、`go/internal/api/`、`go_kernel_bridge.py`、契约测试 |
| FZ-04 | 消除同名 `execute` 审计事件歧义 | `checkpoint.py`、`controller.py`、审计分析测试 |
| FZ-05 | 加密 Approval Store 原始参数与原始 Decision 敏感载荷 | `models.py`、`infra/approval_store.py`、配置与迁移测试 |
| FZ-06 | 修复全部 Mypy 错误和无效配置 | `src/`、`pyproject.toml` |
| FZ-07 | 增加标准 Python build-system 与 src-layout 打包配置 | `pyproject.toml` |
| FZ-08 | 增加 wheel/sdist、安装与 CLI smoke gate | CI、测试脚本 |
| FZ-09 | 统一版本、配置、README、限制与发布说明 | `pyproject.toml`、README、KNOWN_LIMITATIONS、配置 |
| FZ-10 | 清理已移除 gRPC 服务的残留配置和文档 | `config/entrypoints.yaml`、`config_loader.py`、文档、测试 |
| FZ-11 | 增加协议版本检查和 Python/Go contract tests | A2A API、Bridge、Go/Python 测试 |
| FZ-12 | 整理工作区并从干净 clone 完成完整发布复验 | Git/CI/发布流程 |

### 3.2 明确不纳入

| 编号 | 内容 | 计划 |
|---|---|---|
| FZ-N1 | 真实调用目标 Agent entrypoint | v0.37.0 |
| FZ-N2 | `ActionKind.DELEGATION` 与委托专用 R2 策略 | v0.37.0 |
| FZ-N3 | Go SQLite Task/Message/Event 持久化 | v0.37.0 |
| FZ-N4 | mTLS A2A 服务身份 | v0.37.0 |
| FZ-N5 | Task accepted/running/completed/cancelled 完整状态机 | v0.37.0 |
| FZ-N6 | Token 消费端验证、JTI、参数摘要与密钥轮换 | v0.37.0 |
| FZ-N7 | SSE 重放、heartbeat、轮询 fallback | v0.37.0 |
| FZ-N8 | 分布式发现、PostgreSQL、消息总线、多副本 | v0.38.0+ |
| FZ-N9 | 多租户、跨信任域联邦、统一企业 RBAC | 后续企业化阶段 |
| FZ-N10 | Policy Compiler、Permission Graph、R3 LLM 分析 | 后续智能治理阶段 |
| FZ-N11 | v0.34 全部耐久性欠账 | 单独版本处理，不阻塞本次补丁中未关联发布正确性的项 |

---

## 4. FZ-01：`modify` 治理语义收敛

### 4.1 当前问题

当前 `Checkpoint.forward()` 要求 `decision.modified_args` 的规范 JSON 与原 `proposal.arguments` 完全一致，导致 OPA 返回真正修改后的参数时被执行前检查阻断。`modify` 因而名义存在、实际不能安全改写参数。

### 4.2 目标语义

`modify` 表示：**R2 策略允许在明确约束下收紧或规范化参数，并以最终参数执行。**

必须保存三个视图：

- `original_args`：Agent 原始申报参数；
- `policy_modified_args`：策略返回的候选参数；
- `effective_args`：通过复核、最终执行的参数。

### 4.3 安全不变量

1. `modified_args` 必须是 JSON object，不能为任意标量；
2. 修改不能改变 `tool_name`、`agent_id`、`user_id`、`task_id`、`session_id`；
3. 保留治理参数不得作为普通工具参数注入执行器；
4. 修改后必须重新执行工具 Profile 参数约束；
5. 修改后必须重新执行 Permission Interaction/Capability 分析；
6. 修改后必须重新调用 OPA 做最终确认，且使用防循环标记；
7. 二次策略不得再次返回不同的 `modify`，否则 fail-closed；
8. 最终参数摘要必须绑定 Decision，并在 `forward()` 中复验；
9. 审计记录只保存掩码视图、字段差异和摘要，不保存未掩码敏感值；
10. Budget 成本如依赖参数，必须按最终参数重新估算或拒绝修改。

### 4.4 建议流程

```text
原始 Proposal
    ↓
初次 Profile / Interaction / OPA
    ↓ modify(candidate)
校验候选结构与禁止字段
    ↓
以候选参数构造复核 Proposal
    ↓
Profile + Interaction + OPA(final_confirmation=true)
    ↓
allow → 签发绑定 effective_args_hash 的 Decision
其他结果 → fail-closed / require_approval / deny
```

### 4.5 验收测试

- 修改可允许字段后能够执行；
- 修改为 Profile 禁止值时拒绝；
- 修改导致组合风险升级时要求审批或拒绝；
- 二次 `modify` 循环时拒绝；
- `modified_args` 在 Decision 签发后被篡改时拒绝；
- 审计包含差异字段名和摘要，不包含敏感原值；
- 既有 `allow/deny/require_approval` 行为不回归。

---

## 5. FZ-02：A2A 委托返回语义收敛

### 5.1 当前问题

当前 `delegated=true` 只能证明 Go Delegator 允许并建立了 Task；Router 只在内存保存消息，Python 又可能忽略 `route_message()` 失败。因此“委托成功”不能代表目标 Agent 已接收任务。

### 5.2 v0.36.1 状态定义

本版本尚无真实目标 Agent entrypoint，只允许报告以下事实：

| 状态 | 含义 |
|---|---|
| `authorized` | Python 本地工具治理允许进入 A2A 委托门控，且 Go Delegator 允许 |
| `task_created` | Go Task 已创建或确认存在 |
| `message_recorded` | Router 已成功记录委托消息 |
| `target_accepted` | v0.36.1 永远不得报告；v0.37.0 才实现 |

### 5.3 返回规则

- Go Delegator 拒绝或不可达：返回 blocked，保留机器可判定原因；
- Task 创建失败：不得返回成功；
- Token 签发失败：委托 fail-closed，不得返回空 Token 的 allowed；
- Message 路由失败：不得返回 `message_recorded=true` 或笼统 `delegated=true`；
- v0.36.1 可保留 `delegated` 兼容字段，但它只能等于 `authorized && task_created && message_recorded`，并标记为过渡字段；
- 任何返回不得暗示目标 Agent 已经执行。

### 5.4 文档一致性

统一修正文档中的 failover 描述：

- Go Kernel 未启用：普通本地工具调用不受影响；
- 明确请求 `__target_agent_id` 且 Kernel 已启用但不可达：fail-closed；
- 不允许隐式回退到本地执行目标委托，因为这会改变执行主体和信任边界。

---

## 6. FZ-03：A2A Message 与 HTTP 契约一致性

### 6.1 当前问题

- Python data Part 使用 `{"type": "data", "data": {...}}`；
- Go `Part` 使用 `data_json string`；
- proto 的 `SendMessageRequest` 使用 message envelope；
- 当前 HTTP handler 直接接收 Message；
- 三者不能被视为同一线协议。

### 6.2 v0.36.1 统一契约

建议当前 HTTP JSON 使用结构化 `data`：

```json
{
  "message_id": "msg-...",
  "task_id": "task-...",
  "from_agent_id": "agent-a",
  "to_agent_id": "agent-b",
  "parts": [
    {"type": "text", "text": "tool-name"},
    {"type": "data", "data": {"key": "value"}}
  ],
  "protocol_version": "0.36.1"
}
```

规则：

- `text` Part 只允许 `text`；
- `data` Part 只允许 JSON object/array/value；
- 不再用字符串承载二次编码 JSON；
- Python 和 Go 对未知 Part type 均 fail-closed；
- 请求体大小设置明确上限；
- 错误响应使用统一 envelope；
- proto 标记为非权威历史规范；
- 增加同一 fixture 在 Python 和 Go 双向解码的 contract test。

### 6.3 协议版本

- 请求和响应增加 `protocol_version`；
- Bridge 启动或首次调用时读取 Kernel 版本；
- patch 差异允许告警兼容；
- 不兼容 major/minor 差异 fail-closed；
- `/health` 或单独 metadata endpoint 返回 server protocol version；
- 版本比较规则写入测试，不只写日志。

---

## 7. FZ-04：执行审计事件语义

### 7.1 当前问题

`Checkpoint` 与 `LoopController` 都可能写 `action="execute"`，导致一次工具调用出现两条同名事件，下游统计可能误判执行次数。

### 7.2 目标事件

建议拆分为：

- `execution_authorized`：PEP 已完成最终复查并消费 Decision；
- `execution_completed`：执行器返回最终结果；
- `execution_failed`：执行器明确失败；
- `execution_outcome_unknown`：远端超时后不能判断是否执行；
- `execution_blocked`：执行前复查阻断。

若为兼容性必须保留 `execute`，只能保留一条最终结果事件，并通过 `phase` 区分；不得让两个组件独立写同名最终事件。

### 7.3 验收

- 一次成功调用只有一个最终执行结果事件；
- R3 执行次数统计按最终事件计数；
- 审批恢复路径与直接执行路径事件一致；
- Harness 超时不能错误记录为“未执行”；
- 事件包含同一 trace/call/decision/task 关联键；
- 旧审计文件仍可读取和验证哈希链。

---

## 8. FZ-05：Approval Store 敏感载荷加密

### 8.1 当前问题

ApprovalRequest 为跨进程恢复保存 `tool_arguments` 和 `original_decision`。Audit 虽然掩码，但审批 JSONL 中可能存在 API Key、SQL、个人信息或业务敏感参数。

### 8.2 设计目标

- 审批展示继续使用 `arguments_masked`；
- 原始恢复载荷必须使用 authenticated encryption；
- 推荐 AES-256-GCM；
- 密钥只从环境变量或 Secret Broker 获取，不写入配置文件；
- 每条记录使用随机 nonce；
- AAD 至少绑定 `request_id`、`call_id`、`agent_id`、`tool_name` 和 schema version；
- 密钥缺失时，存在需要持久化原始参数的审批能力必须 fail-closed；
- 解密或认证失败时进入 write-blocked/degraded，并阻止审批恢复执行；
- 日志、异常和审计不得输出明文或密钥。

### 8.3 兼容与迁移

v0.36.1 必须明确旧明文 Approval JSONL 的处理方式：

1. 提供一次性、幂等迁移命令或启动前离线迁移工具；
2. 迁移前创建只读备份；
3. 加密文件落盘成功并 fsync 后才替换；
4. 迁移失败不得破坏原文件；
5. 默认不在运行时静默接受明文新记录；
6. 是否临时允许读取旧明文必须由显式迁移开关控制，并输出高等级告警；
7. 发布说明明确旧文件备份的敏感性和销毁要求。

### 8.4 验收

- 新审批记录磁盘中搜索不到原始敏感值；
- 篡改 ciphertext、nonce、AAD 任一字段均无法恢复；
- 错误密钥不能恢复审批；
- 并发/多进程写入保持原 durable 语义；
- 迁移可重复执行且不会重复加密；
- 审批通过、拒绝、超时、清理和恢复测试全绿。

---

## 9. FZ-06/FZ-07/FZ-08：类型、打包与安装基线

### 9.1 Mypy

发布门禁：

```powershell
python -m mypy src
```

必须为 0 error，且：

- 不通过大范围 `ignore_errors` 或扩大 exclude 绕过；
- 删除无效的 `langchain_core.*` override，或确保对应模块仍在检查范围；
- 对外模型、Bridge、Runtime、Server 和 ConfigLoader 优先补齐准确类型；
- 对可选依赖使用局部、可解释的 ignore。

### 9.2 Python build-system

`pyproject.toml` 必须声明标准构建后端，并正确支持 `src/` layout。可选择 Hatchling 或 setuptools，但必须满足：

```powershell
uv build
```

产生 wheel 与 sdist，且发布包只包含预期源码、配置/策略资源和许可证文件。

### 9.3 安装 smoke test

CI 在隔离环境中执行：

```powershell
python -m venv .venv-release
.venv-release\Scripts\python.exe -m pip install dist\loop_controller-0.36.1-py3-none-any.whl
.venv-release\Scripts\lc.exe --help
.venv-release\Scripts\python.exe -c "import loop_controller"
```

Linux CI 使用对应 POSIX 路径。不得依赖仓库根目录的 `pythonpath` 或 editable install 掩盖缺失包文件。

### 9.4 安装文档

- `dev` 是 dependency group，不得继续宣传为 `pip install -e ".[dev]"`，除非改成真实 extra；
- 区分用户安装、开发安装和 server extra；
- README 中所有 `lc` 命令必须由安装 smoke test 保证可用。

---

## 10. FZ-09/FZ-10：版本与遗留入口收敛

### 10.1 版本一致性

以下位置发布时统一为 `0.36.1` / `v0.36.1`：

- `pyproject.toml`；
- `uv.lock` 项目元数据；
- 根 README；
- `src/README.md`；
- `src/KNOWN_LIMITATIONS.md`；
- `go/README.md`；
- `config/go_kernel.yaml` local Agent version；
- 协议 metadata；
- Release notes；
- Git annotated tag。

CI 必须自动比较版本，禁止人工漏改。

### 10.2 gRPC 残留

v0.32.0 已移除 Python gRPC 服务，因此 v0.36.1 应：

- 保持已删除的 `governance_pb2*.py` 不再生成；
- 移除不再消费的 `entrypoints.grpc` 配置和校验；
- 删除误导性的 CLI/README 示例；
- 若为旧配置兼容保留解析，只允许发出明确弃用错误，不得表现为可运行入口；
- proto/A2A 规范与旧 governance gRPC 服务严格区分。

### 10.3 文档事实源

发布后事实源优先级：

1. 当前版本 README；
2. `KNOWN_LIMITATIONS.md`；
3. v0.36.1 开发/发布文档；
4. OpenAPI/JSON Schema 协议；
5. 历史版本开发文档。

历史文档不得被改写成当前承诺，但应标注已过时行为差异。

---

## 11. CI 与发布门禁

### 11.1 必须通过的自动化 Gate

| Gate | 命令/要求 |
|---|---|
| 格式与静态检查 | `python -m ruff check src tests` |
| 类型检查 | `python -m mypy src`，0 error |
| Python 单元测试 | `python -m pytest tests/ -m "not integration" -q` |
| Python 集成测试 | 启动固定版本 OPA 后执行 integration，保持全绿 |
| Go 单元测试 | `cd go && go test ./...` |
| Go race | `cd go && go test -race ./...`（支持的平台） |
| 配置加载 | 默认 config 与生产样例均可加载，危险默认值生产下 fail-closed |
| 构建 | `uv build` 生成 wheel + sdist |
| 安装 | 从 wheel 新环境安装成功 |
| CLI | `lc --help` 成功 |
| 包导入 | 不依赖仓库 `pythonpath` 导入成功 |
| 协议契约 | Python/Go 共用 fixtures 全部通过 |
| Git diff | `git diff --check` 通过 |
| 工作区 | 发布 tag 前 `git status --porcelain` 为空 |
| 版本一致性 | 包、配置、协议、文档、tag 一致 |

### 11.2 CI 可复现性

- `uv sync --locked --dev`，禁止 CI 隐式更新锁文件；
- CI uv 版本与本地开发基线对齐；
- OPA 二进制固定版本并校验 checksum；
- `go.mod` 与 `go.sum` 同时提交；
- wheel smoke test 在全新环境执行；
- 发布只允许从受保护分支和干净 tagged commit 触发。

### 11.3 测试数量

开发文档不再把某个固定 passed 数量作为永久标准。验收报告应记录当次实际结果，但门禁以：

- 收集无异常；
- 预期 skip 有明确原因；
- 所有非预期测试均通过；
- 新增语义路径有针对性测试；

为准，避免后续新增测试导致文档数字立即过时。

---

## 12. 测试计划

### 12.1 Python 定向测试

新增或扩展：

- `tests/test_checkpoint_modify.py`
- `tests/test_audit_execution_semantics.py`
- `tests/test_approval_store_encryption.py`
- `tests/test_go_kernel_bridge.py`
- `tests/test_go_kernel_integration.py`
- `tests/test_config_loader.py`
- 打包/CLI smoke test

测试重点：

- modify 二次复核、防循环、防篡改；
- 审计事件唯一性和旧链兼容；
- Approval 密文、AAD、错误密钥、迁移和多进程；
- Bridge 协议版本和错误语义；
- route 失败不报告成功；
- 默认配置和生产危险默认值。

### 12.2 Go 定向测试

扩展：

- `go/internal/models` JSON contract tests；
- `go/internal/api` 请求/响应 envelope tests；
- Delegator Token 签发失败必须拒绝；
- Publisher 并发取消/发布 race tests；
- HTTP body limit、未知字段和未知 Part type；
- protocol version compatibility tests。

### 12.3 跨语言契约测试

在仓库维护一组版本化 JSON fixtures：

- Agent Card；
- Task；
- Message（text/data）；
- Delegation request/response；
- Error response；
- SSE event。

Python 和 Go 都必须：

1. 解码同一 fixture；
2. 验证字段含义；
3. 重新编码后通过 canonical JSON 比较；
4. 对缺字段、未知类型、不兼容版本给出相同错误类别。

---

## 13. 数据兼容与回退

### 13.1 审计

- 不重写既有 Audit JSONL；
- 旧 `execute` 事件继续可验证；
- R3 Analyzer 对新旧事件建立兼容映射；
- 新事件 schema 必须带版本；
- 回退代码不得导致新事件无法被旧版本识别为安全失败。

### 13.2 Approval

- 发布前先备份旧 Approval 文件；
- 明文到密文迁移必须离线、幂等、原子；
- 回退到 v0.36.0 前必须确认旧版本无法安全读取新密文，因此回退流程要么恢复受保护备份，要么停止审批恢复入口；
- 禁止自动把密文降级回明文。

### 13.3 A2A

- 新协议版本不兼容时 fail-closed；
- v0.36.1 不承诺与任意外部 A2A 实现互操作；
- `delegated` 兼容字段在 v0.37.0 前保留，但文档标记弃用；
- 本版本不得把 `message_recorded` 迁移解释为 `target_accepted`。

### 13.4 发布回退

发布回退必须包括：

- Python wheel/sdist；
- Go 二进制或源码提交；
- 配置版本；
- Approval 加密迁移状态；
- 数据备份位置；
- 已知不兼容点。

不能只回退代码而不处理数据格式。

---

## 14. 安全审查清单

发布前逐项确认：

- [ ] `modify` 不能扩大权限或改变治理身份字段；
- [ ] 修改后参数重新经过必要治理检查；
- [ ] A2A route 失败不报告委托成功；
- [ ] Go Token 签发失败时 fail-closed；
- [ ] Message data 不被静默丢弃；
- [ ] 不兼容协议版本 fail-closed；
- [ ] Approval 原始参数磁盘不可明文检索；
- [ ] Approval 错误密钥/篡改不能恢复执行；
- [ ] 审计中没有敏感原值；
- [ ] 一次执行只有一个最终结果事件；
- [ ] static identity、默认 Token secret 等危险配置在生产模式拒绝启动；
- [ ] SDK 文档明确合作式边界；
- [ ] MCP/HTTP 强治理入口的认证和绕过限制描述准确；
- [ ] Harness 不被描述为通用生产沙箱。

---

## 15. 交付物

v0.36.1 完成时应交付：

1. 治理语义和协议修复代码；
2. Approval 加密与迁移能力；
3. Python/Go 契约 fixtures 和测试；
4. 标准 Python wheel 与 sdist；
5. 可安装的 `lc` CLI；
6. 完整 CI gate；
7. 更新后的 README、KNOWN_LIMITATIONS 和版本文档；
8. v0.36.1 Release notes；
9. 从干净 clone 执行的发布验证记录；
10. annotated Git tag `v0.36.1`，只指向通过全部 gate 的干净提交。

---

## 16. 完成定义（Definition of Done）

只有同时满足以下条件，v0.36.1 才能标记“已完成”：

### 正确性

- [ ] FZ-01 至 FZ-05 全部实现并有回归测试；
- [ ] 无已知 P0/P1 语义缺陷被仅通过文档掩盖；
- [ ] 新旧数据兼容和回退流程已验证。

### 工程

- [ ] Ruff 通过；
- [ ] Mypy 0 error；
- [ ] Python unit/integration 全绿；
- [ ] Go test 与支持平台上的 race test 全绿；
- [ ] wheel/sdist 构建成功；
- [ ] wheel 安装、`lc --help`、import smoke 全部成功；
- [ ] 默认配置加载和生产配置 fail-closed 测试通过；
- [ ] CI 使用锁文件且不产生隐式依赖变更。

### 发布

- [ ] 工作区干净；
- [ ] 当前改动经过独立代码审查；
- [ ] 包、配置、协议、文档、tag 版本统一为 v0.36.1；
- [ ] 发布说明包含已知限制和数据迁移说明；
- [ ] 从干净 clone 完成完整复验；
- [ ] tag 不从 dirty workspace 或未通过 gate 的提交创建。

---

## 17. 推荐实施顺序

为减少交叉修改和回归，按以下顺序推进：

1. **建立冻结基线**：保存当前测试、Mypy、构建、Git 状态；整理审计修复改动；
2. **协议与审计语义**：FZ-02、FZ-03、FZ-04；
3. **modify 正确性**：FZ-01；
4. **Approval 加密与迁移**：FZ-05；
5. **类型和配置清理**：FZ-06、FZ-10；
6. **打包与安装**：FZ-07、FZ-08；
7. **版本和文档统一**：FZ-09、FZ-11；
8. **完整回归和安全审查**；
9. **干净 clone 发布演练**；
10. **创建 v0.36.1 tag 和 Release**。

任何步骤发现新的 P0 正确性或数据安全问题，应停止发布冻结并先修复；不得为了维持计划而降低 gate。

---

## 18. v0.37.0 接续边界

v0.36.1 完成后，v0.37.0 按已确定方向开发：

- OpenAPI/JSON Schema 作为 A2A 唯一权威协议；
- `ActionKind.DELEGATION`；
- Python R2 通过 HTTP 对委托动作授权；
- Go 使用 SQLite 保存 Task、Message、Event、幂等键和状态迁移；
- mTLS 服务身份；
- 异步 HTTP Agent entrypoint；
- 同一信任域首发；
- Agent B 独立通过三种入口治理其工具调用；
- 分层审批；
- 尽力取消 + 状态查询 + `outcome_unknown`；
- 元数据入库，大产物使用外部引用；
- 不在 v0.37.0 同时引入分布式消息总线、多副本和跨域联邦。

v0.36.1 不得提前实现上述大功能，但所有冻结修复应为该方向消除歧义和协议债务。
