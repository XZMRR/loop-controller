这些图片共同构成了一份关于 **AI Agent 工具治理** 的技术分析文档。核心论点是：**对 Agent 工具的治理权不自动来自协议（如 MCP），而是来自对凭证、网络、执行环境等基础设施的控制。** 文档将工具分为四类，并提出了三种获取治理权的方法，最后讨论了工具目录的必要性。

---

### 一、工具类型与治理方式

#### 1. MCP 工具
- **原始路径**：Agent → MCP Server → 真实工具。
- **治理后路径**：Agent → Loop Controller MCP Proxy → 策略/审批 → MCP Gateway → 原始 MCP Server。
- **治理要点**：
  - Agent 只能连接代理，原始 MCP Server 不直接暴露。
  - 下游凭证只交给代理，网络策略禁止绕过。
  - 治理权来源于：控制 Server 地址 + 控制凭证 + 控制网络，而非 MCP 协议本身。

#### 2. HTTP/API 工具
- **示例**：邮件 API、GitHub API、支付 API、CRM API。
- **治理后路径**：Agent → Loop Controller → 策略/审批 → Executor（使用真实 API Key）→ 外部 API。
- **治理要点**：
  - 凭证从 Agent 环境移走，仅存于 Executor 或 Secret Broker。
  - Agent 只能提交工具名和参数，由 Executor 代为调用。
  - 治理权来源于：控制凭证 + 控制网络出口 + 代理执行。若无法控制网络但能移走凭证，仍可管控大部分需认证 API；若 Agent 可自行获取凭证，则无法完全阻断。

#### 3. 本地函数/框架内置工具
- **示例**：LangChain Tool、OpenAI function_tool、AutoGen Function、Python 函数、Cursor/IDE 内置操作。
- **治理方式**：通过 Adapter（如 LangChain Adapter、OpenAI Agents Adapter）包装成受治理工具，路径为 Agent → Wrapped Tool → ToolGovernor → LoopController。
- **治理要点**：
  - 宿主程序必须保证工具列表只有包装后的工具，原始函数和凭证不暴露。
  - **强治理**：将真实函数移到独立 Executor，Agent 进程只保留远程调用 Stub。
  - 若函数与 Agent 同进程且 Agent 有任意代码执行能力，则安全边界脆弱。

#### 4. Shell、文件系统、浏览器等内置能力
- **示例**：Python `open()` 修改文件、`subprocess` 执行 PowerShell、浏览器自动化完成转账、原生网络库调外部接口。
- **治理方式**：不能仅靠包装工具，必须通过操作系统或运行环境治理。
- **具体措施**：容器/沙箱、文件系统挂载权限（只读目录）、独立工作目录、OS 用户权限、seccomp/AppArmor、网络 ACL/Egress Proxy、Browser Gateway、Shell Executor、Workspace Snapshot、临时凭证、Harness Runtime 控制。
- **工具示例**：
  - **文件工具**：Agent 沙箱只访问临时 Workspace → 高风险写入经 File Executor → 策略/审批 → 快照后写入真实目录。
  - **Shell 工具**：Agent 提交命令提议 → Shell Executor 解析并执行策略 → 容器内受限执行。
  - **浏览器工具**：Agent 提交浏览器操作提议 → Browser Gateway → 风险识别/审批 → 受控浏览器会话执行。
- **治理权来源**：控制运行环境 + OS 权限 + 文件挂载 + 网络出口，需要 Harness 或沙箱配合，非单个 SDK 能解决。

---

### 二、获得治理权的三种方法

无论工具类型，治理权本质上通过以下三种方式获得：

#### 方法一：拦截（协作式治理权）
- **路径**：Agent 调工具 → Wrapper/Hook/Adapter → Loop Controller。
- **优点**：接入简单、开发成本低、适合开源/开发版，能快速覆盖 LangChain、OpenAI Agents、AutoGen。
- **缺点**：依赖宿主配合，易有漏洞，Agent 有其他执行路径时可绕过，**不适合作为强安全边界**。

#### 方法二：代理（代理执行治理权）
- **路径**：Agent 只提交调用意图 → Loop Controller/Executor 代为执行。
- **优点**：能绑定批准参数、防止审批后换参数、记录真实执行结果、执行前备份、集中管理凭证。
- **缺点**：若 Agent 仍持有原始凭证仍可绕过；需为不同类型工具开发 Executor。
- **当前项目**：`LoopController.evaluate_and_execute()`（`controller.py`）已采用“判断后执行”方向。

#### 方法三：垄断出口（强制性治理权）
- **条件**：Agent 无凭证 + Agent 无法直连目标 + 所有调用只能经过 Executor。
- **依赖**：Secret Broker、网络策略、IAM、容器/沙箱、专属工具账户、受控文件系统、受控 Browser/Shell、上游系统只信任 Executor。
- **效果**：只有这一档能较强地承诺“拒绝后不会从其他路径执行”。

---

### 三、关于工具目录

- **需要可治理范围的工具目录**，但仅有目录不够。
- 工具目录可通过多种方式产生（具体方式图片未完整展示）。

---

### 总结

该文档系统性地阐述了 AI Agent 工具治理的架构思路，强调 **治理权的本质是对执行路径和凭证的控制**，而非依赖特定协议或框架。治理强度从“协作式”（拦截）到“强制性”（垄断出口）逐级增强，实际落地需根据工具类型选择合适的治理组合（代理、沙箱、凭证管理、网络控制等）。

需要可治理范围的工具目录，但仅有目录不够
工具目录可以通过以下方式产生：

MCP tools/list 自动发现；
框架 Adapter 注册；
OpenAPI/JSON Schema 导入；
企业 Connector 配置；
Harness Tool Manifest；
静态配置；
管理员登记；
Agent 启动时声明。
工具目录至少应记录：

text



tool_id
tool_type
schema
executor
credential_ref
owner
risk_profile
data_scope
allowed_agents
side_effect_level
backup_strategy
但不能依赖 Agent 自己说：

text



“我只有这些工具。”
因为 Agent 还可能通过：

Shell；
Python 代码；
HTTP Client；
浏览器；
文件系统；
动态插件；
产生未登记的副作用。

所以企业治理真正要登记的不是所有函数，而是：

所有可能产生重要副作用的能力和出口。

即使不知道 Agent 内部每个函数，只要控制：

text



网络
凭证
文件系统
Shell
浏览器
数据库连接
云 IAM
仍然可以控制最终副作用。

反过来，即使知道 Agent 的全部 Tool Schema，但没有控制这些出口，也不能形成强治理。


五、针对“外来 Agent”怎么办
可以分三类处理。

1. 可改造的外部 Agent
它支持：

MCP；
HTTP Tool Callback；
Framework Tool Adapter；
UHP/Harness；
自定义 Tool Provider。
那么可以把工具替换成受治理入口，并将其放入受控运行环境。

这种外部 Agent也可以达到较强治理，不要求必须自研。

2. 不可改代码，但可以控制运行环境
可以使用：

容器；
Egress Proxy；
文件挂载；
MCP 配置重定向；
IAM；
API Gateway；
Browser Gateway。
即使无法修改 Agent 内部，也可以在基础设施层控制其副作用。

3. 既不能改造，也不能控制运行环境
例如：

运行在第三方平台；
自带凭证；
自带网络出口；
不提供 Hook；
无法获取工具事件。
这种 Agent 只能：

接收结果；
观察外部日志；
做外围风险监控；
告警；
事后审计。
不能承诺工具级实时阻断。