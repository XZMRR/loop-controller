# Loop Controller 前端开发规划

> 分支：`develop`（原 `frontend/main` → `frontend/r15-port` → 已改名合并为 `develop`）
> 目录：`frontend/`
> 目标：为公司展示构建一个干净、简洁、实用的 Loop Controller 管理控制台
> 版本基线：后端 `v0.54.0`

---

## 1. 项目目标

构建一个基于 Web 的管理控制台，让技术和管理人员能够：

- 实时查看 Agent、工具、审批、审计、系统健康状态；
- 对高风险工具调用进行 approve / deny，并填写审批意见；
- 通过 SSE 实时接收新的审批请求；
- 对 Agent / User / Tool / Secret 执行吊销，触发 Kill Switch；
- 查看系统配置（Agent、Profile、Identity、Entrypoints），后续支持编辑；
- 为 A2A / Go Kernel 治理预留扩展页面。

风格要求：**干净、简洁、实用**，不做花哨动画和夸张宣传语。

---

## 2. 技术栈

| 层级 | 选型 | 说明 |
|---|---|---|
| 框架 | Vue 3 | 响应式、组件化、国内团队熟悉 |
| 语言 | TypeScript | 类型安全，便于维护 |
| 构建工具 | Vite | 快速热更新、简洁配置 |
| UI 组件库 | Element Plus | 管理后台组件丰富，中文文档友好 |
| 状态管理 | Pinia | 官方推荐，TypeScript 支持好 |
| 路由 | Vue Router 4 | 单页应用路由 |
| HTTP 客户端 | Axios | 拦截器统一加 API Key |
| YAML 解析 | js-yaml | 读取本地 `config/*.yaml` 做静态展示 |
| 图标 | @element-plus/icons-vue | 与 Element Plus 风格一致 |

---

## 3. 目录结构

```
frontend/
├── docs/                          # 开发文档
│   ├── frontend_development_plan.md
│   └── frontend_api_gaps.md
├── public/                        # 静态资源
├── src/
│   ├── api/                       # API 封装
│   │   ├── client.ts              # Axios 实例与拦截器
│   │   ├── python.ts              # Python Runtime Admin API
│   │   ├── a2a.ts                 # Go A2A Kernel API（预留）
│   │   └── config.ts              # 本地 YAML 配置读取
│   ├── assets/                    # 图片、样式
│   ├── components/                # 公共组件
│   │   └── Layout.vue             # 侧边栏 + 顶部栏布局
│   ├── router/
│   │   └── index.ts               # 路由配置与登录守卫
│   ├── stores/
│   │   └── auth.ts                # API Key / 后端地址状态
│   ├── types/
│   │   └── index.ts               # 公共类型（可选）
│   ├── views/                     # 页面视图
│   │   ├── Login.vue
│   │   ├── Dashboard.vue
│   │   ├── Approvals.vue
│   │   ├── Agents.vue
│   │   ├── Tools.vue
│   │   ├── Audit.vue
│   │   ├── Settings.vue
│   │   └── A2A.vue
│   ├── App.vue
│   └── main.ts
├── .gitignore
├── index.html
├── package.json
├── tsconfig.json
├── tsconfig.node.json
└── vite.config.ts
```

---

## 4. 功能阶段规划

### 当前阶段定位

底座阶段（管理配置闭环、审批与审计闭环、A2A 接入）已完成并稳定运行，
当前处于**治理页面扩展与质量收口阶段**：

1. **治理页面扩展**（Mock 数据源先行，契约由后端源码核实）
   - 死信队列治理、RBAC 绑定管理、Policy 生命周期等治理页面，按
     `契约层 types.ts + Mock 数据源 + 视图 + vitest 用例` 的标准模式交付。
   - 后端已定义的契约直接读源码核实（server.py 路由与 payload 构造函数）；
     后端未定义的契约（如管理台审批 SSE）按未定契约推进，前端做轮询兜底。
2. **质量收口**
   - SSE 推送替代轮询、Playwright E2E 骨架、空态与错误态统一。

### 已确认的技术决策

- **数据源抽象**：页面只依赖 `DataSource` 接口（`src/api/<domain>/{types,mock}.ts`），
  Mock 单例经 `src/api/<domain>/index.ts` 导出；后端契约就绪后仅需新增 Http 实现
  替换单例，视图与测试零改动。
- **鉴权**：Session Token 登录（v0.54 安全基线）——API Key 换取 session 后即弃，
  axios 拦截器携带 Bearer；401 统一登出跳登录页。
- **数据归属**：RBAC / Policy / 审计等数据 100% 在 Python runtime 侧，Http 实现
  走 `pythonClient`（/api/python），不复用 a2aClient；Go 内核数据仅 A2A 治理页使用。
- **SSE 基础设施**：`api/sse.ts createEventStream`（游标续传/退避重连/401 处理）
  统一服务任务流与审批推送；纯函数在 `api/sse-parse.ts`，node 可测。
- **E2E 策略**：Playwright + `page.route` stub 全部后端依赖，不依赖 Python/Go 进程；
  取消 stub 即联调模式。
- **多 Agent 底座**：数据模型、API 契约、页面筛选与权限设计均按多 Agent、
  多 Profile、多 Owner、多租户可扩展预留。
- **安全原则**：Identity、Secret、Token 默认脱敏不回显明文；管理操作必须写审计；
  前端 YAML fallback 逐步收紧。

### 进度记录（第二轮）

- **GitHub 推送问题已解决**：根因是全局 git 代理指向未运行的 `127.0.0.1:7890`；`gh` 完成登录（keyring）后，绕过代理直连可正常推送。后续如需长期避免，应清除全局 proxy 配置或保持代理常驻。
- **审批与审计闭环基础能力落地**：新增 `GET /v1/admin/approvals`（分页契约 `approvals/total/limit/offset`，支持 `status/agent_id/tool_name/requester_id/approver_id` 筛选），审计接口补齐 `agent_id`/`tool_name` 筛选；前端审批页升级为“审批中心”（待审批/历史双 Tab + 筛选 + 分页）。后端 61 测试全绿。
- **Agent 详情接口落地（多 Agent 底座）**：新增 `GET /v1/admin/agents/{agent_id}`，返回 `AdminAgentDetail`（在列表字段基础上扩展 `description`、`metadata`），404/401/503 语义完整；前端 Agent 管理页新增“详情”操作列与 Drawer 详情视图，接口不可用时回退列表行数据。后端 67 测试全绿，前端构建通过。
- **实现注意**：审批历史查询不改 `ApprovalStore` Protocol，只给 `JSONLApprovalStore` 具体类加只读 `requests`/`responses` 视图属性，server 端 `getattr` 防御，避免对所有 store 实现造成兼容压力；verdict 序列化用 `getattr(record.verdict, "value", record.verdict)` 兼容枚举与字符串。

### 进度记录（第三轮：Profile 工具策略在线编辑与统一 reload）

- **后端新增接口**：`PUT /v1/admin/profiles/{profile_id}/tools`（工具权限整体替换：先经 `ToolPermission` 模型校验，再原子写回 `profiles.yaml`，随后从磁盘重载并原地刷新运行时共享映射，全程写审计）；`POST /v1/admin/profiles/reload`（放弃内存修改，从磁盘统一重载全部 Profile）。
- **统一 reload 底座**：`HotReloader` 纳入 `profiles.yaml` 监视，文件被手工编辑后按轮询周期自动热更新；`Runtime` 新增 `config_dir` 字段供管理接口定位配置文件；`ConfigLoader.reload_profiles()` 与既有 http/harness/revocation 重载方法同构。
- **写回实现**：新增 `infra/profile_config.py`，采用“加载-修改-全量写回 + 紧凑序列化（省略默认值）”，已知取舍是首次写回后文件注释与排版不保留（PyYAML 限制），已在页面提示。
- **前端**：工具策略页升级为在线编辑（每 Profile“编辑”对话框：允许/需审批开关、调用上限、参数黑白名单 JSON 编辑器、增删工具），保存即热更新；“从磁盘重载”按钮对应统一 reload；数据加载优先在线 API，失败回退静态 YAML。
- **验证**：后端 72 个 server 测试全绿（新增 5 个：写回+热更新+审计、未知 Profile 400、非法权限 400 且不落盘、磁盘直改后 reload 同步、无 config_dir 503）；全量 869 通过（3 个 go_kernel 集成测试错误为既有环境问题，与本轮改动无关，已 stash 基线复核）；前端构建通过。

### 进度记录（第四轮：身份、审计、配置底座收口）

- **最小后端 Session 登录落地**（对应已确认决策“短期保留 API Key 联调，同时补最小 Session 登录”）：新增 `POST /v1/admin/session/login`（验证 API Key 后签发 8 小时随机 token）与 `POST /v1/admin/session/logout`（吊销当前 token）；`_check_api_key` 接受 `Authorization: Bearer <session-token>` 作为 API Key 的替代凭据，审计 actor 区分 `api-key:` 与 `session:` 前缀。存储为进程内存（重启全失效，接口预留持久化替换空间），登录/登出均写审计。
- **前端登录改造**：登录时优先换取 Session Token（旧后端无该端点自动降级 API Key 直连）；axios 拦截器优先携带 Bearer Session Token；退出时先调用后端吊销再清理本地状态。
- **审计底座现状**：管理侧所有变更操作（吊销、Kill Switch、审批决策、Profile 编辑/重载、Session 登录/登出）均落审计，actor 可区分来源。
- **配置底座现状**：Profile 在线编辑 + 统一 reload + profiles.yaml 热更新已闭环；Identity/Entrypoints 仍为只读脱敏展示（热更新字段拆分未做，属长期项）。
- **验证**：后端 76 个 server 测试全绿（新增 4 个 session 用例）；前端构建通过。

### 进度记录（第五轮：路由守卫收口 + A2A 接入第一步）

- **路由级权限守卫**：路由守卫此前已存在（未登录跳 `/login`、已登录访问登录页跳首页），本轮补齐缺口——axios 响应拦截器统一处理 401：清理本地凭据、3 秒防抖提示并跳回登录页；登录页自身的 401 不触发跳转，避免循环。
- **A2A 接入第一步（治理视角，不依赖 Go 内核在线）**：
  - `GoKernelBridge` 新增 `ping()`（GET /a2a/v1/agents 探测可达性）与 `list_agents()`（兼容列表/包装两种响应）。
  - 新增管理接口：`GET /v1/admin/a2a/status`（启用/可达/地址/本地 Agent Card 配置）、`GET /v1/admin/a2a/agents`（配置 Agent 与内核注册态合并视图，内核不可达时 registered=null 语义明确）、`GET /v1/admin/a2a/tasks/{task_id}`（任务状态查询；内核未启用 fail-closed 返回 503）。
  - 前端 A2A 治理页从占位升级为三张卡：内核状态（含未启用/不可达引导提示）、Agent 注册状态表、任务查询。
  - 为后续预留：Delegation / route_message / cancel_task 的 bridge 方法已存在，下一步可做管理端发起委托与任务取消。
- **验证**：后端 80 个 server 测试全绿（新增 4 个 A2A 用例）；前端构建通过。

### 进度记录（第六轮：管理端发起委托入口）

- **后端**：新增 `POST /v1/admin/a2a/delegations`。管理端发起的委托与 Agent 自发委托走完全相同的治理路径：`InteractionGovernanceEngine.evaluate()`（Profile/信任/委托深度校验 → OPA interaction 策略）→ allow/modify 后经 Go 内核 `request_delegation` 派发；require_approval 返回升级对象由人工跟进；deny 不派发。全程写交互审计（`build_audit_event` 同一语义，提案标记 `interaction_context="admin-console"` 便于区分来源）。引擎支持构造注入（`build_app(interaction_engine=...)`），默认懒构造。
- **前端**：A2A 治理页新增“发起委托”卡片——发起/目标 Agent 下拉、能力名、风险等级、参数 JSON；结果区展示判定（allow/modify/require_approval/deny）、原因、Decision/Interaction ID、升级对象与派发信息；require_approval 给出人工跟进提示。
- **验证**：后端 84 个 server 测试全绿（新增 4 个：allow 派发、deny 跳过派发、未知 Agent 400、无内核时派发标记跳过）；前端构建通过。

### 进度记录（第七轮：Go 内核启动与完整链路验证）

- **环境就绪**：下载 OPA 1.19.0 至 `tools/opa.exe`（本机 8181 已有 OPA 实例运行且加载 `policies/interaction/default.rego`，直接复用）；Go 模块经 `GOPROXY=https://goproxy.cn,direct` 绕过被墙的 proxy.golang.org 完成编译。
- **Go 内核启动命令**：`go run ./cmd/kernel -addr :8080 -development -discovery-file ..\config\a2a_agents.yaml -interaction-url http://127.0.0.1:8000 -interaction-token dev-token-researcher-001`。
  - `-discovery-file` 静态注册 `loop-controller-local` 与 `research-agent` 两张 Card；
  - `-interaction-token` 必须用 `config/identity.yaml` 静态 token 表中的 Agent 凭证（`dev-token-researcher-001`），authorize 端点要求有效 AgentIdentity 且 `source_agent_id` 与身份匹配（401/403 的根因均是凭证不匹配）。
- **Python 服务启动**：`lc server --port 8000` 需 `LOOP_CONTROLLER_API_KEY` 与 `LOOP_CONTROLLER_AUDIT_HMAC_KEY`；启动时自动向 Go 内核注册本地 Card（201）。注意 `data/audit.jsonl` 若含旧 HMAC key 签名的记录会导致 Store 阻断，需备份重建（已备份为 `*.bak-20260915-194620`）。
- **联调中修正的两个语义问题**：
  1. 管理端委托 handler 预先拒绝"不在本地 config.agents 的 target"——但 A2A 委托的 target 常是仅注册在 Go 内核的外部 Agent；已移除该预检，交由治理引擎通过 Agent Card 查询兜底（测试同步改为"unknown target 委托给引擎"）。
  2. Go 内核语义：请求带 `task_id` 时该任务必须已存在于内核（pending），否则 `delegation task not found` 403；新委托应留空 `task_id`，由内核创建并返回。
- **完整链路验证通过**：Session 登录 → `POST /v1/admin/a2a/delegations`（researcher_001 → research-agent / analyze_sales）→ IIGE 治理 allow（profile → capability → 深度 → trust → OPA interaction 策略）→ Go 内核二次 authorize（200）→ 任务创建 accepted=true 返回 task_id → `GET /v1/admin/a2a/tasks/{task_id}` 查到 pending 任务（research-agent :8001 无真实执行端，development 模式不实际派发，符合预期）。全程审计落盘。
- **验证**：`test_server.py` + `test_delegation_authorizer.py` 共 94 测试全绿。

### 进度记录（第八轮：任务生命周期闭环——取消与 SSE 流式）

- **后端新增两个管理接口**（[server.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/server.py)）：
  - `POST /v1/admin/a2a/tasks/{task_id}/cancel`：透传 `bridge.cancel_task`，内核不可达/拒绝返回 502，无 bridge 503（fail-closed）；
  - `GET /v1/admin/a2a/tasks/{task_id}/stream`：`StreamingResponse` SSE 转发 `bridge.stream_task`。
- **前端**（[python.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/python.ts)、[A2A.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/A2A.vue)）：
  - `cancelA2ATask` + `streamA2ATask`（fetch + ReadableStream 实现 SSE，因 EventSource 无法带 Authorization 头；自动解包内核事件信封取 `payload` 任务快照，收到终态自动断开）；
  - 任务查询卡：状态 tag、非终态任务的"取消任务"按钮、"订阅状态流/停止订阅"按钮、订阅中标识；组件卸载自动断开流。
- **联调发现的语义细节**：Go 内核终态拼写为 `cancelled`（双 l），前端终态集合已兼容两种拼写。
- **完整链路验证**：发起委托 → task pending → 管理端 cancel → 任务 `cancelled`；SSE 流依次收到 `task_created`(pending) 与 `task_cancelled`(cancelled) 两帧。
- **验证**：`tests/test_server.py` 86 测试全绿（新增 cancel 200/502/503 与 stream SSE 内容/503）；前端构建通过。

### 进度记录（第九轮：require_approval 委托挂接审批台）

- **闭环设计**：管理端委托判定为 `require_approval` 时，handler 自动构造审批单提交审批台（`call_id=a2a-delegation:{interaction_id}`，`tool_arguments` 保存委托快照明文供批准后重建）；`POST /v1/admin/approvals/{id}/approve` 批准此类审批单后自动重建 `DelegationRequest` 派发到 Go 内核，响应附 `dispatch` 字段；内核回调 `/interaction/v1/delegations/authorize` 二次评估仍为 require_approval 时，Python 按 call_id 查审批记录，已批准则翻转为 allow。
- **防篡改**：翻转除 call_id 外还校验委托快照与 authorize 请求一致（目标 Agent、工具名、参数），防止复用已批准 interaction 放行被篡改的委托（新增专项测试覆盖一致/篡改/错目标三种情形）。
- **联调修复的三个跨层问题**：
  1. Go `deriveLineage` 原拒绝无 `parent_task_id` 的 `parent_interaction_id`；该字段为纯信息元数据（无父任务时不授予任何特权），已放行根委托携带并透传 authorize（新增 `TestRootDelegationCarriesParentInteractionID`）。
  2. Go `DelegationResponse` 缺 `escalation_target` 字段，authorize 的 require_approval 响应被 `DisallowUnknownFields` 拒绝（400 delegation_failed）；已补字段。
  3. `JsonlApprovalStore` 缺 `requests`/`responses` 只读视图（第二轮只落在了内存实现上），导致翻转查询永远为空；已补齐。
- **派发语义**：批准后重建的 `DelegationRequest` 必须留空 `task_id`（快照中的 task_id 是 Python 侧语义），否则内核以 "delegation task not found" 拒绝。
- **E2E 验证**：发起委托（analyze_sales require_approval）→ 审批单进审批台 → 批准 → 自动派发 `accepted=true` 返回内核 task_id → 任务查询可见（parent_interaction_id 正确回链，allowed_tools=[analyze_sales]，status=pending）。
- **验证**：`tests/test_server.py` 89 全绿 + `test_delegation_authorizer.py` 10 全绿；Go `internal/delegation` 测试全绿；前端构建通过。联调用 `config/interaction_profiles.yaml`、`config/go_kernel.yaml` 本地改动已还原，未提交。

### 进度记录（第十轮：审批状态对账）

- **架构**：Python 审批台为唯一人工审批入口，Go 内核原生审批（DelegationApproval）为执行事实源。内核审批单经 LIST 端点枚举，与审批台记录按 `kernel.request_id == console.decision_id` 关联（Python 派发时 `DelegationRequest.request_id` 即审批单 decision_id）。
- **Go 内核新增 LIST 端点**（对账最大缺口，此前内核审批单完全无法枚举）：
  - store：`ApprovalFilter{Status, InitiatorAgentID, Limit}` + `List()`（动态 WHERE、默认 limit 100 上限 500、按 created_at DESC）；
  - api：`GET /a2a/v1/delegation-approvals`，配 control-token 认证，配置了 control token 时结果强制按 control initiator 过滤，支持 `?status=`/`?limit=` 查询参数。
- **Python bridge 修复与扩展**（[go_kernel_bridge.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/go_kernel_bridge.py)）：
  - 修复 202 误判 bug：`request_delegation` 原 `raise_for_status()` 把内核原生 require_approval（202）当错误吞掉且丢失 approval_id；现解析 `require_approval` verdict 并保留 approval_id，403/400 透传内核 reason；
  - 新增 `list_delegation_approvals` / `get_delegation_approval` / `approve_delegation` / `reject_delegation`（approve/reject 用 approver token）；control token 注入全部既有方法（`token`/`approval_token` 经 `config/go_kernel.yaml` 与 `runtime.py` 传入）。
- **批准派发自动代批准**：`_dispatch_approved_delegation` 在 Python 批准派发时若内核仍返回 require_approval，自动以审批人身份代批准内核审批单（`request_id=req.decision_id` 幂等）触发 `ResumeApproval`（重建 DelegationRequest + 二次 authorize + 防扩大校验 + Consume 建任务）；consumed 审批单返回 `task_id` 回填派发结果，附 `kernel_approval_id`/`kernel_verdict`。
- **管理端对账接口**：`GET /v1/admin/a2a/kernel-approvals?status=`，每项返回内核审批单 + 审批台关联 decision_id/verdict + `reconciled` 标记（pending 与 history 两路按 decision_id 关联）。
- **前端审批中心加"内核对账"页签**（[Approvals.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/Approvals.vue)）：内核状态过滤下拉、对账表格（Approval ID/目标 Agent/工具/内核状态/任务/审批台关联/审批台结论/过期时间），未启用内核时提示。
- **验证**：`tests/test_server.py` + `test_go_kernel_bridge.py` 共 102 全绿（新增代批准 Resume、对账视图、202 解析、403 透传、control/approver token 共 6 个）；Go `internal/api` + `internal/store` 测试全绿（新增 control token initiator 过滤用例）；前端构建通过。

### 进度记录（第十一轮：任务执行闭环）

- **架构**：采用"内核执行"语义——审批 Consume 后内核经 outbox dispatcher 把任务投递给目标 agent entrypoint，stub 回调 accept/start，由内核 executor 调用 Python `/v1/govern/tool-call` 完成工具执行，任务终态 `completed`。
- **Go 内核 dispatcher 接线**（[handlers.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/api/handlers.go)）：`SetEntrypointClient` 内启动 `DelegationDispatchOutboxDispatcher`（仿 `SetR2Authorizer` 模式），`dispatchCancel` 在 `Close()` 释放；Consume 写入的 outbox 记录由此投递（Claim→Dispatch→MarkDelivered/MarkFailed 指数退避）。
- **集成测试**（api_test.go）：`TestApprovalConsumptionDispatchesViaOutboxDispatcher` 全链路验证 require_approval→approve→Consume→dispatcher 投递（URL/task_id/token/delivery_id）。
- **Python entrypoint stub**（[entrypoint_stub.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/entrypoint_stub.py) + CLI `entrypoint-stub` 子命令）：接收内核投递后**先幂等回调 create**（Consume 路径建任务不写 delegation message，而内核 token 校验依赖该 message），再 accept/start；重复投递 200 幂等；回调失败 502 不标记 driven，交由 dispatcher 退避重投。10 个单元测试全绿。
- **Python 侧执行适配**（[server.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/server.py)）：
  - `_handle_govern_tool_call` 对内核 task_id 建立**影子任务**（Python 侧无对应 Task 时同 id 登记），打通 R1-R5 治理与预算链路；
  - 修复 GovernResponse 序列化：工具返回 dict 时 `json.dumps` 为字符串（原为 pydantic 校验 500）。
- **配置/策略补齐**（委托闭环执行前提）：`identity.yaml` 增 `dev-token-research-agent-001`（agent_id=research-agent）；`agents.yaml` 注册 research-agent（复用 `research_assistant_v1` profile，避免测试最小 profile 校验冲突）；`profiles.yaml` 增 analyze_sales；新增 `local_functions.yaml` 注册 analyze_sales/calculate_checksum/transform_data（tests.integration.local_tools 模拟实现）；`execution_policy.yaml` trusted_local 增 analyze_sales；`policies/default.rego` 增 analyze_sales allow 规则；contract fixture 补 `parent_interaction_id` 字段。
- **E2E 全链路验证**（OPA 8181 + Python 8000 + 内核 18080 + stub 8001）：管理端委托 → require_approval → 审批单入台 → Python 审批台批准 → 自动派发内核 → Consume → outbox dispatcher → stub create/accept/start → 内核 executor → govern/tool-call（影子任务+策略 allow+本地函数执行）→ 任务 `completed`，两次复验均通过。
- **回归**：Python `pytest` 981 passed / 8 skipped；Go `go test ./...` 全包通过。

### 进度记录（第十二轮：任务取消闭环）

- **勘察结论**：Go 内核管理端取消链路已完整——`POST /a2a/v1/tasks/{id}/cancel`（`withControlAuth`）→ `handleCancelTask` 级联取消子孙任务 → `cancelTaskExecution` 经 `entrypointClient.Cancel` 推送 `POST /a2a/v1/entrypoint/tasks/{id}/cancel` 到目标 entrypoint（Bearer delegation token），传播确认置 `cancelled`、失败置 `outcome_unknown`、终态幂等；既有集成测试覆盖（`TestCancelMainChainPropagatesToTarget` 等）。**唯一缺口在 stub**：内核推 cancel 时 stub 404 → `confirmed=false` → running 任务被误置 `outcome_unknown`。
- **Python entrypoint stub 扩展**（[entrypoint_stub.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/entrypoint_stub.py)）：新增 `_task_status`/`_task_tokens` 状态跟踪（create 记 accepted→running）；`POST /a2a/v1/entrypoint/tasks/{id}/cancel` 路由——校验 delegation token → 置 cancelled → 移出驱动集合并清 token → 响应 `{"task_id","status":"cancelled"}` 供内核确认；已取消任务被 dispatcher 重投 create 时直接返回 200 不再驱动（防内核 409 重试循环）；`/health` 暴露 `task_status`。
- **管理端查询缺陷修复**（[go_kernel_bridge.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/go_kernel_bridge.py)）：`query_task` 原未带 control token → 内核 401 → 管理端查询 500；补 `headers=self._headers()`（与 `stream_task`/`cancel_task` 对齐），新增 `test_query_task_sends_control_token`。
- **E2E 双场景验证**（OPA 8181 + Python 8000 + 内核 18080 + stub 8001）：
  - 场景 A（accepted 取消，stub `--no-auto-start`）：管理端委托 → 派发 → stub create/accept → 管理端 cancel → 内核推 cancel → stub 确认 → 任务 `cancelled`；
  - 场景 B（running 取消，stub 自动 start）：委托 → 派发 → accept/start → 轮询命中 running 窗口即管理端 cancel → 任务 `cancelled`（非 outcome_unknown，确认传播成功）。
- **回归**：Python `pytest` 988 passed / 8 skipped（stub 16 全绿含 6 个新取消用例，bridge 新增 1 个）；Go `go test ./...` 全包通过；`config/go_kernel.yaml` 临时改动已还原。

### 进度记录（第十三轮：results 回调模式 / 远端执行）

- **架构定位**：落实"多 Agent 强控制治理"设计——执行方式不是全局开关，而是每个 Agent Card 的配置属性：execution_mode=`kernel_executor`（默认，内核 HTTPExecutor 调 Python `/v1/govern/tool-call`）或 `remote_results`（内核 start 后挂起，目标 Agent 在自己的运行时执行，经 entrypoint results 端点回报终态 + ConsumedBudget，喂预算衰减链路）。空值向后兼容。
- **Go 内核全链路**（[models.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/models/models.go) / [delegation.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/delegation/delegation.go) / [handlers.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/api/handlers.go) / [db.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/store/db.go) / [task.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/store/task.go) / [agent.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/store/agent.go)）：AgentCard/Task/EntrypointTaskRequest 增 `execution_mode`；SQLite schema + ensureColumn 迁移 + 全列接线；delegation 直接路径与审批 Consume 路径均透传。
- **PendingResultsHandle**（[execution.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/execution/execution.go)）：实现 Handle 接口（Done/Cancel）；`Complete(result)` 由 sync.Once 保证单次完成；可选 deadline 定时器超时返回 `execution_deadline_exceeded`（对齐 HTTPExecutor 语义）。
- **内核 start 分支**：`remote_results` 任务 UpdateStatus(running) 后注册 PendingResultsHandle 并挂起；`handleEntrypointResults` 先 `CompleteWithConsumption` 落库再唤醒 `finishExecution` goroutine（goroutine 内 Complete 因终态 CAS 失败不覆写）；租约续期复用现有 finishExecution/awaitExecution 机制。
- **store 缺陷修复**：`UpdateStatusWithConsumption` 原只结算父任务消耗、从不写本任务行——UPDATE 增 `consumed_token_count`/`consumed_payment_amount`，使 results 回写的 ConsumedBudget 可查询。
- **Python entrypoint stub 扩展**（[entrypoint_stub.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/entrypoint_stub.py) + CLI `--tool-url`/`--tool-token`）：投递 payload 带 `execution_mode=remote_results` 时在自己的运行时执行——POST `{tool_url}/v1/govern/tool-call`（agent_id=target、user_id=initiator、Bearer tool_token），allow → completed + outcome + 消耗估算；deny/异常 → failed + error_code；随后回调内核 results 端点；未配置 tool_url 仅告警不执行。
- **测试**：Go 集成测试 4 个（completed+ConsumedBudget 落库 / failed 回报 / cancel 后迟到 results 409 终态不覆写 / deadline 超时）+ PendingResultsHandle 单测 3 个；Python stub 单测 19 个全绿。
- **E2E 全链路验证**（OPA 8181 + Python 8000 + 内核 18080 + stub 8001 `--tool-url/--tool-token`）：管理端委托（researcher_001→research-agent, analyze_sales）→ 内核派发 → stub create/accept/start → **内核挂起等 results** → stub 调治理层 tool-call（策略 allow + 本地函数执行）→ results 回调 → 任务 `completed`、`consumed_budget.token_count=74` 回写、outcome 含执行结果；execution_mode 全程透传可见。首轮失败用例（缺 `period` 参数 → `local_function_runtime_error`）亦验证了远端失败回报路径。
- **回归**：Python `pytest` 全量；Go `go test ./...` 全包；`config/go_kernel.yaml` 与 `config/a2a_agents.yaml` 临时改动已还原。

### 进度记录（第十四轮：多 Agent 演示底座 / 两跳重委托链路）

- **目标**：落实"多 Agent 强控制治理架构"定位——planner（researcher_001）委托 research-agent、research-agent 再以自己的 control token 向内核重委托 specialist-agent 的真实两跳链路可系统性演示。
- **Go 内核：按 Agent 的控制凭证**（[main.go](file:///D:/Agent/loop-controller-latest-develop/go/cmd/kernel/main.go) / [r2_authorizer.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/delegation/r2_authorizer.go) / [handlers.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/api/handlers.go)）：新增 `-agent-control-token token=agent_id`（与 `-interaction-agent-token`）flag，映射表注入 `withControlAuth`——每个 Agent 用自己的 token 调内核 delegations/tasks 端点，任务枚举按 initiator 隔离（403 task_access_denied）；新增 `TestAgentControlTokenDelegatesAsBoundInitiator`。
- **SQLite 并发缺陷修复**（[db.go](file:///D:/Agent/loop-controller-latest-develop/go/internal/store/db.go)）：E2E 暴露 `insert idempotency key: database is locked (SQLITE_BUSY)`。排除法（pragma 语法 Go 测试 4 种写法验证生效、DB 文件状态核验、Python sqlite3 外部探针 0 失败）定位到 modernc.org/sqlite 驱动对 database/sql 连接池的并发缺陷——busy_timeout 无法吸收。修复：`db.SetMaxOpenConns(1)` 单连接串行化（WAL 保证读写不互挡）。
- **Python 管理端委托透传 scope/budget**（[server.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/server.py) / [server_models.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/server_models.py)）：`AdminDelegationRequest` 增 `allowed_tools`/`allowed_capabilities`（本轮早前）与 `budget` 字段；直接派发、审批快照（`tool_arguments`）、批准后派发（`_dispatch_approved_delegation`）三处均透传至内核。
- **stub 多 Agent 身份与重委托**（[entrypoint_stub.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/entrypoint_stub.py) + CLI `--agent-id`/`--control-token`/`--redelegate tool=agent`）：stub 以绑定 Agent 身份运行；命中重委托映射时带 `parent_task_id` 以自己的 control token 直接调内核 `POST /a2a/v1/delegations`（内核 deriveLineage 做防扩大/防环/深度校验），轮询子任务终态并聚合 `child_task_id`/`child_outcome`/`consumed_budget` 上报 results。
- **预算结算 409 根因与修复**：两跳链路跑通后子任务 results 回调被 409 `invalid_status_transition` 拒绝——高置信根因是 stub 子委托写死 `budget=0` 而执行成功上报 `consumed>0`，父任务预算结算条件 `consumed <= 子任务budget` 不满足（store/task.go）→ `ErrBudgetExceeded` → 映射 409，任务永久卡 running。修复：子委托继承父任务预算信封 + stub 上报 consumed 统一 clamp 到任务预算（`_clamp_consumed`）；测试增 `test_clamp_consumed_caps_at_task_budget` 与子委托继承断言。
- **多 Agent 演示配置**（config/）：`a2a_agents.yaml` 增 specialist-agent 卡片（research-agent/specialist-agent 均 `execution_mode: remote_results`）；`agents.yaml`/`identity.yaml`（research-agent 控制凭证 + specialist-agent 工作负载凭证）/`agent_trust.yaml`/`interaction_profiles.yaml`/`delegation_policies.yaml`/`profiles.yaml`/`policies/default.rego` 补齐两跳链路的 profile/信任/策略规则。
- **E2E 两跳全链路验证**（OPA 8181 + Python 8000 + 内核 18080 + stub 8001 research-agent `--redelegate calculate_checksum=specialist-agent` + stub 8002 specialist-agent）：管理端委托（带 `budget={token_count:10000}`）→ research-agent stub 重委托 → specialist-agent stub 执行 calculate_checksum（策略 allow + 本地函数）→ results 逐级回报。**根任务 `completed`，`consumed_budget.token_count=137` 非零回写，outcome 含 `child_outcome`（真实校验和）**；子任务 `completed`，depth=1、lineage（root/parent_task_id）、scope 收窄至 `[calculate_checksum]`、预算继承、allow_redelegation=false 全部符合防扩大语义。
- **演示拓扑**（`config/go_kernel.yaml` 已还原，演示时临时启用）：Python 服务 `PYTHONPATH=src LOOP_CONTROLLER_API_KEY=e2e-admin-key LOOP_CONTROLLER_AUDIT_HMAC_KEY=<hmac>` + `go_kernel.enabled=true/base_url=:18080/token=dev-token-researcher-001/approval_token=dev-token-approver-001`；内核 `-agent-control-token dev-token-research-agent-001=research-agent -interaction-agent-token dev-token-research-agent-001=research-agent -discovery-file config/a2a_agents.yaml`；stub 8001 `--agent-id research-agent --control-token dev-token-research-agent-001 --tool-token dev-token-research-agent-001 --redelegate calculate_checksum=specialist-agent`；stub 8002 `--agent-id specialist-agent --tool-token dev-token-specialist-agent-001`。后台服务须用 `Start-Process -WindowStyle Hidden -RedirectStandardOutput/Error` 脱离终端，否则随终端回收；PowerShell 写 JSON 重放文件须用 UTF8Encoding($false) 防 BOM。
- **回归**：Python `pytest` 977 passed / 5 skipped + integration 19 passed / 3 skipped；Go `go test ./...` 全包通过（`SetMaxOpenConns(1)` 对并发幂等测试无回归）。

### 进度记录（第十五轮：前端体验完善 + A2A 链路可视化脚手架）

- **背景与约束**：后端 develop 推进至 v0.54 后，合并评估发现本分支与 v0.54 存在 14 处文本冲突与 5 项语义交叉（审批恢复原子性、stub 接入 durable assignment/lease/fence、remote_results 经 fenced settlement、control token 不进浏览器、contract 迁移到 v0.54 authority）。本轮起**冻结一切后端改动**，仅做前端增量，待 v0.54 合入后再协调。
- **R15-1 体验完善**（纯前端）：
  - 新增 [useAsyncData.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/composables/useAsyncData.ts)：统一 loading / 错误提示 / 静默刷新 / `usePolling` 轮询（页面不可见自动跳过）。
  - [Dashboard.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/Dashboard.vue)：15s 静默轮询 + 手动刷新按钮 + 上次刷新时间。
  - [Audit.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/Audit.vue)：时间范围筛选（前端过滤，后端接口补齐后可改服务端）。
  - [Login.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/Login.vue) + [client.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/client.ts)："高级设置"自定义双后端地址并持久化，默认仍走开发代理。
- **R15-2 审批台增强**（[Approvals.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/Approvals.vue)）：
  - 审批详情抽屉（完整 ID 一键复制 + 通过/拒绝直达操作）；参数/升级链字段待后端审批详情接口补齐。
  - 调研结论：后端 `/v1/wait-for-approval/sse` 是 **Agent 等待自身审批结果**的通道（按 request_id + agent 鉴权），不适合管理台推送；审批台准实时刷新采用 15s 静默轮询。
- **R15-3 A2A 委托链路可视化**（mock 驱动，隔离 v0.54 变化）：
  - 契约层 [types.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/a2a/types.ts)：字段形状以第十四轮 E2E 验证过的内核任务响应为准。
  - 数据源抽象 + Mock 实现（[index.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/a2a/index.ts) / [mock.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/a2a/mock.ts)）：复现 researcher_001 → research-agent → specialist-agent 两跳链路（depth/lineage/scope 收窄/预算账本/child_outcome）。
  - [TaskTree.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/components/TaskTree.vue)：任务树组件 + 任务详情抽屉；[A2A.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/A2A.vue) 新增"多跳委托链路"卡片（Mock 数据源标识）。v0.54 推送后仅需替换 index.ts 中的数据源实现，视图零改动。
- **R15-4 质量基线**：接入 Vitest（`npm run test`），8 个用例覆盖 Mock 数据源与 useAsyncData；`npm run typecheck` 独立脚本；`npm run build` 全量通过。
- **回归**：`vue-tsc --noEmit` 零错误；Vitest 8 passed；vite build 成功。

### 进度记录（第十六轮：R15 移植到 integration/frontend-v054）

- **分支事实**：上游推送 `integration/frontend-v054`（= 546c9af 将前端主线 41c9d9f 与 v0.54 后端整合 + ac116ac entrypoint 强化），R15（809c819）未包含。新基线分支 `frontend/r15-port` 从 `integration/frontend-v054` 切出，cherry-pick 809c819 并解冲突。
- **v0.54 前端安全设计（后续必须遵守）**：
  - 纯 Session 认证：移除 X-API-Key 直连回退与 API Key 持久化，store 启动主动清除旧 `lc_api_key`/`lc_session_token`，token 不落 localStorage；
  - 删除全部 YAML fallback 与 vite `/config/*` 中间件，配置只走 Admin API；
  - 审批操作要求**独立审批凭证**（用后即焚、不走 admin session 的 pythonClient）；
  - 删除"内核对账"页签（v0.54 内核审批端点已变）；SSE 硬化（cursor/Last-Event-ID、重连退避、401 跳登录）。
- **冲突解决原则（裁定：后端安全优先）**：以 v0.54 安全设计为准——Login 恢复 session-only 流程（API Key 不预填、换取 session 后即弃），仅保留与之正交的"自定义后端地址"持久化；Approvals 保留独立审批凭证对话框（`@closed="clearForm"`），详情抽屉与轮询叠加其上；client.ts 自动合并结果复核无误。
- **v0.54 后端变化对 R15-3 的影响**：Task 移除 `execution_mode`、新增 tenant/workload/instance 与 assignment/lease/fence + TaskGraph 调度；lineage/budget 字段保留，契约 types.ts 暂不变，列为核对项。后端仍无任务列表接口，Mock 数据源策略维持。
- **协调事项**（待后端契约确认）：`GET /v1/admin/a2a/tasks` 列表接口、审批详情字段（参数/升级链）、types.ts 与 v0.54 authority 对齐、frontend_api_gaps.md 同步。

### 进度记录（第十七轮：分支整理 + 死端点排查结论 + 文档同步）

- **分支整理**（develop / frontend/main / integration/frontend-v054 均已被超越）：
  - 删除过时分支前做游离提交核查——develop、integration/frontend-v054 内容全部已在主线内；
    frontend/main 仅 R15 提交本体游离（内容已含于 cherry-pick）；main-legacy 为古早设计，确认废弃；
  - `frontend/r15-port` 改名 **`develop`** 成为唯一开发主线（HEAD `7500fe6`），两库（个人/公司）一致。
- **死端点排查结论（误报澄清）**：曾判定 Dashboard/Settings 调用的 `/admin/revoke`、
  `/admin/revocation-list`、`/admin/kill-switch` 为 v0.54 已删除的死端点——**不成立**。
  实际原因：初轮路由普查只 grep 了 `"/v1/` 前缀，遗漏非 `/v1` 的遗留路由（`server.py:3009-3011`）。
  三个端点在 v0.54 仍存在，且其鉴权 `_check_api_key`（`server.py:367-381`）明确接受
  Session Bearer 作为替代凭据，前端登录后调用可正常通过。**无需代码修复**。
- **文档同步**：
  - `frontend_api_gaps.md` 更新至 v0.54（头部版本、§2.1 全量路由表含 3 条遗留 `/admin/*`、
    §2.2 Go 内核 v0.54 路由、§3 兑现状态总览、§6/§7 剩余缺口清单）；
  - 本文档头部基线从 `frontend/main` / v0.48.0 更新为 `develop` / v0.54.0；
  - 新增 §7.2 剩余缺口：A2A 任务树列表端点（高）、审批详情字段、Secret 枚举、
    Agent CRUD、审批转交、Identity/Entrypoints 热更新。

### 进度记录（第十八轮：死信队列页 + Dashboard 增强）

- **任务 1 死信队列治理页**（commit `deb05c8`）：
  - `api/a2a/types.ts` 新增 `A2ADeadLetterItem` / `A2ADeadLetterReplayParams` /
    `A2ADeadLetterDataSource`，契约对齐 Go 内核 `models.TaskAssignment` 序列化
    （`GET /a2a/v1/dead-letters?tenant_id=` → `{assignments:[...]}`；
    `POST /a2a/v1/dead-letters/{id}/replay` ← `{tenant_id, expected_revision, not_before?}`）；
  - `mock.ts` 新增 `MockA2ADeadLetterDataSource`（三种典型失败类别；重放语义与内核一致：
    revision 乐观校验、replay_count+1、state 回 queued 并**退出列表**——list 只返回
    state=dead_letter 条目，与 `ListDeadLetters` 语义一致）；
  - 新视图 `DeadLetters.vue`（路由 `/dead-letters` + 菜单）：租户过滤、15s 轮询、
    失败类别标签、详情抽屉、确认式重放；
  - 测试 `tests/deadletter-mock.test.ts` 5 用例，vitest 总计 13 passed。
- **任务 2（A2A SSE 增强）：核实无需做**——v0.54 整合已带入硬化 SSE
  （cursor/Last-Event-ID、指数退避、401 跳登录、generation 防串扰，`python.ts streamA2ATask`）。
- **任务 5 Dashboard 增强**（commit `9016833`）：Go 内核 Readiness 卡片（`GET /ready`）、
  死信计数卡片（点击跳死信页）、关键指标卡片（`/metrics` 解析 `lc_*`/`a2a_*`/`go_goroutines`
  前 12 条）；增强区块独立加载，失败不影响主卡片。
- **当前剩余队列**（按既定顺序）：
  1. 任务 3：RBAC 绑定管理页（bindings/grants 列表 + revoke；Mock 起步，契约见
     `server.py:3020-3023`，建表参照 DeadLetters.vue 模式）；
  2. 任务 4：Policy 生命周期页（candidates validate/shadow/publish/rollback，Mock 起步）；
  3. 任务 6：审批 SSE 推送替代 15s 轮询（`wait-for-approval/sse`，复用 streamA2ATask 模式）；
  4. 任务 7~9：Playwright E2E 骨架 / 本文档 §4·§6 重写 / 空态与错误态统一。
- **协作约定不变**：纯前端、Mock 隔离未定契约、不碰后端、每批完成跑
  `npm run test && npm run typecheck && npm run build` 后提交
  `develop` 并 `git push origin develop && git push company develop`。

### 进度记录（第十九轮：任务 3 RBAC 绑定管理页）

- **契约层** `api/rbac/types.ts`：字段对齐 Python runtime `_binding_payload` /
  `_grant_payload`（server.py:1951-1972；路由 server.py:3020-3023）；
  `RbacBinding{binding_id, principal, tenant_id(null=平台级), role, granted_by, created_at}`、
  `RbacGrant{grant_id, source_principal, source_tenant, target_tenant, resources[], granted_by, created_at}`；
  Role 枚举与后端一致（platform_admin/tenant_admin/policy_*/approver 共 7 个）。
  **关键事实：RBAC 数据 100% 在 Python 侧**（SQLite rbac_role_bindings /
  rbac_cross_tenant_grants），与 Go 内核无关——Http 实现须走 pythonClient，
  不复用 a2aClient。
- **Mock**：`mock.ts` 复现后端语义——列表只回未吊销记录（吊销=软删退出列表）；
  创建校验对齐 server.py:2004/2007（platform_admin 必须 tenant_id=null、
  租户角色必须挂租户）；revoke 响应形状 { id, state: "revoked" }。
- **视图** `RbacBindings.vue`（路由 `/rbac` + 菜单）：角色绑定 / 跨租户授权双 Tab；
  租户+角色过滤（选项动态推导）、15s 轮询（仅刷新当前 Tab）、
  新建绑定对话框（platform_admin 时租户选择自动锁定为平台级）、
  新建授权对话框（resources 逐行输入）、确认式吊销；grants 区标注
  "仅 platform_admin 可管理"（数据标识，不做前端权限裁剪——enforcement
  默认关闭，权限区分暂不生效）。
- **测试**：`tests/rbac-mock.test.ts` 13 用例（列表形状/创建成功/两类创建拒绝/
  吊销退列表/ not_found/深拷贝隔离），vitest 总计 26 passed；typecheck +
  build 全绿。
- **当前剩余队列**（按既定顺序）：
  1. 任务 4：Policy 生命周期页（candidates validate/shadow/publish/rollback，Mock 起步）；
  2. 任务 6：审批 SSE 推送替代 15s 轮询（`wait-for-approval/sse`，复用 streamA2ATask 模式）；
  3. 任务 7~9：Playwright E2E 骨架 / 本文档 §4·§6 重写 / 空态与错误态统一。

### 进度记录（第二十轮：任务 4 Policy 生命周期页 + 文档表述规范化）

- **契约层** `api/policy/types.ts`：字段对齐 Python runtime——候选 payload
  （server.py:1683-1697）、路由 server.py:3012-3019、validate/shadow/publish/rollback
  handler（server.py:1763-1926）、status 形状（policy_lifecycle.py:227-259）、
  CandidateValidationResult（policy_validation.py:48-56）、ShadowRunResult
  （policy_shadow.py:66-81）。数据 100% 在 Python 侧（policy_delivery 存储），
  与 Go 内核无关——Http 实现走 pythonClient。
- **Mock**：`mock.ts` 复现生命周期语义——draft 校验（源内容含 `violation` 即失败
  转 failed，422 不抛错）、非 draft 重复校验返回当前状态（200 幂等）、
  shadow 仅 validated/published/loaded 可运行且 revision+artifact_sha256 必须匹配、
  publish 仅 validated 可发布、rollback 按历史 revision 找回候选；
  status generation 单调递增，生命周期操作写审计。
- **视图** `Policies.vue`（路由 `/policies` + 菜单）：顶部生命周期状态卡
  （期望/活跃版本、generation、实例加载，独立加载失败不影响列表）；
  候选 Tab（状态标签、详情抽屉含校验阶段结果、按状态显示 校验/影子/发布/回滚到此
  操作）、影子运行对话框（样本 JSON 输入 + 差异统计 + 逐样本对比表）、
  操作审计 Tab；15s 轮询。注意：validateCandidate 有状态副作用，详情抽屉只读
  不得触发校验，校验结果经本页 Map 缓存展示。
- **测试**：`tests/policy-mock.test.ts` 17 用例（六状态覆盖/校验成败/幂等/
  发布与三类拒绝/回滚/影子差异与三类拒绝/审计/深拷贝），vitest 总计 43 passed；
  typecheck + build 全绿。
- **文档表述规范化**：两份文档移除人员指称与日期——进度记录只写轮次，
  协调事项写"待后端契约确认"，分支事实写"上游推送"。后续记录保持此风格。
- **当前剩余队列**（按既定顺序）：
  1. 任务 6：审批 SSE 推送替代 15s 轮询（`wait-for-approval/sse`，复用 streamA2ATask 模式）；
  2. 任务 7~9：Playwright E2E 骨架 / 本文档 §4·§6 重写 / 空态与错误态统一。

### 进度记录（第二十一轮：任务 6 审批推送——SSE 基础模块 + 推送优先轮询兜底）

- **后端核实结论**：管理台审批推送**无可用 SSE 端点**——
  `/v1/wait-for-approval/sse`（server.py:1208-1274）走 `_check_agent_auth`
  （Agent 身份）且校验 request 归属（server.py:1225-1229），是 Agent 等待
  自身审批结果的通道，管理台不可用。后端落地管理台推送端点前，
  前端按未定契约推进。
- **SSE 基础模块** `api/sse.ts`：抽取 `createEventStream`（fetch + AbortController
  携带 Authorization、Last-Event-ID 游标续传、指数退避 1s→10s、401 登出跳
  登录、400/410 游标失效致命错误、204 结束、shouldStop 终态取消、流尾残帧处理）；
  `streamA2ATask` 重构为薄封装复用之，对外签名与语义不变。
- **纯函数拆分** `api/sse-parse.ts`：`parseSseFrame` / `drainSseBuffer` /
  `nextBackoffDelay` 独立成模块（node 环境可测，不依赖 auth store / router）。
- **未定契约**：`streamAdminApprovals`（python.ts）指向
  `GET /v1/admin/approvals/stream`（暂定路径，事件形状暂定
  `{type: 'pending'|'decided', ...}`）；后端落地后仅需对齐 url 与 payload。
- **Approvals.vue 推送优先、轮询兜底**：连接失败（当前环境即如此）自动降级
  15s 轮询，头部标识"已降级轮询"；推送正常时轮询不产生请求，收到事件即时
  刷新待审批列表。
- **测试**：`tests/sse.test.ts` 12 用例（帧解析/多行合并/事件名/注释跳过/
  \0 id/非法 JSON/CRLF/缓冲切分/退避封顶），vitest 总计 55 passed；
  typecheck + build 全绿。
- **当前剩余队列**（按既定顺序）：
  1. 任务 7：Playwright E2E 骨架（登录 → Dashboard → 审批/A2A 冒烟）；
  2. 任务 8：本文档 §4·§6 重写；
  3. 任务 9：空态与错误态统一（Loading / Empty / Error 三态规范）。

### 进度记录（第二十二轮：任务 7 Playwright E2E 骨架）

- **基建**：`@playwright/test` 入 devDependencies + chromium；`playwright.config.ts`
  自动拉起 vite dev server（5173，可复用已有实例）；`npm run test:e2e` 脚本；
  `.gitignore` 补 test-results/playwright-report。
- **stub 策略**（tests/e2e/helpers.ts）：全部后端依赖经 `page.route` 拦截替换
  （session 登录、health、审批、revocation-list、metrics 文本、agents），
  E2E 不依赖 Python runtime / Go 内核进程；有后端进程时取消 stub 即联调模式。
  审批推送流 stub 404，顺带验证 R21 的轮询兜底路径。
- **用例 8 个全绿**（chromium）：登录表单渲染 / 守卫重定向 / 空 Key 提示 /
  401 失败提示与停留 / 登录成功进仪表盘 / 仪表盘四卡片渲染 /
  侧边栏导航四治理页（审批/死信/RBAC/策略）/ 未认证直达受保护页重定向。
- **顺手修复**：Login.vue 对 401 显示友好文案（此前透传 axios 英文报错）。
- **回归**：vitest 55 passed；typecheck + build 全绿；E2E 8 passed。
- **当前剩余队列**（按既定顺序）：
  1. 任务 8：本文档 §4·§6 重写；
  2. 任务 9：空态与错误态统一（Loading / Empty / Error 三态规范）。

### 进度记录（第二十三轮：任务 8 文档 §4·§6 重写）

- **§4 重写**：旧的"三阶段推进顺序"已过时（底座全部完成），替换为
  当前阶段定位（治理页面扩展 + 质量收口）与七条已确认技术决策
  （数据源抽象 / Session 鉴权 / 数据归属 / SSE 基础设施 / E2E 策略 /
  多 Agent 底座 / 安全原则）；进度记录（第二轮~第二十二轮）原样保留，
  标题层级统一为 `###`。
- **§6 重写**：历史底座任务清单替换为当前活跃队列——质量收口三项
  （任务 9 三态规范 / E2E 扩面 / 参数可视化编辑）+ 五项待后端契约
  （审批 SSE 端点 / RBAC·Policy·死信 Http 数据源 / 审计时间范围 /
  用户视图），并给出实施顺序。
- 删除已被取代的"第一/二/三阶段"陈旧小节（内容分别并入了 §4 与 §6）。

### 进度记录（第二十四轮：任务 9 空态与错误态统一——三态规范落地）

- **规范**：数据区统一三态——Loading（v-loading / el-skeleton）、
  Empty（el-table #empty / el-empty）、Error（共享组件 ErrorState 持久块
  + 重试按钮，不再弹 toast）；操作类反馈（保存/审批/重放）仍用 ElMessage。
- **共享组件** `src/components/ErrorState.vue`：el-alert + 重试，
  约定注释写进组件文档。
- **接入页面（9 个）**：Approvals（pending/history 双 Tab + history 补空态）、
  Audit、Agents、Tools（新增 loading；API 与 YAML 回退均失败才出错误态）、
  A2A（Agent 注册表）、DeadLetters、RbacBindings（双 Tab）、
  Policies（候选 + 审计）；useAsyncData 页签 `showError: false` 防与
  ErrorState 双重提示。
- **E2E 扩面**：`error-state.spec.ts` 2 用例（500 故障展示错误块与重试、
  解除故障重试后恢复空态），E2E 总计 10 passed。
- **回归**：vitest 55 passed；typecheck + build 全绿。
- **当前剩余队列**：E2E 扩面（审批操作流：通过/拒绝/吊销/Kill Switch）；
  随后端契约冻结替换 Mock 数据源（§6.2）。

---

## 5. 当前已完成工作

1. ✅ 创建 `frontend/main` 分支。
2. ✅ 初始化 Vite + Vue 3 + TypeScript + Element Plus 脚手架。
3. ✅ 配置 Axios 客户端、路由、Pinia 状态管理、侧边栏布局。
4. ✅ 实现登录页（API Key + 双后端地址 + 健康检查）。
5. ✅ 实现仪表盘、审批台、Agent 管理、工具策略、审计查询、系统配置、A2A 预留页。
6. ✅ 封装 Python Admin API 调用（审批、审计、吊销、Kill Switch、Harness）。
7. ✅ 封装本地 YAML 配置读取（Agent、Profile、Identity、Entrypoints）。
8. ✅ 编写后端接口缺口报告 `frontend/docs/frontend_api_gaps.md`。
9. ✅ 编写本开发规划文档。

---

## 6. 后续开发任务清单

> 底座任务（§6 历史版本所列 Agent/Profile/Identity/审批/A2A 等）已全部完成，
> 逐轮交付明细见 §4 进度记录（第二轮~第二十二轮）。本节只列当前活跃队列。

### 6.1 当前活跃队列（质量收口）

- [x] **任务 9：空态与错误态统一**——Loading / Empty / Error 三态规范，
      各列表页统一空态文案与错误提示（R24 已落地，见 §4 进度记录）
- [ ] E2E 扩面：审批操作（通过/拒绝/吊销/Kill Switch）、RBAC 与 Policy
      页面冒烟用例
- [ ] 工具策略页参数可视化编辑（当前为 JSON 编辑器）

### 6.2 待后端契约（前端已按未定契约或 Mock 就位）

- [ ] 管理台审批 SSE 端点（前端 `streamAdminApprovals` 已就绪，
      推送优先 + 轮询兜底；后端落地 `GET /v1/admin/approvals/stream`
      类端点后仅需对齐 url 与事件 payload）
- [ ] RBAC 绑定 / Policy 治理页 Http 数据源（Mock 语义已对齐
      server.py:1928-2070/1699-1950，契约冻结后按 types.ts 直写实现）
- [ ] 死信队列治理页数据源（契约已对齐 Go 内核 models.TaskAssignment，
      待内核联调环境开放）
- [ ] 审计时间范围筛选（`GET /v1/admin/audit` 扩展参数）
- [ ] `GET /v1/admin/users` 用户视图数据源（Agents 页合并展示用）

### 6.3 实施顺序

1. 任务 9：空态与错误态统一。
2. E2E 扩面（治理页冒烟 + 审批操作流）。
3. 随后端契约冻结逐项替换 Mock 数据源（死信 → RBAC → Policy）。

---

## 7. 开发与部署说明

### 7.1 本地开发

```powershell
cd D:\Agent\loop-controller-latest-develop\frontend
npm install
npm run dev
```

开发服务器默认运行在 `http://127.0.0.1:5173`。

### 7.2 后端启动

```powershell
# 1. 启动 OPA
cd D:\Agent\loop-controller-latest-develop
.venv\Scripts\python.exe -m loop_controller.cli opa-start

# 2. 启动 Python HTTP Server
$env:PYTHONPATH="src"
$env:LOOP_CONTROLLER_API_KEY="your-admin-key"
.venv\Scripts\python.exe -m loop_controller.cli server --host 127.0.0.1 --port 8000
```

### 7.3 代理配置

`vite.config.ts` 中已配置开发代理：

```ts
proxy: {
  '/api/python': 'http://127.0.0.1:8000',
  '/api/a2a': 'http://127.0.0.1:8080',
}
```

生产部署时，建议由 Nginx/网关统一代理，前端不直接暴露 Admin API Key。

### 7.4 构建

```powershell
npm run build
```

产物输出到 `frontend/dist/`，可部署到任意静态文件服务器。

---

## 8. 安全注意事项

1. **Admin API Key 不得暴露到浏览器**：当前演示环境直接输入 API Key，生产环境应通过后端代理或 OAuth 登录换取短期 token。
2. **Secret 值不脱敏不展示**：Secret 管理页面只展示引用名和作用域，不展示明文。
3. **本地 YAML 文件读取限制**：开发模式下通过 `axios.get('/config/...')` 读取，生产部署需确保 `config/` 目录不暴露敏感信息。
4. **CORS**：生产环境应配置后端 `CORSMiddleware` 只允许可信前端域名。

---

## 9. 设计原则

- **简洁实用**：避免过多动画、图标、宣传文案；以表格、表单、标签为主。
- **信息密度适中**：管理后台优先展示关键字段，详情用抽屉或弹窗承载。
- **一致性**：所有页面使用 Element Plus 组件，保持颜色、间距、交互一致。
- **可扩展性**：API 层、状态层、视图层分离，后端接口补齐时只改 `api/` 和 `stores/`。
- **渐进增强**：第一期用静态 YAML + 已有接口快速可用，后续逐步替换为动态 API。
