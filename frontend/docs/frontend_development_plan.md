# Loop Controller 前端开发规划

> 分支：`frontend/main`  
> 目录：`frontend/`  
> 目标：为公司展示构建一个干净、简洁、实用的 Loop Controller 管理控制台  
> 版本基线：后端 `v0.48.0`

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

### 长线开发顺序与决策记录（2026-09-15）

当前已确认进入长线开发阶段，不再只做静态展示或单次演示。

#### 已确认的推进顺序

1. **管理配置闭环**
   - Agent / Profile / Identity / Entrypoints 的在线查看、编辑、保存、reload、审计。
   - 目标：先把“配置能不能长期维护”做扎实，形成管理平台底座。
2. **审批与审计闭环**
   - 待审批、审批历史、审批详情、审批筛选、审计分页筛选、SSE/实时刷新、审计追踪。
   - 目标：把治理运营链路做成可追踪、可复盘、可演示。
3. **A2A 接入**
   - Agent Card、Task、Message、Delegation、跨 Agent 协同。
   - 目标：在前面两个底座稳定后，再接入多 Agent 协作。

#### 已确认的技术决策

- **演示主线**：先围绕“企业研究助手”做完整流程，原因是现有 `researcher_001`、`web_search`、`send_email`、OPA Profile 与审批链路最适合先跑通真实故事。
- **多 Agent 底座要求**：虽然第一阶段先用单一 Agent 演示，但数据模型、API 契约、页面结构、筛选方式和权限设计都必须按“多 Agent、多 Profile、多 Owner、多租户可扩展”预留。
- **鉴权策略**：短期继续保留 Admin API Key 用于联调；同时逐步补最小后端 Session 登录，为长期运行做准备。
- **存储策略**：现阶段继续使用 JSONL / 文件存储，但所有新接口按分页、筛选、稳定契约设计，避免未来切 SQLite / 数据库时返工。
- **配置生效策略**：Profile 优先支持“保存后统一 reload”；Identity / Entrypoints 先明确区分“可热更新字段”和“需要重启字段”。
- **安全原则**：Identity、Secret、Token 等信息默认脱敏，不回显明文；管理操作必须写审计；前端 YAML fallback 逐步收紧。

#### 长线开发为什么这样排

- 先做管理配置闭环，是因为没有稳定的 Agent / Profile / Identity 维护能力，审批、审计、A2A 都只会停留在演示层。
- 再做审批与审计闭环，是因为这是治理系统最重要的可运营能力，也是管理层最容易理解和验收的部分。
- 最后接 A2A，是因为 A2A 依赖前面稳定的身份、配置、审计和审批语义，过早接入会放大接口返工成本。

#### 进度记录（2026-09-15，第二轮）

- **GitHub 推送问题已解决**：根因是全局 git 代理指向未运行的 `127.0.0.1:7890`；`gh` 完成登录（keyring）后，绕过代理直连可正常推送。后续如需长期避免，应清除全局 proxy 配置或保持代理常驻。
- **审批与审计闭环基础能力落地**：新增 `GET /v1/admin/approvals`（分页契约 `approvals/total/limit/offset`，支持 `status/agent_id/tool_name/requester_id/approver_id` 筛选），审计接口补齐 `agent_id`/`tool_name` 筛选；前端审批页升级为“审批中心”（待审批/历史双 Tab + 筛选 + 分页）。后端 61 测试全绿。
- **Agent 详情接口落地（多 Agent 底座）**：新增 `GET /v1/admin/agents/{agent_id}`，返回 `AdminAgentDetail`（在列表字段基础上扩展 `description`、`metadata`），404/401/503 语义完整；前端 Agent 管理页新增“详情”操作列与 Drawer 详情视图，接口不可用时回退列表行数据。后端 67 测试全绿，前端构建通过。
- **实现注意**：审批历史查询不改 `ApprovalStore` Protocol，只给 `JSONLApprovalStore` 具体类加只读 `requests`/`responses` 视图属性，server 端 `getattr` 防御，避免对所有 store 实现造成兼容压力；verdict 序列化用 `getattr(record.verdict, "value", record.verdict)` 兼容枚举与字符串。

#### 进度记录（2026-09-15，第三轮：Profile 工具策略在线编辑与统一 reload）

- **后端新增接口**：`PUT /v1/admin/profiles/{profile_id}/tools`（工具权限整体替换：先经 `ToolPermission` 模型校验，再原子写回 `profiles.yaml`，随后从磁盘重载并原地刷新运行时共享映射，全程写审计）；`POST /v1/admin/profiles/reload`（放弃内存修改，从磁盘统一重载全部 Profile）。
- **统一 reload 底座**：`HotReloader` 纳入 `profiles.yaml` 监视，文件被手工编辑后按轮询周期自动热更新；`Runtime` 新增 `config_dir` 字段供管理接口定位配置文件；`ConfigLoader.reload_profiles()` 与既有 http/harness/revocation 重载方法同构。
- **写回实现**：新增 `infra/profile_config.py`，采用“加载-修改-全量写回 + 紧凑序列化（省略默认值）”，已知取舍是首次写回后文件注释与排版不保留（PyYAML 限制），已在页面提示。
- **前端**：工具策略页升级为在线编辑（每 Profile“编辑”对话框：允许/需审批开关、调用上限、参数黑白名单 JSON 编辑器、增删工具），保存即热更新；“从磁盘重载”按钮对应统一 reload；数据加载优先在线 API，失败回退静态 YAML。
- **验证**：后端 72 个 server 测试全绿（新增 5 个：写回+热更新+审计、未知 Profile 400、非法权限 400 且不落盘、磁盘直改后 reload 同步、无 config_dir 503）；全量 869 通过（3 个 go_kernel 集成测试错误为既有环境问题，与本轮改动无关，已 stash 基线复核）；前端构建通过。

#### 进度记录（2026-09-15，第四轮：身份、审计、配置底座收口）

- **最小后端 Session 登录落地**（对应已确认决策“短期保留 API Key 联调，同时补最小 Session 登录”）：新增 `POST /v1/admin/session/login`（验证 API Key 后签发 8 小时随机 token）与 `POST /v1/admin/session/logout`（吊销当前 token）；`_check_api_key` 接受 `Authorization: Bearer <session-token>` 作为 API Key 的替代凭据，审计 actor 区分 `api-key:` 与 `session:` 前缀。存储为进程内存（重启全失效，接口预留持久化替换空间），登录/登出均写审计。
- **前端登录改造**：登录时优先换取 Session Token（旧后端无该端点自动降级 API Key 直连）；axios 拦截器优先携带 Bearer Session Token；退出时先调用后端吊销再清理本地状态。
- **审计底座现状**：管理侧所有变更操作（吊销、Kill Switch、审批决策、Profile 编辑/重载、Session 登录/登出）均落审计，actor 可区分来源。
- **配置底座现状**：Profile 在线编辑 + 统一 reload + profiles.yaml 热更新已闭环；Identity/Entrypoints 仍为只读脱敏展示（热更新字段拆分未做，属长期项）。
- **验证**：后端 76 个 server 测试全绿（新增 4 个 session 用例）；前端构建通过。

#### 进度记录（2026-09-15，第五轮：路由守卫收口 + A2A 接入第一步）

- **路由级权限守卫**：路由守卫此前已存在（未登录跳 `/login`、已登录访问登录页跳首页），本轮补齐缺口——axios 响应拦截器统一处理 401：清理本地凭据、3 秒防抖提示并跳回登录页；登录页自身的 401 不触发跳转，避免循环。
- **A2A 接入第一步（治理视角，不依赖 Go 内核在线）**：
  - `GoKernelBridge` 新增 `ping()`（GET /a2a/v1/agents 探测可达性）与 `list_agents()`（兼容列表/包装两种响应）。
  - 新增管理接口：`GET /v1/admin/a2a/status`（启用/可达/地址/本地 Agent Card 配置）、`GET /v1/admin/a2a/agents`（配置 Agent 与内核注册态合并视图，内核不可达时 registered=null 语义明确）、`GET /v1/admin/a2a/tasks/{task_id}`（任务状态查询；内核未启用 fail-closed 返回 503）。
  - 前端 A2A 治理页从占位升级为三张卡：内核状态（含未启用/不可达引导提示）、Agent 注册状态表、任务查询。
  - 为后续预留：Delegation / route_message / cancel_task 的 bridge 方法已存在，下一步可做管理端发起委托与任务取消。
- **验证**：后端 80 个 server 测试全绿（新增 4 个 A2A 用例）；前端构建通过。

#### 进度记录（2026-09-15，第六轮：管理端发起委托入口）

- **后端**：新增 `POST /v1/admin/a2a/delegations`。管理端发起的委托与 Agent 自发委托走完全相同的治理路径：`InteractionGovernanceEngine.evaluate()`（Profile/信任/委托深度校验 → OPA interaction 策略）→ allow/modify 后经 Go 内核 `request_delegation` 派发；require_approval 返回升级对象由人工跟进；deny 不派发。全程写交互审计（`build_audit_event` 同一语义，提案标记 `interaction_context="admin-console"` 便于区分来源）。引擎支持构造注入（`build_app(interaction_engine=...)`），默认懒构造。
- **前端**：A2A 治理页新增“发起委托”卡片——发起/目标 Agent 下拉、能力名、风险等级、参数 JSON；结果区展示判定（allow/modify/require_approval/deny）、原因、Decision/Interaction ID、升级对象与派发信息；require_approval 给出人工跟进提示。
- **验证**：后端 84 个 server 测试全绿（新增 4 个：allow 派发、deny 跳过派发、未知 Agent 400、无内核时派发标记跳过）；前端构建通过。

#### 进度记录（2026-09-15，第七轮：Go 内核启动与完整链路验证）

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

#### 进度记录（2026-09-15，第八轮：任务生命周期闭环——取消与 SSE 流式）

- **后端新增两个管理接口**（[server.py](file:///D:/Agent/loop-controller-latest-develop/src/loop_controller/server.py)）：
  - `POST /v1/admin/a2a/tasks/{task_id}/cancel`：透传 `bridge.cancel_task`，内核不可达/拒绝返回 502，无 bridge 503（fail-closed）；
  - `GET /v1/admin/a2a/tasks/{task_id}/stream`：`StreamingResponse` SSE 转发 `bridge.stream_task`。
- **前端**（[python.ts](file:///D:/Agent/loop-controller-latest-develop/frontend/src/api/python.ts)、[A2A.vue](file:///D:/Agent/loop-controller-latest-develop/frontend/src/views/A2A.vue)）：
  - `cancelA2ATask` + `streamA2ATask`（fetch + ReadableStream 实现 SSE，因 EventSource 无法带 Authorization 头；自动解包内核事件信封取 `payload` 任务快照，收到终态自动断开）；
  - 任务查询卡：状态 tag、非终态任务的"取消任务"按钮、"订阅状态流/停止订阅"按钮、订阅中标识；组件卸载自动断开流。
- **联调发现的语义细节**：Go 内核终态拼写为 `cancelled`（双 l），前端终态集合已兼容两种拼写。
- **完整链路验证**：发起委托 → task pending → 管理端 cancel → 任务 `cancelled`；SSE 流依次收到 `task_created`(pending) 与 `task_cancelled`(cancelled) 两帧。
- **验证**：`tests/test_server.py` 86 测试全绿（新增 cancel 200/502/503 与 stream SSE 内容/503）；前端构建通过。

---

### 第一阶段：核心控制台（当前已完成脚手架 + 基础页面）

目标：不依赖后端新增接口，前端可独立运行并展示核心能力。

| 模块 | 功能 | 数据来源 |
|---|---|---|
| 登录页 | 输入 Python Runtime / Go A2A 地址、Admin API Key，测试 `/health` | `GET /health` |
| 仪表盘 | 服务状态、待审批数、Agent 数、Kill Switch 状态 | `/health`, `/v1/admin/approvals/pending`, `config/agents.yaml`, `/admin/revocation-list` |
| 审批台 | 待审批列表、approve/deny + comment、刷新 | `/v1/admin/approvals/pending`, `/v1/admin/approvals/{id}/approve|deny` |
| Agent 管理 | 已注册 Agent 列表、用户列表 | `config/agents.yaml` |
| 工具策略 | Profile 工具权限表（allow/deny/require_approval/参数限制） | `config/profiles.yaml` |
| 审计查询 | 按 session/task/agent/tool 筛选审计事件 | `/v1/admin/audit` |
| 系统配置 | 只读展示 entrypoints/identity、吊销操作、Kill Switch | `config/entrypoints.yaml`, `config/identity.yaml`, `/admin/revoke`, `/admin/kill-switch` |
| A2A 治理 | 预留页面 | Go A2A Kernel（后续） |

### 第二阶段：后端接口补齐后替换静态配置

当后端补齐 `frontend_api_gaps.md` 中的接口后，前端做以下替换：

| 原静态读取 | 替换为后端 API |
|---|---|
| `config/agents.yaml` | `GET /v1/admin/agents` |
| `config/profiles.yaml` | `GET /v1/admin/profiles` |
| `config/entrypoints.yaml` | `GET /v1/admin/entrypoints` |
| `config/identity.yaml` | `GET /v1/admin/identity`（脱敏） |
| 工具策略只读 | `PUT /v1/admin/profiles/{id}/tools/{name}` |
| 审批无历史 | `GET /v1/admin/approvals` |

### 第三阶段：增强与扩展

- SSE 实时审批推送（已预留 `createApprovalSSE`）；
- A2A Agent Card / Task / Delegation 可视化；
- 角色视图切换（技术人员 vs 管理人员）；
- Secret 管理页面；
- 配置热重载；
- 审批转交；
- 审计导出 CSV/JSON。

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

### 6.1 立即可做（不依赖后端）

- [ ] 安装依赖并验证脚手架可运行：`cd frontend && npm install && npm run dev`
- [ ] 完善各页面错误处理与 loading 状态
- [ ] 审批台接入 SSE 实时推送
- [ ] 优化仪表盘数据刷新（定时轮询）
- [ ] 工具策略页支持参数可视化编辑（仅前端 mock）
- [ ] 审计查询支持时间范围筛选
- [ ] 添加审批详情抽屉（展示完整参数、风险原因）
- [ ] 添加单元测试与类型检查

### 6.2 需要后端配合

- [x] 后端补齐 `GET /v1/admin/agents`
- [x] 后端补齐 `GET /v1/admin/profiles`
- [x] 后端补齐 `GET /v1/admin/identity`（脱敏）
- [x] 后端补齐 `GET /v1/admin/entrypoints`
- [x] 后端补齐 `POST /v1/admin/govern/evaluate`（只读调试）
- [ ] 后端实现配置热重载 `POST /v1/admin/reload`
- [ ] 后端扩展审批历史查询 `GET /v1/admin/approvals`
- [ ] 后端修复 Go Kernel control token 桥接

### 6.3 长线开发底座任务（当前主路线）

#### 管理配置闭环

- [ ] 审计查询支持 `agent_id` / `tool_name` 等稳定筛选契约
- [ ] 登录地址策略收口：联调模式固定代理，生产模式反向代理
- [ ] Agent 列表升级为多 Agent 可扩展数据结构
- [ ] 新增 `GET /v1/admin/users` 或合并用户视图数据源
- [ ] 新增 Agent 详情接口与页面
- [ ] 新增 Profile 工具策略保存接口
- [ ] 新增统一配置 reload 接口
- [ ] Identity / Entrypoints 在线编辑与“需重启字段”提示
- [ ] 管理操作统一写审计并支持追踪
- [ ] 逐步收紧前端 YAML fallback，敏感配置禁止回退到明文文件

#### 审批与审计闭环

- [ ] 审批历史接口按分页和筛选设计
- [ ] 审批详情抽屉或详情页
- [ ] 审批实时刷新（SSE 或短期 ticket 方案）
- [ ] 审计接口分页、筛选、时间范围
- [ ] 从审批到审计的完整链路追踪视图

#### A2A 接入

- [ ] 建立真实 `frontend/src/api/a2a.ts`
- [ ] Agent Card / Task / Message / Delegation 页面
- [ ] Python -> Go Kernel control token 桥接
- [ ] 多 Agent 协作演示链路

### 6.4 当前优先实施顺序

1. 修复审计 `agent_id` / `tool_name` 筛选契约。
2. 收口登录地址驱动与联调代理策略。
3. 增加审批历史接口与页面基础能力。
4. 开始做 Agent 详情与多 Agent 兼容结构。
5. 开始做 Profile 工具策略在线编辑与统一 reload。
6. 接入 A2A 前，先稳定身份、审计和配置底座。

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
