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

- [ ] 后端补齐 `GET /v1/admin/agents`
- [ ] 后端补齐 `GET /v1/admin/profiles`
- [ ] 后端补齐 `GET /v1/admin/identity`（脱敏）
- [ ] 后端补齐 `GET /v1/admin/entrypoints`
- [ ] 后端补齐 `POST /v1/admin/govern/evaluate`（只读调试）
- [ ] 后端实现配置热重载 `POST /v1/admin/reload`
- [ ] 后端扩展审批历史查询 `GET /v1/admin/approvals`
- [ ] 后端修复 Go Kernel control token 桥接

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
