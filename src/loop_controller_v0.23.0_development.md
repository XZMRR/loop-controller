# v0.23.0 开发文档：Sandboxed Local Function Executor

## 1. 目标

v0.22.0 通过 HTTP Executor 把 "不需要包装成 MCP Server 的 REST API 工具" 纳入治理。
v0.23.0 要再把 "本地 Python 函数" 也纳入同一套 `ExecutorRegistry` / `Checkpoint` 治理链路，
让 Agent 可以调用企业内部已有的 Python 工具函数（如数据处理、文件解析、计算等），
同时提供**进程级沙箱隔离**作为安全底线。

> **一句话目标**：本地 Python 函数可以像 MCP 工具、HTTP 工具一样被 Loop Controller 治理，
> 并在独立子进程中受限执行，避免 Agent 的函数调用直接污染主进程。

v0.23.0 只做以下三件事：

1. **Local Function Executor**：让 `ExecutorRegistry` 支持注册本地函数工具；
2. **粗粒度沙箱**：通过子进程隔离 + 超时 + 环境变量白名单（叠加系统必要变量） + 文件路径白名单实现最小沙箱；
3. **配置化注册**：通过 `config/local_functions.yaml` 声明式注册函数，无需改代码。

## 2. 背景与动机

### 2.1 为什么需要本地函数执行

企业生产环境中已有大量内部 Python 函数/库：

- 公司内部的数据处理脚本；
- 与私有系统交互的 SDK 函数；
- 计算密集型函数（加密、校验、转换）。

这些函数如果都要包装成 MCP Server 或 HTTP 服务才能被治理，会增加大量胶水代码和运维负担。
Loop Controller 需要支持直接调用本地 Python 函数，同时保持治理闭环一致。

### 2.2 为什么需要沙箱

本地函数与 Loop Controller 主进程共享 Python 运行时，存在以下风险：

- 函数可以任意读写文件系统、访问网络、导入任意模块；
- 函数可能因死循环或内存泄漏拖垮整个治理进程；
- 函数异常可能导致主进程崩溃。

v0.23.0 通过**子进程隔离**把函数执行从主事件中剥离，是最小可行沙箱。
真正细粒度沙箱（seccomp、chroot、容器）由部署层在 v0.24+ 增强。

### 2.3 与 HTTP/MCP 执行器的关系

| 执行器 | 隔离级别 | 适用场景 |
|---|---|---|
| MCPExecutor | 子进程（外部 MCP Server） | 官方/第三方 MCP 工具 |
| HTTPExecutor | 网络边界 | REST API 工具 |
| LocalFunctionExecutor | 子进程（本地函数） | 企业内部 Python 函数 |

三者统一实现 `ToolExecutor`，`Checkpoint` 无需感知执行器类型。

## 3. 设计原则

1. **进程隔离**：每个本地函数调用启动一个独立 Python 子进程，主进程通过 stdin/stdout JSON 通信。
2. **fail-closed**：函数加载失败、执行超时、返回格式非法、违反沙箱策略时返回错误 `ToolResult`，不抛异常中断治理链路。
3. **声明式注册**：函数通过 `module:function` 路径在 YAML 中注册，运行期用 `importlib` 加载。
4. **向后兼容**：不影响 MCP/HTTP 工具；本地函数配置缺失时行为与 v0.22.0 完全一致。
5. **不替代真实沙箱**：v0.23.0 的隔离是粗粒度子进程隔离，生产高危函数仍应部署到容器/VM。

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/local_function_models.py` | 新增 | `LocalFunctionSpec`、`LocalFunctionSandboxConfig` |
| `src/loop_controller/executors/local_function_runner.py` | 新增 | 子进程内函数加载与执行入口 |
| `src/loop_controller/executors/local_function_executor.py` | 新增 | `LocalFunctionExecutor` 实现 `ToolExecutor` |
| `src/loop_controller/executors/__init__.py` | 修改 | 导出本地函数相关类型 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/local_functions.yaml`，纳入工具校验 |
| `src/loop_controller/runtime.py` | 修改 | 创建 `LocalFunctionExecutor` 并注册 |
| `src/loop_controller/models.py` | 可能修改 | 如需要为 ToolResult 增加沙箱相关字段 |
| `config/local_functions.yaml` | 新增 | 示例本地函数注册配置 |
| `tests/test_local_function_executor.py` | 新增 | 本地函数执行与沙箱测试 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.23.0 边界 |
| `src/development_log.md` | 修改 | 追加 v0.23.0 记录 |
| `src/loop_controller_v0.23.0_development.md` | 新增 | 本文档 |

## 5. 配置模型

### 5.1 `config/local_functions.yaml`

```yaml
tools:
  calculate_checksum:
    module: myapp.local_tools
    function: calculate_checksum
    description: 计算文件 SHA-256 校验和
    input_schema:
      type: object
      properties:
        path: {type: string}
      required: [path]
    cost_per_call: 100
    default_risk: medium
    sandbox:
      timeout_seconds: 10
      max_output_bytes: 65536
      allowed_paths: ["/data/input/**", "/data/output/**"]
      env_whitelist: ["PYTHONPATH", "HOME"]
```

### 5.2 Python 模型

```python
class LocalFunctionSandboxConfig(BaseModel):
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)

class LocalFunctionSpec(BaseModel):
    tool_name: str
    module: str
    function: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    cost_per_call: int = 0
    default_risk: RiskLevel = "medium"
    sandbox: LocalFunctionSandboxConfig = Field(default_factory=LocalFunctionSandboxConfig)
```

函数签名约定：

- 同步函数：`def fn(**kwargs) -> Any`
- 异步函数：`async def fn(**kwargs) -> Any`
- 子进程 runner 将统一用 `asyncio.run()` 或 `anyio` 处理。

为简化 v0.23.0，先只支持同步函数；异步函数在 runner 中通过 `asyncio.run()` 调用亦可。

## 6. 执行流程

### 6.1 主进程：`LocalFunctionExecutor.execute()`

1. 按 `tool_name` 找到 `LocalFunctionSpec`；
2. 构造 runner 命令：`python -m loop_controller.executors.local_function_runner`；
3. 根据 `env_whitelist` 构造子进程环境变量（空白名单时继承当前环境，非空时保留白名单 + 系统必要变量），并修正 `PYTHONPATH` 指向源码包；
4. 向子进程 stdin 写入 JSON 任务，包含 `module`、`function`、`arguments`、沙箱参数；
5. 异步等待子进程 stdout，读取结果 JSON；
6. 若超时，kill 子进程，返回 `local_function_timeout`；
7. 若返回非法，返回 `local_function_invalid_output`；
8. 否则封装为 `ToolResult(status="success", content=...)`。

### 6.2 子进程：`local_function_runner.py`

1. 从 stdin 读取 JSON 任务，包含 `module`、`function`、`arguments`、沙箱参数；
2. 可选：设置导入钩子，只允许导入白名单模块（v0.23.0 可选，默认不限制，留接口）；
3. 若 `allowed_paths` 非空，重写 `builtins.open()`，限制文件访问只能在 `allowed_paths` glob 内；
5. `importlib.import_module(module)`，获取函数；
6. 调用函数（同步直接调用，异步用 `asyncio.run()`）；
7. 将结果 JSON 序列化写入 stdout；
8. 异常时写入错误 JSON，`error_code` 为 `local_function_runtime_error`。

### 6.3 错误码

| error_code | 含义 |
|---|---|
| `local_function_not_found` | 工具未注册 |
| `local_function_import_error` | 模块/函数加载失败 |
| `local_function_timeout` | 子进程执行超时 |
| `local_function_invalid_output` | 子进程输出非法 JSON |
| `local_function_runtime_error` | 函数执行抛异常 |
| `local_function_sandbox_violation` | 违反路径/网络等沙箱策略 |

## 7. Runtime 集成

`build_runtime()` 中：

```python
local_specs = config.local_function_specs
local_executor = LocalFunctionExecutor(local_specs)
for name in local_specs:
    executor_registry.register(name, local_executor)
```

`ConfigLoader._check_tool_mapping()` 中 `all_tools` 需要包含 `config.local_function_specs` 的 key。

## 8. 验收标准

1. `pytest tests/` 全部通过；
2. `ruff check src tests examples` 无错误；
3. `mypy src` 无新增错误；
4. `config/local_functions.yaml` 中声明的函数可被 `LocalFunctionExecutor` 调用；
5. 函数返回结果正确映射到 `ToolResult.content`；
6. 超时场景返回 `local_function_timeout`；
7. 函数抛异常返回 `local_function_runtime_error`，不中断主进程；
8. 非法输出返回 `local_function_invalid_output`；
9. 路径白名单违反返回 `local_function_sandbox_violation`；
10. MCP / HTTP 工具不受影响；
11. 更新 `src/KNOWN_LIMITATIONS.md` 与 `src/development_log.md`。

## 9. 不做的事

| 不做 | 原因 |
|---|---|
| 细粒度代码级沙箱（AST 白名单/RestrictedPython） | 复杂且容易绕过；v0.23 用子进程隔离 |
| 容器化 / seccomp / chroot | 部署层增强，超出本版本范围 |
| 网络/CPU/内存 cgroup 限制 | 需要 OS 级能力，v0.24+ |
| 函数代码热更新 | 函数实现变更需重启；配置热更新仍由 HotReloader 负责 |
| 支持任意 Python 对象参数/结果 | 只支持 JSON 可序列化类型 |

## 10. 风险与回退

| 风险 | 缓解措施 |
|---|---|
| 子进程启动开销大 | 单次调用启动子进程；未来可池化 |
| 函数可以导入危险模块 | 通过导入钩子与部署层文件权限双重控制 |
| 超时 kill 子进程遗留 | Windows 下使用 `proc.kill()`；必要时清理进程组 |
| 路径白名单绕过 | 仅做简单 `open` 包装；高危函数应放容器 |
| 返回结果过大撑爆内存 | 限制 `max_output_bytes`，超限时截断/报错 |

## 11. 后续版本预告

| 版本 | 目标 |
|---|---|
| v0.24.0+ | KMS/Vault Secret Backend、HTTP 缓存、本地函数容器化沙箱 |
