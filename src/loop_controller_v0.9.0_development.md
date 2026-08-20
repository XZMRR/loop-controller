# Loop Controller v0.9.0 开发指南：生产环境考研（真实 Agent + 真实工具）

> **状态**：已完成。
> **目标**：不引入新治理架构能力，而是用真实 MCP server、真实 Python Agent 对当前 v0.8.0 架构进行端到端压测，暴露真实生产环境下的问题并修复。

---

## 1. 背景与目标

v0.5.0-v0.8.0 已经把 R0-R3 闭环、MCP Proxy、审批恢复、持久化状态机等核心能力补齐。但这些验证主要依赖 mock server 和小型单测。

真实生产环境会有 mock 测不出的问题：

- 真实 MCP server 的 schema 更复杂，工具描述/参数类型多样；
- 真实 Agent 按自己逻辑重试，对 `require_approval` JSON 的解析不可控；
- stdio/SSE 跨进程通信有竞态、超时、进程清理问题；
- 长会话下 budget / audit / session 的累积行为；
- 审批 UX：Agent 如何获取 decision_id、人如何在 CLI 和 Agent 界面间切换。

v0.9.0 的目标：

- 接入真实 MCP server（filesystem、fetch、sqlite）；
- 编写一个真实 Python MCP client agent；
- 设计 3-5 个真实治理场景；
- 跑通并修复暴露的问题；
- 验证当前架构在真实工具链下的可用性。

---

## 2. 范围与边界

### 2.1 纳入 v0.9.0

| # | 内容 | 优先级 |
|---|---|---|
| 1 | 接入 `@modelcontextprotocol/server-fetch` | P0 |
| 2 | 接入 `@modelcontextprotocol/server-sqlite` | P0 |
| 3 | 接入 `web_search` 真实 server（可选，Brave/Google 需要 API key，优先用 fetch 模拟） | P1 |
| 4 | 更新 `config/mcp_servers.yaml` | P0 |
| 5 | 更新 `config/profiles.yaml` 增加真实场景 profile | P0 |
| 6 | 更新 `policies/default.rego` 覆盖新工具 | P0 |
| 7 | 编写 `examples/research_agent.py` 作为真实 MCP client | P0 |
| 8 | 设计并跑通 3-5 个真实场景 | P0 |
| 9 | 修复暴露的集成问题 | P0 |
| 10 | 更新 README/文档，展示真实使用 | P1 |

### 2.2 明确不纳入

- 不新增 R0/R1/R2/R3 架构组件（不实现 Earned Authority / Permission Interaction Analyzer / R3 分析）；
- 不修改已有核心模型接口；
- 不做多 Agent 交互；
- 不引入 LLM 推理；
- 所有改动都是集成、配置、示例和 bugfix。

---

## 3. 真实工具规划

### 3.1 已接入

| 工具 | MCP server | 当前状态 | 用途 |
|---|---|---|---|
| `read_file` / `write_file` | `@modelcontextprotocol/server-filesystem` | ✅ 已接入 | 文件读写 |
| `send_email` | `loop_controller.mocks.email_server` | ✅ mock | 邮件通知 |
| `web_search` | `loop_controller.mocks.email_server` | ⚠️ 挂在 mock 上 | 搜索 |

### 3.2 计划接入

| 工具 | MCP server | 用途 |
|---|---|---|
| `fetch_url` | `@modelcontextprotocol/server-fetch` | HTTP GET，用于获取网页/接口数据 |
| `query_database` | `@modelcontextprotocol/server-sqlite` | 只读查询内部数据库 |
| `update_database` | `@modelcontextprotocol/server-sqlite` | 写数据库（高风险） |

### 3.3 工具层级

```
低风险：web_search, read_file, query_database
中风险：write_file, fetch_url
高风险：send_email（外部通信）
极高风险：update_database（修改数据）
```

---

## 4. 真实场景设计

### 场景 1：研报 Agent

**任务**：查资料并写报告。

**工具链**：
- `web_search` 或 `fetch_url` 收集信息
- `read_file` 读取 `/data/kb/**`
- `write_file` 写入 `/data/output/**`

**期望治理**：
- `write_file` 路径限制在 `/data/output/**`；
- 写入 `/etc/` 或 `C:\Windows\` → deny；
- 大量写入时触发 budget 限制。

### 场景 2：数据查询 Agent

**任务**：查询内部数据库。

**工具链**：
- `query_database` 查询 `/data/company.db`

**期望治理**：
- `SELECT` 允许；
- `DROP`/`DELETE`/`UPDATE` 自动 deny；
- 命中敏感表 → require_approval。

### 场景 3：外部通知 Agent

**任务**：完成分析后发邮件通知。

**工具链**：
- `send_email`

**期望治理**：
- 收件人 `*@company.com` → require_approval；
- 收件人 `@gmail.com` → deny；
- 审批通过后可重试。

### 场景 4：组合风险 Agent

**任务**：读取文件并立刻发送到外部。

**工具链**：
- `read_file` + `send_email`

**期望治理**：
- 单独 `read_file` allow；
- 单独 `send_email` require_approval；
- 但如果在短窗口内先 `read_file` 后 `send_email`，应触发 session_risk 升级（当前风险状态机已支持）。

### 场景 5：越权 Agent

**任务**：尝试写入系统目录。

**工具链**：
- `write_file` 到 `/etc/passwd` 或 `C:\Windows\test.txt`

**期望治理**：
- 直接 deny。

---

## 5. 真实 Agent 设计

`examples/research_agent.py`：

- 使用 `mcp` Python SDK 作为 stdio client；
- 通过 stdio 启动 `lc proxy`；
- 列出工具；
- 按 scenario 调用工具；
- 处理 `require_approval` 响应，打印 decision_id；
- 支持 `--scenario` 参数选择场景。

```bash
# 启动 OPA
lc opa-start

# 研报场景
python examples/research_agent.py --scenario research

# 外部通知场景
python examples/research_agent.py --scenario notify

# 越权场景
python examples/research_agent.py --scenario exfil
```

---

## 6. 配置更新

### 6.1 `config/mcp_servers.yaml`

新增 fetch 和 sqlite server。

### 6.2 `config/profiles.yaml`

新增 `research_assistant_v2`：

```yaml
profiles:
  research_assistant_v2:
    max_budget_token: 100000
    tools:
      web_search: { allowed: true, max_calls_per_task: 10 }
      read_file: { allowed: true, allowed_args: { path: "/data/kb/**" }, max_calls_per_task: 20 }
      write_file: { allowed: true, allowed_args: { path: "/data/output/**" }, max_calls_per_task: 5 }
      fetch_url: { allowed: true, max_calls_per_task: 5 }
      query_database: { allowed: true, allowed_args: { sql: "SELECT*" }, max_calls_per_task: 10 }
      send_email:
        allowed: true
        require_approval: true
        allowed_args: { to: "*@company.com" }
        max_calls_per_task: 1
```

### 6.3 `policies/default.rego`

覆盖新工具：

- `fetch_url`：允许，但记录风险；
- `query_database`：解析 SQL，非 SELECT → deny；
- `update_database`：deny 或 require_approval。

---

## 7. 验收标准

- 5 个场景至少跑通 3 个；
- 暴露的问题有对应的修复或文档说明；
- `pytest tests/` 仍然全部通过；
- `ruff check src tests` 干净；
- README 增加"真实 Agent 使用"章节。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 真实 MCP server 依赖 npm/Node | 已确认 npx 可用 |
| sqlite server 需要数据库文件 | 在 `data/` 下创建示例数据库 |
| fetch server 需要网络 | 用 `http://localhost` 或 `file://` 先测试 |
| 真实 Agent 代码增加维护成本 | 放在 `examples/` 目录，不进入核心包 |
| 改动过大影响既有测试 | 每次修改后跑全量测试 |
