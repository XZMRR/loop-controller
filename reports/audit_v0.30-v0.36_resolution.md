# v0.30.0–v0.36.0 审计修复与范围判定说明

**审计源文档**：[reports/audit_v0.30-v0.36_development.md](audit_v0.30-v0.36_development.md)  
**修复日期**：2026-09-01  
**判定原则**：

1. 真实存在且影响当前发布一致性的问题必须修复；
2. 代码行为与文档描述不一致时，以代码实际行为为基准修正文档；
3. 范围蔓延、可选能力、风险缓解项若未写入 v0.36.0 发布门禁，则通过文档说明边界，不强制在本次提交中补齐。

---

## 一、已确认并修复的问题

| 问题 | 修复方式 | 关键文件 |
|---|---|---|
| `pyproject.toml` 版本停留在 0.32.0 | 更新为 `0.36.0` | [pyproject.toml](../pyproject.toml) |
| `config/go_kernel.yaml` 引用不存在的 `config/agents.json` | 改为 `config/a2a_agents.yaml`，并新增 YAML/JSON 示例 | [config/go_kernel.yaml](../config/go_kernel.yaml)、[config/a2a_agents.yaml](../config/a2a_agents.yaml) |
| `README.md` 示例路径、OPA 版本、测试数量、路线图过时 | 更新为 v0.36.0 对应内容 | [README.md](../README.md) |
| `KNOWN_LIMITATIONS.md` 版本号与 Docker 后端描述错误 | 版本改为 v0.36.0；修正 Docker 后端说明；新增 V36 边界声明 | [src/KNOWN_LIMITATIONS.md](../src/KNOWN_LIMITATIONS.md) |
| `StaticProvider` 不支持 YAML | 在 Go discovery 中增加 YAML 解析与 struct tag | [go/internal/discovery/discovery.go](../go/internal/discovery/discovery.go)、[go/internal/models/models.go](../go/internal/models/models.go) |
| 本地 Agent Card 入口硬编码 | `Runtime._register_local_agent_card()` 改为从 `go_kernel.yaml` 读取 | [src/loop_controller/runtime.py](../src/loop_controller/runtime.py) |
| Health 未暴露 `durability`、Prometheus 缺少持久化指标 | HealthResponse 增加 `durability`；metrics.py 增加 `loop_controller_persistence_*` 指标并在 durable_io/persistence_probe 中打点 | [src/loop_controller/server_models.py](../src/loop_controller/server_models.py)、[src/loop_controller/server.py](../src/loop_controller/server.py)、[src/loop_controller/metrics.py](../src/loop_controller/metrics.py)、[src/loop_controller/infra/durable_io.py](../src/loop_controller/infra/durable_io.py)、[src/loop_controller/infra/persistence_probe.py](../src/loop_controller/infra/persistence_probe.py) |
| 残留 gRPC 生成文件 | 删除 `src/loop_controller/v1/` 下所有 pb2 文件；同步 ruff/mypy exclude | [src/loop_controller/v1/](../src/loop_controller/v1/)（已删除） |
| CI 缺少 Go 测试 | 新增 `go` job；OPA 下载版本对齐 README v1.19.0 | [.github/workflows/ci.yml](../.github/workflows/ci.yml) |
| 缺失配置样例 | 新增 `config/state.yaml`、`config/execution_policy.yaml`；修正 `config/harness_tools.yaml` 中注释的执行策略字段 | [config/state.yaml](../config/state.yaml)、[config/execution_policy.yaml](../config/execution_policy.yaml)、[config/harness_tools.yaml](../config/harness_tools.yaml) |

---

## 二、判定为“范围外/未纳入发布门禁”的模糊问题

以下问题在审计报告中被标记为缺失或部分实现，但属于**风险缓解项、可选能力或下一版本规划**，未列入 v0.36.0 发布必须完成的门禁。本次通过文档说明其边界，而非立即补全代码。

### 2.1 A2A 可选与风险缓解能力

| 能力 | 当前状态 | 未立即实现的原因 |
|---|---|---|
| 持续 watch（`StaticProvider.Watch`、`HTTPProvider.Watch`） | 返回错误 | v0.36.0 发布门禁为“静态/HTTP 发现可用”，持续 watch 属于增强项，已写入 [KNOWN_LIMITATIONS.md](../src/KNOWN_LIMITATIONS.md) |
| 发现失败 fail-soft | `DiscoveryManager.Sync` 任一 provider 失败即报错 | 当前行为满足“配置错误明显失败”的 fail-closed 偏好；降级策略需要设计部分结果契约，归入后续优化 |
| SSE 轮询 fallback | 未实现 | SSE 是当前主路径，fallback 轮询属于高可用增强 |
| 远程取消与幂等 status 查询 | 超时仅返回 `harness_request_timeout` | Harness 后端需声明 `protocol_version >= 3` 才启用；v0.36.0 保持与 v2 后端兼容 |

### 2.2 v0.31–v0.34 尚未闭环的能力

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 独立 `HarnessPolicyValidator` | 校验逻辑内联在 `HarnessExecutor` | 架构上建议解耦，但当前内联逻辑已覆盖默认场景；拆分属于重构债 |
| `ExecutionModeResolver` Kill Switch / 风险兜底 | 枚举已实现，高级兜底未落地 | 不影响默认 `harness_required` 路径 |
| `config/state.yaml` 真正被 `ConfigLoader` 加载 | 文件仅作为样例 | Runtime 已按扩展名自动选择 SQLite/JSONL 后端；`migrate_from_jsonl` 等迁移开关需单独设计 `StateConfig` 与启动流程 |
| `AuditIndex.verify_chain` 快速路径 / `SqliteRiskStateStore` snapshot | 未实现 | 性能优化项，不影响当前功能正确性 |
| Store 接口异步化 | 部分 Store 仍为同步 | 同步兼容层已覆盖 `@governed` 路径；全面异步化涉及大量接口变更 |

### 2.3 范围蔓延问题

审计报告指出：

> v0.35.0 文档声明“不改动 Python R2 主流程”，但 `controller.py` 已接入跨 Agent 委托门控；v0.36.0 的部分能力在 v0.35.0 中已提前实现。

判定：

- 该现象**真实存在**，但属于开发过程中的正常迭代重叠，不是回归或缺陷；
- v0.35.0 与 v0.36.0 的开发文档已更新为“已完成（骨架已合入 develop）”状态；
- [KNOWN_LIMITATIONS.md](../src/KNOWN_LIMITATIONS.md) 中 V36-1 明确说明：Python 工具治理层仍只治理单 Agent 的 `tool_call`，跨 Agent 委托由独立 Go A2A 内核负责，不在 Python R2 主流程内。

---

## 三、未在本次修复的遗留项及原因

| 遗留项 | 原因 | 后续计划 |
|---|---|---|
| MCP 限流中间件未挂载、SSE 保活/空闲超时、stdio EOF 处理 | 属于 MCP Proxy 稳定性增强，当前主链路可用 | v0.37.0+ 统一治理 |
| 生产 profile 强制 `fsync_enabled=true` | 需要定义“生产 profile”的判定标准（如 `NODE_ENV=production` 或配置文件开关），避免误伤开发配置 | 随配置模型升级一并实现 |
| tail repair 审计元数据（`truncated_bytes`、`tail_hash`、alert） | 当前已有 tail repair 计数指标；完整审计事件需要新增 alert 模型 | v0.37.0+ |
| `_check_dirs_writable` 覆盖 SQLite 路径 | SQLite 后端当前由 Runtime 自动创建目录；该检查需与 `state.yaml` 加载逻辑同步 | 随 `state.yaml` 接入实现 |

---

## 四、验证结果

- `ruff check src tests`：全绿
- `python -m pytest tests/ -q -m "not integration"`：791 passed, 4 skipped
- `python -m pytest tests/ -q -m integration`：22 passed
- `cd go && go test ./...`：全绿

---

## 五、结论

本次修复聚焦于**版本一致性、配置样例正确性、文档与代码对齐、可观测性闭环、工程清理与 CI 完善**。对于审计报告中列出的范围外可选能力与风险缓解项，已通过在 [KNOWN_LIMITATIONS.md](../src/KNOWN_LIMITATIONS.md) 和本文档中明确边界，避免对外错误承诺。未修复的遗留项将在后续版本按优先级逐步落地。
