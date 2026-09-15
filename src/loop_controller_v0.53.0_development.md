# v0.53.0：企业强制治理出口与工作负载隔离基线

> **Status**: 开发完成（支持环境发布门禁待执行）
> **Planning Scope**: `v0.53.0`
> **Implementation Baseline**: `v0.52.0`
> **Target Version**: `v0.53.0`
> **Delivery Version**: `v0.53.0`
> **依赖说明**: 复用 v0.52 已交付的企业身份、多租户、RBAC 与实名审计，不重做管理授权平面。

---

## 1. 版本定位与历史关系

原 v0.51 路线图规划过五项能力：工具凭证集中持有、MCP/HTTP/Harness 唯一受保护出口、Agent Runtime 最小权限、工作负载身份与 delegated subject 分离，以及部署级隔离、防绕过测试和全链路审计关联。该路线图能力未实施，不能声称 v0.51 已发布或已交付这些能力。历史 P51 仅用于说明原路线图归属，不作为本方案任务编号。

本方案在 v0.52.0 实现基线上由 v0.53.0 正式承接历史 v0.51 能力，并扩展为可实施、可验证、fail-closed 的 strict security contract。v0.52 的 `AuthenticatedPrincipal`、tenant、roles、OIDC/JWKS、RBAC 与实名管理审计是前置条件，但管理主体身份不等于执行面的 workload identity，本方案不另建管理 RBAC。

版本规则固定如下：

- 所有公共 wire、delegation token、security capability negotiation、`ExecutionReceipt`、Protected MCP contract 均按 v0.53.0 版本化。
- 实施收口必须生成并同步 v0.53.0 Python package、Go protocol、OpenAPI 与 contract。
- 禁止生成任何活动 v0.51 包、协议、OpenAPI 或 contract；禁止把 Python/Go/API schema/发布元数据降回历史版本。
- 历史 development 文档不因本次实施批量改版本。

---

## 2. 当前基线与边界

- `SecretBroker` 已支持按 `SecretRef` 读取、列举和 reload；HTTP executor 已具备集中解析基础，但各出口尚无统一 strict 注入约束。
- MCP 当前 stdio 子进程只是协议代理，不是 strict 生产网络边界；Harness 的本地、subprocess、isolated、Docker wrapper 与 fallback 均不能直接用于 strict。
- `AgentIdentity` 表达逻辑 Agent，`AuthenticatedPrincipal` 表达管理主体，二者都不证明运行中工作负载实例。
- Go delegation claims 尚未绑定目标 workload；现有 process 自生成实例值只适合 lease/去重。
- sandbox schema 不构成实际隔离保证；真实网络、文件、进程和资源约束必须由 Docker/Kubernetes/VM 部署环境强制。
- execution、delegation、lifecycle 与 audit index 尚未形成 request/decision/task/call/jti/workload/receipt 的统一链路。
- `/health` 与 `/ready` 尚未报告受保护出口、凭证隔离、工作负载身份、A2A、能力协商、审计关联和 runtime assurance。

---

## 3. 威胁模型、目标与非目标

Agent Runtime 按潜在恶意或遭提示注入处理：它可能读取可见环境、文件和挂载，尝试直连 SaaS、数据库、upstream MCP 或 Harness，伪造 agent/tenant/delegated subject，诱导日志或持久化泄密，并尝试 shell、fork/exec、网络扫描和宿主访问。

受信边界包括 LC control plane、独立 MCP Proxy、经 HTTPS+mTLS 认证的 remote Harness、Secret backend 与可信 TLS termination。各组件仍遵循最小凭证与最小网络权限。

目标：

- Agent 只提交逻辑工具参数和凭证引用，不接触真实工具 Secret。
- strict 下副作用只经 protected MCP、protected HTTP 或 remote HTTPS+mTLS Harness。
- 经认证 workload service identity 与 `DelegatedSubject` 分离并绑定 tenant；仅有可信部署绑定时使用实例身份。
- 提供部署 conformance、runtime observation、readiness 和环境内防绕过负向测试。
- 任一 correlation ID 可查询判定、执行、委托与 lifecycle 链路。

非目标：不把 SDK/in-process adapter 当不可绕过边界；不重做 v0.52 RBAC；不内置通用编排器、ABAC/ReBAC、全局 nonce/限流；不要求非对称 delegation PKI、TPM/TEE 或完整远程 attestation；Windows 开发模式不算 strict 生产保证。

---

## 4. 核心模型与强制不变量

### 4.1 身份与委托

`WorkloadIdentity` 由实际 TLS peer 认证：

- `workload_id` 来自证书 URI SAN（SPIFFE-style URI，不要求完整 SPIFFE 控制面）；
- `service`、`principal`、可选 `tenant_id`、`authenticated_at/expires_at`；
- `auth_method` 为 `mtls`、`trusted-proxy-mtls` 或仅开发使用的 `static-dev`；后者不满足 strict；
- `authenticated_instance_id` 只有在可信部署控制面将实例声明绑定到证书/SVID/注册表时才非空。

Go 当前 `hostname+pid+random` 等自生成值定义为 `process_instance_id`，仅用于 lease、去重和运维关联，不是安全身份。请求 header、自报 payload 或 process id 不得认证 callback、执行方或实例。基础设施只能证明 service 时，`authenticated_instance_id=null`；仅当 `require_instance_identity=true` 时空值才 fail-closed。

`DelegatedSubject` 与 workload 分离，字段及唯一权威来源如下：

| 字段 | strict 权威来源 |
|---|---|
| `agent_id` | 已验签 token/Task target，并受 workload registry 可代表范围约束 |
| `user_id` | 原始认证上下文或签名 claim；无真实用户时为 null，禁止 initiator-as-user |
| `tenant_id` | registry、Task 与 token 三方一致 |
| `request_id` | 初始入口生成并持久化，后续只透传 |
| `task_id` | Task store |
| `call_id` | LC 生成并绑定 request/task |
| `decision_id` | Decision store |
| `delegation_jti` | 已验签 token 的 jti；不得记录完整 token |

strict delegation token 必须绑定 `target_workload_id`；只有要求实例身份时再绑定 `target_instance_id`。实际 TLS workload/instance、claim、registry、Task 或 tenant 任一错配即拒绝。

### 4.2 凭证模型

`ToolCredentialRef` 仅含 `name/scope/tenant_id/key/version_mode/version/injection` 等非敏感策略标识。`version_mode` 只能为：

- `pinned`：必须给出具体版本，current alias 切换不影响该引用；
- `current`：版本为空或字面值 current，每次调用开始时原子解析为具体版本。

Broker 在注入前产出不可变 `ResolvedToolCredentialRef`。在途调用固定 `resolved_version`；撤销后，新调用和尚未注入的调用 fail-closed。strict 按 tenant/name/key/版本精确查找，tenant scope 禁止 global fallback；显式 global ref 必须绑定 `allowed_tools` 与 `tenant_allowlist`。Agent 不得控制 scope、tenant、injection 或 version mode。Secret 明文不得进入 Agent env/payload/文件、日志、审计、task DB、token、错误或 evidence。

compatibility 可保留 v0.52 file/memory backend 的 tenant→global 行为，但必须标为 `not_strict`；tenant 版本错配在任何模式都不得回退。

### 4.3 ExecutionReceipt

所有出口返回 v0.53.0 typed `ExecutionReceipt` envelope，共同字段至少包括：`receipt_id/type`、`attester_workload_id`、可空 authenticated instance、全量 correlation、executor/backend、credential ref digest、resolved credential version 或摘要、终态、`result_sha256` 与 `issued_at`。成功、错误、超时、取消都必须有真实终态；执行前拒绝由拒绝方返回失败 receipt/error envelope，不虚构下游执行。

三类 receipt 的证明边界严格区分：

| receipt_type | 签发方 | 证明边界 |
|---|---|---|
| `controller_execution_record` | LC protected HTTP executor | 证明 LC 受控解析并发送，不证明 SaaS 内部执行 |
| `proxy_attestation` | 经 mTLS 认证的 MCP Proxy | 证明 Proxy 转发及其观察结果，不证明 upstream 内部执行 |
| `remote_execution_attestation` | 经 mTLS 认证的 remote Harness | 证明 Harness 执行和 effective sandbox/profile，不等于硬件或部署证明 |

JSON 结果按 RFC 8785/JCS canonical JSON 的 UTF-8 bytes 做 sha256；文本和二进制分别按原始 UTF-8 bytes/原始 bytes，并携带 media type 与 encoding。LC 与 Go 必须复算一致后才能标记 completed。

### 4.4 系统不变量

1. strict 配置、身份、出口、receipt 或 required capability 任一缺失必须 fail-closed。
2. strict 禁止 local function、stdio、旧 SSE、subprocess、isolated subprocess、Docker command wrapper 和所有 local fallback。
3. workload 与 delegated subject 分别记录、分别校验，不得互相推导。
4. registry、token、Task、工具和 Secret tenant 必须一致。
5. request_id 在入口、判定、委托、Harness 与 lifecycle 全链不变。
6. lifecycle callback 只接受专用 kernel service workload；逻辑 source/target agent 仍是 delegated subject。
7. compatibility 保留 v0.52 peer 行为，但 readiness 必须明确 `not_strict`，不得伪装生产强保证。

---

## 5. strict 出口、A2A 与部署闭环

### 5.1 出口 profile

`execution_security.mode` 默认 `compatibility`。strict 仅允许：

- LC 持有引用并具唯一目标网络权限的 protected HTTP executor；
- 独立部署、独立持有凭证的 protected MCP Proxy；
- remote HTTPS+mTLS Harness。

配置加载和 Runtime 组装阶段集中验证实际支持出口集合。任一工具可能解析到禁用 executor、default fallback、未认证或不健康 backend 时启动失败；运行中 backend 失效则调用 fail-closed、readiness 503。

### 5.2 Protected MCP contract

v0.53.0 strict transport 固定为 **MCP Streamable HTTP + HTTPS+mTLS**。Proxy 以 URI SAN 暴露 workload identity，接收 typed delegated subject 与完整 correlation，仅从可信工具规格选出的 credential ref 解析凭证，通过独立身份和 allowlist 访问 upstream，并返回 `proxy_attestation`。网络策略必须阻止 Agent 和 LC 绕过 Proxy 直连 upstream；Agent 不得获得 upstream 地址或凭证。stdio 与旧 SSE 仅 compatibility。合同未完整实现前不得声明 `protected_mcp_network_v1`，也不得将 MCP 列入 strict supported 集合；HTTP/Harness-only strict 可独立 ready。

### 5.3 A2A strict 闭环

调用链固定为：`Go Kernel target executor → Python /v1/govern/tool-call → strict executor resolver → protected MCP/HTTP/remote Harness`。

- target Agent 部署在无目标工具直连权限的网络，只能访问治理入口。
- Go→Python 使用独立 HTTPS+mTLS workload auth，不得复用或回退 interaction token/Bearer。
- v0.53.0 typed request 携带并逐项校验 request/interaction/decision/task/call/jti/tenant/target workload；按配置携带 target instance。
- Python 必须命中 strict resolver，不得进入 compatibility/default/local fallback。
- Go 验证 receipt 的认证来源、全量 correlation、终态与结果摘要后才可 completed。
- workload、tenant、token、correlation 或 capability 任一错配均以稳定错误码 fail-closed。

### 5.4 部署 conformance 与观察

Docker strict 基线：非 root 固定 UID/GID、read-only rootfs、cap-drop ALL、no-new-privileges、PID/CPU/memory 限额、无 host namespace/socket/root mount；Agent 网络只允许治理入口，Secret 只挂受信 executor/Harness。

Kubernetes strict 基线：`runAsNonRoot`、固定 UID/GID、read-only rootfs、禁止提权、drop ALL、seccomp RuntimeDefault、resources/PID、默认拒绝 NetworkPolicy、最小 ServiceAccount 与 Secret mount 分离。

`deployment_conformance` 是 CI/release manifest 静态检查和环境内负向测试制品，不是在线身份。`runtime_observation` 仅报告当前进程可直接观察的 UID、rootfs、capabilities、no-new-privileges 等事实，不能靠 profile/env 自证 NetworkPolicy 或 Secret 未挂载。`runtime_isolation.status` 仅为 `observed|externally_attested|unknown`；无可信外部 proof 时最高 observed。`require_external_deployment_attestation` 默认 false；若为 true，必须验证信任根、freshness、环境/镜像或 manifest digest 与 workload 绑定，否则 fail-closed。

---

## 6. Security Capability Negotiation

能力协商是独立 strict 门禁，不从 v0.53.0 版本号推断。即使请求双方都是 0.53，仍必须由 strict 请求发送 `required_security_capabilities`，经认证响应声明 `supported_security_capabilities`，并同时满足：

1. required 是 supported 的子集；
2. 本次调用所需实际字段全部存在；
3. 每个字段通过类型、绑定关系和语义校验。

冻结能力名：`workload_identity_v1`、`delegated_subject_binding_v1`、`workload_bound_delegation_token_v1`、`execution_receipt_v1`、`tenant_secret_no_fallback_v1`、`protected_mcp_network_v1`（仅启用 MCP 时要求）、`deployment_observation_v1`；未来外部 proof 使用独立可选 `deployment_proof_v1`。

旧 v0.52 peer、缺 required capability、声明能力但缺字段或语义不满足时返回 `required_security_capability_unavailable`。只能走显式 compatibility 并报告 `not_strict`，不得按版本字符串、patch、字段可选性静默降级。

---

## 7. 审计、API、迁移与 readiness

统一审计字段覆盖 request/interaction/decision/task/call/jti、workload/authenticated instance/process instance、delegated agent/user/tenant、executor/backend、receipt/type/status/hash。逻辑 actor 与 workload 分列；process instance 仅用于非安全关联。JSONL 仍为权威追加载体，SQLite audit index 以幂等可空列/关联表迁移并支持从旧 JSONL 重建。任何 Secret、Bearer、完整 delegation token、private key 不得进入 payload。

v0.53.0 扩展 `ExecutionContext`、lifecycle、`/v1/govern/tool-call`、`/health`、`/ready` 与必要审计查询。旧数据库和旧 JSONL 在 compatibility 下继续可用，缺字段按 null 处理。trusted workload 首期使用启动配置，不新增角色管理 API。

readiness 分项报告 supported egress、credential isolation、runtime isolation、workload identity、A2A target execution、security capabilities 与 audit correlation。总体状态只用 `ready|degraded|not_strict|unknown`；runtime assurance 独立使用 `observed|externally_attested|unknown`。strict 只评估配置启用的出口；未启用 MCP 不阻止 HTTP/Harness-only strict ready，启用未完成出口则启动失败。响应不得泄漏证书全文、token、header、Secret 路径或挂载内容。

固定运行顺序：TLS peer 认证 workload → 能力协商 → 解析 delegated subject 与 target binding → 校验 registry/token/Task/tool/ref tenant → 执行既有策略和 v0.52 授权事实 → 建立 correlation → 选择唯一 strict backend 并解析 ref → 执行并获取 typed receipt → 校验来源/correlation/终态/hash → 脱敏审计。任一步失败都保留真实失败终态和非敏感 reason code。

---

## 8. 实施任务

### P53-01：执行安全模型冻结

- [x] 冻结 WorkloadIdentity、DelegatedSubject 权威来源、ToolCredentialRef、三类 ExecutionReceipt 与 capability DTO。
- [x] 明确管理 principal、逻辑 Agent、workload、process/authenticated instance 的边界。
- [x] 建立工具到 executor/backend/fallback/Secret 来源的机器可测清单，并证明 compatibility 零行为变化。

### P53-02：SecretBroker 与凭证引用

- [x] 实现 pinned/current、原子 resolved version、撤销前复验与 immutable in-flight 语义。
- [x] strict tenant 精确查找无 global fallback；显式 global 强制 tool/tenant allowlist。
- [x] Agent 覆盖 scope/injection/tenant/version mode 或提交明文凭证均拒绝。
- [x] 自动化 canary 覆盖 env/payload/log/audit/task DB/evidence 泄漏扫描。

### P53-03：三类受保护出口与回执

- [x] strict 只选择已完成并声明支持的 protected HTTP、remote HTTPS+mTLS Harness、protected MCP。
- [x] MCP 使用 Streamable HTTP + HTTPS+mTLS；stdio/旧 SSE 仅 compatibility。
- [x] 三类 receipt 覆盖四种终态、完整 correlation 和当前确定性 JSON 子集 sha256。
- [ ] local/subprocess/wrapper/fallback 已有自动化 fail-closed；真实环境内 upstream/目标直连门禁待执行。

### P53-04：workload identity 与 delegation token

- [x] URI SAN/TLS peer 映射 workload；process id/header 不参与认证。
- [x] authenticated instance 仅来自可信部署绑定，并按开关处理空值。
- [x] DelegatedSubject 遵循权威来源；token 绑定 target workload/可选 instance。
- [x] registry/Task/token/Secret tenant 和代表范围错配拒绝；只审计 jti。

### P53-05：A2A 与 lifecycle strict 闭环

- [x] callback 和 target execution 只接受登记的 kernel service workload。
- [x] Go→Python strict 使用独立 HTTPS+mTLS，不回退 interaction token。
- [x] v0.53.0 typed 请求校验全量 correlation/tenant/target workload。
- [x] 经 strict resolver 到受保护出口，receipt 验证前不得 completed。

### P53-06：Runtime 与部署 strict profile

- [x] schema 表达模式、支持出口、身份、credential refs、receipt、capability 与 proof 开关。
- [ ] Docker/Kubernetes 基线与静态 deployment conformance 已落地；环境内负向测试待发布环境执行。
- [x] runtime observation 与外部 attestation 分离；Windows 报告 not_strict。
- [x] 不引入容器或 Kubernetes 编排器实现。

### P53-07：审计关联与索引

- [x] execution 补齐 decision、tenant，并统一 correlation/workload/delegated/executor/receipt 字段。
- [x] 任一 correlation ID 可查全链，三类 receipt 证明边界准确。
- [x] SQLite 迁移幂等、JSONL 重建及 token/Secret 泄漏自动化覆盖。

### P53-08：防绕过与隔离测试

- [ ] 代码级禁用路径与静态网络策略已覆盖；真实 Agent/LC 直连 upstream、目标及 Secret mount 负向门禁待执行。
- [x] 覆盖 mTLS/instance header 冒充、delegated/tenant/target mismatch 和伪造 lifecycle。
- [x] 覆盖 A2A token 不回退、Secret no-fallback、旧 v0.52 peer capability fail。
- [x] 覆盖三类 receipt 四种终态及当前确定性 JSON 子集/文本/二进制 hash；完整 RFC 8785 不在实现范围。

### P53-09：配置、health 与 readiness

- [x] compatibility 默认零变化且始终 not_strict。
- [x] readiness 按实际启用出口及 A2A、capability、receipt、runtime assurance 判定。
- [x] strict 任一启用维度失败返回 503；旧 v0.52 peer 返回稳定 capability 错误。
- [x] 所有响应完成敏感信息净化。

### P53-10：文档、协议与 v0.53.0 发布收口

- [x] 实施结果与真实代码一致，不夸大 receipt、instance、runtime observation 或部署 proof。
- [x] Python package version、Go `CurrentProtocolVersion`、OpenAPI、delegation token wire、ExecutionReceipt schema、MCP contract 与 capability negotiation 全部同步为 v0.53.0。
- [x] 生成并校验 v0.53.0 package/protocol/OpenAPI/contract；未生成活动 v0.51 契约或发生版本倒退。
- [x] 按真实完成范围更新 README 与 KNOWN_LIMITATIONS；历史 development 文档未改版本。
- [x] Python 全量测试、mypy、Ruff、Go 全量测试、vet 与 race 已在收口中复验；支持环境 strict 发布门禁待执行。

---

## 9. 测试与发布门禁

- Secret canary 扫描 Agent env、payload、stdout/stderr、日志、JSONL、SQLite、task/decision/approval store 和 evidence；任一明文命中即失败。
- 从隔离环境内部验证非 allowlist 网络、metadata、目标工具、upstream MCP、Harness 直连失败；读取 LC DB/Secret/宿主目录、写 rootfs、提权、Docker socket、超 PID/资源限制均失败。
- 对三类 receipt 分别覆盖成功、错误、超时、取消；校验签发 workload、envelope、证明边界、correlation 和结果摘要。
- 用键序、Unicode、数值边界验证 JCS；文本/二进制按原始 bytes 验证。LC/Go 不一致时不得 completed。
- 双方同为 v0.53.0 仍验证 required subset supported 和实际字段；旧 v0.52 peer、缺能力、伪声明能力均稳定失败且不降级。
- 从任一 correlation ID 查询同一有序时间线；删除 audit index 后从 JSONL 重建结果一致。
- 运行 Python 和 Go 全量测试、静态检查、现有策略/审批/RBAC/审计/Harness 回归及 compatibility e2e。
- strict 发布门禁必须在支持 Docker/Kubernetes 网络与安全策略的环境执行，并留存 deployment_conformance 制品；普通 CI 缺环境不能豁免正式发布。

---

## 10. 迁移、回滚与限制

迁移按 v0.53.0 执行：先以 compatibility 部署 v0.53 peer 并盘点 readiness；迁移真实 Secret 至 backend/Proxy/Harness；完成 protected HTTP/Harness，MCP 合同完整后才加入支持集合；配置 workload mapping、DelegatedSubject 来源与 target workload token binding；切换 Go→Python v0.53.0 typed wire、独立 workload auth、receipt 和 capability negotiation；预生产完成全部负向测试后显式切 strict。SQLite 只增加可空字段/索引，Secret 在任何回滚中都不得返回 Agent env。

回滚只能切回向前兼容二进制或显式、受审计的 compatibility，保留新增数据库列并继续保护网络与 Secret。不得以 local/stdio/旧 SSE/fallback、interaction token 复用、global Secret fallback 或伪造 capability 作为 production strict 兜底。

KNOWN_LIMITATIONS 实施后按真实范围更新：LC 仍不内置编排器；deployment conformance 不是在线身份；无外部 proof 时 runtime assurance 最高 observed；controller/proxy receipt 不证明远端内部行为；Go tenant 授权不做全面重构；MCP 管理 RBAC 合并仍是独立边界。文案必须区分完全解决、部分解决与保持不变。

---

## 11. 总体 DoD

- [ ] 原 v0.51 五项均有代码、配置、部署和测试证据，且不声称 v0.51 已发布。
- [ ] v0.52 管理身份、tenant、roles、OIDC/JWKS、RBAC 与实名审计被准确复用。
- [ ] workload、管理 principal、逻辑 Agent、DelegatedSubject、process/authenticated instance 分离。
- [ ] strict token 强制 target workload binding；tenant 全链一致；Secret pinned/current 和 no-fallback 正确。
- [ ] strict 只使用完整实现并启用的 protected egress；A2A 独立认证并在 receipt 校验后 completed。
- [ ] Protected MCP、三类 receipt、deployment conformance/observation 与能力协商均按本文合同验证。
- [ ] security capability negotiation 独立于 v0.53.0 版本判断；同版本也执行 required subset supported 与字段语义校验，旧 v0.52 peer 仅 compatibility。
- [ ] 审计全链可查且无 token/Secret；迁移与 JSONL 重建幂等。
- [ ] compatibility 零行为变化并明确 not_strict；strict 降级返回 503。
- [ ] v0.53.0 Python/Go protocol、OpenAPI、delegation token、ExecutionReceipt、MCP contract、capability negotiation 与发布元数据同步。
- [ ] 全量测试和支持环境 strict 门禁通过；历史 development 文档不要求改版本。
- [ ] 未生成活动 v0.51 package/protocol/OpenAPI/contract，未发生版本倒退。

---

## 12. 实施结果

- 执行安全模型与配置已落地：`executors/base.py`、`execution_security.py`、`execution_security_constants.py`、`config/execution_security.yaml` 定义 workload/delegated identity、credential ref、capability、receipt、strict resolver 与 assurance。
- SecretBroker/Resolver 已实现 tenant 精确解析、pinned/current 固定版本、global 显式 allowlist、撤销复验与无明文 DTO；HTTP、remote Harness、Protected MCP 三类出口接入 typed receipt。
- Protected MCP 使用 Streamable HTTP + HTTPS/mTLS、peer URI SAN/指纹绑定、`io.loop-controller/protected-mcp-v1` `_meta` 合同和 proxy attestation；stdio/旧 SSE 保持 compatibility。
- Go A2A 已透传并绑定 request/interaction/decision/task/call/jti/tenant/target workload/instance，strict Go→Python 执行和 lifecycle 使用独立认证并校验 capability/receipt 后才完成任务。
- Runtime health/readiness、审计关联索引、deployment observation/proof、Docker/Kubernetes 静态 conformance 与相应测试已落地。
- package、Python/Go 协议常量、活动配置、`contract/a2a_v0.53.0.json`、`openapi/a2a_v0.53.0.yaml` 和共享 schema/path 已同步 0.53.0；保留 v0.52 历史制品。
- compatibility 仍为默认且不宣称 strict；完整本地 uvicorn mTLS、真实 Docker/CNI/Kubernetes 负向 gate 和外部 deployment attestation 仍是发布环境门禁。receipt JSON canonicalization 仅为项目确定性子集，不宣称完整 RFC 8785。

---

## 13. 验证记录

截至发布收口前，v0.53 功能测试已覆盖 Secret canary、tenant/no-fallback、mTLS/workload/instance/delegated mismatch、A2A token no-fallback、三类 receipt 终态与摘要、Protected MCP 合同、审计重建、readiness/runtime assurance 及 Docker/Kubernetes 静态 conformance。

本次收口已执行 `uv lock` 并同步锁文件；`uv run pytest -q` 为 1080 passed、4 skipped，`uv run mypy --no-incremental src` 检查 120 个源文件无错误，`uv run ruff check src tests` 与 `git diff --check` 通过；Go 的 `go test ./...`、`go vet ./...`、`go test -race ./...` 全部通过。收口同时修复 Windows 低分辨率时钟导致的 Go task ID 碰撞，生成 ID 增加进程内原子序号。

本机发布验收已使用临时 CA 和 SPIFFE URI SAN 证书启动真实 uvicorn HTTPS/mTLS remote Harness：可信 CA 客户端证书成功访问 `/health` 并返回 HTTP 200，缺失客户端证书和非信任 CA 客户端证书均在 TLS 层被拒绝。Docker Compose 配置解析通过；Kubernetes 与 Compose strict 清单经 deployment conformance 校验均为 `conformant`。Runtime 已加固为只有读取真实 DeploymentProof 并通过签名、新鲜度、有效期及 workload/instance/profile/environment 绑定校验后，才声明 `deployment_proof_v1` 和 `externally_attested`；缺失、篡改、过期、过旧、未来签发或无时区 proof 均 fail-closed。

本机 Docker daemon 未运行，kubectl 也无可用 Kubernetes API server，因此真实容器/CNI 网络隔离、Agent/LC 绕过受保护出口、Secret mount/rootfs/提权/Docker socket/资源限制负向测试仍须在支持环境执行。Protected MCP 完整 Streamable HTTP 会话、Go→Python strict 全链和外部发布控制面签发 proof 的真实拓扑仍保持为发布环境门禁；总体 DoD 与 P53-08 环境项继续保持未勾选，不声称全部发布门禁完成。
