"""LangChain / LangGraph 工具治理集成（v0.32.0）。"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, cast

from loop_controller.agent_sdk import GovernanceRuntime


def _is_langchain_tool(tool: Any) -> bool:
    """判断对象是否为 LangChain BaseTool（不强制依赖 langchain_core）。"""
    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        return False
    return isinstance(tool, BaseTool)


def _extract_schema(tool: Any) -> dict[str, Any] | None:
    """从 LangChain tool 提取 JSON Schema。"""
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        return cast(dict[str, Any], schema.model_json_schema())
    return None


def govern_langchain_tools(
    tools: list[Any],
    runtime: GovernanceRuntime | None = None,
    *,
    exclude: set[str] | None = None,
) -> list[Any]:
    """把 LangChain BaseTool 列表中的每个 tool 包装成治理版本。

    保持原 tool 的 name/description/args_schema；调用时自动走 Loop Controller。
    未安装 langchain_core 时返回原列表不变。
    """
    exclude = exclude or set()
    rt = runtime or GovernanceRuntime.current()

    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        return tools

    governed_tools: list[Any] = []
    for tool in tools:
        if not isinstance(tool, BaseTool) or tool.name in exclude:
            governed_tools.append(tool)
            continue

        @functools.wraps(tool._run, assigned=["__name__", "__doc__"])
        def _make_wrapper(original: Any, t: BaseTool) -> Callable[..., Any]:
            async def _invoke(**kwargs: Any) -> Any:
                result = await rt.call(t.name, kwargs)
                return result

            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                from loop_controller.agent_sdk import _run_async
                return _run_async(_invoke(**kwargs))

            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _invoke(**kwargs)

            if inspect.iscoroutinefunction(original):
                return async_wrapper
            return sync_wrapper

        tool._run = _make_wrapper(tool._run, tool)
        if hasattr(tool, "_arun"):
            tool._arun = _make_wrapper(tool._arun, tool)
        governed_tools.append(tool)

    return governed_tools
