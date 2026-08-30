"""FastAPI 治理集成（v0.32.0）。

提供路由级治理装饰器 ``@governed_route`` 与整应用包装 ``GovernedFastAPI``。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from loop_controller.agent_sdk import (
    GovernanceRuntime,
    _run_async,
)


def governed_route(
    tool_name: str | None = None,
    *,
    mode: str | None = None,
    budget_unit: str | None = None,
) -> Callable[..., Any]:
    """FastAPI 路由装饰器：把请求参数作为工具参数提交 Loop Controller 治理。

    用法::

        @app.post("/run-tool")
        @governed_route(tool_name="run_tool")
        async def run_tool(request: dict[str, Any]) -> dict[str, Any]:
            # 原函数仅在治理通过后执行
            return {"ok": True}

    参数说明：
        tool_name: canonical_name，默认用被装饰函数名。
        mode/budget_unit: 保留字段。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        name = tool_name or func.__name__
        is_async = inspect.iscoroutinefunction(func)

        async def _invoke(*args: Any, **kwargs: Any) -> Any:
            rt = GovernanceRuntime.current()
            # 优先取 FastAPI 注入的 Request body；否则把关键字参数整体作为 arguments。
            request_obj = kwargs.get("request")
            if request_obj is not None and hasattr(request_obj, "json"):
                body = await request_obj.json()
            else:
                body = kwargs
            return await rt.call(name, dict(body))

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _invoke(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_async(_invoke(*args, **kwargs))

        return sync_wrapper

    return decorator


class GovernedFastAPI:
    """整应用 FastAPI 治理包装（占位/装饰器集合）。

    当前版本主要提供便捷属性访问；具体路由治理请使用 ``@governed_route``。
    """

    def __init__(self, app: Any, runtime: GovernanceRuntime | None = None) -> None:
        self.app = app
        self.runtime = runtime or GovernanceRuntime.current()

    def route(
        self,
        tool_name: str | None = None,
        *,
        mode: str | None = None,
        budget_unit: str | None = None,
    ) -> Callable[..., Any]:
        """返回可在 FastAPI 路由上使用的装饰器。"""
        return governed_route(
            tool_name=tool_name,
            mode=mode,
            budget_unit=budget_unit,
        )
