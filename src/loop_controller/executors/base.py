"""执行器抽象：ToolExecutor / ExecutionContext / ExecutorRegistry。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loop_controller.models import CapabilityProfile, Tool, ToolResult


class ExecutionContext:
    """执行器运行时的治理上下文。"""

    def __init__(
        self,
        *,
        call_id: str,
        task_id: str,
        agent_id: str,
        user_id: str,
        session_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.call_id = call_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.user_id = user_id
        self.session_id = session_id
        self.tenant_id = tenant_id


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行器协议。

    实现者负责把规范化工具名和参数转换为真实副作用，并返回 ToolResult。
    """

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """执行工具调用并返回结果。"""
        ...

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回按 Profile 过滤后的工具列表。"""
        ...


class ExecutorRegistryError(Exception):
    """执行器注册表错误。"""


class ExecutorRegistry:
    """工具名到执行器的注册表，支持按工具注册和默认执行器回退。"""

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}
        self._default: ToolExecutor | None = None

    def register(self, tool_name: str, executor: ToolExecutor) -> None:
        """为特定工具名注册执行器。"""
        if not isinstance(executor, ToolExecutor):
            raise TypeError(f"执行器 {executor!r} 不符合 ToolExecutor 协议")
        self._executors[tool_name] = executor

    def set_default(self, executor: ToolExecutor) -> None:
        """设置默认执行器；当工具没有显式注册时回退到默认执行器。"""
        if not isinstance(executor, ToolExecutor):
            raise TypeError(f"执行器 {executor!r} 不符合 ToolExecutor 协议")
        self._default = executor

    def get_executor(self, tool_name: str) -> ToolExecutor:
        """按工具名获取执行器；不存在且没有默认执行器时抛出异常。"""
        executor = self._executors.get(tool_name)
        if executor is not None:
            return executor
        if self._default is not None:
            return self._default
        raise ExecutorRegistryError(f"工具 {tool_name!r} 没有注册执行器")
