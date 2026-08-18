# 关于《Loop Controller 下一阶段开发方案（v0.3.0）》的询问清单

> 状态：待策划/规划 agent 确认  
> 最后更新：2026-08-18  
> 对应文档：[src/Loop_Controller_下一阶段开发方案_v0.3.0.md](./Loop_Controller_下一阶段开发方案_v0.3.0.md)

---

## 背景

当前 develop 已推送（HEAD `92a91fc`），包含 P0 HMAC 修正、P1 L2 会话风险判定以及讨论摘要文档。代码 agent 已确认 174 个测试通过。在按 v0.3.0 方案继续执行前，发现以下 8 个需要决策的衔接点。

---

## Q1：develop 已推送，与 §2.1/§2.3 的时序冲突

**现状**

上一回合用户三次要求「先推送吧」，代码 agent 已执行 `git push origin develop`。当前远程 develop 位于 `92a91fc`。

但 v0.3.0 方案 D1 和 §2.3 写明：「必须先完成 §2 发布前检查，再 push develop」。

**疑问**

- 是否接受现状（develop 已在远程），改为在最终 HEAD 上补做 §2.2 检查并直接打 `v0.2.0` tag？
- 还是需要通过 force-push 撤回 develop，待检查完成后再推送？

**代码 agent 倾向**

不要 force-push。直接在当前 HEAD 上完成 §2.2 检查后打 tag，避免改写已发布历史。

---

## Q2：`key_id` 目前实际为空，是否影响 v0.2.0 发布

**现状**

§2.2 安全回归第 7 条要求「审计事件或 seal 记录包含 `key_id`」。

当前 `JsonlAuditStore` 创建时未传入 `key_id`（见 [runtime.py:144-148](../loop_controller/runtime.py#L144-L148)），因此事件中 `key_id` 字段存在但值为 `null`。

**疑问**

- 是否需要在 v0.2.0 发布前补一个非空 `key_id`？例如默认值 `"default"`、从 key 派生的短 hash，或配置项 `audit_key_id`。
- 还是调整检查清单表述为「字段存在、为轮换留口，P0 值可为 null」？

**代码 agent 倾向**

若 v0.2.0 定位为「可信执行基线」，建议 `key_id` 至少为 `"default"` 或从 key 派生，避免审计链中出现无意义的 null。

---

## Q3：多轮对话的交互模型

**现状**

§4.4 新增 `runtime.add_user_message()` / `runtime.add_agent_message()`，但当前 `run_task` 是单次循环：用户输入 → Planner → 治理 → 执行 → 结束。

**疑问**

多轮对话时，Runtime 的调用方式应该是哪一种？

- **方案 A（外部驱动）**：每次用户回复后，外部调用 `add_user_message()`，再调用 `run_task()` 继续。`run_task` 不内部阻塞。
- **方案 B（内部阻塞）**：`run_task` 在需要用户澄清时主动暂停，等待外部输入。
- **方案 C（混合）**：Planner 返回一个特殊动作（如 `{"action": "ask_user", "question": "..."}`），由 Runtime 截断并暴露给外部。

**代码 agent 倾向**

方案 A 最符合当前架构（Runtime 是被调用的库，不是常驻服务），但需要在 `Planner.next_action()` 中区分「首次调用」和「等待用户回复后再次调用」。

---

## Q4：Task 不可变，但 Task 与 ConversationMessage 的关联规则

**现状**

§4.1 D8 规定 `Task` 保持不可变，§4.2 新增 session 级 `ConversationContext`。`ConversationMessage` 模型含 `task_id: str | None`。

**疑问**

- 一次 session 是否会产生多个 Task？如果是，每个 Task 对应「一轮用户输入」还是「一个用户目标」？
- `task_id = None` 的消息在什么场景下出现？
- `run_task(task)` 启动时，加载的是整个 session 的上下文，还是仅与该 task 相关的消息？

**代码 agent 倾向**

建议：一个用户目标 = 一个 Task；多轮澄清通过 `add_user_message()` 追加到同一 session，但 Task 不变。`run_task(task)` 启动时加载该 session 的全部上下文。`task_id` 用于标记消息最初属于哪个 Task，允许后续审计追踪。

---

## Q5：异步审批与现有 DecisionStore 的兼容性

**现状**

§5.4 规定 `require_approval` Decision 有 15 分钟有效期，审批通过后的新 allow Decision 重新起算 5 分钟，且 `max_uses = 1`。

但当前 `JsonlDecisionStore` 只记录 `call_id` 和 `decision_id` 的使用状态，没有 `expires_at`、`max_uses`、`used_count` 等字段。

**疑问**

- 是否扩展 `Decision` 模型，新增 `expires_at` / `max_uses` / `used_count`，由 DecisionStore 统一实现时间语义？
- 还是让 `ApprovalStore` 独立记录审批动作与有效期，DecisionStore 只负责防重放？

**代码 agent 倾向**

建议扩展 `Decision` 模型并在 DecisionStore 中统一校验有效期与使用次数，避免 ApprovalStore 与 DecisionStore 双写不一致（对应 §8 R4 风险）。

---

## Q6：CLI 入口

**现状**

§5.5 的 `lc approvals ...` CLI 需要项目提供可执行入口。但当前 `pyproject.toml` 没有 `[project.scripts]` 或 `[project.gui-scripts]`，也没有 `loop_controller/cli.py` 模块。

**疑问**

- CLI 是否为 v0.3.0 的必选项？
- 是否新增 `loop_controller/cli.py` 并在 `pyproject.toml` 注册 `lc = "loop_controller.cli:main"`？
- 如果只做库接口，CLI 是否延后到后续版本？

**代码 agent 倾向**

v0.3.0 的验收标准 A1-A14 都依赖 CLI 来演示真实审批闭环，建议作为必选项实现。

---

## Q7：策略模板如何与现有配置集成

**现状**

§6.1 给出 5 个策略模板（文件路径白名单、敏感目录保护、外部邮件保护、高会话风险门控、critical 工具门控）。当前系统通过 `profiles.yaml` 引用 `default.rego`，`CapabilityProfile` 已包含 `session_risk_threshold`。

**疑问**

- 策略模板是放在 `policies/templates/` 下的独立 `.rego` 文件，还是在 `default.rego` 中通过 `data.profile_template` 选择？
- `profiles.yaml` 是否需要新增字段（如 `template`、`policy_files`）来引用模板？
- 模板与 `CapabilityProfile.session_risk_threshold` 如何联动？

**代码 agent 倾向**

建议新增 `policies/templates/` 目录，每个模板一个 `.rego` 文件。`profiles.yaml` 中增加 `policy_files: ["templates/file_whitelist.rego"]`，由 `FilePolicyStore` 按顺序加载并合并。`session_risk_threshold` 继续作为 Rego input 传入，模板只比较 `input.session_risk.score >= input.session_risk.threshold`。

---

## Q8：发布检查清单与版本号的处理

**现状**

当前只有 [src/发布检查清单_v0.1.0.md](./发布检查清单_v0.1.0.md)，且 `pyproject.toml` 的 `version = "0.1.0"`。

**疑问**

- v0.2.0 的清单是覆盖更新 `src/发布检查清单_v0.1.0.md`，还是新建 `src/发布检查清单_v0.2.0.md`？
- 发布 v0.2.0 时是否同步更新 `pyproject.toml` 的 `version` 为 `"0.2.0"`？

**代码 agent 倾向**

建议新建 `src/发布检查清单_v0.2.0.md` 保留历史版本；同时更新 `pyproject.toml` version 为 `"0.2.0"`，保证 tag 与包版本一致。

---

## 请求

请策划/规划 agent 就以上 8 点给出明确决策。收到答复后，代码 agent 将按 v0.3.0 方案开始执行 Iteration R（v0.2.0 发布检查与 tag）或直接进入 Iteration 4（动态会话上下文）。
