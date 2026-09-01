# v0.36.0 交互治理层启动（二）：A2A 自动发现、流式任务与 Runtime 委托集成

> 一句话目标：**在 v0.35.0 Go 内核骨架基础上，实现 Agent Card 自动发现、Task 流式更新（SSE），并在 Python Runtime/LoopController 中接入可选的跨 Agent 委托治理入口，完成 A2A 交互治理最小闭环。**
>
> 范围限定：自动发现支持本地 YAML 配置与远程 HTTP 拉取；流式更新采用 SSE；Runtime 集成以可选 sidecar 形式注入，fail-closed。

- 状态：**已完成（骨架已合入 develop，提交 0b901d0）**
- 前置版本：v0.35.0 Go 内核骨架
- 版本性质：新增治理层 / 跨 Agent 交互治理第二阶段
- 核心范围：
  - ✅ 输出 v0.36.0 开发文档；
  - ✅ Go 内核：Agent Card 自动发现（YAML + HTTP fetch + 缓存）；
  - ✅ Go 内核：Task 流式更新（SSE）；
  - ✅ Go 内核：Token 签发/校验骨架（JWT HMAC）；
  - ✅ Python Runtime：在 `Runtime`、`LoopController` 接入可选 `GoKernelBridge`；
  - ✅ Python：新增 `config/go_kernel.yaml` 配置样例；
  - ✅ 新增 Go/Python 集成测试。
- 验证目标：
  - ✅ `go test ./...` 全绿；
  - ✅ `pytest tests/test_go_kernel_bridge.py tests/test_go_kernel_integration.py -q` 通过（8 passed）；
  - ✅ `pytest tests/ -m "not integration" -q` 保持全绿（738 passed, 57 skipped, 22 deselected）；
  - ✅ `python -m ruff check src tests` 通过。

---

## 1. 背景

v0.35.0 已经搭好了 Go 交互治理内核的骨架：Agent Registry、Task Manager、Message Router、Delegation Manager，以及 Python 桥接客户端。但骨架本身还不能完成一个真实的跨 Agent 交互闭环，因为缺少三个关键能力：

1. **Agent Card 自动发现**：v0.35.0 只能手工注册；v0.36.0 需要支持从本地配置和远程 URL 自动发现，减少运维负担。
2. **Task 流式更新**：Agent 间任务状态需要实时推送，而不是靠轮询。
3. **Runtime 委托集成**：Go 内核必须被 Python 工具治理层调用，才能在真实业务路径中发挥作用。

v0.36.0 要把这三个能力补齐，让 Loop Controller 第一次具备「治理跨 Agent 委托」的端到端能力。

---

## 2. 分层职责（不变）

- **Python 工具治理层**：继续负责单次工具调用的 R1/R2/R3 治理。
- **Go 交互治理层**：负责 Agent 间交互治理，v0.36.0 新增自动发现、流式推送、Runtime 委托入口。

---

## 3. v0.36.0 范围

### 3.1 纳入本版本

| 编号 | 内容 | 文件位置 |
|---|---|---|
| A2A-12 | 输出 v0.36.0 开发文档 | `src/loop_controller_v0.36.0_development.md` |
| A2A-13 | Agent Card 自动发现 | `go/internal/discovery/` |
| A2A-14 | Task 流式更新（SSE） | `go/internal/stream/` + `go/internal/api/` |
| A2A-15 | JWT HMAC Token 签发/校验骨架 | `go/internal/token/` |
| A2A-16 | Python Runtime 接入 GoKernelBridge | `src/loop_controller/runtime.py` |
| A2A-17 | LoopController 委托门控 | `src/loop_controller/controller.py` |
| A2A-18 | `go_kernel.yaml` 配置样例 | `config/go_kernel.yaml` |
| A2A-19 | Python/Go 集成测试 | `tests/test_go_kernel_integration.py` |

### 3.2 明确不纳入

| 编号 | 内容 | 原因 |
|---|---|---|
| A2A-N6 | 真实远程 Agent 执行（HTTP call target entrypoint） | v0.37.0 实现 |
| A2A-N7 | gRPC 传输层 | v0.37.0 评估 |
| A2A-N8 | 分布式发现（K8s/Consul/MCP registry） | v0.37.0+ 实现 |
| A2A-N9 | 与 `@governed` 装饰器深度融合 | v0.37.0 实现 |
| A2A-N10 | Token 密钥轮换与 JWKS | v0.37.0 实现 |

---

## 4. Go 内核变更

### 4.1 自动发现（`go/internal/discovery`）

支持两种 `AgentDiscoveryProvider`：

- `StaticProvider`：从本地 YAML/JSON 文件加载 Agent Cards；
- `HTTPProvider`：从远程 URL 拉取 Agent Cards，支持缓存与刷新。

`DiscoveryManager` 负责在启动时做一次全量同步，并可选地持续 watch 变化。

```go
package discovery

type AgentDiscoveryProvider interface {
    Name() string
    Discover(ctx context.Context) ([]models.AgentCard, error)
    Watch(ctx context.Context) (<-chan DiscoveryEvent, error)
}
```

### 4.2 流式更新（`go/internal/stream`）

`TaskEventPublisher` 接口：

```go
package stream

type TaskEventPublisher interface {
    Subscribe(ctx context.Context, taskID string) (<-chan models.Task, error)
    Publish(ctx context.Context, task models.Task) error
}
```

API 层暴露 `GET /a2a/v1/tasks/{id}/stream`（SSE），Python 桥接层可通过该端点订阅任务更新。

### 4.3 Token 签发（`go/internal/token`）

使用标准库 `encoding/base64` + `crypto/hmac` 实现最小 JWT-like Token：

```go
package token

type HMACIssuer struct {
    secret []byte
}

func (i *HMACIssuer) Issue(claims DelegationClaims) (string, error)
func (i *HMACIssuer) Validate(token string) (DelegationClaims, error)
```

v0.36.0 只保证格式正确与签名校验；密钥轮换与 JWKS 延后。

### 4.4 Delegator 接口化

把 `delegation.Delegator` 改为依赖接口：

- `AgentQuerier`（来自 `registry`）
- `TaskStore`（来自 `task`）
- `TokenIssuer`
- `TaskEventPublisher`（用于委托成功后发布事件）

这样 `delegation` 包不再直接依赖具体实现，便于测试与未来扩展。

---

## 5. Python Runtime 集成

### 5.1 配置

新增 `config/go_kernel.yaml`：

```yaml
go_kernel:
  enabled: false
  base_url: "http://127.0.0.1:8080"
  timeout: 5.0
  discovery:
    static_file: "config/agents.yaml"
  token_secret: "${GO_KERNEL_TOKEN_SECRET:-change-me-in-production}"
```

### 5.2 Runtime 注入

- `Runtime` 数据类新增 `go_kernel_bridge: GoKernelBridge | None`；
- `build_runtime()` 读取 `go_kernel.enabled`，为 true 时初始化 `GoKernelBridge`；
- `Runtime.start()` 若启用，自动注册本地 Agent Card（从 `agents.yaml` 读取）；
- `Runtime.aclose()` 关闭桥接客户端。

### 5.3 LoopController 委托门控

在 `LoopController.evaluate_and_execute()` 的 `allow/modify` 分支后、本地执行前，新增 `_try_delegate_to_agent()`：

- 若参数中包含 `__target_agent_id`，则向 Go 内核请求委托；
- 返回 `allowed=true` 时，返回 `GovernanceResult` 并携带 `target_entrypoint` 与 `delegation_token`；
- 返回 `allowed=false` 时，返回 `blocked` 结果；
- Go 内核不可用时，fallthrough 到本地执行（不阻断既有路径）。

这样 Agent 只需在调用工具时传入 `__target_agent_id` 即可触发跨 Agent 委托治理。

---

## 6. 测试计划

### 6.1 Go 测试

- `discovery`：静态发现、HTTP 发现、缓存失效；
- `stream`：订阅、发布、SSE handler；
- `token`：签发、校验、篡改检测；
- `delegation`：接口化后 mock 测试。

### 6.2 Python 测试

- `tests/test_go_kernel_integration.py`：
  - 启动 Go 内核；
  - 通过桥接注册 Agent；
  - 触发委托请求并拿到 token；
  - 验证任务可通过 SSE 流式更新。
- 全量单元测试保持通过。

---

## 7. 风险与回退

| 风险 | 缓解 |
|---|---|
| SSE 在 Windows 测试环境中不稳定 | 同时提供 SSE 与轮询 fallback，测试优先使用 SSE，失败时回退轮询； |
| Runtime 注入引入循环依赖 | `GoKernelBridge` 在 `runtime.py` 内部构造，不反向导入 controller/checkpoint； |
| Token 密钥硬编码 | 配置中读取环境变量；v0.37.0 接入密钥管理； |
| 自动发现远程 URL 失败 | fail-soft：远程失败不影响已注册或静态配置的 Agent； |

---

## 8. 后续版本

- **v0.37.0**：真实远程 Agent 执行、分布式发现、Token 密钥管理、与 `@governed` 深度融合。
