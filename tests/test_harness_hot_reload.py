"""Harness 热更新、远程取消与幂等单元测试（v0.34.0）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from loop_controller.executors.base import ExecutionContext
from loop_controller.executors.harness_executor import HarnessExecutor, _HTTPHarnessClient
from loop_controller.executors.harness_models import HarnessToolSpec, HTTPBackendConfig
from loop_controller.executors.harness_protocol import HarnessExecuteResponse, HarnessSandbox
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.hot_reload import HotReloader


def _fake_context(call_id: str = "c1") -> ExecutionContext:
    return ExecutionContext(
        call_id=call_id,
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


def _success_response() -> dict[str, Any]:
    return HarnessExecuteResponse(
        status="success",
        content="ok",
        effective_sandbox=HarnessSandbox(),
    ).model_dump(mode="json")


class _FailingBackend:
    async def start(self) -> None:
        raise RuntimeError("启动失败")

    async def stop(self) -> None:
        pass

    async def check_health(self) -> bool:
        return False

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: Any,
    ) -> Any:
        raise RuntimeError("不会执行")


class TestHarnessExecutorUpdateSpecs:
    @pytest.mark.asyncio
    async def test_update_tool_specs_without_restarting_backend(self) -> None:
        config = HTTPBackendConfig(
            name="http_harness", base_url="https://harness.example"
        )
        executor = HarnessExecutor(
            {"harness_echo": HarnessToolSpec(tool_name="harness_echo", harness="http_harness")},
            {"http_harness": config},
        )
        old_backend = executor._backends["http_harness"]

        new_spec = HarnessToolSpec(
            tool_name="harness_echo",
            harness="http_harness",
            description="updated",
        )
        await executor.update_specs(
            {"harness_echo": new_spec},
            {"http_harness": config},
        )

        assert executor._tool_specs["harness_echo"].description == "updated"
        assert executor._backends["http_harness"] is old_backend
        assert executor._states["http_harness"].max_concurrent_calls == 10

    @pytest.mark.asyncio
    async def test_update_adds_and_starts_new_backend(self) -> None:
        config_a = HTTPBackendConfig(
            name="backend_a", base_url="https://a.example"
        )
        executor = HarnessExecutor(
            {"tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a")},
            {"backend_a": config_a},
        )
        await executor.start()

        config_b = HTTPBackendConfig(
            name="backend_b", base_url="https://b.example"
        )
        await executor.update_specs(
            {
                "tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a"),
                "tool_b": HarnessToolSpec(tool_name="tool_b", harness="backend_b"),
            },
            {"backend_a": config_a, "backend_b": config_b},
        )

        assert "backend_b" in executor._backends
        assert "backend_b" in executor._states
        assert executor._backends["backend_b"]._client is not None
        await executor.stop()

    @pytest.mark.asyncio
    async def test_update_removes_backend_and_drains(self) -> None:
        config_a = HTTPBackendConfig(
            name="backend_a", base_url="https://a.example"
        )
        config_b = HTTPBackendConfig(
            name="backend_b", base_url="https://b.example"
        )
        executor = HarnessExecutor(
            {
                "tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a"),
                "tool_b": HarnessToolSpec(tool_name="tool_b", harness="backend_b"),
            },
            {"backend_a": config_a, "backend_b": config_b},
        )
        await executor.start()
        old_b = executor._backends["backend_b"]
        old_b.stop = AsyncMock()  # type: ignore[method-assign]

        await executor.update_specs(
            {"tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a")},
            {"backend_a": config_a},
        )

        assert "backend_b" not in executor._backends
        assert "backend_b" not in executor._states
        old_b.stop.assert_awaited_once()
        await executor.stop()

    @pytest.mark.asyncio
    async def test_update_rolls_back_when_new_backend_fails(self) -> None:
        config_a = HTTPBackendConfig(
            name="backend_a", base_url="https://a.example"
        )
        executor = HarnessExecutor(
            {"tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a")},
            {"backend_a": config_a},
        )
        await executor.start()
        old_backend = executor._backends["backend_a"]

        original_build = executor._build_backend

        def _build_with_failure(config: Any) -> Any:
            if config.name == "backend_b":
                return _FailingBackend()
            return original_build(config)

        executor._build_backend = _build_with_failure  # type: ignore[method-assign]

        config_b = HTTPBackendConfig(name="backend_b", base_url="https://b.example")
        with pytest.raises(RuntimeError):
            await executor.update_specs(
                {"tool_a": HarnessToolSpec(tool_name="tool_a", harness="backend_a")},
                {"backend_a": config_a, "backend_b": config_b},
            )

        assert executor._backends["backend_a"] is old_backend
        assert "backend_b" not in executor._backends
        await executor.stop()


class TestHarnessCancelAndIdempotency:
    @pytest.mark.asyncio
    async def test_cancel_call_sends_remote_cancel_request(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path == "/harness/v2/cancel":
                return httpx.Response(202)
            return httpx.Response(200, json=_success_response())

        config = HTTPBackendConfig(
            name="remote", base_url="https://harness.example"
        )
        executor = HarnessExecutor(
            {"deploy": HarnessToolSpec(tool_name="deploy", harness="remote")},
            {"remote": config},
        )
        await executor.start()
        backend = executor._backends["remote"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        result = await executor.execute(
            "deploy", {"x": 1}, _fake_context("call-to-cancel")
        )
        assert result.status == "success"
        assert executor._in_flight_calls == {}

        # 由于 execute 结束，call_id 已不在在途映射；cancel_call 返回 False
        cancelled = await executor.cancel_call("call-to-cancel")
        assert cancelled is False

        # 直接调用底层客户端验证取消请求形状
        backend_client = executor._backends["remote"]
        assert isinstance(backend_client, _HTTPHarnessClient)
        ok = await backend_client.cancel_call("direct-cancel")
        assert ok is True
        assert captured[-1].url.path == "/harness/v2/cancel"
        body = captured[-1].read()
        assert b"direct-cancel" in body
        await executor.stop()

    @pytest.mark.asyncio
    async def test_execute_returns_cached_result_for_same_call_id(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_success_response())

        config = HTTPBackendConfig(
            name="remote", base_url="https://harness.example"
        )
        executor = HarnessExecutor(
            {"deploy": HarnessToolSpec(tool_name="deploy", harness="remote")},
            {"remote": config},
        )
        await executor.start()
        backend = executor._backends["remote"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        ctx = _fake_context("same-id")
        first = await executor.execute("deploy", {"x": 1}, ctx)
        second = await executor.execute("deploy", {"x": 2}, ctx)

        assert first.content == second.content == "ok"
        assert second.metadata.get("idempotent") is True
        assert calls == 1
        await executor.stop()


class TestHarnessHotReloadIntegration:
    @pytest.mark.asyncio
    async def test_hot_reload_updates_harness_tool_spec(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "global").mkdir()

        harness_tools = config_dir / "harness_tools.yaml"
        harness_tools.write_text(
            yaml.safe_dump(
                {
                    "tools": {
                        "shell": {
                            "harness": "local",
                            "description": "before",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        secrets_yaml = config_dir / "secrets.yaml"
        secrets_yaml.write_text(
            yaml.safe_dump(
                {
                    "backend": {"type": "file", "base_path": str(secrets_dir)},
                    "hot_reload": {"enabled": True, "poll_interval_seconds": 0.1},
                }
            ),
            encoding="utf-8",
        )

        loader = ConfigLoader()
        specs, backends, policy = loader.reload_harness_tools(config_dir)
        harness_executor = HarnessExecutor(specs, backends, execution_policy=policy)
        harness_tool_names = set(specs.keys())

        http_executor = SimpleNamespace(update_tool_specs=lambda x: None)
        from loop_controller.secrets import FileSecretBackend

        broker = FileSecretBackend(secrets_dir)
        reloader = HotReloader(
            config_dir=config_dir,
            config_loader=loader,
            http_executor=http_executor,  # type: ignore[arg-type]
            secret_broker=broker,
            harness_executor=harness_executor,
            harness_tool_names=harness_tool_names,
            poll_interval_seconds=0.1,
            enabled=True,
        )

        await reloader.start()
        try:
            assert harness_executor._tool_specs["shell"].description == "before"

            harness_tools.write_text(
                yaml.safe_dump(
                    {
                        "tools": {
                            "shell": {
                                "harness": "local",
                                "description": "after",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if harness_executor._tool_specs["shell"].description == "after":
                    break
                await asyncio.sleep(0.05)
            assert harness_executor._tool_specs["shell"].description == "after"
        finally:
            await reloader.stop()
