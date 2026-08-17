"""MCPGateway（§6.5）：工具映射与授权转发。

- stdio 拉起 MCP server 子进程，**生命周期由本组件独占管理**：启动时拉起、
  注册 ``atexit`` 清理；业务代码禁止触碰子进程句柄；
- 规范化工具名 → (server, mcp_name) 的映射只存在于本组件（``tool_mapping``）；
- ``list_tools`` 返回按 CapabilityProfile 过滤后的工具列表；
- ``call_tool`` 是哑代理：只做名称翻译与转发，治理语义全部在 Checkpoint（§6.6）。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import sys
import time
from contextlib import AsyncExitStack

import anyio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from loop_controller.infra.config_loader import MCPServerConfig, ToolMappingEntry
from loop_controller.models import CapabilityProfile, Tool, ToolResult

logger = logging.getLogger(__name__)

# 仅用于把 "python" 解析为当前解释器（保证使用装有依赖的 venv）。
_SCRIPT_LAUNCHERS = {"python", "python3"}


class MCPGatewayError(Exception):
    """MCP 连接/映射错误（防御层；配置校验 T1.2 已保证映射不会缺失）。"""


class _ServerClient:
    """单个 MCP server 的 stdio 会话：启动、调用、关闭。"""

    def __init__(
        self,
        config: MCPServerConfig,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._config = config
        self._env = env
        self._cwd = cwd
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._tools: dict[str, Tool] = {}  # mcp_name -> Tool

    async def start(self) -> None:
        command = list(self._config.command)
        if command and command[0] in _SCRIPT_LAUNCHERS:
            command = [sys.executable, *command[1:]]
        params = StdioServerParameters(
            command=command[0], args=command[1:], env=self._env, cwd=self._cwd
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        listed = await session.list_tools()
        for t in listed.tools:
            # mcp SDK 2.x：Tool 字段为 snake_case（input_schema），非 camelCase。
            self._tools[t.name] = Tool(
                canonical_name="",
                mcp_name=t.name,
                description=t.description,
                input_schema=dict(t.input_schema or {}),
            )

    def get_tool(self, mcp_name: str) -> Tool:
        tool = self._tools.get(mcp_name)
        if tool is None:
            raise MCPGatewayError(
                f"server {self._config.name} 未提供工具 {mcp_name!r}"
            )
        return tool

    async def call(self, mcp_name: str, arguments: dict) -> tuple[str, bool]:
        if self._session is None:
            raise MCPGatewayError(f"server {self._config.name} 未启动")
        result = await self._session.call_tool(mcp_name, arguments)
        # mcp SDK 2.x：字段为 snake_case（is_error），非 camelCase（isError）。
        texts = [item.text for item in result.content if item.type == "text"]
        return ("\n".join(texts), bool(result.is_error))

    async def aclose(self) -> None:
        await self._stack.aclose()


class MCPGateway:
    """全部 MCP server 的聚合入口（规范名视角）。"""

    def __init__(
        self,
        mcp_servers: dict[str, MCPServerConfig],
        tool_mapping: dict[str, ToolMappingEntry],
        *,
        env_extra: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._servers = {
            name: _ServerClient(cfg, env_extra, cwd) for name, cfg in mcp_servers.items()
        }
        self._mapping = tool_mapping  # canonical_name -> ToolMappingEntry
        atexit.register(self.close)

    # -- 生命周期（独占） ---------------------------------------------------

    async def start(self) -> None:
        for client in self._servers.values():
            await client.start()

    async def aclose(self) -> None:
        # anyio 在 Windows 上清理 stdio 子进程时存在 cancel scope 竞态（mcp 2.x 已知
        # 行为）：shield + 逐 server 容错，保证一个 server 清理失败不阻塞其余关闭。
        with anyio.CancelScope(shield=True):
            for client in self._servers.values():
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 - 清理期异常只记日志（Windows anyio 竞态已知）
                    logger.warning("关闭 MCP server %s 失败（Windows 清理竞态，忽略）", client._config.name)

    def close(self) -> None:  # atexit / 显式调用：同步关闭
        try:
            asyncio.run(self.aclose())
        except RuntimeError:
            pass  # 事件循环已在运行（正常路径由 Runtime 显式 aclose）

    # -- 工具面 -------------------------------------------------------------

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回按 Profile 过滤后的工具列表（解决 MCP 默认暴露全部工具的问题）。"""
        tools: list[Tool] = []
        for canonical in profile.tools:
            entry = self._mapping.get(canonical)
            if entry is None:
                raise MCPGatewayError(f"tool_mapping 中不存在 {canonical!r}")
            mcp_tool = self._servers[entry.server].get_tool(entry.mcp_name)
            tools.append(
                Tool(
                    canonical_name=canonical,
                    mcp_name=entry.mcp_name,
                    description=mcp_tool.description,
                    input_schema=mcp_tool.input_schema,
                )
            )
        return tools

    async def call_tool(
        self, tool_name: str, arguments: dict, call_id: str, task_id: str
    ) -> ToolResult:
        """按规范化工具名转发；失败注入为 ``error`` 状态的 ToolResult。"""
        entry = self._mapping.get(tool_name)
        if entry is None:
            raise MCPGatewayError(f"tool_mapping 中不存在 {tool_name!r}")
        client = self._servers[entry.server]
        start = time.perf_counter()
        try:
            content, is_error = await client.call(entry.mcp_name, arguments)
        except Exception as exc:  # noqa: BLE001 - 任何异常都转成 error 结果
            logger.warning("MCP 调用 %s(%s) 失败：%s", tool_name, call_id, exc)
            return ToolResult(
                call_id=call_id,
                task_id=task_id,
                tool_name=tool_name,
                status="error",
                content=str(exc),
                error_code="mcp_call_failed",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        return ToolResult(
            call_id=call_id,
            task_id=task_id,
            tool_name=tool_name,
            status="error" if is_error else "success",
            content=content,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
