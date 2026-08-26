"""HTTP Executor：直接调用 REST API 工具（v0.21.0）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_models import (
    HTTPToolSpec,
    extract_jsonpath,
)
from loop_controller.executors.http_security import HTTPSecurityError, HTTPSecurityPolicy
from loop_controller.models import CapabilityProfile, Tool, ToolResult
from loop_controller.secrets import SecretBroker, SecretNotFoundError

logger = logging.getLogger(__name__)


class HTTPExecutor(ToolExecutor):
    """通过受控 HTTP 客户端直接调用 REST API。

    一个 ``HTTPExecutor`` 实例可以服务多个 HTTP 工具，按 ``tool_name`` 分发到对应
    ``HTTPToolSpec``。
    """

    def __init__(
        self,
        http_client: HTTPClient,
        tool_specs: dict[str, HTTPToolSpec],
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self._client = http_client
        self._tool_specs = tool_specs
        self._secret_broker = secret_broker

    def _get_spec(self, tool_name: str) -> HTTPToolSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise KeyError(f"HTTP 工具 {tool_name!r} 未注册")
        return spec

    def update_tool_specs(self, tool_specs: dict[str, HTTPToolSpec]) -> None:
        """原子替换 HTTP 工具规格（热更新）。"""
        self._tool_specs = tool_specs

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """渲染模板、安全校验、发送 HTTP 请求并返回 ToolResult。"""
        spec = self._get_spec(tool_name)

        try:
            url, headers, body = await spec.build_request(
                arguments,
                secret_broker=self._secret_broker,
                tenant_id=context.tenant_id,
            )
        except KeyError as exc:
            return self._error_result(
                context, tool_name, f"缺少参数: {exc}", "http_missing_argument"
            )
        except ValueError as exc:
            return self._error_result(
                context, tool_name, f"模板渲染失败: {exc}", "http_template_error"
            )
        except SecretNotFoundError as exc:
            return self._error_result(
                context, tool_name, str(exc), "http_auth_error"
            )

        # SSRF / allowlist 校验
        security = HTTPSecurityPolicy(
            spec.allowed_hosts,
            require_dns_resolution=spec.require_dns_resolution,
        )
        try:
            security.check_url(url)
        except HTTPSecurityError as exc:
            return self._error_result(
                context, tool_name, str(exc), exc.error_code
            )

        # 发送请求
        try:
            status, _resp_headers, text, elapsed_ms = await self._client.request(
                spec.method,
                url,
                headers=headers,
                body=body,
                url_checker=security.check_url,
            )
        except HTTPSecurityError as exc:
            return self._error_result(
                context, tool_name, str(exc), exc.error_code
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("HTTP 工具 %s 调用失败", tool_name)
            return self._error_result(
                context, tool_name, f"HTTP 调用失败: {exc}", "http_internal_error"
            )

        # 响应映射
        mapping = spec.response_mapping
        if status in mapping.success_status:
            content = self._map_success_response(text, mapping)
            return ToolResult(
                call_id=context.call_id,
                task_id=context.task_id,
                tool_name=tool_name,
                status="success",
                content=content,
                elapsed_ms=int(elapsed_ms),
            )

        error_code = mapping.error_codes.get(status) or self._default_error_code(status)
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="error",
            content=text[:500],
            error_code=error_code,
            elapsed_ms=int(elapsed_ms),
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回 HTTP 工具元数据列表，按 Profile 过滤。"""
        allowed = set(profile.tools.keys()) if profile.tools else None
        tools: list[Tool] = []
        for name, spec in self._tool_specs.items():
            if allowed is not None and name not in allowed:
                continue
            tools.append(spec.to_tool())
        return tools

    @staticmethod
    def _map_success_response(
        text: str,
        mapping: Any,
    ) -> dict[str, Any]:
        """把 HTTP 响应映射为 ToolResult.content。"""
        content: dict[str, Any] = {}
        body: Any = None
        if text:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text

        if mapping.raw_body_field and body is not None:
            content[mapping.raw_body_field] = body

        if mapping.extract and body is not None:
            for field, path in mapping.extract.items():
                value = extract_jsonpath(body, path)
                if value is not None:
                    content[field] = value

        if not content:
            content["raw"] = body if body is not None else text

        return content

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

    @staticmethod
    def _default_error_code(status: int) -> str:
        if status == 400:
            return "http_bad_request"
        if status == 401:
            return "http_unauthorized"
        if status == 403:
            return "http_forbidden"
        if status == 404:
            return "http_not_found"
        if status == 408:
            return "http_timeout"
        if status == 429:
            return "http_rate_limited"
        if 500 <= status < 600:
            return "http_server_error"
        return "http_error"
