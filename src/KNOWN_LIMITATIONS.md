# 已知局限（Known Limitations）

> 本文件列出 Loop Controller MVP（v0.1.0）**明确声明的能力边界**。每一条都是设计决策的结果，不是缺陷；但使用者必须据此判断当前版本是否适用于自己的场景。**不得在对外材料中声称本版本具备下列未实现的能力。**

---

## 安全相关局限

### L1. 审计哈希链对"最后一行"的删改需依赖 seal 记录

哈希链中，第 N 行的完整性由第 N+1 行的 `prev_hash` 承诺。文件末尾的最后一行没有后继，删除或篡改它无法被 `verify_chain()` 直接检出；但若此前写过 seal 记录，则删除 seal 之后的事件会破坏 seal 的 `chain_hash` 校验，删除 seal 之前的事件会破坏 seal 的 `prev_hash` 链接。

- **当前缓解**：`JsonlAuditStore.seal()` 可手动或周期性调用；启用 HMAC-SHA256 时 seal 记录还受 `seal_key` 域分离签名保护；
- **生产路径**：定期写 seal 记录 + WORM 存储 + 签名日志（post-MVP）。

### L2. 默认 `sha256` 模式下低熵参数可被字典攻击

审计日志默认 `hash_algo=sha256` 时 `args_hash = SHA-256(canonical_json(arguments))`。对已知邮箱、常见文件名等低熵参数，攻击者可彩虹表反推。

- **当前缓解**：P0 已支持 `hmac-sha256`（通过 `LOOP_CONTROLLER_AUDIT_HASH_ALGO=hmac-sha256` + `LOOP_CONTROLLER_AUDIT_HMAC_KEY` 环境变量启用），HMAC key 从环境变量读取、event key 与 seal key 做域分离；
- **升级触发条件**：任何涉及真实 PII 的部署必须启用 HMAC-SHA256。默认 sha256 仅用于无敏感数据的开发/演示。

### L3. 防重放与会话风险状态依赖单进程 asyncio 假设

`DecisionStore` 的"检查 + 记账"原子性与 `RiskStateStore` 的 JSONL 追加均建立在**单进程、单事件循环、无并行写入** 的运行时假设上。

- **当前缓解**：假设已在架构文档 §6.6 / v1.2 §3.2 显式声明；
- **生产路径**：多 worker / 多进程部署前，必须将 DecisionStore 与 RiskStateStore 一起升级为同一套原子语义（SQLite `INSERT OR FAIL` 或分布式锁）。**违反此前提的部署不在安全承诺范围内。**

### L4. 策略明文存储

Rego 策略文件为明文，仅依赖文件系统权限保护。恶意 Agent 若能读取策略文件，可探测完整控制逻辑。

- **当前缓解**：建议 `chmod 600 policies/`；
- **生产路径**：加密存储 + HSM/TEE/密钥代理受信解密（post-MVP）。

### L5. token 预算是估算值，非真实计量

预算按 `mcp_servers.yaml` 中各工具的 `cost_per_call` 固定估算值计费，不含工具返回内容进入 LLM context 的实际消耗。

- **当前缓解**：调用次数上限（`max_calls_per_task`）独立生效，提供硬兜底；
- **生产路径**：LLM usage 上报 + 工具结果长度折算（post-MVP）。

---

## 功能边界（设计内缺失，非缺陷）

| # | 边界 | 说明 |
|---|---|---|
| F1 | 审批为配置打桩 | `ConfigR0Delegate` 按 `approval.yaml` 固定返回 approve/deny，无真实人类通知与 UI |
| F2 | 无 Agent 间交互治理 | 只治理 `tool_call`；多 Agent 委托、inter_agent 均未实现 |
| F3 | 无 Earned Authority | 权限固定，无任务后临时提权；`fixed_ceiling` 保留为空 |
| F4 | ~~LLMPlanner 未实现~~ 已实现（T3.5） | 默认仍关闭（`config/llm_planner.yaml`），开启后由 LLM 动态规划；密钥仅来自环境变量，失败不重试 |
| F5 | 权限组合规则为静态 YAML | 无图分析/能力代数；规则需人工维护 |
| F6 | 审计全量记录无采样 | 高负载场景需自行评估日志量 |
| F7 | 财务支付预算未启用 | `payment_amount` 恒为 0 |

完整演进计划见方案文档 §9.3 post-MVP 路线图。

---

## 环境备注

- **Windows 开发机**：关闭 MCP stdio 子进程时会打 anyio cancel-scope 的 WARNING 日志（mcp SDK 2.x 已知行为），不影响主链路；CI（Linux）下不应出现，出现即说明容错逻辑误吞了正常路径。
- **CI 的 e2e 测试**使用 FakeGateway 替代真实 MCP server；真实链路（stdio 转发）由发布前手动 gate 覆盖（见发布检查清单）。
