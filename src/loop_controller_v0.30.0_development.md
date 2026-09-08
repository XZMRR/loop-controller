# v0.30.0 持久化一致性与崩溃恢复加固

> 一句话目标：**统一 Loop Controller 的本地持久化语义，使关键状态在写失败、进程崩溃、断电、损坏尾行和同机多进程并发下保持可验证的一致性。**

- 状态：开发方案（待实现）
- 前置版本：v0.29.0 审批与状态恢复闭环
- 版本性质：基础设施可靠性与安全边界加固
- 核心范围：durable append、原子提交、损坏恢复、跨进程事务、启动探测、权限基线

---

## 1. 背景

v0.29.0 修复了人工审批跨进程可见性、预算预留清扫和决策恢复，但项目中的持久化实现仍主要建立在以下假设上：

- 单进程写入；
- 进程正常退出；
- 文件系统不会在单行中间中断；
- `flush()` 等价于数据已经可靠落盘；
- 启动时加载到内存的状态不会被其他进程修改。

这些假设不足以支撑治理控制平面。Loop Controller 的 Decision、Budget、Approval、Reservation、Authority、Audit 和 Evidence 都属于安全关键状态。如果系统向调用方返回成功，但状态只存在于内存或操作系统页缓存中，崩溃后就可能出现：

- 已消费 Decision 恢复为未消费；
- 已批准或拒绝的审批结果消失；
- 已占用或已提交的预算回退；
- 相同 `call_id` 被再次执行；
- Audit 与 Evidence 链尾不一致；
- 损坏尾行吞掉后续新记录；
- 多个 worker 同时通过同一份预算或防重放检查。

v0.30.0 不新增治理功能，而是统一“写入成功”的含义：

> 只有当记录已在跨进程事务内完整写入、完成必要的同步，并且内存索引已基于持久化结果更新后，操作才可向上层返回成功。

---

## 2. 当前问题清单

### P0-1：关键 JSONL writer 只有 `flush()`，没有 `fsync()`

当前 Task、Budget、Reservation、Authority、Alert、Decision、Conversation、Session、RiskState、Approval、Audit 和 Evidence JSONL 都只调用 `flush()`。

`flush()` 只将 Python 缓冲交给操作系统，不保证断电或宿主机崩溃后数据仍存在。接口可能返回成功，但记录在重启后消失。

代表位置：

- `src/loop_controller/budget.py`
- `src/loop_controller/infra/decision_store.py`
- `src/loop_controller/infra/approval_store.py`
- `src/loop_controller/infra/audit_store.py`
- `src/loop_controller/audit/evidence_backends.py`

### P0-2：部分 Store 先修改内存、后写入磁盘

受影响组件：

- `JsonlBudgetLedger`
- `JsonlDecisionStore`
- `JsonlApprovalStore`
- Session backend
- Conversation store

当 append、文件锁或磁盘写入失败时，调用方收到失败，但当前进程的内存状态已经改变。后续调用可能使用从未持久化的状态继续执行。

### P0-3：损坏尾行被忽略但未截断

Conversation、Session、RiskState 和 Approval 等实现允许忽略物理末尾的不完整 JSON 行，但继续写入前不会将文件截断至最后一个完整换行。

后续 append 会直接粘在残缺尾行之后，使新记录也成为非法 JSON。

### P0-4：关键状态缺少跨进程“刷新—判断—写入”事务

仅给单次 append 加锁不足以保证状态一致性。例如两个进程可能同时：

- 判断同一个 Decision 尚未消费；
- 判断同一个 `call_id` 尚未出现；
- 判断预算额度充足；
- 判断审批结果不存在；
- 基于相同 Audit/Evidence tail 分配相同序号。

文件锁必须覆盖整个事务，而不是只覆盖 `write()`。

### P1-1：损坏行策略不统一

当前不同 Store 对损坏中间行分别采取：

- fail-closed；
- WARNING 后跳过；
- 无条件过滤。

安全状态被跳过可能导致风险分数降低、审批结果消失或审计链被静默修补。

### P1-2：启动写探测覆盖不完整

配置加载阶段只探测部分路径，遗漏 Session、Task、Budget、Reservation、Authority、Alert、Evidence checkpoint 等生产持久化目标。

固定探测文件名在多进程启动时也会发生冲突。

### P1-3：原子替换不具备断电持久性

Evidence checkpoint 和 Revocation YAML 使用临时文件 + `os.replace()`，但缺少：

- 临时文件 `fsync()`；
- 父目录 `fsync()`；
- 唯一临时文件名；
- 跨进程读—改—写锁。

### P1-4：文件和目录权限依赖系统默认值

敏感文件普遍通过 `mkdir()` 和文本 `open("a")` 创建，没有统一的目录 `0700`、文件 `0600` 基线，也没有检查已有路径是否对其他用户可写。

---

## 3. 设计原则

### 3.1 成功语义

对于安全关键写入：

```text
获取跨进程锁
  → 刷新锁内最新状态
  → 校验业务前置条件
  → 生成完整记录
  → 写入
  → flush
  → fsync
  → 更新内存索引
  → 释放锁
  → 返回成功
```

任何步骤失败：

- 不更新内存状态；
- 不向调用方报告成功；
- 返回稳定错误码；
- Store 保持可重试或明确进入 degraded/write-blocked 状态。

### 3.2 安全优先级

遇到无法确定的持久化状态时：

1. 防止重复执行优先于可用性；
2. 防止超额预算优先于自动恢复额度；
3. 防止审批结果翻转优先于允许重新审批；
4. Audit/Evidence 完整性优先于继续执行面写入。

### 3.3 范围边界

本版本支持：

- 同机多个进程或 worker 共享本地数据目录；
- 本地受支持文件系统上的并发序列化写入；
- 进程崩溃和物理末尾半行恢复；
- 写失败后内存/磁盘一致性。

本版本不承诺：

- 多主机共享 NFS/SMB 的强一致；
- 分布式共识或 active-active；
- 远程数据库；
- 跨地域复制。

---

## 4. 总体架构

新增共享持久化模块：

```text
src/loop_controller/infra/durable_io.py

DurableJsonlFile
  ├─ lock_path
  ├─ transaction()
  ├─ refresh_locked()
  ├─ repair_tail_locked()
  ├─ append_locked(record)
  └─ fsync

DurableAtomicFile
  ├─ unique_temp_path
  ├─ write + fsync(temp)
  ├─ os.replace
  └─ fsync(parent directory)
```

所有 Store 保留现有 Protocol 和业务接口，内部统一使用上述原语。

```text
Controller / Checkpoint / Runtime
              │
              ▼
      Existing Store Protocols
              │
              ▼
   Jsonl*Store / Evidence Backend
              │
              ▼
      Durable I/O primitives
        ├─ sidecar lock
        ├─ tail repair
        ├─ complete UTF-8 write
        ├─ flush + fsync
        └─ atomic replace
```

---

## 5. 详细设计

## 5.1 共享 Durable JSONL 原语

新增：

- `src/loop_controller/infra/durable_io.py`

建议接口：

```python
class DurableIOError(RuntimeError):
    pass

class CorruptedJsonlError(DurableIOError):
    pass

class DurableJsonlFile:
    def __init__(
        self,
        path: Path,
        *,
        lock_timeout_seconds: float = 5.0,
        fsync_enabled: bool = True,
    ) -> None: ...

    @contextmanager
    def transaction(self) -> Iterator["DurableJsonlTransaction"]: ...

class DurableJsonlTransaction:
    def read_complete_lines(self) -> list[bytes]: ...
    def repair_incomplete_tail(self) -> bool: ...
    def append_json(self, payload: Mapping[str, object]) -> None: ...
```

实现要求：

1. 使用固定 sidecar 锁文件：`<target>.lock`；
2. 使用 `portalocker` 提供 Windows/POSIX 跨进程锁；
3. 锁覆盖读取、修复、校验和写入整个事务；
4. JSON 先序列化为完整 UTF-8 bytes，再执行写循环；
5. 每条记录必须以单个 `\n` 结束；
6. 写入后执行 `flush()` + `os.fsync()`；
7. 异常统一转为 `DurableIOError`，保留原异常为 cause；
8. sidecar 锁文件不承载业务数据；
9. 不依赖文本模式换行转换，避免 Windows CRLF 偏移问题。

### 5.1.1 fsync 配置

新增配置：

```yaml
persistence:
  fsync_enabled: true
  lock_timeout_seconds: 5
  repair_incomplete_tail: true
```

- 默认 `fsync_enabled=true`；
- 仅测试或明确接受数据丢失的开发环境可关闭；
- 关闭时 health 中暴露 `durability="unsafe"`；
- 安全关键生产配置检查可拒绝 `fsync_enabled=false`。

## 5.2 统一残尾恢复和损坏策略

### 5.2.1 物理末尾半行

只允许自动修复以下情况：

- 损坏行位于物理 EOF；
- 文件末尾没有完整换行；
- 该尾部无法解析为合法记录。

恢复流程：

```text
获取独占锁
  → 定位最后一个完整换行
  → 保存告警 metadata（原文件、截断字节数、tail hash）
  → truncate 到完整换行后
  → flush + fsync
  → 继续重放
```

不得仅 WARNING 后继续向物理 EOF append。

### 5.2.2 中间损坏行

| Store 类型 | 策略 |
|---|---|
| Decision / Budget / Reservation / Authority / Approval | fail-closed，拒绝启动执行面或写入 |
| Audit / Evidence | write-blocked + critical alert，禁止静默跳过 |
| RiskState | fail-closed，避免风险分数低估 |
| Session / Task / Conversation | degraded；允许只读诊断，不允许覆盖原文件继续写 |
| Alert | fail-closed，保留原始损坏证据 |

所有损坏必须写入 AlertStore；若 AlertStore 本身不可用，则至少输出结构化 ERROR 日志。

## 5.3 内存状态提交顺序

以下 Store 改为“先持久化，后更新内存”：

### `JsonlBudgetLedger`

- `set_budget()`
- `check_and_reserve()`
- `commit()`
- `refund()`

锁内读取最新账本并计算候选值，append + fsync 成功后才提交 `_max` / `_used` / `_reserved`。

### `JsonlDecisionStore`

- `record_proposal()`
- `use_decision()`
- `record_finalized()`

防重放检查、`max_uses` 检查与记录写入必须在同一个跨进程事务内。

### `JsonlApprovalStore`

- `submit_request()`
- `record_response()`

`record_response()` 必须在同一个 sidecar lock 内执行：

```text
刷新最新 response
  → 检查是否已有结果
  → 相同记录：幂等返回
  → 不同记录：ApprovalStoreError
  → append + fsync
  → 更新内存
```

这也用于彻底修复 v0.29.0 中“两个审批进程同时写入 approve/deny”的剩余竞态。

### Session / Conversation

- 生成候选内存结构；
- append + fsync 成功后再替换当前内存状态；
- Conversation 的容量淘汰只能在落盘成功后提交。

## 5.4 跨进程关键事务

### 5.4.1 Decision

锁内事务必须覆盖：

- 增量重放；
- `call_id` 防重复检查；
- Decision 存在/过期检查；
- `max_uses` 检查；
- use/finalized 记录写入；
- 内存索引更新。

### 5.4.2 Budget

`check_and_reserve()` 的检查和 reserve 必须在一个事务中：

```text
refresh latest ledger
  → used + reserved + cost <= max
  → append reserve + fsync
  → update memory
```

两个 worker 不得同时基于旧额度通过检查。

### 5.4.3 Reservation / Authority

- Reservation 创建与状态迁移必须锁内读取当前终态；
- terminal state 不得被其他进程覆盖；
- Authority token 的 issue/consume/revoke 使用同一事务协议；
- 同一个 token 只能有一次成功 consume。

### 5.4.4 Approval

- 首条 response 胜出；
- 后续相同 response 幂等；
- 后续不同 response 返回冲突；
- 冲突记录不得落盘；
- 所有进程在下一次 refresh 后观察到相同结果。

### 5.4.5 Audit 与 Evidence

Audit/Evidence 仍保留顺序 JSONL，不迁移至 SQLite。

跨进程锁覆盖：

- 读取最新链尾；
- seq 分配；
- `prev_hash` 绑定；
- record append + fsync；
- checkpoint 更新；
- 必要时远端 anchor 发布。

若链尾在锁内刷新后与进程内缓存不一致，必须以磁盘已验证链尾为准重新构造下一条记录。

## 5.5 Durable Atomic Replace

新增：

```python
def durable_atomic_replace(
    path: Path,
    content: bytes,
    *,
    mode: int = 0o600,
    lock_timeout_seconds: float = 5.0,
) -> None: ...
```

流程：

1. 获取 `<path>.lock`；
2. 在目标同目录创建唯一临时文件（PID + UUID）；
3. 以安全权限创建；
4. 写完整内容；
5. `flush + fsync(temp)`；
6. `os.replace(temp, target)`；
7. POSIX 上 `fsync(parent directory)`；
8. 释放锁；
9. 异常时只删除本次临时文件。

迁移：

- Evidence checkpoint；
- Revocation YAML；
- 本地 Audit/Evidence checkpoint；
- 其他使用 temp + replace 的生产状态文件。

Windows 不支持目录 fd fsync 时，应明确记录平台差异，不得伪装为已经执行。

## 5.6 启动持久化探测

新增统一 `PersistenceProbe`，在 Runtime 开放执行面前检查所有生产目标：

- audit path；
- evidence path/checkpoint；
- decision path；
- approval path；
- budget ledger；
- reservation store；
- authority log；
- alert store；
- task store；
- session path；
- conversation path；
- risk state；
- revocation path。

每个目标检查：

1. 父目录可创建唯一探测文件；
2. 已有目标可读；
3. 已有目标可取得 sidecar lock；
4. append 型目标可安全打开；
5. replace 型目标可创建同目录临时文件并 replace；
6. JSONL 无中间损坏；
7. 物理残尾可按配置修复；
8. 权限满足基线。

探测文件名必须包含 PID + UUID，避免并发启动互删。

### 启动状态

| 状态 | 含义 | 行为 |
|---|---|---|
| `healthy` | 全部持久化目标通过 | 开放执行面 |
| `tail_repaired` | 仅修复物理残尾 | 开放执行面并发告警 |
| `degraded` | 非关键上下文 Store 不可写/损坏 | 只读诊断，默认不开放执行面 |
| `write_blocked` | 治理、预算、审批或审计状态不可确定 | 禁止工具执行 |
| `lock_unavailable` | 关键 Store 锁超时 | 不启动第二写实例 |

## 5.7 权限基线

### POSIX

- 新建数据目录默认 `0700`；
- 新建状态文件默认 `0600`；
- sidecar lock 默认 `0600`；
- 已存在安全关键文件若 group/other writable，则拒绝启动执行面；
- 对用户显式配置的已有目录只检查，不无条件 chmod。

### Windows

- 不承诺通过 POSIX mode 完成 ACL 加固；
- 启动时记录当前账户和目标目录；
- 文档要求使用专属服务账户和专属数据目录 ACL；
- 若实现 Windows ACL 检查，应只检查，不自动重写管理员配置的 ACL。

## 5.8 Health 与指标

Health 新增：

```json
{
  "persistence": {
    "status": "healthy",
    "fsync_enabled": true,
    "tail_repairs": 0,
    "lock_failures": 0,
    "corrupted_stores": [],
    "unsafe_permissions": []
  }
}
```

Prometheus 指标：

- `loop_controller_persistence_append_total{store,status}`
- `loop_controller_persistence_fsync_seconds{store}`
- `loop_controller_persistence_lock_wait_seconds{store}`
- `loop_controller_persistence_lock_failures_total{store}`
- `loop_controller_persistence_tail_repairs_total{store}`
- `loop_controller_persistence_corruption_total{store,position}`
- `loop_controller_persistence_write_blocked{store}`

禁止指标标签携带 task_id、agent_id、decision_id 等高基数字段。

---

## 6. Store 迁移清单

| 组件 | durable append | 锁内事务 | 先落盘后内存 | 残尾修复 | 中间损坏 |
|---|---:|---:|---:|---:|---|
| Task | 必须 | 追加锁 | 不适用 | 必须 | fail-closed |
| Budget | 必须 | 必须 | 必须 | 必须 | fail-closed |
| Reservation | 必须 | 必须 | 已基本满足 | 必须 | fail-closed |
| Authority | 必须 | 必须 | 已基本满足 | 必须 | fail-closed |
| Alert | 必须 | 追加锁 | 已基本满足 | 必须 | fail-closed |
| Decision | 必须 | 必须 | 必须 | 必须 | fail-closed |
| Approval | 必须 | 必须 | 必须 | 必须 | fail-closed |
| Conversation | 必须 | 追加锁 | 必须 | 必须 | degraded/write-blocked |
| Session | 必须 | 追加锁 | 必须 | 必须 | degraded/write-blocked |
| RiskState | 必须 | 必须 | 校验后提交 | 必须 | fail-closed |
| Audit | 必须 | 必须 | 链尾成功后提交 | 必须 | write-blocked |
| Evidence | 必须 | 必须 | 链尾成功后提交 | 必须 | write-blocked |
| Revocation YAML | atomic replace | 必须 | 已基本满足 | 不适用 | fail-closed |

---

## 7. 配置变更

新增配置块：

```yaml
persistence:
  fsync_enabled: true
  lock_timeout_seconds: 5
  repair_incomplete_tail: true
  enforce_permissions: true
  fail_on_unsafe_permissions: true
```

校验规则：

- `lock_timeout_seconds > 0`；
- 生产 profile 下 `fsync_enabled` 必须为 true；
- 生产 profile 下 `repair_incomplete_tail` 建议为 true；
- 不允许每个 Store 自行配置互相冲突的锁协议；
- 所有共享同一目录的进程必须使用相同 persistence 配置。

---

## 8. 测试计划

## 8.1 Durable append 单元测试

- 完整记录以 UTF-8 bytes 写入并以 `\n` 结束；
- append 后调用 `fsync`；
- `fsync` 失败时抛 `DurableIOError`；
- 文件锁超时返回稳定错误；
- Windows CRLF 不影响偏移；
- 大记录执行完整写循环，不产生部分记录。

## 8.2 崩溃注入测试

为以下阶段注入异常：

1. 序列化前；
2. 获取锁后、append 前；
3. 部分 write 后；
4. flush 后、fsync 前；
5. fsync 后、更新内存前；
6. atomic replace 前后。

断言：

- 失败前内存不提交；
- 重启重放结果与已确认返回结果一致；
- 最多出现可识别残尾，不出现静默中间损坏；
- Decision/Approval/Authority 不出现双重成功。

## 8.3 多进程测试

使用 `multiprocessing` 启动两个独立进程：

- 同时消费 `max_uses=1` Decision：仅一个成功；
- 同时使用相同 call_id：仅一个 proposal 成功；
- 同时 reserve 剩余一次额度：仅一个成功；
- 同时 approve/deny：仅第一个结果成功，第二个明确 conflict；
- 同时 consume authority token：仅一个成功；
- 同时 append Audit/Evidence：seq 单调且链不分叉。

不得用两个同进程 Store 实例替代真正的多进程测试。

## 8.4 损坏恢复测试

- 无换行的物理残尾被锁内截断；
- 截断后新 append 可正常重放；
- 中间损坏 Decision/Budget/Approval/RiskState → fail-closed；
- 中间损坏 Audit/Evidence → write-blocked + critical alert；
- Session/Conversation 损坏 → degraded，原文件不被静默覆盖；
- tail repair alert 包含 store、截断字节数和 tail hash，不包含敏感原文。

## 8.5 Atomic replace 测试

- 临时文件与目标同目录；
- 临时文件名唯一；
- temp fsync 发生在 replace 前；
- POSIX directory fsync 发生在 replace 后；
- 两进程并发更新 Revocation 时无丢更新；
- 异常只清理本次临时文件。

## 8.6 启动探测测试

- 所有持久化目标均被探测；
- 固定目录不可写 → write-blocked；
- 文件可读但不可锁 → lock_unavailable；
- 多进程同时 probe 不互相删除文件；
- unsafe permissions 按配置拒绝启动或告警；
- health 暴露 persistence 状态。

---

## 9. 实施顺序

### 阶段 A：共享原语

1. 实现 `DurableJsonlFile`；
2. 实现 `durable_atomic_replace()`；
3. 完成独立单元测试与故障注入测试。

### 阶段 B：关键治理 Store

依次迁移：

1. Approval；
2. Decision；
3. Budget；
4. Reservation；
5. Authority。

每迁移一个 Store，就补一个真正的多进程竞争测试。

### 阶段 C：审计证据链

1. Audit；
2. Evidence backend；
3. Evidence checkpoint；
4. Revocation atomic replace。

### 阶段 D：其余 Store 与启动门控

1. RiskState；
2. Session / Conversation / Task / Alert；
3. PersistenceProbe；
4. Health、指标、权限检查。

---

## 10. 验收标准

- `python -m pytest -q` 全部通过；
- `python -m ruff check src tests` 通过；
- `python -m mypy src/loop_controller` 通过；
- 所有生产 JSONL writer 使用统一 durable append，不再各自实现裸 `open("a") + flush()`；
- 安全关键写入默认执行 `fsync()`；
- Budget、Decision、Approval、Session、Conversation 不再先改内存后落盘；
- Approval、Decision、Budget、Reservation、Authority 的业务判断与写入位于同一跨进程事务；
- Audit/Evidence 多进程并发不产生重复 seq 或链分叉；
- 物理残尾可自动截断并告警，中间损坏不会被静默跳过；
- Evidence checkpoint 和 Revocation 使用 durable atomic replace；
- Runtime 启动前探测全部持久化目标，关键失败时执行面 write-blocked；
- 新建 POSIX 数据目录和文件满足 `0700` / `0600` 基线；
- 多进程竞争与故障注入测试覆盖核心状态。

---

## 11. 明确不做

- PostgreSQL、Redis、etcd 等远程状态后端；
- 多主机共享数据目录；
- active-active 与分布式共识；
- JSONL 日志轮转、压缩、归档；
- Store Protocol 大规模重构；
- Anchor outbox 与批量发布；
- KMS/HSM；
- Agent Egress Gateway；
- UI 控制台。

---

## 12. v0.30.0 完成后的能力边界

可以准确描述为：

> Loop Controller 的本地关键状态在同机多进程竞争、单进程崩溃、写入失败和物理残尾场景下具有统一、可验证的持久化语义；操作只有在持久化成功后才对内存和调用方可见。

仍不能描述为：

- 支持跨主机共享存储的强一致；
- 支持无共享存储的多副本高可用；
- 具备分布式事务或共识；
- 能在磁盘硬件完全损坏后自动恢复数据；
- 能替代外部备份、WORM 归档或远端证据仓库。
