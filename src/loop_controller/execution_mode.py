"""执行模式解析：决定工具走本地执行器还是 Harness。"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loop_controller.executors.harness_executor import HarnessExecutor
    from loop_controller.executors.harness_models import HarnessExecutionPolicy


class ExecutionMode(Enum):
    """工具执行模式。"""

    TRUSTED_LOCAL = "trusted_local"
    HARNESS = "harness"
    DENY = "deny"


class ExecutionModeResolver:
    """v0.31.0：根据执行策略判断工具应如何执行。

    决策优先级：
    1. trusted_local 白名单；
    2. 工具级策略覆盖；
    3. 全局默认模式；
    4. 若默认要求 Harness 但无健康后端 → deny。
    """

    def __init__(
        self,
        policy: HarnessExecutionPolicy,
        harness_executor: HarnessExecutor | None,
    ) -> None:
        self._policy = policy
        self._harness = harness_executor

    @property
    def harness_executor(self) -> HarnessExecutor | None:
        """返回解析器持有的 Harness 执行器；供 ExecutorRegistry 使用。"""
        return self._harness

    def resolve(self, tool_name: str) -> ExecutionMode:
        if tool_name in self._policy.trusted_local_tools:
            return ExecutionMode.TRUSTED_LOCAL

        tool_policy = self._policy.tools.get(tool_name)
        mode = tool_policy.mode if tool_policy is not None else self._policy.default_mode

        if mode == "trusted_local":
            return ExecutionMode.TRUSTED_LOCAL
        if mode == "harness_required":
            if self._harness is None or not self._harness.is_tool_available(tool_name):
                return ExecutionMode.DENY
            if (
                self._policy.fail_closed_when_unhealthy
                and not self._harness.has_healthy_backend(tool_name)
            ):
                return ExecutionMode.DENY
            return ExecutionMode.HARNESS
        if mode == "harness_preferred":
            if self._harness is not None and self._harness.is_tool_available(tool_name):
                if self._harness.has_healthy_backend(tool_name):
                    return ExecutionMode.HARNESS
                if not self._policy.allow_fallback_to_local:
                    return ExecutionMode.DENY
            return ExecutionMode.TRUSTED_LOCAL
        return ExecutionMode.DENY
