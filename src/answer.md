这是一份**高质量、高信噪比的工程审查**，覆盖了安全边界、状态一致性、工程质量和架构演进四个维度。以下是我的中立分析：

---

## 一、总体评价

**审查者的核心判断是准确的**：当前代码是"可运行治理 MVP"，不是"生产级安全治理系统"。P0/P1/P2 的分级合理，且每个问题都给出了**可执行的修复路径**而非空泛批评。

特别值得肯定的是：
- **P0 定位精准**：预算不返还和审批绑定缺失都是**可被利用的漏洞**，不是代码风格问题。
- **对 Main 分支的考古有价值**：审查没有简单要求"合并 main"，而是逐条评估哪些能力值得迁移到 develop 的新 Runtime 架构上。
- **诚实性审查**（审计链不能声称"不可篡改"）体现了对安全承诺的审慎态度。

---

## 二、按优先级分析：哪些必须参考

### 🔴 必采纳（阻塞级）

#### 1. P0：预算预留在拒绝路径未返还
**这是实实在在的 DoS 漏洞。**

攻击者只需发送大量越权请求（如读取 `/etc/passwd`），每次都会预留预算但不返还，合法任务的预算将被耗尽。v1.0 文档 §3.8 虽然粗糙（`token_count=1` 占位），但**"拒绝不扣费"是基本语义承诺**，当前实现违背了这一承诺。

**建议立即修复**，且审查给出的"pending/finalized 生命周期"方案比零散补丁更可持续。

#### 2. P0：审批记录缺少强绑定验证
**这是审批绕过风险。**

v1.0 文档 §3.10 只要求了 `approver_id != requester_id` 的组装期校验，但审查发现 `finalize_after_approval()` 缺少对 `decision_id`、`request_id`、`call_id` 的回指验证。这意味着一个合法的 `ApprovalRecord` 可能被重用到另一个不相关的 Decision 上。

审查列出的 6 条校验规则**应全部采纳**。

#### 3. P1：DecisionStore 损坏偏 fail-open
**与架构的 fail-closed 原则直接冲突。**

v1.0 文档 §4.1 明确启动校验应 fail-closed，但 DecisionStore 在加载时跳过损坏行，导致防重放状态丢失。审查建议"非法 JSON 阻止 Runtime 启动"**完全符合 v1.0 的设计原则**。

---

### 🟡 强烈建议采纳（质量门禁级）

#### 4. P1：CI 中 OPA 路径不一致
这会导致**测试静默跳过**，使"124 passed"这个数字产生误导——关键集成测试可能根本没跑。审查建议的 `OPA_PATH` 统一优先 + 平台回退 + skip 数量门禁**应立刻实施**。

#### 5. P1：工程质量门禁（Ruff / mypy）
MVP 阶段代码量小，正是建立门禁的最佳时机。拖到后期修复成本指数上升。建议采纳，但**不必阻塞功能开发**，可以并行推进。

#### 6. P2：完整 E2E 使用真实组件
当前 FakeGateway 的 E2E 测的是"治理逻辑"，没测"真实 MCP 调用路径"。审查建议的发布前测试（真实 OPA + filesystem MCP + mock email MCP）**是 MVP 对外演示前的最低要求**。

---

### 🟢 可参考但需权衡（架构演进级）

#### 7. Main 的跨任务 Session 风险状态
**这与 v1.0 的 MVP 范围有冲突。**

v1.0 文档 §1.2 明确将"跨 turn 风险累积"移出 MVP，§3.9 规定 `RiskStateManager` 纯内存、任务结束即弃。审查建议从 main 吸收 Session 风险状态，本质上是**把 post-MVP 能力提前引入**。

**中立建议**：
- 如果 main 的实现稳定且迁移成本低，可以作为 **v1.1 增强** 吸收，但**不应阻塞 MVP 的冻结和发布**。
- 如果吸收，必须补充 v1.0 文档中未定义的 Session 过期/衰减规则（审查提到了，但 main 的实现可能也不完整）。

#### 8. Main 的拒绝路径预算退款逻辑
这与 P0 直接呼应，但审查指出 main 的实现"较简单"。**建议以 develop 的新 Runtime 架构为基座重新实现**，而不是移植旧代码。采纳其"拒绝不消耗预算"的语义，而非具体实现。

#### 9. Main 的编排边界文档
这部分是**纯文档收益**，没有代码风险。建议直接吸收并合入 v1.0 架构文档，明确 Runtime 生成可信 ID、Checkpoint 唯一权威等边界。

---

## 三、可以商榷或补充的点

### 1. 审计链完整性表述
审查建议对外表述为"支持链式篡改检测"而非"不可篡改"。**完全同意**。但可补充：在 MVP 演示中，可以现场运行 `verify_chain()` 并展示"篡改一行后校验失败"的效果，这比文字声明更有说服力。

### 2. MCP 供应链风险（npx -y）
审查建议固定 Node 版本和 npm 包版本。**合理**，但 MVP 阶段可以退而求其次：在 `mcp_servers.yaml` 中增加 `version` 字段并文档化，不要求锁定到具体 hash。

### 3. 示例中的 `/data/...` 路径问题
审查指出系统根目录可能有权限问题。**建议改为项目内相对路径**（如 `project_root/data/`），并通过 `ConfigLoader` 解析为绝对路径，这样跨平台演示更稳定。

---

## 四、对"推荐整合顺序"的评价

审查推荐的 9 步顺序**大体合理**，但我建议微调：

| 审查顺序 | 我的建议 | 理由 |
|---------|---------|------|
| 1. 修复预算生命周期 | ✅ 保持第 1 | P0 安全漏洞 |
| 2. 强化审批记录绑定 | ✅ 保持第 2 | P0 安全漏洞 |
| 3. 修复 DecisionStore | ✅ 保持第 3 | 与 1/2 同属状态一致性 |
| 4. 统一 OPA_PATH | ✅ 保持第 4 | 否则测试不可信 |
| 5. 修复 Ruff/mypy | ⚠️ 降至与 6 并行 | 不阻塞功能，可 CI 并行跑 |
| 6. 迁移 Session 风险 | ⚠️ 明确为 v1.1 | 超出 MVP 范围，不要阻塞发布 |
| 7. 增加正式 E2E | ✅ 提前到第 5 | 演示前必须完成 |
| 8. 清理旧示例 | ✅ 保持 | 低优先级 |
| 9. 合并回主分支 | ❌ 删除此步骤 | develop 应成为新主分支，而非合并回旧 main |

---

## 五、一句话总结

> **这份审查的 P0 和 P1 项应被视为开发阻塞项（blockers），必须修复；P2 和工程门禁应在 MVP 冻结前完成；Main 分支的 Session 风险状态是优质增强，但建议作为 v1.1 引入，避免扩大 MVP 范围。审查整体值得团队逐条对照执行。**

---

## 六、修复状态（v0.3.0 develop 分支）

> 以下修复全部在 `develop` 分支完成，`main` 分支保留为历史参考，不再向 `main` 做 merge/rebase。

| 优先级 | 问题 | 状态 | 关键提交 | 自动化验证 |
|---|---|---|---|---|
| P0 | 拒绝路径预算未返还 | ✅ 已修复 | `b162e89` | `tests/test_checkpoint.py::test_evaluate_refund_on_policy_deny` |
| P0 | 审批记录强绑定验证 | ✅ 已修复 | `b162e89` | `tests/test_checkpoint.py::test_finalize_after_approval_binding_validation` |
| P1 | DecisionStore 损坏 fail-open | ✅ 已修复 | `260f544` | `tests/test_decision_store.py::test_corrupt_log_fail_closed` |
| P1 | CI OPA 路径不一致 | ✅ 已修复 | `ef30cd7` | `.github/workflows/ci.yml` + `tests/conftest.py::resolve_opa_bin` |
| P1 | 工程质量门禁 | ✅ 已修复 | `5d30257` | CI lint job（ruff + mypy）+ OPA 可用性校验 |
| P2 | 完整 E2E 用真实组件 | ✅ 已修复 | `e9ce095` | `tests/test_e2e_real_mcp.py` |
| P2 | 旧示例清理 | ✅ 已修复 | `ba74a07` | 删除过期的 `examples/research_assistant_example.py` |

### 当前基线验证

- `python -m pytest tests/`：**205 passed**
- `python -m ruff check src tests`：**All checks passed**
- `python -m mypy src`：**Success**

### 未纳入本次修复（v1.1 或后续）

- **Main 的跨任务 Session 风险状态**：按 §四 建议，作为 v1.1 增强，不阻塞 MVP 冻结。
- **Main 的编排边界/文档**：有价值的纯文档内容，可后续以 cherry-pick/手动拷贝方式进入 `develop`，不 merge 历史。
