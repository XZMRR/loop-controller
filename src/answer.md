已查看。以下是对 8 个问题的定案；其中 Q3、Q5、Q7 我没有完全照代码 agent 的倾向，而是做了架构上的修正。

# 总体结论

1. **不 force-push develop**；
2. **v0.2.0 先补 `key_id` 与版本号，再打 tag**；
3. **多轮交互采用“外部驱动 + 显式 ask_user 信号”**；
4. **Task 继续表示用户目标，ConversationContext 绑定 session**；
5. **Decision 有效期与消费次数由 DecisionStore 统一管理**；
6. **CLI 是 v0.3.0 必选项**；
7. **策略模板按 profile 显式选择，不做文本拼接式合并**；
8. **新建 v0.2.0 检查清单，并同步 `pyproject.toml` 版本号**。

---

# Q1：develop 已推送，与发布检查时序冲突

## 定案

**接受现状，不 force-push。**

处理顺序调整为：

1. 在当前 `origin/develop` HEAD 上补做 v0.2.0 发布检查；
2. 若发现问题，直接新增修正 commit；
3. 所有检查通过后，在最终 HEAD 上打 `v0.2.0`；
4. push tag。

也就是说，原计划中的：

```text
检查 → push → tag
```

现实调整为：

```text
已 push → 补检查 → 必要时 fix-forward → tag 最终 HEAD
```

## 理由

- develop 已经公开，改写历史没有必要；
- 当前问题不是代码污染，而是发布流程顺序变化；
- 只要 tag 打在最终验证过的 HEAD 上，发布语义仍然正确。

---

# Q2：`key_id` 目前为空，是否影响 v0.2.0

## 定案

**影响，发布前必须修。**

不要接受审计事件里长期存在无意义的 `null`。

建议实现：

```yaml
audit_key_id: "default"
```

同时允许环境变量覆盖：

```bash
LOOP_CONTROLLER_AUDIT_KEY_ID="default"
```

规则：

- `key_id` 必须非空；
- 默认值可以是 `"default"`；
- 不从 HMAC key 派生；
- 不把 key 本身、key 的明文 hash 作为 `key_id`；
- 未来轮换时由部署方显式更新 `key_id`。

## 理由

`key_id` 的作用是运维识别：

```text
这条审计链当时使用的是哪一把 key
```

它不是防伪材料，因此没必要从 secret 派生；显式配置更清晰，也更方便未来轮换。

## 发布要求

将原检查项改为：

- [ ] HMAC 模式下 `key_id` 非空；
- [ ] 默认值为 `"default"` 或部署方显式配置；
- [ ] seal 与 audit event 使用同一个 `key_id`；
- [ ] 更换 `key_id` 后验证工具能正确识别。

---

# Q3：多轮对话的交互模型

## 定案

采用：

```text
方案 A 的外部驱动模型
+
方案 C 的显式 ask_user 信号
```

但不是把 `ask_user` 做成一个普通 tool call。

## 最终交互模型

Runtime 仍然是库，不内部阻塞等待用户。

Planner 的返回类型扩展为：

```python
PlannedAction | UserQuestion | None
```

例如：

```python
class UserQuestion(BaseModel):
    question: str
    reason: str | None = None
```

Runtime 行为：

| Planner 返回 | Runtime 行为 |
|---|---|
| `PlannedAction` | 走正常 R2 治理与工具执行 |
| `UserQuestion` | 记录 agent message，返回 `needs_user_input` |
| `None` | 返回 `completed` |

外部调用方收到：

```python
TaskRunResult(status="needs_user_input")
```

之后由外部调用方完成：

```python
runtime.add_user_message(...)
runtime.resume_task(...)
```

## 为什么不选纯方案 A

纯方案 A 没有回答一个关键问题：

> Agent 如何明确告诉 Runtime：“我现在不是结束，而是在等用户补充”？

如果仅靠自然语言结尾猜测，可靠性太差。

## 为什么不选方案 B

`run_task` 内部阻塞等待输入会把 Runtime 从库变成交互服务，破坏当前部署形态，也会让测试、超时、取消和后续 Proxy 服务化都更复杂。

## 为什么不让 ask_user 成为工具调用

`ask_user` 不产生外部副作用，不应该进入 R2 的工具治理链路。它是 R1 与调用方之间的控制信号，不是 tool call。

---

# Q4：Task 与 ConversationMessage 的关联规则

## 定案

采用：

```text
一个用户目标 = 一个 Task
一个 session 可以包含多个 Task
一次多轮澄清属于同一个 Task
ConversationContext 绑定 session
```

## 具体规则

### 1. Task 的语义

`Task` 表示用户目标，而不是一次消息回合。

例如：

```text
帮我写个合规报告
```

这是一个 Task。

后续：

```text
主题是 AI 数据安全
需要包含 GDPR 和中国个保法
```

仍然是同一个 Task 的补充输入。

### 2. 什么时候创建新 Task

当用户开启一个新目标时创建新 Task。

例如：

```text
再帮我写一份产品介绍
```

这是新 Task，但仍可复用同一个 session。

### 3. `task_id = None` 的含义

保留该可能性，但 v0.3.0 正常路径不应产生。

约定：

- 用户消息和 Agent 消息在 v0.3.0 中都必须带 `task_id`；
- `task_id = None` 仅保留给未来的 session 级消息；
- 当前 R2 构建治理上下文时，可以忽略 `task_id = None` 的消息。

### 4. `run_task` 加载哪些上下文

加载整个 session 的上下文，但排序与截断时优先：

1. 当前 Task 的描述；
2. 当前 Task 的最近消息；
3. 同一 session 内其他 Task 的最近消息。

这样可以同时支持：

- 同一目标的多轮澄清；
- 同一 session 中多个相关目标之间的上下文继承。

---

# Q5：异步审批与 DecisionStore 的兼容性

## 定案

**DecisionStore 统一管理 Decision 的有效期与消费状态。ApprovalStore 只管理审批事件。**

但要修正代码 agent 的表述：

- `expires_at`、`max_uses` 属于 `Decision`；
- `used_count` 不属于不可变 `Decision`，它属于 DecisionStore 的运行状态。

## 职责边界

### Decision 模型

`Decision` 是不可变授权凭证，包含：

- `decision_id`
- `call_id`
- `verdict`
- `expires_at`
- `max_uses`
- `modified_args`
- `policy_hits`
- `reason`

如果当前实现缺少 `expires_at` 或 `max_uses`，需要补齐。

### DecisionStore

DecisionStore 负责：

- 注册 Decision；
- 校验 `decision_id` 是否存在；
- 校验是否过期；
- 校验 `used_count < max_uses`；
- 消费 Decision；
- 防止重复使用；
- 防 `call_id` 重放。

建议内部记录：

```text
decision_id
call_id
task_id
session_id
status
expires_at
max_uses
used_count
created_at
consumed_at
```

### ApprovalStore

ApprovalStore 只负责：

- 记录审批请求；
- 记录审批人；
- 记录 approve / deny / expire；
- 保存审批理由；
- 支持重启恢复。

它不判断最终能否执行。

## 审批通过后的流程

```text
ApprovalStore 记录 approved
→ ApprovalService / Checkpoint 创建新的 allow Decision
→ DecisionStore 注册该 Decision
→ forward 时由 DecisionStore 校验并消费
```

这样可以避免 §8 R4 提到的双写不一致问题。

---

# Q6：CLI 入口

## 定案

**CLI 是 v0.3.0 必选项。**

新增：

```text
src/loop_controller/cli.py
```

并在 `pyproject.toml` 中注册：

```toml
[project.scripts]
lc = "loop_controller.cli:main"
```

v0.3.0 最小命令：

```bash
lc approvals list
lc approvals show <decision_id>
lc approvals approve <decision_id> --approver alice --reason "..."
lc approvals deny <decision_id> --approver alice --reason "..."
```

## 理由

v0.3.0 的核心叙事是“真实治理闭环”。如果没有 CLI，异步审批只能停留在库接口，无法完成可演示、可操作的人工闭环。

Web UI 仍然不做。

---

# Q7：策略模板如何与现有配置集成

## 定案

部分采纳代码 agent 建议，但不允许简单的文本拼接式合并。

采用：

```text
policies/
  default.rego
  templates/
    file_whitelist.rego
    sensitive_directories.rego
    external_email.rego
    session_risk.rego
    critical_tools.rego
```

`profiles.yaml` 新增：

```yaml
policy_files:
  - "default.rego"
  - "templates/file_whitelist.rego"
  - "templates/external_email.rego"
```

## 合并方式

不要把多个 `.rego` 文件拼接成一个大文件。

正确方式是：

1. PolicyStore 记录 profile 选择的 policy files；
2. PolicyEngine 查询 base policy 与被选择的 template policy；
3. 收集多个 candidate decisions；
4. 用统一优先级合并：

```text
deny > require_approval > modify > allow
```

## 版本号

`policy_version` 必须反映当前 profile 实际启用的 policy files。

建议计算方式：

```text
sha256(
  排序后的 relative path
  + 每个文件内容 hash
)
```

不能只 hash 整个 `policies/` 目录，否则无法区分“文件存在但未被该 profile 启用”和“该 profile 实际启用了该模板”。

## `session_risk_threshold`

继续由 `CapabilityProfile` 提供，并进入 Rego input：

```rego
input.session_risk.score >= input.session_risk.threshold
```

模板不自己定义阈值。

---

# Q8：发布检查清单与版本号

## 定案

两个都做。

### 1. 新建 v0.2.0 检查清单

保留：

```text
发布检查清单_v0.1.0.md
```

新增：

```text
发布检查清单_v0.2.0.md
```

理由：

- v0.1.0 清单是历史发布记录；
- 不覆盖历史版本；
- 每个版本的发布门槛可以独立追溯。

### 2. 更新包版本号

发布前将：

```toml
version = "0.2.0"
```

写入 `pyproject.toml`。

tag 必须打在包含以下内容的最终提交上：

- `key_id` 非空；
- `pyproject.toml` 版本号为 `0.2.0`；
- v0.2.0 检查清单已更新；
- 全量测试通过；
- 手动真实 MCP gate 通过。

---

# 调整后的执行顺序

当前最优顺序变为：

## Step 1：发布修正

先做一个小的 release-preparation commit：

- 补 `audit_key_id`；
- 补 v0.2.0 检查清单；
- 更新 `pyproject.toml` version；
- 更新 README / KNOWN_LIMITATIONS；
- 跑全量测试与真实 MCP gate。

## Step 2：打 v0.2.0 tag

在最终 HEAD 上：

```bash
git tag -a v0.2.0 <final-head> -m "v0.2.0: trusted execution baseline"
git push origin v0.2.0
```

## Step 3：进入 Iteration 4

开始动态会话上下文，但采用本文 Q3/Q4 的修正模型：

- 外部驱动；
- 显式 `UserQuestion`；
- `resume_task`；
- session 级 ConversationContext。

## Step 4：进入 Iteration 5

实现异步审批，但按 Q5 的职责边界：

- ApprovalStore 记录审批；
- DecisionStore 统一判断能否执行；
- `used_count` 不进入不可变 Decision。

---

# 对代码 agent 倾向的处理结果

| 问题 | 是否采纳代码 agent 倾向 | 说明 |
|---|---|---|
| Q1 | 采纳 | 不 force-push |
| Q2 | 采纳 | 发布前补非空 `key_id` |
| Q3 | 部分采纳 | 外部驱动是对的，但需要显式 `UserQuestion` |
| Q4 | 采纳并补充 | 一个用户目标一个 Task；session 加载完整上下文 |
| Q5 | 部分采纳 | DecisionStore 统一校验是对的，但 `used_count` 不进 Decision |
| Q6 | 采纳 | CLI 是 v0.3.0 必选项 |
| Q7 | 部分采纳 | `policy_files` 是对的，但不能文本拼接合并 |
| Q8 | 采纳 | 新清单 + 更新版本号 |

这些决定可以直接作为 v0.3.0 的执行口径。