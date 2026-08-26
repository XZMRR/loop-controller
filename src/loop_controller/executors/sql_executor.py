"""SQL 执行器：在参数化查询约束下执行数据库操作（v0.24.0）。

当前默认支持 sqlite（标准库），其他驱动可通过安装 extras 后扩展。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.sql_models import DataSourceConfig, SQLToolSpec
from loop_controller.models import CapabilityProfile, Tool, ToolResult

_READ_ONLY_PREFIX_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class SQLExecutor(ToolExecutor):
    """治理下的 SQL 执行器；默认只读，写操作需显式 read_only=false。"""

    def __init__(
        self,
        tool_specs: dict[str, SQLToolSpec],
        data_sources: dict[str, DataSourceConfig],
    ) -> None:
        self._tool_specs = tool_specs
        self._data_sources = data_sources

    def _get_spec(self, tool_name: str) -> SQLToolSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise KeyError(tool_name)
        return spec

    def _get_data_source(self, name: str) -> DataSourceConfig:
        ds = self._data_sources.get(name)
        if ds is None:
            raise KeyError(name)
        return ds

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        try:
            spec = self._get_spec(tool_name)
        except KeyError:
            return self._error_result(
                context, tool_name, f"SQL 工具 {tool_name!r} 未注册", "sql_tool_not_found"
            )

        try:
            data_source = self._get_data_source(spec.data_source)
        except KeyError:
            return self._error_result(
                context,
                tool_name,
                f"数据源 {spec.data_source!r} 未注册",
                "sql_data_source_not_found",
            )

        sql = arguments.get("sql")
        if not isinstance(sql, str):
            return self._error_result(
                context, tool_name, "缺少 'sql' 参数或类型非法", "sql_arg_not_allowed"
            )
        parameters = arguments.get("parameters") or {}
        if not isinstance(parameters, dict):
            return self._error_result(
                context, tool_name, "'parameters' 必须是 dict", "sql_arg_not_allowed"
            )

        validation_error = self._validate_sql(spec, sql)
        if validation_error:
            return self._error_result(context, tool_name, validation_error[0], validation_error[1])

        try:
            rows = await asyncio.wait_for(
                self._execute_sql(data_source, spec, sql, parameters),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError:
            return self._error_result(context, tool_name, "SQL 执行超时", "sql_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                context, tool_name, f"SQL 执行失败: {exc}", "sql_runtime_error"
            )

        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="success",
            content=rows,
            elapsed_ms=0,
        )

    def _validate_sql(self, spec: SQLToolSpec, sql: str) -> tuple[str, str] | None:
        """校验 SQL 语义，返回 (message, error_code) 或 None。"""
        # 只读模式：仅允许 SELECT / WITH
        if spec.read_only and not _READ_ONLY_PREFIX_RE.match(sql):
            return ("只读 SQL 工具禁止非 SELECT/WITH 语句", "sql_read_only_violation")

        # 禁止模式（如 ; 和 --）
        for pattern in spec.forbidden_regexes:
            if pattern.search(sql):
                return (
                    f"SQL 命中禁止模式 {pattern.pattern!r}",
                    "sql_injection_blocked",
                )

        # 允许模式二次校验
        if spec.allowed_regexes and not any(p.search(sql) for p in spec.allowed_regexes):
            return (
                f"SQL 未命中任何允许模式 {spec.allowed_patterns!r}",
                "sql_injection_blocked",
            )

        return None

    async def _execute_sql(
        self,
        data_source: DataSourceConfig,
        spec: SQLToolSpec,
        sql: str,
        parameters: dict[str, Any],
    ) -> Any:
        """根据数据源驱动执行 SQL。"""
        driver = data_source.driver.lower()
        if driver == "sqlite":
            return await self._execute_sqlite(data_source, spec, sql, parameters)
        # 其他驱动未安装/未实现
        raise SQLDriverMissingError(
            f"SQL 驱动 {data_source.driver!r} 未安装或未实现，请安装 loop-controller[{driver}]"
        )

    async def _execute_sqlite(
        self,
        data_source: DataSourceConfig,
        spec: SQLToolSpec,
        sql: str,
        parameters: dict[str, Any],
    ) -> Any:
        """使用 sqlite3 在线程中执行 SQL。"""
        database = data_source.database or ":memory:"

        def _run() -> Any:
            conn = sqlite3.connect(database)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, parameters)
                rows = [dict(row) for row in cur.fetchall()]
                conn.commit()
                return rows
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回 SQL 工具元数据列表，按 Profile 过滤。"""
        allowed = set(profile.tools.keys()) if profile.tools else None
        tools: list[Tool] = []
        for name, spec in self._tool_specs.items():
            if allowed is not None and name not in allowed:
                continue
            tools.append(spec.to_tool())
        return tools

    @staticmethod
    def _error_result(
        context: ExecutionContext,
        tool_name: str,
        message: str,
        error_code: str,
    ) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="error",
            content=message,
            error_code=error_code,
        )


class SQLDriverMissingError(Exception):
    """SQL 驱动未安装或未实现。"""
