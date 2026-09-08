"""可插拔执行器抽象与实现。

v0.20.0 只实现 MCPExecutor；v0.21.0 新增 HTTPExecutor，通过 ExecutorRegistry
零侵入扩展 REST API 工具执行。
"""

from __future__ import annotations

from loop_controller.executors.base import ExecutionContext, ExecutorRegistry, ToolExecutor
from loop_controller.executors.harness_executor import HarnessExecutor
from loop_controller.executors.harness_models import (
    DockerBackendConfig,
    HarnessBackendConfig,
    HarnessSandboxConfig,
    HarnessToolSpec,
    HTTPBackendConfig,
    SubprocessBackendConfig,
)
from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.executors.http_models import (
    HTTPAuthConfig,
    HTTPResponseMapping,
    HTTPToolSpec,
)
from loop_controller.executors.local_function_executor import LocalFunctionExecutor
from loop_controller.executors.local_function_models import (
    LocalFunctionSandboxConfig,
    LocalFunctionSpec,
)
from loop_controller.executors.mcp_executor import MCPExecutor

__all__ = [
    "DockerBackendConfig",
    "ExecutionContext",
    "ExecutorRegistry",
    "HarnessBackendConfig",
    "HarnessExecutor",
    "HarnessSandboxConfig",
    "HarnessToolSpec",
    "HTTPAuthConfig",
    "HTTPBackendConfig",
    "HTTPClient",
    "HTTPExecutor",
    "HTTPResponseMapping",
    "HTTPToolSpec",
    "LocalFunctionExecutor",
    "LocalFunctionSandboxConfig",
    "LocalFunctionSpec",
    "MCPExecutor",
    "SubprocessBackendConfig",
    "ToolExecutor",
]
