"""执行器抽象：ToolExecutor / ExecutionContext / ExecutorRegistry。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loop_controller.execution_mode import ExecutionMode
from loop_controller.models import CapabilityProfile, Tool, ToolResult

if TYPE_CHECKING:
    from loop_controller.execution_mode import ExecutionModeResolver


def extract_declared_secret_refs(arguments: dict[str, Any]) -> list[str]:
    """递归提取调用参数声明的 Secret 引用（仅作为可信配置的补充）。"""
    refs: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "secret_ref":
                    if isinstance(nested, str):
                        refs.add(nested)
                    elif isinstance(nested, dict) and isinstance(nested.get("name"), str):
                        refs.add(nested["name"])
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(arguments)
    return sorted(refs)


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

    def secret_refs_for(self, tool_name: str) -> list[str]:
        """返回执行器当前配置中工具实际依赖的 Secret 引用。"""
        ...

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
    """工具名到执行器的注册表，支持按工具注册和默认执行器回退。

    v0.31.0 新增 execution_mode_resolver：当存在时，resolve_executor()
    根据执行策略决定使用本地执行器还是 Harness 执行器。
    """

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}
        self._default: ToolExecutor | None = None
        self._mode_resolver: ExecutionModeResolver | None = None

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

    def set_mode_resolver(self, resolver: ExecutionModeResolver) -> None:
        """v0.31.0：注入执行模式解析器。"""
        self._mode_resolver = resolver

    def get_executor(self, tool_name: str) -> ToolExecutor:
        """按工具名获取执行器；不存在且没有默认执行器时抛出异常。"""
        executor = self._executors.get(tool_name)
        if executor is not None:
            return executor
        if self._default is not None:
            return self._default
        raise ExecutorRegistryError(f"工具 {tool_name!r} 没有注册执行器")

    def resolve_executor(self, tool_name: str) -> ToolExecutor | None:
        """v0.31.0：根据执行策略解析最终执行器。

        返回 None 表示该工具被策略拒绝（deny）。
        """
        if self._mode_resolver is None:
            return self.get_executor(tool_name)
        mode = self._mode_resolver.resolve(tool_name)
        if mode == ExecutionMode.DENY:
            return None
        if mode == ExecutionMode.HARNESS:
            return self._mode_resolver.harness_executor
        return self.get_executor(tool_name)

    def resolve_secret_refs(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> list[str]:
        """合并执行器可信配置与调用参数补充声明中的 Secret 引用。"""
        executor = self._executors.get(tool_name) or self._default
        trusted_refs = executor.secret_refs_for(tool_name) if executor is not None else []
        declared_refs = extract_declared_secret_refs(arguments)
        return sorted(set(trusted_refs) | set(declared_refs))
