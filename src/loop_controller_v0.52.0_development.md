# v0.52.0：多租户、企业身份与 RBAC

> **Status**: 已完成
> **Target Version**: `0.52.0`
> **基线**: v0.50.0 代码边界
> **依赖说明**: v0.52 已在 v0.50.0 代码边界上完成多租户、企业身份与 RBAC 收口。

---

## 1. 版本定位与当前真实边界

v0.50.0 交付了策略生命周期平面，但 V50-1 明确声明策略管理面仍是**单一粗粒度 admin key**。KNOWN_LIMITATIONS 中已有四处声明指向同一缺口：

- **V33-3**（src/KNOWN_LIMITATIONS.md:417-419）：MCP Proxy admin profile 白名单与 HTTP Admin API key 是两套互不相通的权限模型，"尚未统一为单一 RBAC 体系"；
- **V261-2**（:292-294）：管理操作统一走 HTTP Admin API + API key，"不提供角色继承、细粒度管理权限或多租户管理员域"；
- **V22-3 / V26-4**（:143-146、:282-284）：`tenant_id` 仅为命名空间预留，无租户鉴权、资源隔离、跨租户访问控制；
- **V50-1**（:433-435）：策略管理 admin key 不能区分 creator/validator/publisher/auditor，不能表达双人复核或职责分离。

本节以调研事实（文件:行号对应 v0.50.0 冻结点）划定 v0.52 的真实起点。

### 1.1 现有身份模型

- `AgentIdentity`（src/loop_controller/identity/models.py:15-26）已含 `tenant_id: str | None = None` 字段（v0.26 预留）与 `profile_id`，frozen 语义；凭证载体 `IdentityCredential` 支持 token / cert_cn / cert_sans / cert_subject（:29-46）。
- `IdentityProvider` Protocol（identity/provider.py:11-28）：`verify(credential) -> AgentIdentity | None` + `get_agent` / `get_user`。
- 三种 Provider（identity/jwt.py、mtls.py、static.py）均**只做验证 + 映射到 agents.yaml 注册表**，无签发逻辑；Python 侧唯一签发是内部 AuthorityToken（authority.py:106-176，动态权限提升，与访问控制正交）。
- **没有任何 Provider 填充 `tenant_id`**：jwt.py:38-42 的 claim_mappings 仅提取 agent_id / user_id / harness_id。

### 1.2 管理入口认证现状

- `_check_api_key`（server.py:258-271）：单一全局 admin key，支持 `x-api-key` header 或 Bearer 两种载体，长度比较 + `hmac.compare_digest` 常量时间比较；key 为 None 直接拒绝。
- admin actor 匿名化（server.py:281-294）：`_admin_actor_id()` 把请求 key 的 SHA-256 前 12 hex 记为 `api-key:<hash12>`；`_audit_admin_operation`（:296-322）统一写 system 审计。
- v0.50 策略端点全部走该 api key（/v1/admin/policy/*，路由注册 :1581-1588）；`/v1/opa/bundles/*` 用独立 `bundle_reader_token`（:1313-1317），`/v1/opa/status` 用独立 `status_writer_token`（:1337-1340）；三者启动校验强制互不相同（config_loader.py:1455-1463）。
- 唯一例外：审批 approve/deny 用 `approval_auth` 命名 principal（见 1.3）。
- MCP Proxy 侧 admin 工具走 profile 白名单（proxy_server.py:285-298、712-718），默认空列表即全部拒绝（V33-3）。

### 1.3 审批身份（唯一已有的"命名人类 principal"框架）

- `resolve_approver_principal`（approval_service.py:25-57）：`entrypoints.yaml` 的 `approval_auth.credentials` 逐项取 `principal` + `token_env`，与环境变量值 `hmac.compare_digest` 比较得到已认证 principal；再查 `approval_auth.allowlist`，不在名单抛 `ApprovalAuthorizationError`（401/403 区分在 server.py:1045-1116）。
- `build_approval_record`（approval_service.py:60-128）强制 principal == `approver_id`、审批人 ≠ 请求者 ≠ 执行 Agent、必须在 users 注册表存在。
- 审计 actor 直接记审批 principal（server.py:1114），**不匿名化**——这是全仓唯一实名 admin 操作审计的先例。

### 1.4 Go 内核身份

- Delegation token（go/internal/token/token.go:19-134）：HS256 JWT-like，claims 含 initiator/target、task 链、allowed_tools/capabilities、depth、budget、exp。
- workload credential 为静态 Bearer 三分类（go/cmd/kernel/main.go:220-250：control-token+initiator 成对、approval-token+approver-principal 成对、executor-token），无动态工作负载身份。
- Go 模型只有字符串 agent id，**无 tenant/role 概念**；proto 的 tenant_id 字段（governance.proto:83、:114）尚未被任何代码消费。
- Python 桥（go_kernel_bridge.py:337-358）仅在 register_agent 等调用附加 delegation token，fail-closed。

### 1.5 tenant_id 预留现状

**纯字段预留 + secret/证据命名空间隔离，无鉴权级隔离**：

- 字段存在：models.py:99（Task）、:120（Agent）、identity/models.py:24、revocation.py:34（吊销匹配 tenant 维度 :157）、secrets/models.py（SecretRef/SecretValue）。
- 真实隔离仅两处：① SecretBroker 目录布局 `{base}/global/` 与 `{base}/tenants/{tenant_id}/`（secrets/file_backend.py:22-64，encrypted 变体 :34-82 共享布局）；② 证据链按租户分文件 `{tenant_id}.jsonl`（audit/evidence_backends.py:29-32）。
- Task store 的 upsert 已带 tenant_id（state_db.py:1242-1274）；`approval_requests` 表（:192-200）**无 tenant_id 列**。
- 吊销匹配已按 tenant 维度生效（revocation.py:157：entry.tenant_id 与 identity.tenant_id 不一致则跳过）——这是唯一已消费 tenant_id 的判定逻辑。

### 1.6 配置结构惯用法

AppConfig 顶层 section 见 config_loader.py:259-305。新增 section 五步惯用法（以 v0.50 `policy_delivery` 为模板）：

1. frozen dataclass，敏感值只存 env 变量名（config_loader.py:230-244）；
2. AppConfig 加 `field(default_factory=...)`（:305）；
3. `load()` 中调用 `_load_<section>`（:348、:450）；
4. `_load_<section>` 读 YAML、兼容裸映射、构造校验（:697-713）；
5. `_check_<section>` 启动校验（:470、:1446-1469）。

### 1.7 审计 actor 现状

- 运行时链路 actor = proposal.agent_id（checkpoint.py:408-409）；系统事件 actor = "checkpoint"（:578-579）；admin 操作 actor = `api-key:<hash12>` 匿名（server.py:281-294）；审批操作 actor = 实名 principal（server.py:1114）。
- `policy_change_audit.actor`（state_db.py:308-321）：publish/rollback 记 `_admin_actor_id` 匿名 hash（server.py:1258、:1281）；validate 事件记 candidate 的 `created_by`（infra/policy_delivery.py:328-334）——**validate 的触发者没有被记录**，双人复核无从判定。

---

## 2. 明确边界（v0.52 不做）

1. **不改 Go 内核的租户语义**。delegation token / 内核 HTTP 面不引入 tenant/role 校验；proto tenant_id 字段仅在 Python 桥透传，内核不消费。
2. **不做计费拆分、不做配额计费**。tenant 是安全域，不是计费单元。
3. **不做多 Bundle / 按租户拆包**（V50-4 维持）。策略线上生效仍只有一份 current pointer；tenant 在策略面只做 candidate 归属与操作权限隔离，不做"每租户不同线上策略"。
4. **不替换 `Agent.profile_id` 一对一静态绑定**。RBAC 是管理面/控制面角色模型，与 R1/R2 链路的 capability 天花板（models.py:117）正交共存，不改动其语义。
5. **不做 ABAC / ReBAC / 泛化对象级 ACL**。权限粒度是"端点 × 租户"，不引入属性条件求值或关系图。
6. **不引入外部 DB / MQ**。RBAC 存储复用 SQLite（state_db 模式），静态配置兜底。
7. **不做身份动态申领/注册**。Agent/User 注册仍走 agents.yaml / users.yaml 静态注册表；OIDC 只做验证与 claim 映射，不做 LC 侧用户生命周期管理。
8. **不实现 v0.51 的工作负载身份分离**。凭证集中持有、Runtime 最小权限属 v0.51 范围；v0.52 仅做管理面凭证的角色化与轮换机制。

---

## 3. 方案总览

在 v0.50 的三平面之上叠加**统一访问控制平面（Access Control Plane）**，四层结构：

```
凭证层   OIDC JWT（roles/tenant claim） | 静态角色凭证（principal + env token） | 兼容层（legacy admin key）
   ↓ 认证 Authentication
主体层   AuthenticatedPrincipal { principal_id, tenant_id, roles[], auth_method }
   ↓ 授权 Authorization（端点 × 租户 permission 映射，默认拒绝）
判定层   RBAC Enforcer（角色→权限映射 + 双人复核 + 跨租户 grant）
   ↓
审计层   实名 actor + permission_denied 拒绝事件（原 api-key 匿名模式仅保留给兼容层）
```

核心设计决策：

| 决策 | 选择 | 理由 |
|------|------|------|
| RBAC 默认姿态 | `enforcement: disabled`（保持现状）或 `enforce`（默认拒绝），**显式 opt-in** | 与 V33-3"默认关闭"姿态一致；enable 后未绑定角色一律 403 |
| legacy admin key 兼容 | 可配置映射为 `platform_admin` 或拒绝，enforcement 下默认仍生效但可关闭 | 避免 break 现有部署；关闭后仅剩角色凭证可用 |
| 审批凭证 | 复用 `approval_auth.credentials` 模式，纳入统一 RBAC 的 `approver` 角色 | approval_service.py:25-57 是唯一已验证的命名 principal 框架 |
| tenant 解析权威 | **注册表优先**：agents.yaml 的 `tenant_id` 为准，claim tenant 与注册表不一致则拒绝（fail-closed）；注册表未填时才接受 claim 值且需 `allow_claim_tenant: true` | 防止任意 claim 伪造租户 |
| 双人复核 | publish/rollback 要求存在 `result='success'` 且 `validated_by != created_by` 的 validation 证据 | 利用 policy_validations 既有证据链，不新建流程 |
| 跨租户访问 | 默认拒绝；显式 `cross_tenant_grants` 白名单（主体 × 源租户 × 目标租户 × 资源） | 与吊销/证据的 tenant 维度语义对齐 |

---

## 4. 详细设计

### 4.1 企业身份接入（identity 扩展）

**目标**：OIDC/JWT 携带 tenant 与 roles；JWKS 支持 kid 轮换。

1. **claim_mappings 扩展**（identity/jwt.py:38-42）：`claim_mappings` 增加 `tenant_id` 与 `roles` 两个可选映射。`roles` 为字符串列表 claim（如 `groups` / `realm_access.roles` 点路径），解析为 `list[str]`。
2. **tenant 绑定校验**（新增于 JWTIdentityProvider.verify，jwt.py:91-104 段）：
   - agent 注册表有 `tenant_id` → claim tenant 必须与其一致，否则返回 None（拒绝，fail-closed）；
   - 注册表未填 + 配置 `allow_claim_tenant: true` → 采用 claim tenant；
   - 注册表未填 + 未允许 → `tenant_id = None`（平台级，仅 platform_admin 可访问）。
3. **roles 进 AgentIdentity**：`AgentIdentity` 增加 `roles: tuple[str, ...] = ()`（frozen，向后兼容）。roles 仅作为**认证结果携带**，授权判定不直接信任 claim roles——见 4.2 的角色绑定。claim roles 与绑定角色的关系：claim roles 是"身份断言"，角色绑定是"授权事实"，enforcer 以绑定为准，claim roles 仅用于 `rbac.yaml` 的 `dynamic_role_bindings`（按 claim role 名自动映射为 LC 角色的可选机制，默认关闭）。
4. **JWKS kid 轮换**（jwt.py:58-104）：JWKS 客户端增加——
   - 按 `kid` 索引的 key 缓存 + `cache_ttl_seconds`（默认 300）；
   - 未知 kid 触发即时刷新（带 5s 单飞去重）；
   - 刷新失败且缓存未过**硬过期**（`hard_ttl_seconds`，默认 3600）→ 继续用缓存 key 验证（可用性优先），硬过期后 fail-closed；
   - 禁止 HTTP→HTTPS 降级，jwks_url 必须 https（本地开发显式 `allow_http_jwks: true`）。
5. **多 issuer**：`identity_config.providers` 允许多个 JWT provider（既有结构），roles/tenant 映射按 provider 各自配置。

### 4.2 RBAC 领域模型与判定层

新建 `src/loop_controller/rbac/` 包：

```
rbac/
  models.py      Role、Permission、RoleBinding、CrossTenantGrant、AuthenticatedPrincipal
  store.py       RoleBindingStore 协议 + SQLite 实现（state_db 扩展表）
  enforcer.py    Enforcer：authorize(principal, permission, tenant) -> Decision
  credentials.py 静态角色凭证解析（principal + env token，compare_digest）
```

**角色集**（冻结，不开放自定义角色名）：

| 角色 | 权限 |
|------|------|
| `platform_admin` | 全部权限 + RBAC 绑定管理 + 跨租户（bootstrap/超管，建议仅凭证持有） |
| `tenant_admin` | 本租户：approval.decide、admin.kill_switch、admin.revoke、rbac.binding.manage（本租户）、policy.status.read |
| `policy_creator` | policy.candidate.create / read / update（draft 态）、policy.shadow.run |
| `policy_validator` | policy.candidate.read、policy.validate.run、policy.shadow.run |
| `policy_publisher` | policy.candidate.read、policy.publish、policy.rollback（受双人复核约束） |
| `policy_auditor` | policy.candidate.read、policy.audit.read、policy.status.read（只读） |
| `approver` | approval.decide（需同时在 approval_auth.allowlist，见 4.4 双轨锚定） |

治理面（/v1/govern/*）的 executor 身份沿用既有 AgentIdentity 机制，**不纳入本角色表、不新增凭证**，v0.52 不改变其行为——表中不列出以免误读为需走 RBAC。

**权限命名空间**：`policy.candidate.create|read|update`、`policy.validate.run`、`policy.shadow.run`、`policy.publish`、`policy.rollback`、`policy.audit.read`、`policy.status.read`、`approval.decide`、`rbac.binding.manage`、`admin.kill_switch`、`admin.revoke`。`bundle.read` / status 上报维持独立 machine credential，**不进 RBAC**（机器身份与人类角色分离）。

**Enforcer 语义**：

```python
authorize(principal, permission, tenant) -> Decision(allowed, reason)
```

- principal.tenant_id == tenant：查角色绑定（SQLite 表 ∪ 静态配置 ∪ 动态 claim 映射，优先级递减）；
- principal.tenant_id != tenant：仅当 platform_admin 或存在有效 cross_tenant_grant 时允许，且仅限 grant 列明的资源；
- 租户级资源访问中 tenant_id 为 None 的资源（平台级）只有 platform_admin 可访问；
- 全部判定路径 fail-closed：任何解析异常 → 拒绝。

**叠加规则（二次对象级检查，与 permission 判定分离但同属授权层，由 server 在 enforcer 通过后强制执行）**：

- `policy.publish` / `policy.rollback`：enforcer 通过后，还需满足 principal 为 platform_admin，或持有 `publish_scope: global` 的 cross_tenant_grant。原因：发布切换的是全局唯一的 current pointer（V50-3/V52-3），影响面是平台级而非租户级。普通租户内的 `policy_publisher` 角色只覆盖"发起发布动作"的权限，实际生效以该二次检查为准——enforcer 与 server 实现必须以本节为共同规约，禁止只在端点层口头约定。

**双人复核**（职责分离）：

- `policy_validations` 增加 `validated_by TEXT NOT NULL` 列（迁移：旧行填 `created_by` 并标记不可用于双人复核——通过新增 `separation_ok INTEGER NOT NULL DEFAULT 0` 判定）；
- `record_validation` 签名增加 `actor`，validate 审计事件 actor 记录触发者（修复 infra/policy_delivery.py:328-334 记 created_by 的缺陷）；
- `publish` / `rollback`：要求存在 `result='success' AND separation_ok=1 AND validated_by != created_by` 的 validation；否则 409 `separation_of_duties_violation`；
- 显式逃生口：仅 `platform_admin` 可执行 waiver（配置 `allow_self_publish: true` 只放开 platform_admin 的 self-publish，**不对 policy_publisher 开放**）；waiver 必须提供理由（空理由 400）；每次 waiver 写 `separation_waived` 审计事件（actor、理由、candidate_id）。默认姿态仍是双人复核，逃生口权限严格高于正门。

### 4.3 凭证体系

**三类凭证，认证顺序固定**：

1. **Bearer JWT**（OIDC）→ AuthenticatedPrincipal（principal = claim sub 或映射 agent_id，roles 来自 claim，tenant 来自 4.1 校验）；
2. **静态角色凭证**：header `x-lc-principal` + `Authorization: Bearer <token>`，`rbac.yaml` 的 `credentials[{principal, tenant, token_env, roles[]}]`，token 比较 `hmac.compare_digest`（复用 approval_service.py:49 模式）；
3. **legacy admin key**（仅 enforcement 下兼容层）：命中后映射为 `platform_admin`（可配置 `legacy_key_role` 或 `reject`）。

凭证即身份：同一 principal 的 token 轮换 = `token_env` 环境变量值替换，**不落盘**；旧 token 即时失效（无宽限期，轮换动作原子生效）。轮换审计：LC 不记录 token 值，记录 `credential_rotated` 事件（principal、env 变量名、操作者）。

### 4.4 端点授权映射

server.py 改造：`_check_api_key` 保留为兼容层原语，新增 `_authenticate(request) -> AuthenticatedPrincipal` 与 `_require(request, permission)` 装饰式判定；每个 admin 端点声明所需 permission（集中映射表，便于审查）：

| 端点 | permission |
|------|-----------|
| POST /v1/admin/policy/candidates | policy.candidate.create |
| GET /v1/admin/policy/candidates* | policy.candidate.read |
| POST .../validate | policy.validate.run |
| POST .../shadow | policy.shadow.run |
| POST .../publish | policy.publish |
| POST .../rollback | policy.rollback |
| GET /v1/admin/policy/audit | policy.audit.read |
| GET /v1/admin/policy/status | policy.status.read |
| POST /v1/admin/approvals/{id}/approve\|deny | approval.decide（且 approval_auth principal 校验保留，双重约束） |
| POST /v1/admin/kill-switch、/v1/admin/revoke | admin.kill_switch / admin.revoke |
| RBAC 绑定管理 API（新增 /v1/admin/rbac/*） | rbac.binding.manage |

**审批 tenant 双轨锚定**：审批人的实名身份由 approval_auth 认证（principal），其租户可见性由 RBAC 绑定决定。两端 tenant 必须一致：approval decide 时，请求 tenant 对应的 approver RBAC 绑定（principal × tenant 的 approver 角色）必须存在，否则 403。即 rbac.yaml 静态凭证里手填的 tenant 与 approval_auth principal 不能各说各话——不一致即拒绝，fail-closed。

候选对象级归属：`policy_candidates` 增加 `tenant_id` 列；creator 的 tenant 即 candidate 归属；非归属租户的角色即使同名权限也只能读本租户 candidate（publish 等写操作严格绑定归属租户）。**例外**：发布动作本身是全局生效（单 pointer），因此 policy.publish 额外要求 platform_admin 或 `publish_scope: global` 的显式 grant——这是"对象级授权"的最小实现，不做泛化。

MCP Proxy 侧（proxy_server.py:285-298）：admin profile 白名单保持现状，但 admin 工具调用审计 actor 从匿名改为已认证 principal（若 enforcement 开启）；**不**在 v0.52 合并两套模型，统一工作留待后续（V33-3 降级为"部分解决"）。

### 4.5 数据域隔离

- `policy_candidates` / `policy_validations` / `policy_change_audit` 增加 `tenant_id` 列（audit 表通过 candidate join 亦可，但独立列更简单——冻结为 candidates/validations 加列，audit 走 join）；
- `approval_requests` / `approval_responses` 增加 `tenant_id` 列（task store 已有先例 state_db.py:1242-1274）；pending 列表查询按 approver 可见租户过滤；
- 审计查询（/v1/admin/audit）：tenant_admin 强制本租户过滤，platform_admin 可选全量；
- SecretBroker / evidence 租户命名空间已就绪（file_backend.py:22-64、evidence_backends.py:29-32），v0.52 仅补鉴权：agent 只能读 global 与自身 tenant 的 secret（SecretBroker get/list 增加 tenant 断言，fail-closed）。
- 吊销（revocation.py:157）已有 tenant 维度匹配，无需改动，补测试即可。

### 4.6 跨租户访问

`cross_tenant_grants` 表：`grant_id, source_principal, source_tenant, target_tenant, resources(TEXT json 数组), granted_by, created_at, revoked_at`。语义：

- 默认无任何 grant → 跨租户一律 403；
- grant 只对列明 resources（permission 名）生效，绑定到 source_principal + source_tenant 组合；
- 授权动作本身需 platform_admin；每次 grant/revoke 写审计。

### 4.7 审计与实名

- 认证成功的 admin 操作：actor = principal_id（实名，替代 api-key 匿名 hash）；auth_method 记入 detail（jwt / static-credential / legacy-key）；
- **拒绝也要审计**：每次 403 写 `permission_denied` 事件（actor、endpoint、required_permission、principal_tenant、reason），不进 decision log（防刷爆），进独立 `rbac_denials` 表（**无自动清理/无 TTL，与审计日志一样由部署方负责归档**）；
- 兼容层（legacy key）继续匿名 hash 模式并打标 `legacy: true`，便于追踪迁移；
- 红线维持：严禁 token / claim 全文 / Rego 源码入审计。

### 4.8 SQLite Schema 增量

```sql
CREATE TABLE IF NOT EXISTS rbac_role_bindings (
    binding_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    tenant_id TEXT,              -- NULL = 平台级
    role TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rbac_bindings_principal ON rbac_role_bindings(principal, revoked_at);

CREATE TABLE IF NOT EXISTS rbac_cross_tenant_grants (
    grant_id TEXT PRIMARY KEY,
    source_principal TEXT NOT NULL,
    source_tenant TEXT NOT NULL,
    target_tenant TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS rbac_denials (
    denial_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    required_permission TEXT NOT NULL,
    principal_tenant TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 迁移（state_db 既有 PRAGMA user_version 模式）
ALTER TABLE policy_candidates ADD COLUMN tenant_id TEXT;
ALTER TABLE policy_validations ADD COLUMN validated_by TEXT;
ALTER TABLE policy_validations ADD COLUMN separation_ok INTEGER NOT NULL DEFAULT 0;
ALTER TABLE approval_requests ADD COLUMN tenant_id TEXT;
ALTER TABLE approval_responses ADD COLUMN tenant_id TEXT;
-- 旧 validation 行回填：validated_by = created_by, separation_ok = 0
```

迁移注意：既有 `ensure_schema` 幂等模式 + user_version 递增迁移；旧库打开时新列缺省 NULL 不破坏既有读路径（读取端用 COALESCE 或显式判空）。

### 4.9 配置

新增 `config/rbac.yaml` + `RbacConfig`（config_loader 五步惯用法）：

```yaml
rbac:
  enforcement: disabled            # disabled | enforce
  allow_claim_tenant: false
  dynamic_role_bindings: false     # claim roles 自动映射 LC 角色
  allow_self_publish: false
  legacy_key_role: platform_admin  # platform_admin | reject
  bindings:                        # 静态兜底（无 DB / bootstrap 用）
    - principal: admin-alice
      tenant: acme
      roles: [tenant_admin, policy_publisher]
      token_env: LC_RBAC_TOKEN_ADMIN_ALICE
  cross_tenant_grants: []          # 也可运行时 API 管理
```

启动校验（`_check_rbac`）：enforcement 开启时 bindings 非空或允许 OIDC dynamic 映射（否则全拒锁死）；token_env 均设置；legacy_key_role 与 enforcement 组合合法。

### 4.10 Go 侧改动（最小透传）

- `go/internal/models`：delegation claims 透传 `tenant_id`（proto 字段已存在 governance.proto:83、:114），Python 桥在 register_agent 时从 AgentIdentity 填入；
- 内核**不做** tenant 校验（明确边界 1）；内核 audit 落库原样携带 tenant 值，供 Python 侧审计归集。

---

## 5. 测试覆盖

新增 `tests/test_rbac.py`、`tests/test_identity_tenant.py`、`tests/test_rbac_server.py`：

1. **identity**：claim tenant 与注册表一致 / 不一致拒绝 / 未注册 + allow_claim_tenant / roles claim 解析（点路径、非列表容错）；
2. **JWKS**：kid 未知触发刷新、TTL 过期、硬过期 fail-closed、allow_http_jwks 拒绝 http；
3. **enforcer 单元**：每角色权限矩阵、未绑定角色拒绝、跨租户默认拒绝、grant 生效、revoked binding 失效；
4. **双人复核**：creator==validator 409、separation_ok=0 拒绝、waiver 审计、validated_by 记录；
5. **server 矩阵**：每端点 × 每角色 401/403/200 全组合（复用 test_policy_lifecycle_server.py:36-64 三 token fixture 模式扩展）；
6. **凭证轮换**：token_env 替换后旧 token 401；compare_digest 常量时间路径；
7. **数据域**：tenant A 读不到 tenant B candidate/approval/audit；SecretBroker 跨租户读取拒绝；
8. **拒绝审计**：403 产生 rbac_denials 行；actor 实名；
9. **回归**：全量回归通过，enforcement=disabled 默认配置维持兼容行为；
10. **Go 侧**：`go test ./internal/...` 通过，含 register_agent 时 delegation claims 携带 tenant_id 的断言（Python 桥透传，内核不消费不校验）。

## 6. 文档与版本同步

- KNOWN_LIMITATIONS：V22-3 / V26-4 / V261-2 / V33-3 / V50-1 改写为"部分解决"并指向 v0.52；新增 V52-1（Go 内核不校验 tenant）、V52-2（不做计费/配额）、V52-3（单 Bundle 全局生效）、V52-4（无 ABAC）、V52-5（MCP admin 白名单未合并）；
- README：当前版本 → v0.52.0；
- 版本同步：pyproject/uv.lock、contract/openapi 重命名 a2a_v0.52.0、协议常量、config。

## 7. 实施任务拆解

| 任务 | 内容 | 验收 |
|------|------|------|
| P52-01 | identity claim 扩展（tenant/roles）+ 注册表绑定校验 + AgentIdentity.roles | 新单测过 |
| P52-02 | rbac 包：models/store/enforcer + SQLite 表 + 迁移 | 单测过 |
| P52-03 | JWKS kid 轮换缓存 + 静态角色凭证 | 单测过 |
| P52-04 | server 认证/授权中间件 + 端点 permission 映射 | 矩阵测试过 |
| P52-05 | 双人复核（validated_by/separation_ok + publish 校验 + waiver） | 单测过 |
| P52-06 | 数据域（candidates/validations/approval tenant 列 + 查询过滤 + SecretBroker 断言） | 单测过 |
| P52-07 | cross_tenant_grants + 管理 API + 审计 | 单测过 |
| P52-08 | 审计实名 + rbac_denials | 单测过 |
| P52-09 | rbac.yaml 配置 + _check_rbac + Go 桥 tenant 透传 | 启动校验过 |
| P52-10 | 文档收口 + 版本同步 + 全量验证（Python/Go/ruff/mypy） | 全绿 |

## 8. 验收清单

- [x] enforcement=disabled 默认配置：全量回归通过；
- [x] enforcement=enforce：未绑定角色请求全部 403 且写 rbac_denials；
- [x] OIDC JWT 角色/租户端到端：注册表绑定校验 fail-closed；
- [x] publish 双人复核：self-publish 默认 409，waiver 路径写审计；
- [x] 跨租户访问默认拒绝，grant 后仅列明资源放行；
- [x] token 轮换即时生效，旧 token 401；
- [x] 审计 actor 实名（兼容层除外），无 token/claim 全文入审计；
- [x] KNOWN_LIMITATIONS / README / 版本契约同步至 0.52.0。
