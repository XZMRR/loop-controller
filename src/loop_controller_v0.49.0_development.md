# v0.49.0：审批可靠性收敛与治理默认硬化

**Status**: 已完成
**Target Version**: `0.49.0`
**Goal**: 清理历史占位残留，将 Checkpoint 降级改为显式 opt-in，统一工具审批身份边界，把 Python 工具审批迁入统一 SQLite 事实源并为两个审批平面建立可靠通知，同时同步公开能力边界。

---

## 1. 版本定位

v0.46～v0.48 已依次完成可信单跳委托、可信递归委托和可恢复委托审批。当前主要缺口不是新增 Agent 调度或工具执行能力，而是治理系统自身仍有以下不一致：

1. `Noop*` 历史占位类和“迭代 1”说明仍留在生产包；
2. `Checkpoint` 省略关键依赖时会隐式选择内存、恒通过或无分析后端，与 fail-closed 原则不一致；
3. Python 工具审批写入路径的认证成熟度需向 Go A2A 审批的独立 principal 与对象级授权模型看齐；
4. Go 委托审批已有可靠 outbox，但尚无面向审批人的主动通知；
5. Python 工具审批仍以 JSONL 为权威事实源，`ApprovalWatcher` 只提供进程内唤醒，既无可靠通知 outbox，也无法与 SQLite outbox 原子提交。

本版本聚焦“审批可靠性 + 默认治理硬化”。OPA Bundle 策略交付、shadow 模拟与策略变更审计拆分到 v0.50.0，避免一个版本同时改造审批平台和策略交付平台。

## 2. 核心不变量

1. 缺失关键治理依赖时默认拒绝构造；降级只能通过显式 opt-in 或显式注入测试后端启用。
2. 生产构造路径不得隐式启用 no-op、恒通过或仅内存治理后端。
3. 合法测试/嵌入适配器可以保留，但必须明确命名、显式选择且可观测，不能伪装成生产默认。
4. 审批人身份来自认证 principal；审批写入不得接受请求体自报 approver。
5. 一套运行实例只能有一个审批权威事实源，禁止 JSONL 与 SQLite 双写或同时作为权威源。
6. Python 工具审批状态变更与通知 outbox enqueue 必须在同一个 `StateDatabase` SQLite 事务内提交。
7. 审批通知采用至少一次投递：允许重复，消费者必须幂等，不允许因进程崩溃丢失已提交通知。
8. 通知载荷不得携带明文敏感参数。
9. 本版本不改变 v0.46～v0.48 已冻结的 Agent 委托治理语义。

## 3. 实施范围

### P49-01 历史占位与公共 API 清理

- 移除或私有化已被真实现替代、且生产与测试均无必要用途的 `NoopAuthorityManager`；
- 移除或私有化已被真实现替代、且生产与测试均无必要用途的 `NoopAuditAnalyzer`；
- 清理 `checkpoint.py` 模块级“迭代 1/2”陈旧状态说明；
- 将仍有合法测试用途的内存/禁用适配器从“占位实现”重新定义为显式测试或嵌入适配器；
- 收敛公共导出，避免调用方误把测试适配器当成生产治理后端；
- 全仓库扫描 `Noop`、`占位`、`恒通过`、`no-op` 的生产可达引用并记录处置结果。

**完成定义**：公共导出无已废弃 `Noop*`；生产构造路径无隐式占位引用；保留的测试适配器具有明确用途、命名和专项测试。

### P49-02 Checkpoint 显式降级与 fail-closed 构造

- 为 `Checkpoint.__init__` 增加显式 `allow_degraded: bool = False`；
- 默认状态下，缺少 `decision_store`、`budget_ledger`、`reservation_store`、`permission_analyzer` 或 `authority_manager` 等关键依赖时直接抛出配置错误，并列出缺失项；
- 只有 `allow_degraded=True` 或调用方显式注入测试适配器时，才能启用内存/无限预算/禁用分析实现；
- 显式降级时发出结构化 warning，并通过只读 `degraded_backends` 和 Runtime 健康状态暴露当前降级项；
- `build_runtime` 必须显式注入真实后端，不设置 `allow_degraded=True`；
- 逐项更新现有测试和最小嵌入示例，明确标记其降级意图。

**完成定义**：裸构造不再静默降级；未显式允许降级且缺少关键依赖时 fail-closed；显式降级可告警、可探测；生产 `build_runtime` 无降级项。

### P49-03 Python 工具审批身份认证收敛

- 审计 Python 工具审批 CLI 与 HTTP/内部 API 的写入入口；
- 对照 Go A2A 审批模型补齐独立 approver credential、认证 principal、对象级授权及 `401/403` 语义；
- CLI 不再将 `--approver` 自报值作为可信身份；认证主体必须来自受信配置、环境注入或受保护凭证；
- 审批/拒绝统一记录 `principal`、`request_id`、`decision_id` 和动作摘要；
- 未认证、越权、身份与对象不匹配全部 fail-closed。

**完成定义**：工具平面和 Go A2A 审批均要求认证 principal；无凭证无法审批/拒绝，越权返回 403；请求体或 CLI 自报值不能改变可信审批身份。

### P49-04 Python 工具审批迁入 SQLite

#### P49-04A 审批事实源与兼容边界

在 Python `StateDatabase` 中新增：

```text
approval_requests
approval_responses
approval_notification_outbox
```

- 新增 `SqliteApprovalStore`，兼容既有 `ApprovalStore` 查询语义；
- 保留敏感审批参数的加密落盘能力，不得因 SQLite 化退化为明文；
- request/decision ID 幂等，审批终态不可覆盖；
- Runtime 按路径后缀选择后端：`.db/.sqlite/.sqlite3` 使用 SQLite，`.jsonl` 使用兼容后端；
- 生产默认配置切换为 SQLite；CLI 与 Runtime 必须使用同一个配置和同一事实源；
- 禁止 JSONL/SQLite 双写；不自动迁移历史 JSONL 数据，本版本提供显式迁移或导入命令时必须支持校验和 dry-run。

JSONL 仅作为单进程开发/兼容模式：不承诺审批状态与通知 outbox 原子性，不得用于多 worker 生产部署。

#### P49-04B 领域级原子操作

不得通过“先调用 `ApprovalStore`，再调用 Outbox”拼接原子性。应在 `StateDatabase` 或 SQLite 审批仓储中提供领域级事务操作：

```text
submit_request_and_enqueue_notification(...)
record_response_and_enqueue_notification(...)
```

每个操作内部使用同一个连接和显式事务：

```text
BEGIN IMMEDIATE
  创建请求或执行审批状态 CAS
  插入 approval_notification_outbox
COMMIT
```

- 事务失败时审批状态和通知均回滚；
- 相同逻辑事件使用稳定 event/delivery ID，重复请求幂等；
- 不暴露可被外层错误组合的“事务一半”公共路径。

**完成定义**：SQLite 主路径不存在审批已提交但通知未入队的崩溃窗口；并发审批只能有一个终态；Runtime 与 CLI 重启后读取一致状态；JSONL 兼容边界被明确测试。

### P49-05 双平面审批可靠通知

两个审批平面的基础不同，分别实施。

#### P49-05A Go A2A 委托审批通知

- 新建独立的 Go `approval_notification_outbox`，现有 `approval_audit_outbox` 继续专用于 Python 审计回写；
- “复用”仅指复用 lease、claim-token fencing、指数退避及 ACK 所有权校验的实现模式或公共代码，禁止 webhook dispatcher 与审计 dispatcher 竞争同一 outbox 行、claim 或 `delivered_at`；
- 审批进入 `pending`（稳定事件名沿用 `created`）及迁移到 `approved / rejected / expired / cancelled / consumed` 时，在审批状态变更的同一个数据库事务中分别写入 audit outbox 和 notification outbox；修复现有 create/transition/expire 路径中非原子或漏入队的行为；
- 两张 outbox 分别拥有稳定且包含目的地的 delivery ID，以及独立的 attempts、next-attempt、claim-token、lease、last-error 和 delivered 状态；一个目的地失败不得阻塞或完成另一个目的地；
- 现有 `approval_audit_outbox` dispatcher 必须接入生产启动/停止生命周期，不得只保留未接线抽象；新增可配置 webhook dispatcher，支持启停和认证头；
- webhook 禁用语义固定为“不为 webhook 目的地入队”；已入队后暂停消费不得删除记录，恢复后继续投递；审计投递不可因 webhook 配置而停用；
- 载荷只包含审批 ID、source/target、scope 摘要、预算、deadline 和状态，不含明文 `effective_args`。

#### P49-05B Python 工具审批通知

- `ApprovalWatcher` 继续作为本机 SSE/长轮询低延迟唤醒器，不承担可靠投递；
- webhook dispatcher 从 `approval_notification_outbox` 消费；
- 实现 claim、lease、claim token、retry、attempts、next attempt、delivered 和 ACK fencing；
- 进程重启后继续投递未完成通知；
- 载荷只包含 request/decision ID、状态、脱敏目标和必要审计元数据，不含明文参数。

**完成定义**：Go 审计与 webhook 使用独立持久化记录和 ACK 状态，审批状态、审计事件和通知事件同事务提交；Python 工具审批状态与通知同事务提交；两个平面的审批事件均至少一次送达；失败和重启后可恢复；陈旧 worker 无法 ACK 新租约；消费者可凭稳定且包含目的地的 delivery ID 去重；通知载荷通过脱敏测试。

### P49-06 能力边界与发布文档同步

- 更新 `KNOWN_LIMITATIONS.md` 的版本定位和受本版本影响的边界；
- 更新 V29-1：区分可靠 webhook 与 `ApprovalWatcher` 本机唤醒，说明至少一次语义和消费者幂等责任；
- 更新 V29-2：SQLite 为生产审批主路径；JSONL 仅兼容/单进程使用，不具备状态与通知同事务保证；
- V22-1 继续明确 Rego 不由 Python `HotReloader` 重载，OPA Bundle 策略交付留待 v0.50；
- 对每条限制标注“已解决 / 部分解决 / 仍保留”，禁止删除仍真实存在的兼容边界；
- 同步 README、CLI 示例和配置说明中的审批后端及审批身份使用方式。

**完成定义**：发布文档、README、CLI 示例、配置和实现一致；不存在已被本版本改变但仍按历史能力描述的公开边界。

## 4. 明确不做

- OPA Bundle Server、策略发布、shadow 模拟和策略变更审计（v0.50.0）；
- 企业唯一工具出口和部署级防绕过（v0.51.0）；
- 多租户、RBAC、OIDC/JWKS 和跨组织身份（v0.52.0）；
- Web 控制台、SIEM/SSO 集成及正式性能压测；
- 多人会签、审批链编排和审批 UI；
- 消息队列、动态 Agent 调度、DAG、PostgreSQL；
- 删除 JSONL 审批兼容后端或自动隐式迁移历史数据。

## 5. 实施顺序

```text
P49-01  清理历史 Noop 与陈旧说明
P49-02  Checkpoint 显式 allow_degraded
P49-03  工具审批认证 principal 收敛
P49-04A Python 审批迁入 StateDatabase
P49-04B 审批状态与通知原子提交
P49-05A Go 委托审批 webhook
P49-05B Python 工具审批 webhook
P49-06  限制与发布文档同步
```

后续步骤依赖前一步的事务和身份边界，不应并行绕过：尤其不能在 SQLite ApprovalStore 落地前宣称 Python 工具审批通知可靠。

## 6. 总体完成定义

1. 已废弃 `Noop*` 不再出现在公共 API，生产路径无隐式占位实现；
2. `Checkpoint` 缺少关键依赖时默认拒绝构造，只有显式 opt-in 才能降级；
3. Python 工具审批使用认证 principal，CLI/请求体自报 approver 不可信；
4. Python 生产审批事实源切换为 SQLite，审批状态与通知 enqueue 在同一事务；
5. JSONL 审批兼容模式的单进程和弱可靠边界被明确保留；
6. Go 与 Python 审批通知都具备至少一次投递、重试、fencing 和脱敏；
7. `KNOWN_LIMITATIONS.md`、README、配置、CLI 示例和实现同步；
8. 不改变 v0.46～v0.48 的 Agent 委托治理协议；
9. OpenAPI、contract fixture、Go/Python DTO 与版本一致；
10. Go build/vet/test、Python全量测试及 `git diff --check` 全部通过；
11. 活动版本统一为 `0.49.0`。

## 7. 实施结果

- 已移除废弃 `NoopAuthorityManager` / `NoopAuditAnalyzer` 公共残留，并清理 Checkpoint 历史迭代说明；保留的内存/禁用实现仅作为显式测试或嵌入适配器。
- `Checkpoint` 已默认 fail-closed，只有显式 `allow_degraded=True` 才构造降级后端；降级项通过 warning、`degraded_backends` 与 Runtime 健康状态暴露，生产 `build_runtime` 显式注入治理依赖。
- Python CLI 与 HTTP 审批写入已改用认证 principal、allowlist 与对象级授权；CLI 不再接受 `--approver` 自报身份。
- Python 工具审批生产默认事实源已切换到 SQLite；请求/响应与通知 outbox 通过领域级事务原子提交，JSONL 仅保留单进程兼容路径且不双写、不隐式迁移。
- Go A2A 审批审计 outbox 与 webhook notification outbox 已分离，并接入生产 dispatcher 生命周期；Python webhook dispatcher 已接入 Runtime 生命周期。两个平面均实现至少一次投递、租约/claim-token fencing、重试与脱敏载荷。
- `ApprovalWatcher` 明确保留为本机低延迟唤醒器，不承担可靠跨进程投递。
- 活动 Python/Go 版本、OpenAPI、schema/example、contract fixture、配置与测试引用已同步到 `0.49.0`；历史 development 文档保持原版本。
- `KNOWN_LIMITATIONS.md`、README、CLI 示例及审批配置说明已按上述实际边界同步。

## 8. 验证记录

- 历史残留清理：废弃 `NoopAuthorityManager` / `NoopAuditAnalyzer` 已移除或私有化，生产路径无隐式占位后端。
- Checkpoint 显式降级：专项测试覆盖默认拒绝、显式 `allow_degraded=True`、告警与健康状态可观测；生产 Runtime 无降级项。
- 工具审批身份认证：专项测试覆盖可信 principal、allowlist、401/403、请求体伪造和旧 `--approver` 拒绝。
- SQLite ApprovalStore 与原子 Outbox：专项测试覆盖加密持久化、幂等、事务回滚、租约重领、claim-token fencing 与重启恢复。
- Go A2A 审批通知：专项测试覆盖审计/通知独立进度、所有生命周期原子入队、`expired`、重试、fencing、禁用和数据库重启恢复。
- Python 工具审批通知：专项测试覆盖 webhook 成功、认证头、失败退避、暂停/重启恢复、脱敏和禁用不入队。
- `KNOWN_LIMITATIONS.md` 与发布文档同步：V22-1、V29-1、V29-2、README、CLI 与配置说明已更新。
- 版本统一：活动 Python/Go/OpenAPI/contract/config/test 引用统一为 `0.49.0`；历史文档保留历史版本。
- Go 验证：`go build ./...`、`go vet ./...`、`go test ./...` 全部通过。
- Python 验证：`uv run pytest -q` 通过，`947 passed, 4 skipped, 6 warnings`；新增回归覆盖审批对象级授权、CLI webhook 目的地、跨后端最近审批查询，以及 SQLite 多进程首次 WAL 初始化竞争。
- 工程检查：`git diff --check` 通过；VS Code diagnostics 无错误。

---

## 附录：后续版本开发方向

以下是方向性蓝图，进入对应版本时仍需重新核查代码并冻结范围。

### v0.50.0：OPA 策略交付、模拟与变更审计

- 采用 OPA Bundle Server / Bundle API 建立正式策略交付通道；
- 候选策略生命周期：`draft → validated → published → loaded`；
- 发布前执行 `opa check` / `opa test`；
- OPA 加载目标 bundle revision 后才确认生效；
- 线上决策与候选策略 shadow 试算比较；
- 发布入口认证、bundle hash/revision 与加载结果审计；
- 校验或加载失败保持旧线上策略。

### v0.51.0：企业强制治理出口基线

- 工具凭证集中持有，不发放给 Agent；
- MCP / HTTP / Harness 成为唯一受保护工具出口；
- Agent Runtime 最小网络、文件系统和进程权限；
- 工作负载身份与 delegated subject 分离；
- 部署级隔离、防绕过测试及全链路审计关联。

### v0.52.0：多租户与身份边界

- tenant / org / workspace 与 Agent 归属；
- 管理员、审批者、执行者 RBAC；
- 跨租户访问和委托策略；
- OIDC/JWKS、密钥轮换；
- 租户级数据域与敏感参数策略。

### v0.53.0：生产可观测性与工程基线

- 性能压测、延迟/吞吐和容量基线；
- SLO、运行时指标和告警；
- Web 控制台与审批待办；
- SIEM/SSO 集成；
- Checkpoint、Proxy、审批和 Outbox 健康指标。

### v0.54.0：多 Agent 平台化（候选）

- Agent Card 能力路由和动态调度；
- 并行子任务与 DAG；
- worker 集群与任务队列；
- 预算感知调度和失败重试；
- 组织级 federation。
