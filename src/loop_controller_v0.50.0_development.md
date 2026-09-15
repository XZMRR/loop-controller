# v0.50.0：OPA Bundle 策略交付、加载确认、Shadow 模拟与变更审计

**Status**: 已完成
**Target Version**: `0.50.0`
**验证记录**: Python 全量 983 passed / 4 skipped（含 `test_policy_delivery.py` 43、`test_policy_validation_shadow.py` 47、`test_policy_lifecycle_server.py` 36）；Go `go build ./...` 与 `go test ./internal/...` 通过；发布前审查 9 项问题（3 高 6 中，两个独立验证代理复核确认）已全部修复并回归通过；版本契约已统一（pyproject/uv.lock 0.50.0、contract/openapi 重命名 a2a_v0.50.0、协议常量 0.50.0）。
**Goal**: 在当前 Starlette、SQLite、文件系统和 OPA HTTP sidecar 边界内，建立可验证、可回滚、可审计的 Rego 策略交付闭环；发布成功不等于生效，只有规定的 OPA 实例全部确认加载目标 revision 后才标记 `loaded`。

---

## 1. 版本定位与当前真实边界

v0.49.0 已将 Python 工具审批主路径迁入 SQLite，并完成管理入口认证与可靠通知。本版本只解决 OPA 策略从候选快照到生产实例确认加载之间的控制面问题，不改变现有决策执行语义。

必须以以下代码事实为实施起点：

1. `src/loop_controller/infra/hot_reload.py` 的 Python `HotReloader` 只重载 HTTP 工具、Harness、Secret 和吊销配置，**不监控、不解析、不发布 Rego**；本版本也不把 Rego 塞入该轮询器。
2. `src/loop_controller/infra/policy_store.py` 的 `FilePolicyStore` 只是历史 Decision `policy_version` 的辅助来源：它仅用 `glob("*.rego")` 扫描策略根目录顶层，**未递归覆盖 `policies/interaction/default.rego`**，并且只返回 SHA-256 前 12 位。该值既不代表完整策略树，也不代表 OPA Bundle 内容，**不得作为 bundle revision、ETag 或发布 CAS 值**。
3. 当前策略包含至少两个决策 package：
   - `loop_controller.tool_permission`；
   - `loop_controller.interaction.delegation`。
4. `OPAPolicyEngine` 与启动校验通过 `httpx` 调用 OPA HTTP API，异常时 fail-closed。Loop Controller（下称 LC）不是 OPA 进程管理器。
5. **生产 OPA 由部署层以 sidecar/同主机服务方式管理，LC 不 fork、拉起、停止或重启 OPA**。测试可以由 fixture 启动临时 OPA 子进程，但不能把该行为带入生产代码。
6. 当前服务框架是 Starlette，状态事实源是标准库 `sqlite3`（WAL），网络客户端是 `httpx`；本版本用标准库 `tarfile`、`gzip`、`hashlib` 和文件系统生成、保存 bundle，**不引入外部数据库、消息队列或新的打包服务**。
7. 根目录 `README.md` 当前示例写有 `.venv\Scripts\lc opa-start`，但 `src/loop_controller/cli.py` 没有 `opa-start` 子命令。实施本版本时必须把该说明修正为部署层直接执行 OPA（例如 `opa run --server ...`）或容器 sidecar 配置；LC 不补造 `opa-start` 命令。

## 2. 目标与非目标

### 2.1 本版本目标

- 从管理员显式提交的源文件创建**不可变 candidate snapshot**；
- 实现 `draft → validated → published → loaded/failed/superseded` 生命周期；
- 以完整 64 位小写十六进制 SHA-256 作为内容寻址 revision；
- 生成字节级确定性的 OPA `tar.gz` bundle，并内含规范 `.manifest`；
- 发布前强制执行 `opa check --strict`、`opa test` 和双 package 空 input default-deny 契约；
- 提供受 admin principal 保护的候选、校验、shadow、发布、回滚和查询 API；
- 提供使用独立 machine credential 的 Bundle `GET/HEAD`，支持强 ETag 与 `304 Not Modified`；
- 提供使用独立 status credential 的 OPA status receiver；
- 以 required instance 集合和 TTL 判断 `loaded`，暴露 expected/active/stale/error；
- shadow 只消费管理员显式上传并已脱敏的样本集，候选只在隔离 evaluator 上执行；
- 审计策略变更的 actor、base/target revision、hash、校验/shadow/发布/加载结果。

### 2.2 明确不做

- 不做自动灰度、自动发布、自动回滚；
- 不接 OPA decision log plugin，也不从线上 Decision/AuditStore 自动抽取样本；
- 不做多租户；
- 不实现细粒度 RBAC、OIDC/JWKS；v0.50 仅使用粗粒度 admin key，RBAC 留到 v0.52；
- 不让 shadow 请求进入线上 OPA，不修改线上 OPA 的数据、bundle 或决策流量；
- 不由 LC 管理 OPA 进程或 sidecar 生命周期；
- 不引入 PostgreSQL、Redis、Kafka、外部 MQ、对象存储或额外 Python 打包依赖；
- 不修改现有 `FilePolicyStore.current_version()` 的历史 Decision 语义来冒充 bundle revision。

## 3. 核心术语与状态机

### 3.1 对象

- **candidate**：一次不可变策略源快照及其元数据。创建完成后源文件不可修改；修正策略必须创建新 candidate。
- **revision**：规范化 bundle 未压缩逻辑内容的完整 SHA-256 标识，格式固定为 `^[0-9a-f]{64}$`。revision 与唯一 artifact 一一对应。
- **artifact**：`<revision>.tar.gz`；其内容包含全部递归 Rego/Data 文件和 `.manifest`。
- **current pointer**：SQLite 中当前期望生产 OPA 加载的 revision，是唯一发布事实源；磁盘符号链接或文本指针不得作为权威源。
- **required instance**：配置中必须确认加载后，revision 才能成为 `loaded` 的 OPA 实例。
- **fresh report**：状态接收时间 `received_at` 距当前时间不超过 `status_ttl_seconds` 的有效上报。

### 3.2 生命周期

```text
create
  └─> draft
        ├─ validate fail ─> failed
        └─ validate pass ─> validated
                              ├─ shadow（可重复，不改变状态）
                              └─ publish(CAS) ─> published
                                                   ├─ 全部 required 新鲜确认 ─> loaded
                                                   ├─ 明确加载错误 ─────────> failed
                                                   └─ 后续 revision 发布 ───> superseded
```

约束：

- `draft` 只能转为 `validated` 或 `failed`；失败 candidate 不可原地编辑或重新校验，须创建新 candidate。
- `validated` 只有显式发布才进入 `published`。
- `published` 不表示 OPA 已生效；未收齐 required 实例确认时保持 `published`。
- `loaded` 只由状态聚合器在全部 required 实例对目标 revision 给出新鲜、无错误的成功上报后设置。
- 任一 required 实例对当前目标 revision 上报明确错误时，candidate 转为 `failed`，但 current pointer 保留目标 revision 供诊断，**不会自动回滚**；管理员显式回滚后该失败 revision 才被 `superseded`。
- 发布新 revision 时，先前处于 `published` 或 `loaded` 的 current candidate 转为 `superseded`。终态记录不删除、不改写 artifact。
- 重复 status report、GET 和 HEAD 不改变不可变内容；同一 transition 的审计写入必须幂等。

## 4. 文件布局与确定性 Bundle

建议使用独立受限目录，不能让 Agent 工作目录可写：

```text
<data_dir>/policy_delivery/
  candidates/<candidate_id>/source/...
  artifacts/<revision>.tar.gz
  samples/<sample_set_id>.json
  tmp/...
```

### 4.1 Candidate snapshot

创建 candidate 时：

1. 接口接受受限数量的相对 POSIX 路径与 UTF-8/JSON 内容，不接受服务器本地任意路径；
2. 拒绝绝对路径、空路径、`..`、反斜杠逃逸、NUL、符号链接、硬链接、设备文件和大小/文件数超限；
3. 允许递归路径，至少支持 `.rego`、`.json`；不打包 `.git`、缓存、测试输出、Secret 或任意隐藏文件，`.manifest` 由服务生成，客户端不得提交；
4. 路径按 UTF-8 字节序排序，写入 `tmp/<uuid>`，逐文件 fsync 后原子 rename 到 `candidates/<candidate_id>/source`；
5. snapshot 建成后只读，SQLite 保存源文件清单及每文件完整 SHA-256；任何后续阶段只从 snapshot 读取。

### 4.2 `.manifest` 与 revision

生成 bundle 前构造规范 `.manifest`：

```json
{
  "revision": "<64-char-sha256>",
  "roots": ["loop_controller"]
}
```

revision 不能直接对包含自身 revision 的最终归档做哈希，否则形成自引用。计算规则固定为：

1. 对 snapshot 中所有待打包文件生成规范条目：`path + NUL + byte_length + NUL + sha256(file_bytes) + LF`；
2. 附加固定的 manifest 语义条目：`roots=["loop_controller"]`、bundle format version；此时不含 `revision` 字段；
3. 对上述 UTF-8 字节串计算 SHA-256，得到 revision；
4. 将 revision 写入 `.manifest`，再生成归档；
5. 重新解包校验 `.manifest.revision`、文件清单和逐文件 hash 与 SQLite 一致。

因此 revision 是完整逻辑内容标识，而不是压缩文件 hash。另存 `artifact_sha256`（对最终 `.tar.gz` 原始字节计算）用于传输完整性与审计。两者都必须是完整 64 位 hash，并明确字段名，禁止混用。

### 4.3 字节级确定性 `tar.gz`

仅使用 `tarfile`、`gzip` 和标准库 I/O：

- 成员以 POSIX 相对路径按 UTF-8 字节序排列；`.manifest` 的固定排序位置写入实现规范；
- JSON 使用 UTF-8、排序 key、紧凑分隔符、结尾单个 `\n`；
- tar 使用固定格式，成员 `mtime=0`、`uid=gid=0`、`uname=gname=""`，普通文件 mode 固定为 `0644`；
- 禁止目录外引用及链接成员；
- gzip `mtime=0`，且 header 不携带变化的原文件名；压缩级别固定；
- 同一 snapshot 在不同进程重复构建必须得到相同 revision、相同 `artifact_sha256` 和逐字节相同归档。

### 4.4 原子 artifact 发布

- 在同一文件系统的 `tmp/` 创建归档，写完后 flush + fsync；
- 校验归档，再以原子 rename/link-if-absent 安装为 `artifacts/<revision>.tar.gz`，并 fsync 父目录；
- 若目标已存在，仅当现有文件 `artifact_sha256` 完全一致时幂等成功，否则 fail-closed 并审计 hash conflict；
- Bundle API 只服务已完整安装且 SQLite 标记 `artifact_ready=1` 的 artifact，绝不暴露临时文件或半包。

## 5. 发布前验证契约

验证在不可变 snapshot 的临时只读副本上执行。`opa` 二进制由部署配置提供；缺失、超时、非零退出、输出不可解析均使 candidate 进入 `failed`。生产 LC 不启动 OPA server，但允许以受限 subprocess 执行一次性 OPA CLI 校验。

顺序固定：

1. `opa check --strict <snapshot-root>`；
2. `opa test <snapshot-root>`（设置超时，保存退出码和截断/脱敏后的 stdout/stderr 摘要）；
3. 构建确定性 bundle；
4. 对 bundle 中两个 package 分别执行空 input 查询：
   - `data.loop_controller.tool_permission.decision`；
   - `data.loop_controller.interaction.delegation.decision`；
5. 两者必须返回结构合法的 default-deny：`verdict == "deny"`，且不得缺少 decision、返回 allow/modify/require_approval 或产生多值/错误；
6. 验证 `.manifest`、revision、artifact hash 和可由 OPA 加载性。

空 input 契约用于防止默认开放和 package 丢失，不替代各 Rego 单元测试。`opa test` 零测试是否允许必须配置并默认拒绝；正式 candidate 至少包含测试，防止“语法正确但无行为证明”。校验记录需保存 OPA 版本、命令阶段、开始/结束时间、退出码、结果摘要；不得保存环境变量、credential 或完整敏感样本。

## 6. SQLite Schema

在现有 `StateDatabase` schema 中增量增加以下表；继续使用 `sqlite3`、WAL、`busy_timeout` 和显式 `BEGIN IMMEDIATE`。时间统一为 UTC ISO 8601。

```sql
CREATE TABLE policy_candidates (
    candidate_id TEXT PRIMARY KEY,
    revision TEXT UNIQUE,
    state TEXT NOT NULL CHECK (state IN
      ('draft','validated','published','loaded','failed','superseded')),
    base_revision TEXT,
    source_manifest_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    artifact_path TEXT,
    artifact_sha256 TEXT,
    artifact_size INTEGER,
    artifact_ready INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    published_at TEXT,
    loaded_at TEXT,
    superseded_at TEXT,
    failure_stage TEXT,
    failure_code TEXT,
    failure_summary TEXT
);

CREATE TABLE policy_validations (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    opa_version TEXT,
    check_ok INTEGER NOT NULL,
    test_ok INTEGER NOT NULL,
    tool_default_deny_ok INTEGER NOT NULL,
    interaction_default_deny_ok INTEGER NOT NULL,
    result_sha256 TEXT NOT NULL,
    result_summary_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES policy_candidates(candidate_id)
);

CREATE TABLE policy_current (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision TEXT,
    candidate_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT,
    updated_at TEXT,
    FOREIGN KEY(candidate_id) REFERENCES policy_candidates(candidate_id)
);

CREATE TABLE opa_instance_status (
    instance_id TEXT PRIMARY KEY,
    revision TEXT,
    bundle_name TEXT NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    opa_reported_at TEXT,
    received_at TEXT NOT NULL,
    report_sha256 TEXT NOT NULL
);

CREATE TABLE policy_sample_sets (
    sample_set_id TEXT PRIMARY KEY,
    content_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE policy_shadow_runs (
    run_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    sample_set_id TEXT NOT NULL,
    baseline_revision TEXT NOT NULL,
    target_revision TEXT NOT NULL,
    evaluator_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running','passed','failed')),
    total INTEGER NOT NULL DEFAULT 0,
    same_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    result_path TEXT,
    result_sha256 TEXT,
    started_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    failure_code TEXT,
    FOREIGN KEY(candidate_id) REFERENCES policy_candidates(candidate_id),
    FOREIGN KEY(sample_set_id) REFERENCES policy_sample_sets(sample_set_id)
);

CREATE TABLE policy_change_audit (
    audit_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    candidate_id TEXT,
    base_revision TEXT,
    target_revision TEXT,
    source_sha256 TEXT,
    artifact_sha256 TEXT,
    result TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_policy_candidates_state ON policy_candidates(state);
CREATE INDEX idx_policy_status_revision ON opa_instance_status(revision);
CREATE INDEX idx_policy_audit_created ON policy_change_audit(created_at);
```

说明：

- `source_sha256` 是规范 source manifest 的完整 hash；`revision` 是 bundle 逻辑 revision；`artifact_sha256` 是最终压缩字节 hash。
- `failure_summary`、`error_summary`、`detail_json` 必须长度受限且脱敏，不能写 token、环境变量、未脱敏 input 或完整 stderr。
- required instance 集合来自受保护静态配置，例如 `policy_delivery.required_instances`，不由 status 请求动态创建。表中的未知 instance 上报应拒绝，防止攻击者扩大/缩小 quorum。
- 样本正文和 shadow 逐条结果放受限文件系统，SQLite 只存寻址 hash、摘要和计数，符合现有文件系统 + SQLite 边界。

## 7. API 契约与认证平面

### 7.1 管理 API：admin principal

以下接口挂到现有 Starlette 服务，统一使用现有管理认证风格，但本版本必须要求已配置 admin key；未配置时管理写接口不可用，而不是开放。认证支持 `Authorization: Bearer` 或约定 header，使用 `hmac.compare_digest`，日志只记录不可逆 principal/key id，不记录 key。

```text
POST /v1/admin/policy/candidates
GET  /v1/admin/policy/candidates
GET  /v1/admin/policy/candidates/{candidate_id}
POST /v1/admin/policy/candidates/{candidate_id}/validate
POST /v1/admin/policy/sample-sets
POST /v1/admin/policy/candidates/{candidate_id}/shadow
GET  /v1/admin/policy/shadow-runs/{run_id}
POST /v1/admin/policy/candidates/{candidate_id}/publish
POST /v1/admin/policy/rollback
GET  /v1/admin/policy/status
GET  /v1/admin/policy/audit
```

- 创建 candidate 请求包含 `base_revision`、文件列表和可选说明；返回 `201`、`candidate_id`、`state=draft`、source hash。
- validate 为显式动作；重复请求对同一不可变 candidate 返回现有结果。
- publish 请求必须再次携带 `base_revision`，只允许 `validated`；成功返回 `202 published`，不能提前返回 `loaded`。
- rollback 请求包含目标历史 `revision` 和当前 `base_revision`。回滚不是移动指针到旧 candidate，也不篡改历史状态；服务从历史不可变 artifact/source 创建一个新的 candidate/发布事件，并将其作为新一代 current 发布，因此 actor、原因、旧 base 和目标历史 revision 均可审计。
- 典型响应：`400` 输入无效，`401` 未认证，`403` principal 无管理权限（v0.50 粗粒度），`404` 对象不存在，`409` base CAS/状态冲突，`413` 超限，`422` 校验失败，`503` SQLite/文件系统/隔离 evaluator 不可用。

**粗粒度限制**：v0.50 的 admin key 一旦通过，可调用全部策略管理动作，不能区分 creator/validator/publisher/auditor，不能表达双人复核或职责分离。该限制必须进入 README/KNOWN_LIMITATIONS；细粒度 RBAC 与企业身份接入留到 v0.52。

### 7.2 Bundle API：独立 machine credential

```text
GET  /v1/opa/bundles/current
HEAD /v1/opa/bundles/current
GET  /v1/opa/bundles/{revision}
HEAD /v1/opa/bundles/{revision}
```

- 只接受独立 `bundle_reader` machine credential；不得复用 admin key、Agent credential 或 status credential。
- `current` 在单次请求开始时从 SQLite 读取 current revision，并固定到该 immutable artifact，避免流式响应期间指针变化。
- 成功响应：`Content-Type: application/gzip`、`Content-Length`、`ETag: "<artifact_sha256>"`、`X-OPA-Bundle-Revision: <revision>`、`Cache-Control: private, no-cache`。
- `If-None-Match` 精确匹配强 ETag 时返回 `304`，无 body；HEAD 与 GET 的状态码和 headers 一致但不发送 body。
- ETag 使用 artifact hash，不使用 `FilePolicyStore` 的 12 位版本，也不使用可猜短值。
- 不存在 current 返回 `404`；artifact/SQLite 不一致返回 `503` 并告警，绝不提供半包或回退到目录即时打包。

### 7.3 OPA status receiver：独立 status credential

```text
POST /v1/opa/status
```

- 只接受独立 `opa_status_writer` credential；不得复用 bundle reader 或 admin key。
- 请求体限制大小并严格解析 OPA status payload，只提取配置 bundle 名、instance id、active revision、加载状态、错误码/脱敏摘要和 OPA 时间。
- `instance_id` 必须在静态 allowlist；bundle 名必须匹配本服务配置；server 以 `received_at` 判断 freshness，不信任客户端时间作为 TTL 基准。
- 相同 canonical payload 可幂等覆盖；旧 report 不得把较新的 `received_at`/状态倒退。认证失败返回 `401`，未知 instance/bundle 返回 `403`，格式错误返回 `400`，合法接收返回 `202`。
- status receiver 只记录事实并触发聚合，不接受“请标记 loaded”命令。

建议三个 credential 使用不同环境变量/Secret 文件、不同 key id 和轮换窗口；应用启动时检测值相同则 fail-closed。生产 TLS、网络策略与 sidecar egress 限制由部署层负责。

## 8. 发布事务、CAS 与加载确认

### 8.1 base_revision CAS

所有 candidate 在创建时记录 `base_revision`（可以为首次发布的 `null`）。发布必须在一个 `BEGIN IMMEDIATE` 中：

```text
读取 policy_current
校验 current.revision IS base_revision
校验 candidate.state == validated
校验 artifact_ready == 1 且磁盘 hash 匹配
将旧 current candidate（若为 published/loaded）置 superseded
更新 policy_current(revision, candidate_id, generation + 1)
将新 candidate 置 published，写 published_at
写 policy_change_audit(event=publish, actor, base, target, hashes, result=success)
COMMIT
```

- CAS 不匹配返回 `409`，candidate 保持 `validated`，不能覆盖他人已发布变更。
- artifact 必须在事务前原子安装；事务失败可留下无引用 immutable artifact，允许后台/人工清理，但不得出现 current 指向未完成 artifact。
- SQLite commit 后即使响应丢失，按 candidate/base 查询可幂等确认结果；不得通过重试生成不同 revision。
- 审计成功事件与 pointer 更新同事务。事务前的验证失败、artifact 冲突等失败事件另以独立短事务记录。

### 8.2 loaded 判定与 TTL

设静态集合 `R = required_instances`，当前目标为 `E = policy_current.revision`。仅当对每个 `i ∈ R` 同时满足以下条件，才可将目标 candidate 从 `published` 原子转为 `loaded`：

1. 存在 instance `i` 的状态记录；
2. `now - received_at <= status_ttl_seconds`；
3. 上报 bundle name 正确；
4. `revision == E`；
5. 上报状态明确表示加载成功且无 error。

缺任一报告、revision 仍旧或报告过期，只能显示 `pending/stale`，不能 `loaded`。非 required 的观察实例不参与 quorum。`required_instances` 不能为空；生产配置为空时 readiness 失败。

TTL 到期不会回写篡改历史 `loaded` 状态，但实时健康视图必须变为 stale/readiness false；收到新鲜目标 revision 后恢复。若 required 实例明确报告目标 revision 加载错误，则记录实例 `error`，当前 candidate 标记 `failed` 并审计；旧 OPA 是否仍服务旧策略由 OPA 自身负责，LC 不声称目标已生效、不自动改 pointer。

加载聚合在 status 写入后的同一临界区或紧随其后的短事务执行，并以 `WHERE state='published' AND revision=?` CAS，保证重复/并发上报只产生一次 `loaded` 审计事件。

### 8.3 显式 rollback

- 仅允许选择已存在、hash 校验通过且曾 `validated/published/loaded/superseded` 的历史 revision；
- 以历史 snapshot/artifact 为输入创建新的 rollback candidate，记录 `rollback_of_revision`（可放扩展字段或审计 detail）；
- 新发布仍需 `base_revision == current` CAS；历史 artifact 内容相同，因此 target content revision 可相同，但 `policy_current.generation` 必须增加，新的 candidate/publish audit id 不同；
- rollback 发布后同样等待 required 实例重新、新鲜上报该 generation 的目标 revision。由于 OPA status 原生主要上报 revision，若 revision 与当前已相同，接收端必须要求报告 `received_at > published_at`，禁止复用发布前旧报告完成加载确认；
- 不自动触发 rollback，不把 `failed` 偷换成成功。

## 9. Shadow 模拟

### 9.1 样本来源与脱敏契约

Shadow 只接受管理员显式上传的 JSON 样本集。每条至少包含：

```json
{
  "sample_id": "stable-id",
  "package": "loop_controller.tool_permission",
  "input": {},
  "expected": null,
  "redaction_attestation": {
    "profile": "policy-shadow-v1",
    "attested_by": "admin-principal"
  }
}
```

- package 仅允许配置 allowlist 中的两个决策 package；
- 上传者必须声明样本已按显式脱敏规则处理；服务再执行字段名 denylist、体积、深度、字符串长度及高风险模式检查，失败则拒绝整个样本集；
- 禁止自动读取 `JsonlAuditStore`、SQLite conversation、Approval 明文、Decision 原始参数、decision log 或线上请求作为 shadow 输入；
- 样本集不可变，使用完整 content SHA-256 寻址，文件权限与 artifact 同级受限；API 返回摘要，不回显全部 input。

### 9.2 隔离 evaluator

- baseline 可使用对应不可变 bundle 的专用 evaluator；candidate 必须发送到**隔离的 OPA endpoint/evaluator**，例如部署层提供的独立 shadow OPA，或受控的一次性 `opa eval --bundle <candidate-artifact>`；
- evaluator URL 必须与生产 OPA URL 不同，并在配置阶段拒绝相同规范化 scheme/host/port；禁止调用生产 OPA 的 policy/data 写 API，禁止改变其 bundle 配置；
- 若使用隔离 HTTP evaluator，由部署层管理进程，LC 仍不 fork OPA server；若使用一次性 `opa eval`，每个调用必须有超时、并发和输出限制，且只读访问指定 artifact；
- shadow 只产生比较报告，不进入 `OPAPolicyEngine` 线上路径，不写 DecisionStore，不调用工具，不触发审批，不影响 current pointer。

比较项至少包括 baseline/target 的 `verdict`、`reason`、`policy_hits`、`modified_args`（规范 JSON hash），统计 allow→deny、deny→allow、approval 变化、modify 变化和 evaluator error。逐条结果只保存 `sample_id` 与结果摘要/hash，不复制输入。是否允许发布由管理员根据报告决定；本版本不设自动阈值发布，也不自动阻断已 validated candidate，除非 evaluator 执行失败使本次 shadow run 为 `failed`。

## 10. 变更审计

每个创建、验证、shadow、发布、加载、失败、supersede、rollback 和认证拒绝事件至少记录：

- `actor`：可信 admin principal 或固定 machine principal/instance id；不能来自请求体自报；
- `candidate_id`、`base_revision`、`target_revision`；
- `source_sha256`、`artifact_sha256`、样本集/结果 hash（适用时）；
- `result`：success/failed/conflict/pending；
- 稳定 `failure_code` 和脱敏摘要；
- 时间、current generation、required/fresh/loaded/stale/error 实例计数。

`policy_change_audit` 是策略控制面查询事实源，并可将净化后的同类 `AuditEvent` 追加到现有审计链以统一外部取证；但不能用“先 SQLite 后 JSONL”的双写假装原子。current pointer 与关键策略审计以 SQLite 同事务为硬保证，现有 AuditStore 投影失败应告警并可重放，不能回滚已提交发布。审计中严禁 token、Rego 源全文、未脱敏 shadow input、环境变量和完整 OPA stderr。

## 11. Health 与 Readiness

保留现有 `/health`、`/v1/health`，增加明确的策略交付字段，并新增或明确 `/ready`（或等价 readiness 路由），避免只用一个 `status=degraded` 混淆存活与可接流量。

建议响应摘要：

```json
{
  "opa_reachable": true,
  "policy": {
    "expected_revision": "...",
    "active_revision": "...",
    "state": "loaded",
    "required_instances": 2,
    "fresh_instances": 2,
    "loaded_instances": 2,
    "stale_instances": [],
    "error_instances": [],
    "status_ttl_seconds": 60
  }
}
```

定义：

- `expected_revision`：SQLite current pointer；
- `active_revision`：仅当全部 required 实例新鲜且 revision 一致时为该 revision，否则为 `null`；单实例细节通过 admin status 查看；
- `stale_instances`：缺报告、TTL 超时或仍上报旧 revision的 required 实例；
- `error_instances`：明确报告加载错误或状态格式错误的 required 实例；
- `opa_reachable`：现有线上决策 OPA 的只读探测结果，不以 Bundle GET 成功替代。

Liveness 只证明 LC 进程与基础事件循环存活，不因 OPA 暂时不可达而重启 LC。Readiness 必须同时满足：SQLite/文件系统健康、线上 OPA reachable、存在 expected revision、全部 required 实例新鲜确认 expected revision、无 error、active==expected。否则返回 `503`；现有决策调用依旧由 `OPAPolicyEngine` fail-closed。

首次部署尚无 current revision、发布后等待上报、TTL 过期、OPA 不可达、active/expected 不同或加载失败，均 readiness false，并给出稳定状态码而非敏感错误文本。

## 12. 安全限制

1. **职责凭证隔离**：admin、bundle reader、status writer 三类 credential 必须不同；固定时间比较；永不写日志/审计/响应。
2. **网络边界**：生产通过 TLS、loopback/mesh 和 NetworkPolicy 限制 Bundle/Status 端点；Starlette 自身不替代边界 TLS 和防火墙。
3. **输入限制**：管理和 status 请求必须设置 body、文件数、单文件、总展开大小、JSON 深度、并发与速率限制；防 path traversal、tar bomb 和 gzip bomb。
4. **文件权限**：candidate、artifact、样本和结果目录只允许 LC 服务账号写；OPA bundle reader 只需网络读取权限；Agent 账号不可写。
5. **子进程限制**：只允许配置好的绝对 OPA binary；参数列表调用，禁止 shell；环境变量 allowlist；超时后终止；输出长度受限。
6. **SSRF 防护**：OPA production/shadow URL 只能来自受保护配置，不接受请求参数 URL；`httpx` 使用 `trust_env=False` 访问本地/sidecar，防环境代理改道。
7. **失败关闭**：hash 不匹配、SQLite/文件系统不一致、required 配置为空、凭证缺失/重复、OPA 校验不可用均不得发布或报告 ready。
8. **粗粒度 admin key 风险**：本版本无法职责分离、对象级发布授权和双人复核；生产应缩小持有人集合、定期轮换并依赖网络 ACL。RBAC 明确留 v0.52。
9. **revision 非秘密**：SHA-256/ETag 用于完整性和缓存，不是认证手段；不能因为 URL 含 revision 而省略 machine credential。
10. **保留旧策略**：验证失败不会更新 current；加载失败不会删除旧 artifact，但 LC 不保证 OPA 在失败时继续使用哪个 revision，实际 active 以新鲜 status 为准。

## 13. 事务与系统不变量

1. candidate snapshot 一旦创建不可修改；状态变化不改变源文件和 artifact。
2. revision、source hash、artifact hash 均为完整 SHA-256；`FilePolicyStore` 的 12 位版本永不进入 bundle 控制面。
3. 一个 revision 对应唯一确定性 artifact；相同输入必须逐字节相同。
4. Bundle API 只读取 SQLite 指向且已原子安装、复核 hash 的 artifact。
5. `policy_current` 只有一行，所有发布/rollback 均使用 `base_revision` + `BEGIN IMMEDIATE` CAS。
6. pointer 更新、旧 candidate supersede、新 candidate published 和成功发布审计同事务提交。
7. `published != loaded`；只有全部 required instance 新鲜报告目标 revision 且无错误才 loaded。
8. 发布之前收到的旧 status，即便 revision 字符串相同，也不能确认本次 publish/rollback generation。
9. status TTL 由 LC `received_at` 计算；客户端时间不能延长 freshness。
10. Shadow candidate 永不进入线上 OPA，shadow 结果永不自动更新 current。
11. rollback 是新的发布操作，不修改、删除或重新打开历史 candidate。
12. 任何自动回滚、自动灰度、decision log 自动采样均不存在；文档和 API 不得暗示存在。
13. 状态与审计失败时 fail-closed；不得出现 pointer 已变但关键 SQLite 审计缺失。
14. LC 不 fork 生产 OPA；部署层负责 OPA sidecar 的启动、存活、bundle plugin 与 status plugin 配置。

## 14. 实施顺序

### P50-01 数据模型、配置与存储

- 增量 schema、领域模型、状态迁移校验；
- 配置 artifact/sample 目录、OPA binary、production/shadow endpoint、bundle 名、required instances、TTL 和三类 credential；
- 启动时验证目录权限、credential 隔离、required 非空、endpoint 隔离。

### P50-02 Candidate snapshot 与确定性构建

- 实现安全路径校验、不可变 snapshot、规范文件清单；
- 实现完整 revision、`.manifest`、确定性 tar/gzip、artifact hash；
- 实现临时文件 fsync、原子安装和冲突校验。

### P50-03 严格校验

- 接入 `opa check --strict`、`opa test`；
- 增加双 package 空 input default-deny；
- 记录 OPA 版本、稳定结果码与脱敏摘要。

### P50-04 管理 API 与发布 CAS

- 候选/校验/查询接口；
- publish 与 rollback 领域事务；
- 粗粒度 admin principal 与策略审计。

### P50-05 Bundle GET/HEAD

- 独立 bundle reader credential；
- current/revision 路由、强 ETag、If-None-Match/304、HEAD；
- 读取时 hash/size 不一致 fail-closed。

### P50-06 Status receiver 与加载聚合

- 独立 status writer credential、allowlisted instance；
- required quorum、TTL、generation freshness、loaded/failed/superseded CAS；
- expected/active/stale/error 健康摘要。

### P50-07 Shadow

- 显式脱敏样本集上传和不可变存储；
- 隔离 evaluator、baseline/target 比较、结果 hash 与摘要；
- 验证 shadow 对生产 endpoint、DecisionStore 和 current pointer 零写入。

### P50-08 Health、文档与发布收口

- readiness 与现有 health 对齐；
- 修正根 `README.md` 中不存在的 `lc opa-start`，改为部署层 OPA/sidecar 指令；
- 更新 `KNOWN_LIMITATIONS.md`、配置示例、版本号与测试说明；
- 注意：本开发方案创建阶段只新增本文档，README 修正属于后续实现任务，不在本次文档提交中修改。

依赖顺序不可颠倒：没有不可变 artifact 与校验，不得实现发布；没有 CAS current，不得接 Bundle current；没有独立 status 事实，不得声称 loaded；没有 endpoint 隔离，不得开放 shadow。

## 15. 测试计划

### 15.1 单元测试

- snapshot：递归收集 interaction、排序、UTF-8、路径穿越、绝对路径、链接、隐藏文件、大小/数量限制；
- revision：任一文件名/内容/roots 变化均改变完整 revision；同内容重复构建稳定；证明不使用 12 位 `FilePolicyStore` 值；
- tar.gz：固定 metadata、gzip mtime/name、成员顺序、`.manifest`，跨进程构建逐字节一致；
- artifact：临时文件不可见、rename 原子性、现有同 hash 幂等、同 revision 异 hash 拒绝；
- 状态机：每条合法/非法 transition、终态不可逆、failed 不自动回滚；
- CAS：base 匹配成功、不匹配 409、两个并发 publisher 仅一个成功；事务注入失败后 pointer/state/audit 全回滚；
- rollback：创建新 candidate/event/generation，不改历史；同 revision 时拒绝复用 publish 前 status；
- TTL：边界时刻、缺报告、旧 revision、过期、未知/非 required instance、错误恢复；所有 required 新鲜后仅一次 loaded；
- ETag：GET/HEAD headers、quoted strong ETag、If-None-Match 304、无 body、artifact hash 不一致 503；
- 认证：三类 credential 互不可用、缺失/相同配置 fail-closed、常量时间比较路径、响应/日志不泄密；
- 审计：actor/base/target/hash/result 完整，错误摘要脱敏，关键事件与 pointer 同事务。

### 15.2 OPA 集成测试

使用测试 fixture 显式启动临时 OPA；这不改变生产“不 fork OPA”的边界：

- 合法策略通过 `opa check --strict` 和 `opa test`；语法错误、strict 错误、测试失败、超时均 failed 且 current 不变；
- 删除任一 package 或将空 input 默认改为 allow 时验证失败；
- OPA 从 Bundle GET 拉取，验证 ETag/304；
- 模拟至少两个 required instance：只有全部上报新鲜目标 revision 后 loaded；旧/缺失/过期/error 均不 loaded；
- 发布 B 时 A 保持可取但 current 指向 B；B 加载失败不自动回到 A；显式 rollback A 产生新 generation 并重新等待确认；
- OPA/Bundle/status 短暂不可用恢复后状态正确，无虚假 loaded。

### 15.3 Shadow 与安全测试

- 上传显式脱敏样本成功；包含 token、Authorization、明文 Secret、高风险字段或超限内容时整批拒绝；
- 证明实现不调用 AuditStore/Conversation/Approval 原文读取接口；
- production 与 shadow URL 相同配置启动失败；请求体不能覆盖 evaluator URL；
- shadow 执行期间 production OPA 未收到候选请求，current、Decision、Approval、工具执行计数均不变；
- baseline/target verdict、approval、modify 差异计数正确；evaluator timeout/error 仅使 shadow run failed，不发布、不回滚；
- tar traversal/bomb、JSON 深度、请求体限流、SSRF、命令注入、stdout/stderr Secret 脱敏测试。

### 15.4 回归与工程验证

- 现有 `OPAPolicyEngine` fail-closed、工具与 interaction 决策测试全部通过；
- 现有 `HotReloader` 测试明确证明 Rego 不在其监听集合；
- 现有 Decision 的 `policy_version` 兼容，不把它误断言为 bundle revision；
- Starlette 管理 API、health、认证和请求限制回归通过；
- Windows/Linux 确定性 artifact fixture hash 一致；
- Python 全量测试、ruff、mypy（按仓库当前门槛）、`git diff --check` 通过。

## 16. 总体完成定义

1. 管理员可创建不可变 candidate，完整递归覆盖 `policies/interaction`，且原 snapshot 无法被后续请求修改；
2. 生命周期完整实现为 `draft → validated → published → loaded/failed/superseded`，非法跳转被拒绝；
3. revision、source hash、artifact hash 均为完整 SHA-256 且语义清楚；确定性 tar.gz 和 `.manifest` 可重复构建验证；
4. 发布前必须通过 `opa check --strict`、`opa test` 和两个 package 的空 input default-deny 契约；失败不改变 current；
5. 管理 API 受可信 admin principal 保护，并明确粗粒度 key 限制；
6. Bundle GET/HEAD 使用独立 machine credential，正确实现 ETag、If-None-Match 和 304；
7. status receiver 使用另一套独立 credential，只接受 allowlist instance；
8. 只有 required instance 集合全部新鲜确认目标 revision，candidate 才 loaded；TTL、旧 revision、缺失和 error 均进入 stale/error 视图并使 readiness false；
9. artifact 安装原子，SQLite current pointer 以 `base_revision` CAS 原子更新，关键审计同事务；
10. rollback 被实现为重新发布历史不可变 revision的新 candidate/新 generation，并重新等待加载确认；
11. shadow 只使用显式脱敏样本集，候选仅在隔离 evaluator 执行，对生产 OPA、current 和线上决策零影响；
12. 审计可回答谁在何时以哪个 base 发布/回滚到哪个 target、对应 source/artifact/result hash、验证/shadow/加载结果是什么；
13. health/readiness 清楚暴露 OPA reachable、expected、active、stale、error，且不把进程存活等同于策略就绪；
14. 生产 LC 不 fork OPA，部署文档明确由 sidecar/部署层管理；
15. 根 README 中不存在的 `lc opa-start` 已修正，且没有为掩盖文档错误而新增该命令；
16. 未引入外部 DB/MQ 或非必要依赖；不包含自动灰度、自动回滚、decision log plugin、多租户或 RBAC；
17. 所有专项、集成、安全与现有回归测试通过，公开能力描述与代码一致。

---

## 发布前审查修复记录

发布前审查发现 9 项问题（3 高 6 中），经两个独立验证代理复核全部确认存在（#7、#9 校准为 minor），已按以下方式修复并回归通过：

| # | 级别 | 问题 | 修复 |
|---|------|------|------|
| 1 | 高 | 相同 status 重放可刷新 `received_at`，伪造新 generation loaded | `receive_status` 对相同 `report_sha256` 严格幂等返回，不再更新 `received_at`；freshness 与 generation/revision 绑定在聚合时双重校验 |
| 2 | 高 | shadow 未绑定 candidate/revision/artifact，评估对象不精确 | `PolicyShadowService.for_candidate` 显式校验 candidate_id/revision/artifact_sha256 三元组；server shadow 端点要求请求体携带 revision/artifact_sha256 并与 candidate 匹配（409）；baseline/candidate 均使用只读 artifact 的隔离 evaluator（`opa eval --bundle`） |
| 3 | 高 | 不兼容 OPA status plugin 标准 payload | `_parse` 按标准 schema 解析 `labels`（instance id）+ `bundles.<name>`（active_revision/status/errors）嵌套结构，要求目标 bundle key 显式存在；自定义格式必须显式携带 `schema_version: loop-controller-status-v1` |
| 4 | 中 | artifact 安装后可写、校验后按 Path 重开存在 TOCTOU | artifact 安装后 chmod 0o444 只读；`build_artifact` 硬链接后立即删除 temp 再 chmod（Windows 硬链接共享属性）；server Bundle 端点直接消费 `bundle()` 单次读取并校验 hash 的 bytes，不再按 Path 重开 |
| 5 | 中 | required 聚合依赖全表行数，历史行污染 | `_aggregate` 只按 required instance IDs 查询并逐一判定，quorum 不依赖全表行数 |
| 6 | 中 | 校验证据与状态非原子 | `record_validation` 在同一事务写入 `policy_validations`（OPA version/stage 结果/result_sha256）、candidate 状态迁移与 change audit |
| 7 | minor | rollback create+publish 跨事务 | rollback 合并为单事务：创建新 candidate（published）、supersede 旧 candidate、CAS 更新 current、写 rollback audit 一次完成 |
| 8 | 中 | rollback 允许 failed 来源 | rollback 要求目标 revision 存在 `event_type='loaded' AND result='success'` 审计证据（曾成功 loaded），否则拒绝 |
| 9 | minor | snapshot 先于 DB 写入，DB 失败留孤儿目录 | `create_candidate` 在 `insert_candidate` 失败时补偿清理本次创建的 candidate 目录（恢复可写权限后递归删除） |

---

## 附录：v0.51—v0.54 方向

以下为方向性蓝图，进入对应版本前仍需基于届时代码重新核查和冻结范围。

### v0.51.0：企业强制治理出口基线

- 工具凭证集中持有，不发放给 Agent；
- MCP / HTTP / Harness 成为唯一受保护工具出口；
- Agent Runtime 最小网络、文件系统和进程权限；
- 工作负载身份与 delegated subject 分离；
- 部署级隔离、防绕过测试及全链路审计关联。

### v0.52.0：多租户、企业身份与 RBAC

- tenant / org / workspace 与 Agent、策略 bundle 归属；
- 管理员、策略创建者、验证者、发布者、审计者、审批者与执行者 RBAC；
- 职责分离、双人复核和对象级授权；
- OIDC/JWKS、工作负载身份与密钥轮换；
- 跨租户访问、委托策略和租户级数据域。

### v0.53.0：生产可观测性与工程基线

- 性能压测、bundle/status 容量与延迟基线；
- SLO、运行时指标、告警与 dashboard；
- Web 控制台与策略/审批待办；
- SIEM/SSO 集成；
- Checkpoint、Proxy、审批、Outbox、OPA 加载与 shadow 健康指标。

### v0.54.0：多 Agent 平台化（候选）

- Agent Card 能力路由和动态调度；
- 并行子任务与 DAG；
- worker 集群与任务队列；
- 预算感知调度和失败重试；
- 组织级 federation。
