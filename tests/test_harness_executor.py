"""HarnessExecutor 单元测试（v0.25.0）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loop_controller.executors import ExecutionContext, HarnessExecutor
from loop_controller.executors.harness_models import (
    HarnessToolSpec,
    HTTPBackendConfig,
)
from loop_controller.executors.harness_protocol import HarnessExecuteResponse
from loop_controller.models import CapabilityProfile, Tool, ToolPermission


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


def _mock_transport(payload: dict[str, Any], status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _echo_tool_spec() -> HarnessToolSpec:
    return HarnessToolSpec(
        tool_name="harness_echo",
        harness="http_harness",
        description="echo test",
        input_schema={"type": "object"},
        cost_per_call=10,
    )


class TestHarnessToolSpec:
    """Harness 工具规格模型测试。"""

    def test_to_tool_returns_tool(self) -> None:
        spec = _echo_tool_spec()
        tool = spec.to_tool()
        assert isinstance(tool, Tool)
        assert tool.canonical_name == "harness_echo"
        assert tool.mcp_name == "harness_echo"
        assert tool.description == "echo test"


class TestHarnessExecutorHTTP:
    """Harness HTTP 后端执行器测试。"""

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        response = HarnessExecuteResponse(
            status="success",
            content={"echo": "hello"},
        ).model_dump()
        config = HTTPBackendConfig(
            name="http_harness",
            type="http",
            base_url="http://example.com",
            timeout_seconds=5,
        )
        executor = HarnessExecutor(
            {"harness_echo": _echo_tool_spec()},
            {"http_harness": config},
        )
        # 注入 mock transport 避免真实网络请求
        await executor.start()
        backend = executor._backends["http_harness"]
        assert isinstance(backend, type(executor._backends["http_harness"]))
        backend._client = httpx.AsyncClient(
            transport=_mock_transport(response),
            timeout=config.timeout_seconds,
        )

        result = await executor.execute(
            "harness_echo",
            {"message": "hello"},
            _fake_context(),
        )
        assert result.status == "success"
        assert result.content == {"echo": "hello"}
        assert result.error_code is None
        await executor.stop()

    @pytest.mark.asyncio
    async def test_execute_http_error(self) -> None:
        config = HTTPBackendConfig(
            name="http_harness",
            type="http",
            base_url="http://example.com",
            timeout_seconds=5,
        )
        executor = HarnessExecutor(
            {"harness_echo": _echo_tool_spec()},
            {"http_harness": config},
        )
        await executor.start()
        backend = executor._backends["http_harness"]
        backend._client = httpx.AsyncClient(
            transport=_mock_transport({}, status_code=500),
            timeout=config.timeout_seconds,
        )

        result = await executor.execute(
            "harness_echo",
            {"message": "hello"},
            _fake_context(),
        )
        assert result.status == "error"
        assert "500" in str(result.content)
        assert result.error_code == "harness_http_error"
        await executor.stop()

    @pytest.mark.asyncio
    async def test_execute_backend_not_found(self) -> None:
        spec = HarnessToolSpec(
            tool_name="harness_echo",
            harness="missing_backend",
        )
        executor = HarnessExecutor({"harness_echo": spec}, {})

        result = await executor.execute(
            "harness_echo",
            {"message": "hello"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "harness_backend_not_found"

    @pytest.mark.asyncio
    async def test_execute_tool_not_registered(self) -> None:
        executor = HarnessExecutor({}, {})

        with pytest.raises(KeyError):
            await executor.execute(
                "unknown_tool",
                {},
                _fake_context(),
            )

    @pytest.mark.asyncio
    async def test_list_tools_filtered_by_profile(self) -> None:
        executor = HarnessExecutor(
            {
                "harness_echo": _echo_tool_spec(),
                "harness_shell": HarnessToolSpec(
                    tool_name="harness_shell",
                    harness="http_harness",
                ),
            },
            {},
        )
        profile = CapabilityProfile(
            profile_id="test",
            tools={
                "harness_echo": ToolPermission(tool_name="harness_echo", allowed=True),
            },
        )
        tools = await executor.list_tools(profile)
        assert [t.canonical_name for t in tools] == ["harness_echo"]


class TestHarnessExecutorRequestShape:
    """验证 HarnessExecutor 发给后端的请求形状。"""

    @pytest.mark.asyncio
    async def test_request_body_contains_context_and_sandbox(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        config = HTTPBackendConfig(
            name="http_harness",
            type="http",
            base_url="http://example.com",
        )
        executor = HarnessExecutor(
            {"harness_echo": _echo_tool_spec()},
            {"http_harness": config},
        )
        await executor.start()
        backend = executor._backends["http_harness"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await executor.execute(
            "harness_echo",
            {"message": "hello"},
            _fake_context(),
        )
        await executor.stop()

        assert captured["body"]["tool"] == "harness_echo"
        assert captured["body"]["arguments"] == {"message": "hello"}
        assert captured["body"]["context"]["call_id"] == "c1"
        assert captured["body"]["sandbox"]["timeout_seconds"] == 30.0
