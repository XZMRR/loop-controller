# Loop Controller v0.30.0–v0.36.0 开发文档-代码一致性审计报告

**审计日期**：2026-09-01  
**工作目录**：`c:\Users\26343\Desktop\loop-controller`  
**审计范围**：`src\loop_controller_v0.30.0_development.md` 至 `src\loop_controller_v0.36.0_development.md`，对应源码 `src\loop_controller\`、`go\`、`tests\`、`config\`、`proto\`、`.github\workflows\` 等。  
**审计方法**：阅读开发文档提取目标 → 在源码/测试/配置中检索对应实现 → 实测关键测试命令 → 按“已实现 / 部分实现 / 未实现 / 无法确认”逐项判定。

---

## 一、总体结论

7 个版本的核心功能**基本都已实现**，当前 `pytest tests/ -m "not integration"` 为 **791 passed, 4 skipped, 22 deselected**，集成测试 **22 passed**，`ruff check src tests` 全绿。但普遍存在以下问题：

1. **版本号与标签管理混乱**：`pyproject.toml` 仍为 `0.32.0`，代码注释/开发文档混用 v0.33.0/v0.35.0/v0.36.0。  
2. **配置样例滞后或不一致**：`config/go_kernel.yaml` 引用不存在的 `config/agents.json`；v0.34.0 的 `config/state.yaml` 完全缺失。  
3. **可观测性未闭环**：v0.30.0 要求的持久化 Prometheus 指标完全未实现；Health 未暴露 `durability="unsafe"`。  
4. **部分“可选/风险缓解”能力未落地**：A2A 持续 watch、远程发现 fail-soft、SSE 轮询 fallback、MCP 限流挂载等。  
5. **范围蔓延**：v0.35.0 文档声明“不改动 Python R2 主流程”，但 `controller.py` 已接入跨 Agent 委托门控；v0.36.0 的部分能力在 v0.35.0 中已提前实现。

---

## 二、v0.30.0 持久化一致性与崩溃恢复加固

### 2.1 文档目标

实现跨进程安全的 JSONL 持久化原语，将 Budget、Decision、Approval、Reservation、Authority、RiskState、Session、Conversation、Task、Alert、Audit、Evidence、Revocation 等 Store 迁移到统一 durable 写入；引入 `PersistenceProbe` 启动探测；补齐 Health/Prometheus 可观测性。

### 2.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | 共享 `DurableJsonlFile` 原语 | ✅ 已实现 | [`src/loop_controller/infra/durable_io.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/durable_io.py) |
| 2 | sidecar 锁文件 + `portalocker` | ✅ 已实现 | `durable_io.py` |
| 3 | 完整 bytes 写入 + flush/fsync | ✅ 已实现 | `durable_io.py` |
| 4 | 物理残尾自动截断 | ✅ 已实现 | `durable_io.py` |
| 5 | tail repair 告警元数据 | ❌ 未实现 | 截断后未记录 `truncated_bytes`、`tail_hash`，未发 alert |
| 6 | 中间损坏 fail-closed（Decision/Budget/Reservation/Authority/Approval/RiskState） | ✅ 已实现 | 各 Store `_refresh_locked` 抛错 + `PersistenceProbe.write_blocked` |
| 7 | Audit/Evidence 损坏 write-blocked + alert | ✅ 已实现 | `JsonlAuditStore.write_blocked`、`EvidenceChain.degraded_reason` |
| 8 | Session/Conversation/Task 损坏 degraded | ⚠️ 部分实现 | `Session._load` 实际 fail-closed，与文档 degraded 有差异 |
| 9 | 先落盘后更新内存 | ✅ 已实现 | `budget.py`、`decision_store.py`、`approval_store.py` 等 |
| 10 | 原子替换 `durable_atomic_replace` | ✅ 已实现 | `durable_io.py` |
| 11 | Evidence checkpoint 迁移 | ✅ 已实现 | [`src/loop_controller/audit/evidence.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/audit/evidence.py) |
| 12 | Revocation YAML 原子替换 | ✅ 已实现 | [`src/loop_controller/identity/revocation.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/identity/revocation.py) |
| 13 | `PersistenceProbe` 启动探测 | ✅ 已实现 | [`src/loop_controller/infra/persistence_probe.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/persistence_probe.py) |
| 14 | POSIX 0700/0600 权限基线 | ✅ 已实现 | `durable_io.py` |
| 15 | Health persistence 字段 | ⚠️ 部分实现 | [`src/loop_controller/server.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/server.py) 已暴露摘要，但缺少 `durability="unsafe"` |
| 16 | Prometheus 持久化指标 | ❌ 未实现 | [`src/loop_controller/metrics.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/metrics.py) 无 `loop_controller_persistence_*` 指标 |
| 17 | 生产 profile 强制 `fsync_enabled=true` | ❌ 未实现 | [`src/loop_controller/infra/config_loader.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/config_loader.py) 未校验 |
| 18 | 多进程竞争测试 | ✅ 已实现 | `tests/test_stage_b_multiprocessing.py`、`tests/test_stage_cd_multiprocessing.py` |

### 2.3 主要缺口

1. **持久化指标完全缺失**：无法观测 fsync 耗时、锁等待、tail repair、损坏事件。  
2. **生产 fsync 强制校验缺失**：生产 profile 可能意外关闭 fsync。  
3. **tail repair 无审计元数据**：截断操作未留下证据。  
4. **部分 Store 读取路径未统一**：`TaskStore.get`、`AlertStore._replay`、Audit/Evidence 只读查询仍用裸 `open/read_text`，跨进程读取可能读到中间状态。  
5. **Session 损坏策略与文档不符**：实际 fail-closed，文档要求 degraded。

### 2.4 结论

核心 durable I/O 与 Store 跨进程事务已落地，但可观测性与生产强制校验未闭环，尚未完全达到开发文档自述的“可验证的持久化语义”。

---

## 三、v0.31.0 Harness 协议 v2 与执行模式

### 3.1 文档目标

引入 Harness 协议 v2（含沙箱回执）、执行模式解析器、独立策略校验器、内置子进程/Docker 后端、HTTP/gRPC 管理接口、配置 `execution_policy.yaml`。

### 3.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | `ExecutionModeResolver` | ⚠️ 部分实现 | [`src/loop_controller/execution_mode.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/execution_mode.py) 已实现枚举与基础分支，但缺少 Kill Switch / Revocation 优先、风险等级兜底映射 |
| 2 | Harness 协议 v2 | ✅ 已实现 | [`src/loop_controller/executors/harness_protocol.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/harness_protocol.py) |
| 3 | `HarnessPolicyValidator` 独立模块 | ❌ 未实现 | 校验逻辑内联在 `HarnessExecutor` 中，未做“更宽松拒绝”分级 |
| 4 | 内置隔离子进程后端 | ⚠️ 部分实现 | [`src/loop_controller/executors/isolated_subprocess_harness.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/isolated_subprocess_harness.py) 仅支持 `deny_all`，无真正文件/网络拦截 |
| 5 | Docker 后端 | ⚠️ 部分实现 | [`src/loop_controller/executors/docker_harness_backend.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/docker_harness_backend.py) 仅构造命令，未处理 runner 协议协商 |
| 6 | `Checkpoint.forward()` 执行模式路由 | ⚠️ 部分实现 | [`src/loop_controller/checkpoint.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/checkpoint.py) 已接入，但健康检查/fail-closed/fallback 逻辑下沉到 `ExecutorRegistry` |
| 7 | `ExecutorRegistry` default 为 Harness | ❌ 未实现 | [`src/loop_controller/executors/base.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/base.py) 未设 default |
| 8 | HTTP 管理接口 | ⚠️ 部分实现 | [`src/loop_controller/server.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/server.py) 已实现 drain/reset，但路径与文档不一致（`status` 实际为 `backends`） |
| 9 | gRPC 管理 RPC | ❌ 未实现 | proto 未新增 Harness 相关 RPC |
| 10 | MCP admin/审计工具 | ✅ 已实现 | [`src/loop_controller/proxy_server.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/proxy_server.py) |
| 11 | `config/execution_policy.yaml` | ❌ 未实现 | 目录中无此文件 |
| 12 | `config/harness_tools.yaml` 改造 | ⚠️ 未实现 | 文件仍全被注释，无默认 backend 示例 |
| 13 | 健康轮询/热更新/并发控制 | ✅ 已实现 | [`src/loop_controller/executors/harness_executor.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/harness_executor.py) |

### 3.3 测试缺口

- `tests/test_execution_mode.py` ❌ 缺失  
- `tests/test_harness_policy_validator.py` ❌ 缺失  
- `tests/test_harness_protocol_v2.py` ❌ 缺失  
- `tests/test_server_harness_admin.py` ❌ 缺失  
- `tests/test_checkpoint_harness_fail_closed.py` ❌ 缺失

### 3.4 结论

Harness v2 模型、HTTP 后端、健康检查、热更新、MCP admin 已实现，但执行模式解析器缺兜底、策略校验未独立、配置未按文档交付、gRPC 管理接口缺失、关键测试大面积缺失。

---

## 四、v0.32.0 接入方式收敛与审批后自动重试

### 4.1 文档目标

核心包只保留 `@governed`、MCP Proxy、HTTP REST API 三种接入方式；移除 FastAPI 与 gRPC 服务；将 LangChain 降级为示例；新增 `wait_for_approval` 自动重试。

### 4.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | 接入方式收敛为 3 种 | ✅ 已实现 | [`src/loop_controller/integrations/__init__.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/integrations/__init__.py) |
| 2 | 移除 FastAPI | ✅ 已实现 | 文件不存在，`pyproject.toml` 无 fastapi 依赖 |
| 3 | 移除 gRPC 服务 | ✅ 已实现 | `grpc_server.py`、`grpc_client.py` 不存在，CLI 无 `grpc-server` |
| 4 | LangChain 降级为示例 | ✅ 已实现 | [`examples/integrations/langchain_example.py`](file:///c:/Users/26343/Desktop/loop-controller/examples/integrations/langchain_example.py) |
| 5 | 审批后自动重试 | ✅ 已实现 | [`src/loop_controller/models.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/models.py) `wait_for_approval/retry_after_approval`，[`src/loop_controller/agent_sdk.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/agent_sdk.py) `@governed(wait_for_approval=True)` |
| 6 | 保留治理参数透传 | ✅ 已实现 | `agent_sdk.py` `_RESERVED_GOVERNANCE_KEYS` |
| 7 | `pyproject.toml` 移除 fastapi/grpc 可选依赖 | ✅ 已实现 | [`pyproject.toml`](file:///c:/Users/26343/Desktop/loop-controller/pyproject.toml) |
| 8 | 测试补强 | ✅ 已实现 | `tests/test_agent_sdk.py`、`tests/integration/test_functional_agent.py` |

### 4.3 主要缺口

1. **残留 gRPC 生成文件**：[`src/loop_controller/v1/governance_pb2_grpc.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/v1/governance_pb2_grpc.py) 与 `governance_pb2.py` 仍存在。  
2. **版本标签超前**：多处文件头部写“v0.33.0”。  
3. **缺少 `retry_after_approval()` 直接单测**：当前仅通过 `@governed` 间接覆盖。

### 4.4 结论

v0.32.0 目标已基本实现，无重大功能缺口。

---

## 五、v0.33.0 Python 工具治理层健壮性加固

### 5.1 文档目标

Agent SDK 并发安全、LangChain 示例修复、HTTP REST API 安全加固、MCP Proxy 安全、配置校验 fail-closed、CI 分层。

### 5.2 验收标准核对

| # | 验收项 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | `GovernanceRuntime.current()` 使用 ContextVar | ✅ 已实现 | [`src/loop_controller/agent_sdk.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/agent_sdk.py) |
| 2 | `GovernanceResult.model_dump()` 不输出内部引用 | ✅ 已实现 | [`src/loop_controller/models.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/models.py) `PrivateAttr` |
| 3 | `hook_tool_registry` 替换失败可回滚 | ✅ 已实现 | `agent_sdk.py` |
| 4 | `wait_for_approval=True` 超时清理审批请求 | ✅ 已实现 | `models.py` + [`src/loop_controller/controller.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/controller.py) `cancel_approval` |
| 5 | LangChain 示例位置参数 | ✅ 已实现 | `examples/integrations/langchain_example.py` |
| 6 | MCP admin 工具非 admin profile 返回 `admin_forbidden` | ✅ 已实现 | [`src/loop_controller/proxy_server.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/proxy_server.py) |
| 7 | MCP 错误响应不暴露原始异常 | ⚠️ 部分实现 | `internal_error` 已脱敏，部分业务错误码仍暴露 |
| 8 | HTTP 全局异常统一 JSON | ✅ 已实现 | [`src/loop_controller/server.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/server.py) |
| 9 | API Key 校验长度不一致安全返回 | ✅ 已实现 | `server.py` |
| 10 | Query 参数非法返回 400 | ✅ 已实现 | `server.py` |
| 11 | `_check_dirs_writable` 覆盖全部持久化路径 | ✅ 已实现 | [`src/loop_controller/infra/config_loader.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/config_loader.py) |
| 12 | `ValidationError` 统一包装 | ✅ 已实现 | `config_loader.py` |
| 13 | README / KNOWN_LIMITATIONS 与代码一致 | ❌ 未实现 | README 示例路径错误；KNOWN_LIMITATIONS Docker 描述与 `DockerBackendConfig` 行为矛盾 |
| 14 | CI 拆分为 unit/integration | ✅ 已实现 | [`.github/workflows/ci.yml`](file:///c:/Users/26343/Desktop/loop-controller/.github/workflows/ci.yml) |
| 15 | OPA fixture 消除 TOCTOU | ✅ 已实现 | `tests/conftest.py` |
| 16 | 全量测试通过 | ✅ 已实现 | 791 passed / 22 integration passed |

### 5.3 P0 缺口

1. **MCP SSE mTLS 身份识别未按设计实现**：未从 ASGI SSL 套接字直接读取客户端证书，仍依赖 Header。  
2. **MCP Proxy 限流未挂载**：`_MCPRateLimitMiddleware` 已定义但未加入 middleware 栈。  
3. **SSE 连接治理缺失**：无 30s ping / 120s 空闲超时，`request._send` 仍被直接调用。  
4. **stdio 对端断开/EOF 未处理**：`run_stdio_async` 直接调用 `stdio_server()`。  
5. **工程化文档不一致**：README 引用 `examples/research_agent_example.py` 不存在；KNOWN_LIMITATIONS 与 `config_loader.py` 矛盾。

### 5.4 结论

Agent SDK、HTTP API、配置校验、CI 等核心目标已实现；MCP Proxy 安全与连接治理仍有部分未落地，文档一致性需修正。

---

## 六、v0.34.0 状态持久化 SQLite 化与 Harness 生产化

### 6.1 文档目标

Decision/RiskState SQLite 后端；统一 `StateDatabase`；`AuditIndex`；Runtime 自动按扩展名选择后端；Harness 平滑热更新、远程取消、幂等缓存；配置 `state.yaml` 与 JSONL→SQLite 迁移开关。

### 6.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | 统一 `StateDatabase` SQLite 后端 | ✅ 已实现 | [`src/loop_controller/infra/state_db.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/state_db.py) |
| 2 | `SqliteDecisionStore` | ✅ 已实现 | [`src/loop_controller/infra/sqlite_decision_store.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/sqlite_decision_store.py) |
| 3 | `SqliteRiskStateStore` | ⚠️ 部分实现 | [`src/loop_controller/infra/sqlite_risk_state_store.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/sqlite_risk_state_store.py) 已实现基本接口，**缺少只读 snapshot** |
| 4 | Runtime 自动按扩展名选择后端 | ✅ 已实现 | [`src/loop_controller/runtime.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/runtime.py) |
| 5 | Decision/RiskState 协议异步化 | ❌ 未实现 | `risk_state.py` 仍为同步 |
| 6 | Checkpoint 统一事务边界 | ❌ 未实现 | 各 store 独立构造，未用 `state_db.transaction()` 包裹 |
| 7 | `inflight_calls` 持久化 | ❌ 未实现 | `HarnessExecutor` 仅在内存维护 `_in_flight_calls` |
| 8 | `AuditIndex` SQLite 索引 | ✅ 已实现 | [`src/loop_controller/infra/audit_index.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/audit_index.py) |
| 9 | 审计写入降级不丢事件 | ✅ 已实现 | [`src/loop_controller/infra/audit_store.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/infra/audit_store.py) |
| 10 | 审计链索引快速校验 `up_to` | ❌ 未实现 | `verify_chain` 仍为全文件扫描 |
| 11 | 审计与证据写入同一事务 | ❌ 未实现 | 顺序执行，无统一事务 |
| 12 | Harness `update_specs` 平滑热更新 | ✅ 已实现 | [`src/loop_controller/executors/harness_executor.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/executors/harness_executor.py) |
| 13 | Harness HTTP 取消协议 | ✅ 已实现 | `harness_protocol.py` + `harness_executor.py` |
| 14 | `call_id` 幂等缓存 | ✅ 已实现 | `HarnessExecutor._idempotency_cache` |
| 15 | 超时后自动 cancel / 幂等 status 查询 | ❌ 未实现 | 超时仅返回 `harness_request_timeout` |
| 16 | `config/state.yaml` | ❌ 未实现 | 文件不存在 |
| 17 | `AppConfig` 迁移开关 | ❌ 未实现 | `config_loader.py` 无 `migrate_from_jsonl` |
| 18 | `_check_dirs_writable` 覆盖 SQLite 路径 | ❌ 未实现 | 仅覆盖原 JSONL 路径 |

### 6.3 测试缺口

- `tests/test_state_db.py` ❌ 缺失  
- `tests/integration/test_harness_long_running.py` ❌ 缺失  
- `tests/stress/test_audit_large_file.py` ❌ 缺失

### 6.4 结论

耐久性骨架与 Runtime 自动切换已实现，但配置层、迁移层、审计链快速校验、RiskState snapshot、接口异步化明显滞后。

---

## 七、v0.35.0 A2A 交互治理层骨架

### 7.1 文档目标

定义 A2A 最小协议；搭建 Go kernel 骨架（Registry/Task Manager/Router/Delegation Manager）；实现 Python 桥接客户端；保持 Python R2 主流程不变。

### 7.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| A2A-1 | 分层职责与协议文档 | ✅ 已实现 | `src/loop_controller_v0.35.0_development.md` |
| A2A-2 | proto / JSON 模型 | ✅ 已实现 | [`proto/loop_controller/a2a/v1/a2a.proto`](file:///c:/Users/26343/Desktop/loop-controller/proto/loop_controller/a2a/v1/a2a.proto) |
| A2A-3 | Go 模块骨架 | ✅ 已实现 | [`go/`](file:///c:/Users/26343/Desktop/loop-controller/go/) |
| A2A-4 | Agent Registry | ✅ 已实现 | [`go/internal/registry/`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/registry/) |
| A2A-5 | Task Manager | ✅ 已实现 | [`go/internal/task/`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/task/) |
| A2A-6 | Message Router | ✅ 已实现 | [`go/internal/router/`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/router/) |
| A2A-7 | Delegation Manager | ✅ 已实现 | [`go/internal/delegation/`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/delegation/) |
| A2A-8 | Go HTTP/JSON API | ✅ 已实现 | [`go/cmd/kernel/main.go`](file:///c:/Users/26343/Desktop/loop-controller/go/cmd/kernel/main.go) + `go/internal/api/` |
| A2A-9 | Python 桥接客户端 | ✅ 已实现 | [`src/loop_controller/go_kernel_bridge.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/go_kernel_bridge.py) |
| A2A-10 | Go 单元测试 | ✅ 已实现 | `go/internal/*/*_test.go` |
| A2A-11 | Python 桥接测试 | ✅ 已实现 | `tests/test_go_kernel_bridge.py` |

### 7.3 超出文档范围的提前实现

- `go/internal/discovery/`、`go/internal/stream/`、`go/internal/token/` 已提前实现（文档列为 v0.36.0）。  
- `src/loop_controller/controller.py` 主流程已接入 `_try_delegate_to_agent`，超出 v0.35.0“不改动 R2 主流程”的声明。

### 7.4 主要缺口

1. `pyproject.toml` 版本仍为 `0.32.0`。  
2. 多处代码/配置/测试头部标注“v0.36.0”，与 v0.35.0 文档标题冲突。  
3. CI 缺少 `go test ./...` job。  
4. `pyproject.toml` 未显式排除 `go/` 目录（文档风险缓解项未落实，实际工具也未扫描）。

### 7.5 结论

A2A 骨架与 Python 桥接已落地并通过测试，但工程治理（版本号、标签、CI）不到位，且存在范围蔓延。

---

## 八、v0.36.0 A2A 自动发现、流式任务与 Runtime 委托集成

### 8.1 文档目标

在 v0.35.0 骨架基础上，实现 Agent Card 自动发现（YAML + HTTP）、Task SSE 流式更新、Runtime/LoopController 可选委托集成、Go/Python 集成测试。

### 8.2 逐项核对

| # | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| A2A-12 | 开发文档 | ✅ 已实现 | `src/loop_controller_v0.36.0_development.md` |
| A2A-13 | Agent Card 自动发现 | ⚠️ 部分实现 | [`go/internal/discovery/discovery.go`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/discovery/discovery.go) 支持 JSON/HTTP + 缓存，**不支持 YAML**，**持续 watch 未实现**，远程失败未 fail-soft |
| A2A-14 | Task 流式更新（SSE） | ✅ 已实现 | [`go/internal/stream/stream.go`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/stream/stream.go) + `go/internal/api/handlers.go` |
| A2A-15 | JWT HMAC Token 签发/校验 | ✅ 已实现 | [`go/internal/token/token.go`](file:///c:/Users/26343/Desktop/loop-controller/go/internal/token/token.go) |
| A2A-16 | Python Runtime 接入 GoKernelBridge | ✅ 已实现 | [`src/loop_controller/runtime.py`](file:///c:/Users/26343/Desktop/loop-controller/src/loop_controller/runtime.py) |
| A2A-17 | LoopController 委托门控 | ✅ 已实现 | `src/loop_controller/controller.py` `_try_delegate_to_agent` |
| A2A-18 | `go_kernel.yaml` 配置样例 | ⚠️ 已实现但样例错误 | [`config/go_kernel.yaml`](file:///c:/Users/26343/Desktop/loop-controller/config/go_kernel.yaml) 指向不存在的 `config/agents.json` |
| A2A-19 | Go/Python 集成测试 | ✅ 已实现 | `tests/test_go_kernel_bridge.py` + `tests/test_go_kernel_integration.py` |

### 8.3 验证结果

- `go test ./...`：通过  
- `pytest tests/test_go_kernel_bridge.py tests/test_go_kernel_integration.py -q`：8 passed  
- `pytest tests/ -m "not integration" -q`：791 passed（文档写 738 passed，已增长）  
- `ruff check src tests`：全绿

### 8.4 主要缺口

1. **YAML 发现未实现**：`StaticProvider` 只解析 JSON。  
2. **持续 watch 未实现**：两个 provider 的 `Watch` 直接返回错误。  
3. **发现失败策略偏严**：`Manager.Sync` 任一 provider 失败即整体报错，未 fail-soft。  
4. **配置样例错误**：`go_kernel.yaml` 引用 `config/agents.json` 不存在。  
5. **本地 Agent Card 入口硬编码**：`Runtime._register_local_agent_card()` 写死 `http://127.0.0.1:8000`，未从配置读取。  
6. **SSE fallback 未实现**：文档风险项中 SSE 失败回退轮询未落实。  
7. **文档接口示例小瑕疵**：Token `Issue` 示例缺少 `ttl` 参数。

### 8.5 结论

v0.36.0 核心闭环已实现并通过测试，但文档与实现、配置样例、可选/风险缓解能力存在不一致。

---

## 九、跨版本共性问题

| 问题 | 影响 | 涉及版本 |
|---|---|---|
| `pyproject.toml` 版本未更新 | 包版本停留在 0.32.0 | 全部 |
| 版本标签混乱 | 代码/注释/配置混用 v0.33/35/36 | v0.32、v0.35、v0.36 |
| 配置样例滞后/错误 | `state.yaml` 缺失，`go_kernel.yaml` 引用不存在文件，执行策略配置被合并且全注释 | v0.31、v0.34、v0.36 |
| 可观测性未闭环 | 持久化 Metrics 缺失、Health 缺 `durability` | v0.30 |
| 范围蔓延 | v0.35 提前实现 v0.36 能力并改动 R2 主流程 | v0.35、v0.36 |
| 文档基线过时 | 测试通过数已增长，文档仍写旧数字 | 多个版本 |

---

## 十、修复建议优先级

### P1（发布前必须）

1. 更新 `pyproject.toml` 版本号，统一所有代码/文档版本标签。  
2. 修正 `config/go_kernel.yaml` 引用路径；补齐 A2A Agent Card 示例文件（YAML 或 JSON）。  
3. 修正 `README.md` 示例路径错误；修正 `KNOWN_LIMITATIONS.md` Docker 后端描述与 `config_loader.py` 一致。  
4. 为 v0.30 补齐持久化 Prometheus 指标，或至少暴露 Health `durability` 字段。  
5. 在生产 profile 下强制校验 `fsync_enabled=true`。

### P2（应尽快）

1. v0.31：补齐 `ExecutionModeResolver` Kill Switch / 风险兜底；独立 `HarnessPolicyValidator`；补齐 5 个缺失测试文件。  
2. v0.33：挂载 MCP 限流中间件；实现 SSE 保活/空闲超时/stdio EOF 处理。  
3. v0.34：新增 `config/state.yaml` 与 `migrate_from_jsonl` 开关；扩展 `_check_dirs_writable` 覆盖 SQLite 路径；实现 `AuditIndex.verify_chain` 快速路径与 `SqliteRiskStateStore` snapshot；推进 Store 异步化。  
4. v0.36：为 `StaticProvider` 增加 YAML 解析；实现 DiscoveryManager fail-soft；实现 SSE 轮询 fallback；从配置读取本地 Agent Card 入口。

### P3（工程债）

1. 清理残留 `src/loop_controller/v1/governance_pb2*.py`（确认无引用后删除）。  
2. 在 CI 中增加 `go test ./...` job。  
3. 在 `pyproject.toml` 中显式排除 `go/` 目录（如文档承诺）。  
4. 统一探测文件命名为 PID+UUID，避免多进程启动冲突。

---

*报告生成位置：[`reports/audit_v0.30-v0.36_development.md`](file:///c:/Users/26343/Desktop/loop-controller/reports/audit_v0.30-v0.36_development.md)*
