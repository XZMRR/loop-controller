"""Agent 接入 SDK（v0.32.0）：@governed 装饰器与 GovernanceRuntime。

目标：让愿意接入 Loop Controller 的 Agent 在**工具定义处**完成治理，
无需改造每个调用点。支持同步/异步函数、统一工具注册表 Hook。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, TypeVar

from loop_controller.controller import LoopController, build_controller
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import GovernanceResult
from loop_controller.tool_governor import ToolGovernor

T = TypeVar("T")


class GovernanceDeniedError(Exception):
    """Loop Controller 拒绝、阻断或执行出错时抛出。"""

    def __init__(self, result: GovernanceResult) -> None:
        self.result = result
        super().__init__(f"{result.status}: {result.reason}")


class GovernanceRuntime:
    """Agent 侧治理运行时。

    封装 ``LoopController`` 与 ``ToolGovernor`` 的构造和生命周期，
    提供 ``@governed`` 装饰器所需的当前运行时上下文。
    """

    _current: GovernanceRuntime | None = None

    def __init__(
        self,
        controller: LoopController,
        agent_id: str,
        user_id: str,
        *,
        default_task_context: str = "",
    ) -> None:
        self._controller = controller
        self._agent_id = agent_id
        self._user_id = user_id
        self._default_task_context = default_task_context
        self._governor = ToolGovernor(
            controller,
            agent_id=agent_id,
            user_id=user_id,
            default_task_context=default_task_context,
        )

    @property
    def controller(self) -> LoopController:
        return self._controller

    @property
    def governor(self) -> ToolGovernor:
        return self._governor

    @classmethod
    def current(cls) -> GovernanceRuntime:
        """返回最近一次 ``from_config``/``set_current`` 设置的运行时。"""
        if cls._current is None:
            raise RuntimeError(
                "当前没有活动的 GovernanceRuntime；"
                "请先调用 GovernanceRuntime.from_config() 或 set_current()"
            )
        return cls._current

    @classmethod
    def set_current(cls, runtime: GovernanceRuntime) -> None:
        cls._current = runtime

    @classmethod
    def reset_current(cls) -> None:
        """清除当前全局运行时；主要用于测试和显式生命周期管理。"""
        cls._current = None

    @classmethod
    async def from_config(
        cls,
        config_path: str | Path,
        *,
        opa_url: str | None = None,
        agent_id: str,
        user_id: str,
        default_task_context: str = "",
        env_extra: dict[str, str] | None = None,
    ) -> GovernanceRuntime:
        """从配置文件构造运行时并设为当前实例。"""
        config_dir = Path(config_path)
        if config_dir.is_file():
            config_dir = config_dir.parent
        config = ConfigLoader().load(config_dir, opa_base_url=opa_url)
        controller = await build_controller(
            config,
            opa_url=opa_url or "",
            env_extra=env_extra,
        )
        rt = cls(
            controller,
            agent_id=agent_id,
            user_id=user_id,
            default_task_context=default_task_context,
        )
        cls.set_current(rt)
        return rt

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_context: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """提交工具调用并返回原始执行结果；非 allow 状态抛出 GovernanceDeniedError。"""
        result = await self._controller.evaluate_and_execute(
            agent_id=self._agent_id,
            user_id=self._user_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            task_context=task_context if task_context is not None else self._default_task_context,
            session_id=session_id,
            task_id=task_id,
        )
        if result.status == "allow":
            return result.content
        raise GovernanceDeniedError(result)

    async def aclose(self) -> None:
        """关闭底层控制器。"""
        await self._controller.aclose()

    def hook_tool_registry(
        self,
        registry: Any,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        """批量为统一工具注册表中的所有工具加上治理包装。

        支持两种注册表形态：
        - 有 ``tools`` 属性（dict[str, Callable]），如简单字典注册表；
        - 有 ``list_tools()`` 和 ``get(name)`` 方法，如面向对象注册表。
        """
        exclude = exclude or set()
        items: list[tuple[str, Callable[..., Any]]] = []

        # 支持 dict/Mapping 形态：直接取键 "tools"
        if isinstance(registry, Mapping) and "tools" in registry:
            tools = registry["tools"]
            if isinstance(tools, dict):
                items.extend(tools.items())
        else:
            tools = getattr(registry, "tools", None)
            if isinstance(tools, dict):
                items.extend(tools.items())
            else:
                list_fn = getattr(registry, "list_tools", None)
                get_fn = getattr(registry, "get", None)
                if list_fn is None or get_fn is None:
                    raise TypeError(
                        "registry 必须提供 tools 字典，或 list_tools()/get(name) 方法"
                    )
                for name in list_fn():
                    items.append((name, get_fn(name)))

        register = getattr(registry, "register", None)
        for name, fn in items:
            if name in exclude:
                continue
            governed_fn = governed(tool_name=name)(fn)
            if register is not None:
                register(name, governed_fn)
            elif tools is not None and isinstance(tools, dict):
                tools[name] = governed_fn


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """在当前线程运行一个协程；兼容无事件循环的情况。

    如果调用处已经在一个运行中的事件循环里，同步等待协程会导致事件循环死锁或
    RuntimeError，因此显式抛出清晰异常，提示用户改用 async 函数或在非 async 上下文
    中调用。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "在已有事件循环中无法同步等待治理结果；"
        "请把被 @governed 装饰的函数改为 async，或在非 async 上下文中调用。"
    )


def _pack_arguments(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """用原函数签名把位置/关键字参数绑定为 dict。"""
    sig = inspect.signature(fn)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def _make_governed_wrapper(
    fn: Callable[..., Any],
    *,
    tool_name: str,
) -> Callable[..., Any]:
    """根据原函数是否 async，返回保持签名的治理包装函数。"""
    name = tool_name or fn.__name__

    async def _call(*args: Any, **kwargs: Any) -> Any:
        rt = GovernanceRuntime.current()
        arguments = _pack_arguments(fn, *args, **kwargs)
        return await rt.call(name, arguments)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _call(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return _run_async(_call(*args, **kwargs))

    return sync_wrapper


def governed[T](
    fn: Callable[..., T] | None = None,
    *,
    tool_name: str | None = None,
) -> Callable[..., Any] | Callable[[Callable[..., T]], Callable[..., Any]]:
    """标记一个工具函数需要经过 Loop Controller 治理。

    支持同步/异步函数；保留原函数签名；调用时自动打包参数。

    Args:
        tool_name: 在 Loop Controller 中注册的 canonical_name；默认使用函数名。

    Raises:
        GovernanceDeniedError: 当 Loop Controller 返回 deny / blocked / error / require_approval。
    """

    def decorator(func: Callable[..., T]) -> Callable[..., Any]:
        return _make_governed_wrapper(
            func,
            tool_name=tool_name or func.__name__,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


async def launch_agent(
    agent_module: str,
    config: str | Path,
    *,
    opa_url: str | None = None,
    agent_id: str,
    user_id: str,
    workspace: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
    default_task_context: str = "",
    _run: Callable[..., Any] | None = None,
) -> Any:
    """在治理上下文中启动 Agent。

    - 读取 Loop Controller 配置；
    - 创建 ``GovernanceRuntime`` 并设为当前；
    - 导入并运行 ``agent_module``（格式 ``module.path:func_name``）。

    注意：本函数不提供强运行时隔离，仅作为接入便利入口。
    """
    rt = await GovernanceRuntime.from_config(
        config,
        opa_url=opa_url,
        agent_id=agent_id,
        user_id=user_id,
        default_task_context=default_task_context,
        env_extra=env_extra,
    )
    try:
        module_path, sep, func_name = agent_module.partition(":")
        if not sep:
            raise ValueError("agent_module 必须使用 'module.path:func_name' 格式")
        from importlib import import_module

        mod = import_module(module_path)
        entry = getattr(mod, func_name)
        # workspace 通过环境变量 LOOP_CONTROLLER_WORKSPACE 透传给 Agent，
        # 避免修改本进程全局工作目录（os.chdir 线程不安全且副作用不可控）。
        if workspace is not None:
            import os

            os.environ["LOOP_CONTROLLER_WORKSPACE"] = str(workspace)
        runner = _run or entry
        if inspect.iscoroutinefunction(runner):
            return await runner()
        return runner()
    finally:
        await rt.aclose()
