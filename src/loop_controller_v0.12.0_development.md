# v0.12.0 开发方案：R3 Asynchronous Audit Analyzer（异步审计分析器）

> **目标**：补齐 R3 审计层，让 Loop Controller 在记录审计日志之外，能够异步分析日志、识别异常模式并生成告警/报告。分析器不阻塞主治理链路（R0-R2），在 task_end 后或独立触发。
>
> **状态**：已完成。

---

## 1. 背景与动机

当前系统已经具备完整的审计日志能力（`JsonlAuditStore`）：每次 propose/evaluate/execute/approve/deny/task_end 都会写入带哈希链的事件。但 R3 层只有"记录"，没有"分析"：

- 无法自动发现 Agent 的异常行为模式；
- 无法对高频 deny、权限蠕变、token 异常消费等风险发出告警；
- 无法为安全运营人员生成可读的审计摘要。

v0.12.0 引入异步审计分析器，消费已有审计日志，输出结构化告警与报告。

---

## 2. 核心设计原则

| 原则 | 说明 |
|------|------|
| **异步不阻塞** | 审计分析在 task_end 后触发，不影响主链路延迟。 |
| **只读消费** | 分析器只读取审计日志，不修改已有链。 |
| **声明式规则** | 异常检测规则通过 YAML 配置，便于安全运营调整。 |
| **可扩展** | 架构预留 LLM-based analyzer 接口，v0.12.0 先实现规则版。 |
| **可独立运行** | 提供 CLI 入口 `lc audit analyze`，支持手动分析指定 session/task。 |

---

## 3. 新增模型

### 3.1 AuditAlert

```python
class AuditAlert(BaseModel):
    alert_id: str
    session_id: str
    task_id: str | None  # 部分告警跨 task，可为空
    rule_id: str
    severity: Literal["low", "medium", "high", "critical"]
    title: str
    description: str
    evidence: list[str]  # event_id 列表
    created_at: datetime
```

### 3.2 AuditReport

```python
class AuditReport(BaseModel):
    report_id: str
    session_id: str
    task_id: str | None
    generated_at: datetime
    summary: str
    alert_ids: list[str]
    event_count: int
    metadata: dict[str, Any]
```

### 3.3 AuditRule（配置）

```python
class AuditRule(BaseModel):
    rule_id: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    conditions: AuditRuleConditions
```

### 3.4 AuditRuleConditions

```python
class AuditRuleConditions(BaseModel):
    # 以下条件是 OR 还是 AND 由 rule 类型决定
    min_denies_within_seconds: int | None = None
    min_denies_count: int | None = None
    consecutive_denies: int | None = None
    action_sequence: list[str] | None = None  # 动作序列匹配
    has_any_action: list[str] | None = None
    has_all_actions: list[str] | None = None
    authority_token_exhausted: bool = False
```

---

## 4. 审计规则配置

新增 `config/audit_rules.yaml`：

```yaml
enabled: true

rules:
  - id: rapid_denies
    description: "5 分钟内出现 3 次 deny"
    severity: medium
    conditions:
      min_denies_count: 3
      min_denies_within_seconds: 300

  - id: consecutive_denies
    description: "连续 3 次 deny，可能为暴力探测"
    severity: high
    conditions:
      consecutive_denies: 3

  - id: data_exfil_with_token
    description: "使用动态权限 token 完成了 data_read → email_external 链路"
    severity: high
    conditions:
      action_sequence:
        - authority_granted
        - execute
      has_any_action:
        - authority_used

  - id: authority_token_exhausted
    description: "Authority token 预算被耗尽"
    severity: low
    conditions:
      authority_token_exhausted: true
```

---

## 5. 核心组件

### 5.1 AuditAnalyzer（接口）

```python
class AuditAnalyzer(Protocol):
    async def analyze_session(self, session_id: str) -> AuditReport: ...
    async def analyze_task(self, task_id: str) -> AuditReport: ...
```

### 5.2 RuleBasedAuditAnalyzer

职责：

- 从 `AuditStore` 读取指定 session/task 的事件；
- 按顺序应用 `audit_rules.yaml` 中的规则；
- 命中时生成 `AuditAlert`；
- 汇总生成 `AuditReport`；
- 将告警与报告持久化到 `AlertStore`。

规则匹配实现：

1. **rapid_denies**：扫描事件，按滑动窗口统计 `deny` 动作；窗口内 count >= threshold 则命中。
2. **consecutive_denies**：扫描事件序列，连续 N 个 `deny` 命中。
3. **action_sequence**：按时间顺序匹配动作序列（子串匹配）。
4. **has_any_action / has_all_actions**：集合包含检查。
5. **authority_token_exhausted**：扫描 `authority_used` 事件，检查 `metadata.remaining_budget == 0`。

### 5.3 AlertStore

JSONL 持久化：

- `alert.jsonl`：追加 `AuditAlert`；
- `audit_report.jsonl`：追加 `AuditReport`；
- 启动时重放恢复索引；
- 损坏行 fail-closed。

提供：

- `save_alert(alert)` / `list_alerts(session_id)`
- `save_report(report)` / `get_report(report_id)`

---

## 6. 与现有架构集成

### 6.1 Runtime 集成

在 `run_task()` 的 `finally` 块中，任务结束后异步触发分析：

```python
if ended:
    audit.append(_audit_event(task, action="task_end", masker=runtime.masker))
    runtime.checkpoint.forget_task(task.task_id)
    runtime.task_store.complete(task.task_id)
    # v0.12.0：异步触发 R3 审计分析
    if runtime.audit_analyzer is not None:
        asyncio.create_task(
            runtime.audit_analyzer.analyze_task(task.task_id),
            name=f"audit-analyze-{task.task_id}",
        )
```

注意：使用 `asyncio.create_task` 不阻塞返回，异常由事件循环默认处理；分析器内部 catch 所有异常，避免影响主链路。

### 6.2 ConfigLoader 集成

- 新增 `audit_rules_path` 到 `AppConfig`（默认 `./data/audit_rules.jsonl` 用于 alert store，但规则文件是 `config/audit_rules.yaml`）。
- 加载 `config/audit_rules.yaml`。

### 6.3 CLI 集成

新增命令：

```bash
lc audit analyze --session-id <id>
lc audit analyze --task-id <id>
lc audit list-alerts --session-id <id>
```

---

## 7. 改动面清单

| 文件 | 改动 |
|------|------|
| `src/loop_controller/models.py` | 新增 `AuditAlert`, `AuditReport`, `AuditRule`, `AuditRuleConditions` |
| `src/loop_controller/audit_analyzer.py` | 新增 `AuditAnalyzer` Protocol、`RuleBasedAuditAnalyzer` |
| `src/loop_controller/infra/alert_store.py` | 新增 `AlertStore` / `JsonlAlertStore` |
| `src/loop_controller/infra/config_loader.py` | 加载 `audit_rules.yaml`，扩展 `AppConfig` |
| `src/loop_controller/runtime.py` | `Runtime` 增加 `audit_analyzer`，task_end 后触发分析 |
| `src/loop_controller/cli.py` | 新增 `lc audit analyze/list-alerts` 命令 |
| `config/audit_rules.yaml` | 新增审计规则配置 |
| `tests/test_audit_analyzer.py` | 新增测试 |
| `src/development_log.md` | 记录 v0.12.0 完成 |

---

## 8. 验收标准

- [ ] `pytest tests/` 全部通过；
- [ ] `ruff check src tests` 无告警；
- [ ] `config/audit_rules.yaml` 可正确加载；
- [ ] 任务产生 3 次 deny 后，能生成 `rapid_denies` 告警；
- [ ] 连续 3 次 deny 能生成 `consecutive_denies` 告警；
- [ ] task_end 后自动触发分析，不阻塞主链路返回；
- [ ] CLI `lc audit analyze --task-id <id>` 可手动生成报告。

---

## 9. 风险与注意事项

1. **异步任务异常**：分析器必须内部 catch 所有异常，避免未捕获异常拖垮事件循环。
2. **大数据量扫描**：当前实现全量扫描 session/task 事件；未来可按时间窗口分页。
3. **规则语义冲突**：多个规则可能同时命中同一事件，告警允许重复。
4. **CLI 身份校验**：手动分析命令只允许查询自己有权限的 session/task（MVP 可先不实现细粒度权限）。
