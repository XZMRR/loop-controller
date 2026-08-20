# v0.11.0 开发方案：Earned Authority Manager（动态权限提升）

> **目标**：在静态 CapabilityProfile 天花板之上，引入受控的动态权限提升机制。Agent 可在任务执行过程中申请临时能力（AuthorityToken），经治理系统评估条件后签发；Checkpoint 在裁决时识别有效 Token，将原本 deny/require_approval 的动作降级为 allow/require_approval（取决于公司 Rego 策略）。
>
> **状态**：已完成。

---

## 1. 背景与动机

当前 `CapabilityProfile` 是**静态天花板**：Agent 启动时即固定可用工具、参数范围、审批要求。这在很多真实任务中过于僵硬：

- Agent 需要阶段性访问更敏感的工具（如外部邮件、数据库写入）；
- 完全禁止导致任务失败；
- 完全开放违背最小权限原则。

Earned Authority Manager 提供第三条路：**按上下文、条件、预算、时间窗口动态授予临时能力**，并在使用后自动失效或衰减。

---

## 2. 核心设计原则

| 原则 | 说明 |
|------|------|
| **最小权限基线** | 静态 Profile 仍是默认天花板；动态权限是**显式例外**。 |
| **条件驱动** | 每次 grant 必须满足声明式条件（用户确认、预算、历史干净等）。 |
| **一次性/限时** | Token 有有效期和预算上限，过期或耗尽后自动失效。 |
| **审计完整** | grant / use / revoke / expire 都写入审计链。 |
| **Rego 最终裁决** | Token 只改变 input 事实；是否允许仍由 Rego 决定。 |
| **不可累积** | 同一任务不能无限叠加 Token，防止权限蠕变。 |

---

## 3. 新增模型

### 3.1 AuthorityRequest

```python
@dataclass(frozen=True)
class AuthorityRequest:
    request_id: str
    agent_id: str
    task_id: str
    requested_capabilities: list[str]
    reason: str
    user_confirmation: bool = False
```

### 3.2 AuthorityToken

```python
@dataclass(frozen=True)
class AuthorityToken:
    token_id: str
    request_id: str
    agent_id: str
    task_id: str
    granted_capabilities: list[str]
    budget: BudgetCost
    remaining_budget: BudgetCost
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None
    audit_record_id: str
```

### 3.3 AuthorityConditions（配置）

```python
@dataclass(frozen=True)
class AuthorityConditions:
    user_confirmation: bool = False
    budget_remaining: int | None = None          # 任务剩余预算阈值
    no_recent_denials_within_steps: int | None = None
    require_task_context_regex: str | None = None
```

### 3.4 AuthorityGrantRule（配置）

```python
@dataclass(frozen=True)
class AuthorityGrantRule:
    capability: str
    description: str
    conditions: AuthorityConditions
    max_duration_seconds: int
    budget_limit: BudgetCost
```

---

## 4. 能力规则配置

新增 `config/authority_rules.yaml`：

```yaml
authority_grants:
  email_external:
    description: "允许在用户确认后向外部邮箱发邮件"
    conditions:
      user_confirmation: true
      budget_remaining: 10
      no_recent_denials_within_steps: 5
    max_duration_seconds: 300
    budget_limit:
      token_count: 5

  database_write:
    description: "允许在任务上下文匹配时写入数据库"
    conditions:
      user_confirmation: true
      require_task_context_regex: "update.*customer"
    max_duration_seconds: 60
    budget_limit:
      token_count: 2
```

---

## 5. 核心组件

### 5.1 EarnedAuthorityManager

职责：

- `request_authority(request: AuthorityRequest, context) -> AuthorityToken | DenyReason`
- `validate_token(token_id, proposal) -> AuthorityToken | None`
- `consume_budget(token_id, cost) -> bool`
- `revoke_token(token_id, reason)`
- `revoke_expired_tokens(now)`
- `active_tokens_for(task_id) -> list[AuthorityToken]`

评估流程：

1. 查找 `authority_rules.yaml` 中对应能力的规则；
2. 检查全局开关（如 `enabled: true`）；
3. 逐项验证 `AuthorityConditions`；
4. 检查是否已存在同能力 active token（防止重复 grant）；
5. 预留预算（从任务预算中扣除 token 预算）；
6. 生成 token，持久化，写审计事件；
7. 返回 token。

### 5.2 AuthorityStore

JSONL 持久化，与 `JsonlDecisionStore` / `JsonlReservationStore` 风格一致。

- 追加写入 token 状态变更（created / used / revoked / expired）；
- 加载时重放状态机，重建 active token 集合；
- 损坏行 fail-closed（与 DecisionStore 一致）。

### 5.3 审计集成

新增审计事件类型：

- `authority_granted`
- `authority_used`
- `authority_revoked`
- `authority_expired`

---

## 6. 与现有链路集成

### 6.1 ActionProposal 扩展

```python
class ActionProposal(BaseModel):
    ...
    authority_token_ids: list[str] = Field(default_factory=list)
```

Agent/Planner 在发起动作时，可把已持有的 token_id 放入该字段，声明"我已被授权"。

### 6.2 Checkpoint 集成

在 `evaluate()` 步骤 5（权限组合分析）之后、步骤 6（OPA）之前，新增步骤 5.5：

```python
# 步骤 5.5：动态权限提升校验
if rule is not None and proposal.authority_token_ids:
    valid_tokens = self._authority_manager.validate_for_proposal(
        proposal, required_capabilities=extracted_capabilities
    )
    if valid_tokens:
        # Token 存在：把这一事实加入 proposal，但不直接覆盖 deny
        # 最终裁决交给 Rego
        proposal = proposal.model_copy(
            update={"authority_token_ids": [t.token_id for t in valid_tokens]}
        )
```

注意：Token 不直接短路 deny，而是作为 input 事实交给 Rego。这是为了保持 Rego 的公司策略最终裁决权。

### 6.3 policy_input 扩展

```python
"action": {
    "combination_risk_tags": proposal.combination_risk_tags,
    "combination_risk_score": proposal.combination_risk_score,
    "authority_token_ids": proposal.authority_token_ids,
}
```

### 6.4 default.rego 更新

```rego
# 有 token 的 data_exfil：从 deny 降级为 require_approval
decision := {"verdict": "require_approval", "reason": "data exfil with authority token requires final approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["capability_data_exfil_token_approval"]} if {
    some tag in input.action.combination_risk_tags
    tag == "data_exfil"
    count(input.action.authority_token_ids) > 0
}
```

公司领导可以根据风险偏好调整：
- 保守：有 token 仍然 require_approval；
- 宽松：有 token 则 allow；
- 严格：有 token 也 deny（Token 仅用于其他场景）。

### 6.5 Runtime 注入

```python
authority_manager = EarnedAuthorityManager(
    rules=config.authority_rules,
    store=JsonlAuthorityStore(config.authority_log_path),
    budget_ledger=budget_ledger,
    audit_logger=...,
)
```

---

## 7. 改动面清单

| 文件 | 改动 |
|------|------|
| `src/loop_controller/models.py` | 新增 `AuthorityRequest`, `AuthorityToken`, `AuthorityConditions`, `AuthorityGrantRule`；扩展 `ActionProposal` |
| `src/loop_controller/authority.py` | 新增 `EarnedAuthorityManager` |
| `src/loop_controller/infra/authority_store.py` | 新增 `AuthorityStore` / `JsonlAuthorityStore` |
| `src/loop_controller/infra/config_loader.py` | 加载 `authority_rules.yaml` |
| `src/loop_controller/checkpoint.py` | 步骤 5.5 集成 token 校验 |
| `src/loop_controller/policy_engine.py` | `build_policy_input` 透传 `authority_token_ids` |
| `src/loop_controller/runtime.py` | 注入 `EarnedAuthorityManager` |
| `src/loop_controller/audit.py` | 新增 authority 审计事件类型 |
| `policies/default.rego` | 基于 token 的降级/审批规则 |
| `config/authority_rules.yaml` | 新增配置 |
| `tests/test_authority.py` | 新增测试 |
| `tests/test_checkpoint.py` | 更新覆盖 token 路径 |
| `src/development_log.md` | 记录 v0.11.0 完成 |

---

## 8. 验收标准

- [ ] `pytest tests/` 全部通过；
- [ ] `ruff check src tests` 无告警；
- [ ] `config/authority_rules.yaml` 可正确加载；
- [ ] 用户确认 + 预算充足时，可成功签发 `email_external` token；
- [ ] 持有效 token 的 `send_email` 外部邮件动作，Rego 从 deny 降级为 require_approval；
- [ ] token 过期或预算耗尽后，动作恢复 deny；
- [ ] 每次 grant/use/revoke 都有审计记录。

---

## 9. 风险与注意事项

1. **Token 不能被 Agent 伪造**：token_id 由 AuthorityManager 生成并持久化，Checkpoint 只信任 store 中的记录。
2. **用户确认不可被 Agent 自行标记**：`user_confirmation` 必须由 R0 审批接口或显式用户输入设置。
3. **预算双重扣除风险**：Token 签发时从任务预算中预留，使用时从 token 预算扣除；拒绝路径必须返还 token 预留。
4. **Token 与 Capability 多对多**：一个 Token 可包含多个 capability，但一个动作可能需要多个 capability；需验证 Token 集合是否覆盖所需能力。
