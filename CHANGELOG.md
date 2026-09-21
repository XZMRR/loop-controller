# Changelog

本文件记录 Loop Controller 每个版本的显著变更。格式基于
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased] — v0.55-dev（backend/v0.55-dev 分支）

### Added

- **治理台（Vue 3 + Element Plus）全面接入真实后端**：A2A 任务树
  （`HttpA2ATaskDataSource`）、RBAC 绑定/授权、Policy 生命周期
  （validate/shadow/publish/rollback）、死信队列（列表 + 重放）全部从 Mock
  切换为 Http 数据源，Mock 实现保留供单测使用。
- **审批推送**：`GET /v1/admin/approvals/stream`（HMAC 签名游标、400/410
  语义、10s 心跳），前端 SSE 推送优先 + 轮询兜底。
- **管理台 A2A 代理端点**：`GET /v1/admin/a2a/tasks`（逐任务 RBAC 校验 +
  载荷消毒）、`/v1/admin/a2a/dead-letters`（鉴权 + 404/409/502 映射）。
- **Go 内核控制面凭据装配**：`GoKernelBridge` 按 `go_kernel.yaml` 声明的
  `*_token_env` 装配 Authorization，mTLS 三段文件齐全时装配客户端证书。
- **前端测试资产**：vitest 83 用例 + Playwright E2E 19 用例（后端依赖
  全部 `page.route` stub，不依赖 Python/Go 进程）。
- **CI 新增前端 job**：vitest / typecheck / build / Playwright E2E。

### Fixed

- Python 代理死信重放响应适配 Go 内核 `{assignment}` 包裹结构。
- Go 死信 list/replay 端点补 `protocol_version` 协议版本包裹。
- `go_kernel.yaml` 样例 mTLS 占位路径不存在时装配失败的问题（改为
  存在性检查）。
- Windows 下 OPA CLI 传入盘符路径被误解析为 URL scheme 的问题。

### Changed

- 前端数据源统一为"契约接口 + Http 实现 + Mock 测试替身"模式，
  视图层零改动切换。

## [0.54.0]

### Added

- 可靠多 Agent 调度：Agent spec/status 与容量路由、durable
  assignment/outbox、lease/attempt/fence。
- 预算感知 retry/failover/dead-letter，有界静态 DAG（task-graphs）。
- 可恢复 SSE、正式 SQLite migration、readiness 与低基数 Prometheus
  metrics。

完整说明见 [README](README.md) 第 5 节与
[src/KNOWN_LIMITATIONS.md](src/KNOWN_LIMITATIONS.md)。

## [0.53.0]

### Added

- workload/delegated identity、Secret 引用、受保护出口、ExecutionReceipt、
  能力协商与静态部署 conformance。

## [0.52.0]

### Added

- 多租户、企业身份与 RBAC。

## [0.50.0] - [0.51.0]

### Added

- OPA Bundle 策略交付、加载确认、shadow 模拟与变更审计。

## [0.49.0]

### Added

- 审批可靠性收敛与治理默认硬化：SQLite 工具审批主路径、双平面可靠
  webhook、认证审批 principal、Checkpoint 显式降级。

## [0.48.0]

### Added

- 委托审批持久化与可靠恢复派发。

## [0.46.0] - [0.47.0]

### Added

- 可信单跳 A2A 委托闭环（Bearer 身份绑定、独立工作负载凭证）。
- 可信递归委托、权限与预算衰减。

## [0.43.0] - [0.45.0]

### Added

- Python 治理核心状态 SQLite 化（预算/预留/权限令牌/任务/告警/会话/对话）。

## [0.42.0]

### Added

- registry / router / discovery 持久化。

## [0.41.0]

### Added

- 分布式可靠性：outbox 原子领取、exec 租约故障转移。

## [0.40.0]

### Added

- Agent 委托执行生命周期闭环：Task 状态机 CAS、SSE 重放、委派 token
  参数绑定。

## [0.35.0] - [0.39.0]

### Added

- A2A 交互治理层：Go kernel、Agent Registry、Task Manager、Delegation
  Manager、自动发现、流式任务、Runtime 委托集成、单实例可靠 A2A 闭环
  （OpenAPI 权威协议、SQLite 状态层、真实委托执行）、独立 Agent
  Interaction Governance（IIGE）平面、交互治理协议与运行闭环收敛。

## [0.33.0] - [0.34.0]

### Added

- Python 工具治理层健壮性加固：SDK 并发安全、API 入口防御、错误脱敏、
  admin 权限隔离、配置校验 fail-closed、CI 分层。
- 状态持久化 SQLite 化与 Harness 生产化：热更新、远程取消、幂等、资源
  隔离。

