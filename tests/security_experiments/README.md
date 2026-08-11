# Agent 安全现有手段测试环境

> **目标**：通过实际运行现有 Agent 安全方案，理解其能力边界，为 Loop Controller 设计提供实证输入。
> **当前阶段**：已完成 T1 OpenAI Agents SDK Guardrails 测试和 T3 MCP 工具权限边界测试。
>
> **数据声明**：测试中使用的文档、姓名、邮箱、手机号、项目代号、密码等均为**虚构合成数据**，仅用于验证 Guardrail 对敏感信息的识别与过滤能力。

---

## 重要前提：Guardrail 需要部署吗？

**不需要。**

OpenAI Agents SDK 是一个 Python 库，Guardrail 是运行在你本地 Python 进程中的函数。你不需要部署任何额外的服务或 Agent。

测试脚本里的 Agent 是在运行时临时创建的，调用的是你配置的 LLM API（OpenAI 官方或兼容服务如 Kimi）。

---

## 快速开始

### 1. 创建虚拟环境

```bash
cd tests/security_experiments
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
# source venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 API Key 和 Base URL

```bash
# Windows
copy .env.example .env

# Linux/Mac
# cp .env.example .env
```

编辑 `.env` 文件：

```env
# OpenAI 官方 API
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL 留空即可

# 或 Kimi OpenAI 兼容 API
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.moonshot.cn/v1
OPENAI_MODEL=kimi-k2.5
```

### 4. 运行测试

```bash
cd t1_openai_agents_guardrails

# 建议先从最简单的开始
python t1_4_goal_hijack.py

# 然后测试多轮对话场景
python t1_4b_goal_hijack_multiturn.py

# 再测试信息泄露型目标劫持
python t1_4c_info_exfiltration.py

# 最后测试 Guardrail 机制
python t1_1_input_guardrail.py
python t1_2_output_guardrail.py
python t1_3_tool_guardrail.py

# T3 MCP 工具权限边界测试（不需要 LLM API）
cd ../t3_mcp_permission_boundary

# 基础版
python t3_mcp_client_test.py

# 真实攻击场景版（含路径遍历、符号链接绕过）
python t3_mcp_client_realistic_test.py
```

---

## 关于 Kimi 配置的说明

Kimi 开放平台提供 **OpenAI 兼容 API**，可以用 OpenAI Agents SDK 直接调用。

但需要注意两点：

1. **Base URL**：设置为 `https://api.moonshot.cn/v1`
2. **API 类型**：OpenAI Agents SDK 默认使用 OpenAI Responses API，但 Kimi 等第三方服务通常只支持 Chat Completions API。

测试脚本中的 `_config.py` 会自动检测：如果 `OPENAI_BASE_URL` 不是 `openai.com`，则切换到 Chat Completions API。

Kimi 常用模型名：

- `moonshot-v1-8k`
- `moonshot-v1-32k`
- `moonshot-v1-128k`

---

## 测试目录

| 编号 | 文件 | 测试主题 | 关注重点 |
|------|------|----------|----------|
| T1.1 | `t1_1_input_guardrail.py` | 输入 Guardrail：提示注入检测 | Guardrail 接口如何嵌入 Agent 生命周期 |
| T1.2 | `t1_2_output_guardrail.py` | 输出 Guardrail：敏感信息过滤 | 输出校验阶段与控制粒度 |
| T1.3 | `t1_3_tool_guardrail.py` | 工具 Guardrail：高风险动作拦截 | 工具调用前的策略校验 |
| T1.4 | `t1_4_goal_hijack.py` | 目标劫持：间接提示注入 | 现有框架对目标锁定的缺失 |
| T1.4b | `t1_4b_goal_hijack_multiturn.py` | 多轮对话目标劫持 | 上下文建立后的注入 |
| T1.4c | `t1_4c_info_exfiltration.py` | 信息泄露型目标劫持 | 不直接越权，通过总结泄露敏感信息 |
| T3 | `t3_mcp_client_test.py` | MCP 工具权限边界（基础） | Client 侧 Policy Gateway + Server enforcement |
| T3-realistic | `t3_mcp_client_realistic_test.py` | MCP 工具权限边界（真实攻击场景） | 路径遍历、符号链接绕过、越权写入 |

---

## 测试心态

不要关注"Guardrail 能不能防住攻击"。关注：

1. 现有框架把控制点放在哪里；
2. 现有框架如何表达策略（代码级函数 vs 声明式配置）；
3. Guardrail 触发后系统如何响应；
4. 每次检查带来的成本和延迟；
5. 现有方案缺少什么（这正是 Loop Controller 的机会）。

---

## 记录模板

每个测试运行后，请在 [test_results.md](./test_results.md) 中记录：

```markdown
## T1.1 输入 Guardrail 测试结论

### 测试配置
- 模型：...
- Guardrail 类型：...

### 能做什么
- ...

### 不能做什么
- ...

### 对 Loop Controller 的启发
- ...
```
