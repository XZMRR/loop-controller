# Develop MVP 审查反馈与 Main 分支可吸收内容

## 1. 总体结论

`develop` 已经从早期 Mock 原型扩展为较完整的治理 MVP，配置化 Runtime、真实 OPA、真实 MCP stdio、跨重启防重放、哈希链审计、分级掩码、ScriptedPlanner 和 LLMPlanner 都是明显进展，适合作为后续集成和演示的主要基础。

本次在 Windows 环境安装 OPA 1.0.1 后执行全量测试，结果为：

```text
124 passed in 18.16s
```

但当前版本仍建议定位为“可运行治理 MVP”，不宜表述为生产级安全治理系统。以下问题建议按优先级处理。

## 2. 建议优先修复的问题

### P0：预算预留在拒绝路径中可能没有返还

`Checkpoint.evaluate()` 在权限组合和 OPA 判定前先预留预算，但以下结果路径未统一调用 `refund()`：

- 权限组合规则返回 deny；
- OPA 返回 deny；
- OPA 返回非法 verdict；
- 审批拒绝；
- 审批请求冲突；
- `modify` 参数复核失败并返回 blocked；
- MCP Gateway 返回 `ToolResult(status="error")` 而不是抛异常。

当前仅在 Gateway 抛异常时返还预算。这可能导致被拒绝的动作持续占用任务预算，并允许通过越权请求耗尽合法任务预算。

建议：

1. 为每次 reserve 建立明确的 pending/finalized 生命周期；
2. 所有 deny、blocked 和审批失败路径统一 refund；
3. 明确定义工具返回 error 时是否计费；
4. 增加 Checkpoint 级测试，直接断言各拒绝路径后的 reserved/committed 状态。

### P0：审批记录缺少强绑定验证

`ApprovalRecord` 虽然包含 `request_id`、`decision_id` 和 `approver_id`，但 `finalize_after_approval()` 当前主要依据 `record.verdict` 转换 Decision，没有验证记录是否属于当前审批请求和 Decision。

建议至少校验：

- 原 Decision 必须是 `require_approval`；
- `record.decision_id == decision.decision_id`；
- `record.request_id == request.request_id`；
- `record.approver_id == decision.escalation_target`；
- `call_id`、`task_id`、`agent_id` 与请求上下文一致；
- Decision 和审批结果仍在有效期内；
- 同一审批结果不可重复应用。

并补充错误 decision、错误 request、错误审批人、过期和重放等负向测试。

### P1：DecisionStore 损坏记录目前偏 fail-open

`JsonlDecisionStore._load()` 遇到非法 JSON 时直接跳过。如果损坏行记录了已见 `call_id` 或已使用 `decision_id`，重启后可能丢失防重放状态。

建议：

- 非法 JSON、缺失关键字段或字段类型错误时阻止 Runtime 启动；
- 错误中报告文件路径和行号；
- 增加截断末行、中间行损坏、缺字段和非法类型测试；
- 生产演进时使用 SQLite 唯一约束或其他原子持久化方案。

### P1：审计链完整性能力需准确表述

当前哈希链能够检测多数中间行删改、插入和换序，但不能可靠检测最后一行删除或整体重写。因此对外建议表述为“支持链式篡改检测”，不要表述为“日志不可篡改”。

生产演进方向：

- 周期性 seal 记录；
- 外部签名或时间戳；
- HMAC/数字签名；
- WORM 或远端只追加存储。

### P1：CI 中 OPA 路径不一致

CI 下载 Linux OPA 到 `tools/opa` 并设置 `OPA_PATH`，但共享 fixture 固定查找 `tools/opa.exe`。这可能使 Linux CI 中部分真实 OPA 测试被 skip。

建议：

- 所有 OPA fixture 统一优先读取 `OPA_PATH`；
- Windows 默认回退到 `tools/opa.exe`，Linux 默认回退到 `tools/opa`；
- CI 环境下 OPA 缺失或启动失败应 fail，不应 skip；
- CI 增加 skip 数量检查，防止关键集成测试被静默跳过。

### P1：工程质量门禁尚未闭合

当前 pytest 全量通过，但 Ruff 和 mypy 仍有错误。CI 目前只执行 pytest。

建议 CI 至少增加：

- `ruff check`；
- `mypy src`；
- 测试覆盖率及最低门槛；
- Python 3.12/3.13 版本矩阵；
- OPA 测试不得 skip 的门禁。

mypy 当前重点包括 PyYAML 类型存根、风险等级 Literal、MCP Tool description 可空值以及 ConfigLoader 的 Any 返回。

### P2：完整 E2E 仍使用 FakeGateway

现有自动化 E2E 覆盖了真实配置、OPA、Checkpoint、审批、审计和掩码，但 Gateway 使用 FakeGateway，DecisionStore 也未完全复用正式 Runtime 的持久化组装。

建议保留当前快速 E2E，同时增加一个发布前测试，使用：

- `build_runtime()`；
- `JsonlDecisionStore`；
- 真实 OPA；
- 本地 mock email MCP；
- filesystem MCP；
- `JsonlAuditStore`。

### P2：MCP 与部署边界

当前 MCP Gateway 依赖“调用者已通过 Checkpoint 授权”的架构约束，Gateway 自身不重复检查 Profile 或 Decision。应确保原始 Gateway 不暴露给 Agent 或其他业务模块。

另外，filesystem MCP 通过未固定版本的 `npx -y` 获取依赖，建议固定 Node 版本和 npm 包版本，降低演示失败与供应链漂移风险。

### P2：示例和文档存在过期内容

- 正式示例顶部仍称审批尚未接通，但当前 Runtime 已接通审批；
- 旧 `research_assistant_example.py` 使用当前包已移除的旧 API；
- README 中“7 条启动校验”与当前 LLM API Key 校验数量不一致；
- `/data/...` 使用系统根目录，在不同平台可能有权限或路径语义问题。

建议更新正式示例说明，删除或迁移旧示例，并将演示数据目录改成项目内或配置化目录。

## 3. Main 分支中建议吸收的内容

`main` 与 `develop` 从共同提交后分别演进，不是简单的新旧版本关系。以下内容建议从 `main` 选择性移植到 `develop`，而不是直接覆盖或整体合并。

### 3.1 跨任务 Session 风险状态

`main` 实现了按 Session 累计拒绝次数，并在达到阈值后阻止同一 Session 后续任务。`develop` 当前主要控制单任务调用次数、预算和动作历史，缺少跨任务持续风险状态。

建议在新 Runtime 架构中增加 SessionRiskStore：

- 按 `session_id` 记录 deny、approval、critical risk 等事件；
- 风险达到阈值后在预算预留前直接阻断；
- 状态持久化并设置过期/衰减规则；
- 在审计中记录阈值命中及状态版本；
- 增加跨 Task 的 E2E 测试。

这能强化 R3 持续监督反馈到 R2 控制的闭环。

### 3.2 拒绝路径预算退款逻辑

`main` 在权限组合 deny、Policy deny、审批未配置和审批拒绝时显式退款。虽然实现较简单，但其预算生命周期方向比当前 `develop` 更完整。

建议吸收这种“拒绝不消耗执行预算”的语义，并在 `develop` 中统一实现，而不是逐个分支零散补丁。

### 3.3 编排边界、ID 与状态管理文档

`main` 对上层 Agent 编排与治理控制平面的边界、可信 ID 生成和状态职责做了补充。建议将这些内容与 `develop` 当前 Runtime 实现对齐后合入架构文档，明确：

- Planner 只能产生不可信业务草案；
- Runtime 生成可信 `call_id` 和身份字段；
- Checkpoint 是唯一权威判定点；
- MCP Gateway 是唯一工具执行出口；
- Decision 和 Approval 必须与 Task/Call/Agent 强绑定；
- 哪些状态按 Task、Session、Agent 或全局管理。

### 3.4 跨任务预算与风险联合测试思路

`main` 有跨 Task 验证预算和 Session 风险拦截的 E2E 测试。建议将测试场景迁移到 `develop` 的 Pydantic 模型、异步 Runtime 和持久化存储体系，而不是直接复制旧测试代码。

推荐覆盖：

1. 同一 Session 多个 Task 连续 deny；
2. 达阈值后的动作在预算 reserve 前被拒绝；
3. 不同 Session 相互隔离；
4. 状态重启恢复；
5. 风险衰减或 Session 结束后的清理。

### 3.5 OPA 环境配置经验

`main` 已有通过环境变量选择 OPA 地址和策略包的实践。`develop` 可以进一步统一：

- `OPA_URL`；
- `OPA_PATH`；
- policy package；
- timeout；
- 启动校验行为；
- 测试 fixture 的跨平台路径解析。

避免示例、测试和 CI 分别硬编码不同路径。

## 4. 推荐整合顺序

1. 修复预算生命周期；
2. 强化审批记录绑定；
3. 修复 DecisionStore 损坏日志语义；
4. 统一 OPA_PATH 并补齐 CI gate；
5. 修复 Ruff/mypy；
6. 将 `main` 的 Session 风险状态迁移到新 Runtime；
7. 增加正式组件完整 E2E；
8. 清理旧示例和过期文档；
9. 最后再考虑合并回主分支。

不建议直接把 `develop` merge 到当前 `main`。两个分支都修改了 Checkpoint、Policy Engine、MCP Gateway 和领域模型，而且 `develop` 已将多个旧模型模块合并进 `models.py`。更安全的方式是在 `develop` 基础上建立集成分支，按上述顺序选择性迁移 `main` 的能力，再通过 Pull Request 评审。

## 5. 对外演示建议

建议以 `develop` 作为演示基线，先使用 ScriptedPlanner 保证确定性，再把 LLMPlanner 作为增强演示。

演示重点：

1. 正常读写动作经 R2 allow 后由 MCP 执行；
2. 内部邮件触发审批并可配置 approve/deny；
3. 外部邮箱或越界路径被 OPA 拒绝；
4. 审计日志展示 trace、掩码、策略版本和哈希链；
5. 强调“LLM 提出动作，Loop Controller 决定能否执行”。

对外边界：

- 审批仍是配置打桩，没有真实 UI；
- 防重放当前基于单进程 asyncio 假设；
- 哈希链不是不可篡改存储；
- 预算是估算值；
- E2E 的部分链路仍使用 FakeGateway；
- 当前是较完整 MVP，不是生产级安全治理平台。
