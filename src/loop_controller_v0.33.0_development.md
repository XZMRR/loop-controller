# v0.33.0 工具治理层健壮性加固（一）：SDK 与 API 入口安全

> 一句话目标：**堵住 Agent SDK、MCP Proxy、HTTP REST API 与配置校验中当前最危险的安全、稳定与正确性漏洞，使 `@governed` 主路线和网络接入面达到可生产部署的基线。**
>
> 范围限定：本版本聚焦 Python 工具治理层内部的 SDK、API 入口与工程化；执行器/审计耐久性等深度改造放在 v0.34.0，分层职责文档放在 v0.35.0。

- 状态：**已完成**
- 前置版本：v0.32.0 接入方式收敛与审批后自动重试
- 版本性质：健壮性加固第一阶段
- 核心范围：Agent SDK 并发安全、API 入口安全与稳定、配置校验 fail-closed、CI 与测试策略
- 验证目标（已完成）：
  - `pytest tests -m "not integration" -q` 全绿：702 passed, 57 skipped, 22 deselected；
  - `pytest tests/integration -m integration -q` 保持 22 skipped/passed（取决于 OPA 二进制是否可用）；
  - `python -m ruff check src tests` 通过；
  - 新增关键安全路径的单元测试：HTTP REST API 5 个、MCP Proxy 6 个、Agent SDK 与配置校验若干。

---

## 1. 背景

v0.32.0 完成了接入方式收敛与 `@governed` 审批后自动重试，集成测试达到 22 passed。但代码审查发现，当前代码仍处于“功能正确但健壮性不足”的状态，存在可被绕过的认证、并发安全隐患，以及配置校验不严格、测试策略不合理等问题。

v0.33.0 要解决的**不是**继续加功能，而是把现有三条接入线（`@governed`、MCP Proxy、HTTP REST API）上的高严重问题堵住，为后续 v0.34.0 的执行器/审计深度加固和 v0.35.0 的治理分层文档打好基础。

---

## 2. 当前问题清单（本次版本处理）

### P0-1：Agent SDK 存在并发与状态管理隐患

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| SDK-H1 | `GovernanceRuntime.current()` 使用全局类属性，多协程/多线程会互相覆盖运行时上下文 | `src/loop_controller/agent_sdk.py:31–77` | 高 |
| SDK-M1 | `GovernanceResult._controller/_runtime` 作为普通字段参与深拷贝，可能引发副作用 | `src/loop_controller/models.py:509–523` | 中 |
| SDK-M2 | `hook_tool_registry` 替换过程缺少原子性与回滚，中途失败会导致注册表半替换 | `src/loop_controller/agent_sdk.py:177–185` | 中 |
| SDK-M3 | `wait_for_approval` 超时后未清理底层审批请求，可能留下待审批垃圾状态 | `src/loop_controller/models.py:549–565` | 中 |
| SDK-M4 | `GovernanceRuntime` 缺少异步上下文管理器，用户容易忘记 `aclose()` | `src/loop_controller/agent_sdk.py:79–142` | 中 |
| SDK-L1 | `_controller/_runtime` 会出现在 `model_dump()` 序列化输出中 | `src/loop_controller/models.py:509–510` | 低 |

### P0-2：LangChain 示例健壮性不足

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| LC-H1 | 同步包装器丢弃位置参数 | `examples/integrations/langchain_example.py:65–72` | 高 |
| LC-M1 | 未使用 `functools.wraps` 保留原函数元数据 | `examples/integrations/langchain_example.py:65–84` | 中 |
| LC-L1 | 未透传 `_loop_controller_*` 治理保留参数 | `examples/integrations/langchain_example.py:62–63` | 低 |

### P0-3：MCP Proxy 存在安全绕过与稳定性风险

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| MCP-H1 | SSE mTLS 身份可被反向代理 Header 伪造绕过 | `src/loop_controller/proxy_server.py:283–313` | 高 |
| MCP-H2 | admin 工具（kill_switch、revoke）未校验 agent 是否属于 admin profile | `src/loop_controller/proxy_server.py:346–358, 578–581` | 高 |
| MCP-H3 | 缺少全局异常处理器，未捕获异常可能泄露堆栈或挂起 SSE 连接 | `src/loop_controller/proxy_server.py:333–336` | 高 |
| MCP-H4 | SSE handler 直接访问 `request._send`，无断开/取消处理 | `src/loop_controller/proxy_server.py:333–336` | 高 |
| MCP-H5 | MCP Proxy 缺少限流与请求体大小限制 | `src/loop_controller/proxy_server.py:242–252` | 高 |
| MCP-H7 | 错误响应携带原始异常信息，可能泄露内部状态 | `src/loop_controller/proxy_server.py:595–597, 687–690, 710–712, 752–764, 778–780` | 高 |
| MCP-M1 | SSE 长连接无保活/空闲超时 | `src/loop_controller/proxy_server.py:328–344` | 中 |
| MCP-M2 | stdio 传输未处理对端断开/EOF | `src/loop_controller/proxy_server.py:273–281` | 中 |

### P0-4：HTTP REST API 安全与错误处理缺陷

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| HTTP-H1 | 缺少全局异常处理器，未捕获异常落到 Starlette 默认 500 | `src/loop_controller/server.py:906–976` | 高 |
| HTTP-H2 | 缺少限流与请求体大小限制 | `src/loop_controller/server.py:326–330, 388–390` | 高 |
| HTTP-M1 | API Key 校验存在运算符优先级缺陷与 `compare_digest` 长度异常 | `src/loop_controller/server.py:158–173` | 中 |
| HTTP-M2 | 未配置 CORS | `src/loop_controller/server.py:906–910` | 中 |
| HTTP-M3 | CLI server 不支持 TLS/mTLS 参数 | `src/loop_controller/cli.py:333–336` | 中 |
| HTTP-M4 | Query 参数解析缺少校验，非法输入产生 500 | `src/loop_controller/server.py:420–421, 472–473, 829–830` | 中 |

### P0-5：配置校验与工程化

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| CFG-H1 | `_check_dirs_writable` 未覆盖 session/task/预算等关键持久化路径 | `src/loop_controller/infra/config_loader.py:782–799` | 高 |
| CFG-H2 | 多处 YAML→模型构造未统一包装为 `ConfigValidationError` | `src/loop_controller/infra/config_loader.py` 多处 | 高 |
| DOC-H1 | README 引用不存在的示例文件 | `README.md:152` | 高 |
| DOC-H2 | `KNOWN_LIMITATIONS.md` 与 `DockerBackendConfig` 实际行为不一致 | `src/KNOWN_LIMITATIONS.md:253–256` | 高 |
| CI-H1 | CI 直接跑全部测试，未区分 integration | `.github/workflows/ci.yml:56–59` | 高 |
| CI-H2 | OPA fixture 存在端口抢占 TOCTOU 风险 | `tests/conftest.py:70–73` | 高 |

---

## 3. 设计原则

1. **Fail-closed**：认证、授权、配置校验失败时默认拒绝，不暴露内部信息。
2. **最小权限**：admin 工具只对明确授权的 agent profile 开放；Harness 只透明白名单环境变量。
3. **状态隔离**：`GovernanceRuntime` 不再依赖全局可变类属性，改用 `contextvars` 保证单线程协程内的上下文安全。
4. **原子性**：`hook_tool_registry` 等批量替换操作要么全部成功，要么回滚，不留半替换状态。
5. **防御性接口**：所有网络入口增加统一异常处理、限流、请求体上限、参数校验，避免 DoS 与信息泄露。
6. **CI 分层**：集成测试与单元测试分离，避免重测试拖慢日常 CI。

---

## 4. 详细设计

### 4.1 Agent SDK 并发安全改造

#### 4.1.1 `GovernanceRuntime` 上下文改为 `ContextVar`

当前实现：

```python
class GovernanceRuntime:
    _current: GovernanceRuntime | None = None
```

改造后：

```python
import contextvars

_RUNTIME_CTX: contextvars.ContextVar[GovernanceRuntime] = contextvars.ContextVar("loop_controller_runtime")

class GovernanceRuntime:
    @classmethod
    def current(cls) -> GovernanceRuntime:
        try:
            return _RUNTIME_CTX.get()
        except LookupError:
            raise RuntimeError("No GovernanceRuntime is active")

    @classmethod
    def set_current(cls, rt: GovernanceRuntime) -> contextvars.Token[GovernanceRuntime]:
        return _RUNTIME_CTX.set(rt)

    @classmethod
    def reset_current(cls, token: contextvars.Token[GovernanceRuntime] | None = None) -> None:
        if token is not None:
            _RUNTIME_CTX.reset(token)
        else:
            try:
                _RUNTIME_CTX.reset(_RUNTIME_CTX.get())
            except LookupError:
                pass
```

**兼容性**：现有同步代码 `set_current(rt)` / `reset_current()` 仍可工作；返回 token 是为了支持嵌套上下文。

#### 4.1.2 `GovernanceResult` 内部引用改为 `PrivateAttr`

```python
class GovernanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    _controller: Any = PrivateAttr(default=None)
    _runtime: Any = PrivateAttr(default=None)
```

- 避免深拷贝内部对象；
- 避免序列化泄露；
- `with_controller` 直接返回 `model_copy(deep=True)` 后再设置 PrivateAttr。

#### 4.1.3 `hook_tool_registry` 原子替换

实现两阶段写入：

```python
def hook_tool_registry(self, registry: Any, *, exclude: set[str] | None = None) -> None:
    ...
    # 阶段 1：构造所有 governed_fn，不修改注册表
    replacements: dict[str, Any] = {}
    for name in tool_names:
        replacements[name] = self._wrap_tool(name, original_fn)

    # 阶段 2：一次性替换；若失败则回滚
    try:
        for name, governed_fn in replacements.items():
            _set_tool(registry, name, governed_fn)
    except Exception:
        for name in replacements:
            _set_tool(registry, name, originals[name])
        raise
```

#### 4.1.4 `wait_for_approval` 超时清理

`wait_for_approval` 超时后调用 controller 提供的取消接口（或 approval_manager 的撤销方法）清理待审批请求：

```python
async def wait_for_approval(...):
    try:
        ...
    except asyncio.TimeoutError:
        await controller.cancel_approval(self.request_id)
        raise GovernanceDeniedError(...)
```

若 controller 暂无 `cancel_approval`，则先实现 `ApprovalManager.revoke_request` 并在 controller 暴露。

#### 4.1.5 `GovernanceRuntime` 异步上下文管理器

```python
@classmethod
async def from_config(cls, path: str, *, agent_id: str | None = None, user_id: str | None = None) -> GovernanceRuntime:
    ...

async def __aenter__(self) -> GovernanceRuntime:
    set_current(self)
    return self

async def __aexit__(self, exc_type, exc, tb) -> None:
    await self.aclose()
```

示例：

```python
async with GovernanceRuntime.from_config("config/workdir") as rt:
    result = await my_governed_tool(...)
```

---

### 4.2 LangChain 示例修复

- `_make_sync_wrapper` 支持位置参数：按 `args_schema` 字段名把位置参数映射为关键字参数；
- 使用 `functools.wraps(original_run)` 保留元数据；
- 在 `_invoke` 中分离 `_loop_controller_*` 保留参数并透传给 `rt.call()`。

---

### 4.3 MCP Proxy 安全加固

#### 4.3.1 解决 mTLS Header 伪造

`_resolve_sse_identity` 不再无条件信任反向代理转发的证书 Header。方案二选一：

- **方案 A（推荐）**：从 Uvicorn/ASGI SSL 套接字直接获取对端证书，仅当存在真实 TLS 客户端证书时才构造身份；
- **方案 B**：维护可信代理 IP/网络列表，并对转发的 Header 做 HMAC 签名校验。

本版本先实现**方案 A**，即当 `client_ca_cert` 配置存在时，优先使用 ASGI 的 `scope["client"]` 与底层 SSL 套接字信息，而不是 HTTP Header。

#### 4.3.2 admin 工具权限隔离

在调用 `trigger_kill_switch`、`revoke_decision` 等 admin handler 前检查：

```python
if agent.profile_id not in self._admin_profile_ids:
    return self._error_result("admin access denied", error_code="admin_forbidden")
```

`_admin_profile_ids` 从 `entrypoints.yaml` 的 `admin.agent_profiles` 读取，默认为空列表（默认关闭 admin 工具）。

#### 4.3.3 全局异常处理器

在 `build_mcp_app()` 中注册：

```python
@app.exception_handler(Exception)
async def universal_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled exception in MCP proxy")
    if request.url.path.endswith("/sse"):
        # 关闭 SSE 流
        await request.close()
    return JSONResponse({"error": "internal_error"}, status_code=500)
```

#### 4.3.4 SSE 连接取消与保活

- 使用官方 `request.send()` 替代 `request._send`；
- `connect_sse` 外层加 `try/except/finally`，捕获 `asyncio.CancelledError` 后清理 `SseServerTransport`；
- 每 30 秒发送一次 SSE comment ping，超过 120 秒无活动则主动关闭连接。

#### 4.3.5 限流与请求体限制

- MCP Proxy 增加基于内存的 rate limiter（按 agent_id + IP）；
- 设置最大请求体大小 `MAX_MCP_BODY_SIZE = 1MB`；
- SSE 并发连接数上限可配置，默认 100。

#### 4.3.6 错误响应脱敏

所有 `self._error_result(str(exc))` 改为：

```python
logger.exception("MCP handler failed: %s", exc)
return self._error_result("internal_error", error_code="internal_error")
```

对外只返回统一错误码与固定文案。

---

### 4.4 HTTP REST API 安全加固

#### 4.4.1 全局异常处理器

在 `build_app()` 中注册 `HTTPException` 与通用异常处理器：

```python
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)
```

返回统一 JSON：

```json
{"error": "<code>", "message": "<fixed text>"}
```

#### 4.4.2 API Key 校验修复

```python
def _check_api_key(self, header: str | None, token: str | None) -> bool:
    candidates = []
    if header:
        candidates.append(header)
    if token:
        candidates.append(token)
    for candidate in candidates:
        if len(candidate) == len(self._api_key):
            if hmac.compare_digest(candidate, self._api_key):
                return True
    return False
```

避免运算符优先级与 `compare_digest` 长度异常问题。

#### 4.4.3 限流、请求体与 CORS

- 增加 `RateLimitMiddleware`（基于内存，按 client identity + path）；
- 设置 `MAX_HTTP_BODY_SIZE`，超过返回 413；
- 从 `entrypoints.yaml` 读取 `cors.origins` 并注册 `CORSMiddleware`。

#### 4.4.4 Query 参数校验

所有 `int()` / `float()` 转换加 `try/except`，返回 400：

```python
try:
    limit = int(request.query_params.get("limit", "100"))
except ValueError:
    return JSONResponse({"error": "invalid_parameter", "message": "limit must be int"}, 400)
```

#### 4.4.5 TLS/mTLS CLI 参数

`lc server` 新增：

```
--ssl-certfile
--ssl-keyfile
--ssl-client-ca
```

传给 `uvicorn.Config`。

---

### 4.5 配置校验 fail-closed

#### 4.5.1 扩展 `_check_dirs_writable`

覆盖 `AppConfig` 中所有持久化文件路径：

```python
paths_to_check = [
    config.audit_log_path,
    config.decision_log_path,
    config.risk_state_path,
    config.conversation_path,
    config.approval_store_path,
    config.session_path,
    config.task_store_path,
    config.budget_ledger_path,
    config.reservation_store_path,
    config.authority_log_path,
    config.alert_store_path,
]
```

#### 4.5.2 统一 `ConfigValidationError`

所有 `_load_*` 方法在构造 Pydantic/dataclass 时包装异常：

```python
try:
    agent = Agent(**item)
except (ValidationError, TypeError) as exc:
    raise ConfigValidationError(f"agents[{idx}] invalid: {exc}") from exc
```

---

### 4.6 测试与 CI 策略

#### 4.6.1 CI 拆分

`.github/workflows/ci.yml` 改为两个 job：

```yaml
unit:
  run: pytest tests/ -m "not integration" -q

integration:
  run: pytest tests/integration -m integration -q
  timeout-minutes: 30
  # 允许重试
```

#### 4.6.2 OPA 端口 TOCTOU 修复

让 OPA 绑定 `0` 端口，fixture 解析实际端口：

```python
proc = subprocess.Popen([opa, "run", "--addr", "127.0.0.1:0", ...])
# 读取 OPA 日志或查询 /health 获取实际端口
```

或复用单一 session-scoped `opa_server`，避免子 fixture 重复启动。

#### 4.6.3 新增测试覆盖

- `tests/test_agent_sdk.py`
  - `test_contextvar_runtime_isolation`
  - `test_hook_registry_atomic_rollback`
  - `test_wait_for_approval_timeout_cancels_request`
  - `test_governed_wait_for_approval_sync`
- `tests/test_proxy_server_security.py`（新建）
  - `test_admin_tool_requires_admin_profile`
  - `test_error_response_does_not_leak_internal`
  - `test_rate_limit_blocks_excessive_requests`
- `tests/test_server_security.py`（新建或补充）
  - `test_api_key_compare_digest_safe`
  - `test_invalid_query_param_returns_400`
  - `test_unhandled_exception_returns_json`
- `tests/test_config_loader.py`
  - 扩展 `test_check_dirs_writable_covers_all_paths`
  - 补充 `test_validation_error_wrapped_in_config_validation_error`

---

## 5. 配置变更

### 5.1 `entrypoints.yaml` 新增 admin profile 配置

```yaml
admin:
  agent_profiles:
    - admin_profile
```

### 5.2 `entrypoints.yaml` 新增 CORS 配置

```yaml
http:
  cors:
    origins:
      - "http://localhost:3000"
```

### 5.3 新增限流配置（可选）

```yaml
rate_limit:
  requests_per_minute: 120
  burst: 20
```

---

## 6. 接口变更

### 6.1 新增

- `GovernanceRuntime.set_current()` 返回 `contextvars.Token`
- `GovernanceRuntime.reset_current(token=None)` 支持 token 回滚
- `GovernanceRuntime.__aenter__` / `__aexit__`
- `LoopController.cancel_approval(request_id)`（或 `ApprovalManager.revoke_request`）
- `entrypoints.yaml` 增加 `admin.agent_profiles`、`http.cors`、`rate_limit`

### 6.2 修改

- `GovernanceResult._controller/_runtime` 改为 `pydantic.PrivateAttr`
- `hook_tool_registry` 改为两阶段原子替换
- `_resolve_sse_identity` 不再无条件信任证书 Header
- admin 工具增加 profile 校验
- HTTP/MCP 错误响应统一脱敏

### 6.3 保留

- `@governed` 语义不变（仅内部运行时上下文机制升级）
- MCP Proxy 协议不变
- HTTP REST API 路径与成功响应格式不变

---

## 7. 测试计划

### 7.1 单元测试

- `tests/test_agent_sdk.py`：ContextVar 隔离、PrivateAttr、原子回滚、超时清理、同步审批等待。
- `tests/test_proxy_server_security.py`：admin 权限隔离、错误脱敏、限流。
- `tests/test_server_security.py`：API Key 安全、参数校验、全局异常处理器。
- `tests/test_config_loader.py`：目录可写覆盖、配置异常包装。

### 7.2 集成测试

- `tests/integration/test_functional_agent.py`：保持现有用例通过，验证 `@governed` 行为未变。
- `tests/integration/test_mcp_proxy.py`：保持现有 MCP 路径通过。
- `tests/integration/test_langchain_agent.py`：验证位置参数与元数据修复。

### 7.3 CI 验证

- `pytest tests/ -m "not integration" -q` 全绿；
- `pytest tests/integration -m integration -q` 22 passed；
- `python -m ruff check src tests` 通过。

---

## 8. 验收标准

1. `GovernanceRuntime.current()` 使用 `ContextVar`，多协程上下文互不干扰；
2. `GovernanceResult.model_dump()` 不再输出 `_controller` / `_runtime`；
3. `hook_tool_registry` 替换失败时能回滚到原始函数；
4. `@governed(wait_for_approval=True)` 超时后审批请求被清理；
5. LangChain 示例支持位置参数并保留原函数元数据；
6. MCP Proxy admin 工具在非 admin profile 下返回 `admin_forbidden`；
7. MCP Proxy 错误响应不暴露原始异常信息；
8. HTTP server 全局异常返回统一 JSON，不泄露堆栈；
9. API Key 校验对长度不一致、优先级错误均安全返回 `False`；
10. Query 参数非法返回 400 而非 500；
11. `_check_dirs_writable` 覆盖所有持久化路径；
12. 配置加载 `ValidationError` 统一包装为 `ConfigValidationError`；
13. README 示例路径、`KNOWN_LIMITATIONS` 与代码一致；
14. CI 拆分为 unit 与 integration 两个 job；
15. OPA fixture 消除端口抢占 TOCTOU；
16. 全量测试保持通过。

**验收结论**：以上 16 项验收标准在 v0.33.0 开发周期内全部达成。单元测试基线 `pytest tests/ -m "not integration" -q` 为 702 passed / 57 skipped / 22 deselected；`ruff check src tests` 全绿；集成测试在有效 OPA 二进制环境下保持 22 passed（本地无有效 OPA 时 22 skipped）。

---

## 9. 非目标

- **执行器深度改造**：Harness 沙箱强化、Docker 容器泄漏、审计 O(n²) 等进入 v0.34.0；
- **治理分层文档**：Python 工具治理层与 Go 交互治理层职责划分进入 v0.35.0；
- **新增接入方式**：本版本不新增 A2A、GraphQL、WebSocket；
- **前端审批 UI**：仍通过轮询/状态查询实现；
- **多租户 SaaS**：控制平面仍假设本地部署。

---

## 10. 风险与回退

| 风险 | 缓解 |
|---|---|
| `ContextVar` 改动影响现有单线程同步用法 | 保持 `set_current()` / `reset_current()` 无参调用兼容，返回 token 作为可选增强 |
| MCP Proxy mTLS 改造影响现有反向代理部署 | 增加配置开关 `trust_proxy_headers: bool`（默认 `false`），旧部署可显式开启 |
| admin 工具默认关闭导致现有集成测试失败 | 测试 fixture 配置 `admin.agent_profiles: ["admin_profile"]`，保持测试通过 |
| 全局异常处理器改变 HTTP 响应格式 | 仅影响 500/未捕获异常路径；成功响应和已有错误码保持不变 |
| 限流中间件可能误伤正常高并发 | 限流配置可选，默认关闭或高阈值，生产可显式调低 |

---

## 11. 备注

- v0.33.0 是“工具治理层跑稳”第一阶段，优先解决安全与稳定性漏洞；
- v0.34.0 将进入执行器、审计、审批链路的耐久性与故障恢复；
- v0.35.0 在工具治理层稳定后，输出与 Go 交互治理层的分层职责文档，并启动 A2A/Go 内核设计。
