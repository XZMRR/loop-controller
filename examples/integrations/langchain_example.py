"""LangChain / LangGraph 工具治理集成示例（v0.33.0）。

本文件是从核心包迁移出来的可选示例，不作为 loop_controller 核心 API 维护。
推荐用法：把 LangChain BaseTool 用 loop_controller.governed 装饰，或参考本示例
对已有 BaseTool 列表做批量包装。
"""

from __future__ import annotations

import functools
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


def _schema_field_names(tool: Any) -> list[str]:
    """返回 args_schema 中定义字段的顺序列表，用于把位置参数映射为关键字参数。"""
    schema = _extract_schema(tool)
    if schema is None:
        return []
    return list(schema.get("properties", {}).keys())


def govern_langchain_tools(
    tools: list[Any],
    runtime: GovernanceRuntime | None = None,
    *,
    exclude: set[str] | None = None,
) -> list[Any]:
    """把 LangChain BaseTool 列表中的每个 tool 包装成治理版本。

    保持原 tool 的 name/description/args_schema；调用时自动走 Loop Controller。
    未安装 langchain_core 时返回原列表不变。

    支持通过以 ``_loop_controller_`` 为前缀的关键字参数透传治理上下文：
    ``_loop_controller_session_id``、``_loop_controller_task_id``、
    ``_loop_controller_task_context``。
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

        tool_name = tool.name
        field_names = _schema_field_names(tool)

        async def _invoke(
            bound_tool: Any,
            bound_name: str,
            bound_fields: list[str],
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            from loop_controller.agent_sdk import _extract_reserved_governance_kwargs

            reserved, user_kwargs = _extract_reserved_governance_kwargs(dict(kwargs))
            # 把位置参数按 args_schema 字段顺序映射为关键字参数
            for idx, arg in enumerate(args):
                if idx < len(bound_fields):
                    user_kwargs[bound_fields[idx]] = arg
            return await rt.call(
                bound_name,
                user_kwargs,
                session_id=reserved.get("session_id"),
                task_id=reserved.get("task_id"),
                task_context=reserved.get("task_context"),
            )

        def _make_sync_wrapper(
            bound_tool: Any,
            original_run: Callable[..., Any],
            bound_name: str,
            bound_fields: list[str],
        ) -> Callable[..., Any]:
            @functools.wraps(original_run)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                from loop_controller.agent_sdk import _run_async

                return _run_async(
                    _invoke(bound_tool, bound_name, bound_fields, *args, **kwargs)
                )

            return sync_wrapper

        def _make_async_wrapper(
            bound_tool: Any,
            original_arun: Callable[..., Any],
            bound_name: str,
            bound_fields: list[str],
        ) -> Callable[..., Any]:
            @functools.wraps(original_arun)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _invoke(
                    bound_tool, bound_name, bound_fields, *args, **kwargs
                )

            return async_wrapper

        original_run = getattr(tool, "_run", None)
        original_arun = getattr(tool, "_arun", None)
        if original_run is not None:
            tool._run = _make_sync_wrapper(tool, original_run, tool_name, field_names)
        if original_arun is not None:
            tool._arun = _make_async_wrapper(tool, original_arun, tool_name, field_names)
        governed_tools.append(tool)

    return governed_tools
