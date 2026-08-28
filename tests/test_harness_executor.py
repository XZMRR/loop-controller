"""HarnessExecutor 单元测试（v0.25.0）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from loop_controller.checkpoint import Checkpoint
from loop_controller.executors import ExecutionContext, ExecutorRegistry, HarnessExecutor
from loop_controller.executors.harness_models import (
    HarnessAuthConfig,
    HarnessHealthConfig,
    HarnessToolSpec,
    HTTPBackendConfig,
    SubprocessBackendConfig,
)
from loop_controller.executors.harness_protocol import HarnessExecuteResponse
from loop_controller.identity.revocation import RevocationEntry, RevocationList
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.metrics import (
    HARNESS_CALL_DURATION,
    HARNESS_CALLS_TOTAL,
    HARNESS_HEALTH,
    HARNESS_IN_FLIGHT,
    HARNESS_OVERLOADED_TOTAL,
    HARNESS_QUEUE_WAIT,
)
from loop_controller.models import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Decision,
    Tool,
    ToolPermission,
)
from loop_controller.runtime import Runtime


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


def _metric_value(metric: Any, sample_name: str, labels: dict[str, str]) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == sample_name and sample.labels == labels:
                return float(sample.value)
    return 0.0


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


class TestHarnessSecretRefs:
    def test_config_loader_parses_explicit_secret_refs(self, tmp_path: Path) -> None:
        path = tmp_path / "harness_tools.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "backends": {
                        "remote": {
                            "type": "http",
                            "base_url": "https://harness.example",
                            "api_key_env": "HARNESS_API_KEY",
                        }
                    },
                    "tools": {
                        "deploy": {
                            "harness": "remote",
                            "secret_refs": ["DEPLOY_TOKEN"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        specs, backends = ConfigLoader()._load_harness_tools(path)

        assert specs["deploy"].secret_refs == ["DEPLOY_TOKEN"]
        backend = backends["remote"]
        assert isinstance(backend, HTTPBackendConfig)
        assert backend.api_key_env == "HARNESS_API_KEY"

    def test_refs_merge_deduplicate_and_remain_backend_isolated(self) -> None:
        executor = HarnessExecutor(
            {
                "deploy": HarnessToolSpec(
                    tool_name="deploy",
                    harness="production",
                    secret_refs=["DEPLOY_TOKEN", "PROD_API_KEY"],
                ),
                "report": HarnessToolSpec(
                    tool_name="report",
                    harness="reporting",
                    secret_refs=["REPORT_TOKEN"],
                ),
            },
            {
                "production": HTTPBackendConfig(
                    name="production",
                    base_url="https://prod.example",
                    api_key_env="PROD_API_KEY",
                ),
                "reporting": HTTPBackendConfig(
                    name="reporting",
                    base_url="https://report.example",
                    api_key_env="REPORT_API_KEY",
                ),
            },
        )

        assert executor.secret_refs_for("deploy") == ["DEPLOY_TOKEN", "PROD_API_KEY"]
        assert executor.secret_refs_for("report") == ["REPORT_API_KEY", "REPORT_TOKEN"]
        assert executor.secret_refs_for("missing") == []

    def test_subprocess_env_is_not_treated_as_secret(self) -> None:
        executor = HarnessExecutor(
            {
                "echo": HarnessToolSpec(
                    tool_name="echo",
                    harness="local",
                )
            },
            {
                "local": SubprocessBackendConfig(
                    name="local",
                    command=["python", "harness.py"],
                    env={"ORDINARY_SETTING": "visible"},
                )
            },
        )

        assert executor.secret_refs_for("echo") == []

    @pytest.mark.asyncio
    async def test_revoked_backend_api_key_blocks_request_and_audit_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_value = "super-secret-api-key"
        monkeypatch.setenv("HARNESS_API_KEY", secret_value)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.headers["x-harness-api-key"] == secret_value
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        spec = HarnessToolSpec(tool_name="deploy", harness="remote")
        config = HTTPBackendConfig(
            name="remote",
            base_url="https://harness.example",
            api_key_env="HARNESS_API_KEY",
        )
        executor = HarnessExecutor({"deploy": spec}, {"remote": config})
        backend = executor._backends["remote"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        registry = ExecutorRegistry()
        registry.register("deploy", executor)
        agent = Agent(
            agent_id="agent-1",
            name="Agent",
            profile_id="profile-1",
            owner_id="user-1",
        )
        audit_path = tmp_path / "audit.jsonl"
        checkpoint = Checkpoint(
            profiles={},
            policy_engine=object(),
            policy_store=object(),
            executor_registry=registry,
            identity=ConfigIdentityProvider(
                agents={agent.agent_id: agent}, users={"user-1": "User"}
            ),
            revocation_list=RevocationList(
                [
                    RevocationEntry(
                        type="secret",
                        id="HARNESS_API_KEY",
                        reason="compromised credential",
                    )
                ]
            ),
            audit_store=JsonlAuditStore(audit_path),
        )
        proposal = ActionProposal(
            task_id="task-1",
            call_id=uuid.uuid4().hex,
            agent_id=agent.agent_id,
            tool_name="deploy",
            arguments={},
            task_context="test",
        )
        decision = Decision(
            decision_id=uuid.uuid4().hex,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            verdict="allow",
            reason="allowed",
            policy_version="test",
            profile_version="test",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        checkpoint._decision_store.record_decision(decision)

        blocked = await checkpoint.forward(proposal, decision, user_id="user-1")

        assert blocked.status == "blocked"
        assert blocked.error_code == "revoked"
        assert calls == 0
        audit_text = audit_path.read_text(encoding="utf-8")
        assert "HARNESS_API_KEY" in audit_text
        assert secret_value not in audit_text

        checkpoint._revocation_list = RevocationList()
        restored_decision = decision.model_copy(
            update={
                "decision_id": uuid.uuid4().hex,
                "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            }
        )
        checkpoint._decision_store.record_decision(restored_decision)
        restored = await checkpoint.forward(proposal, restored_decision, user_id="user-1")

        assert restored.status == "success"
        assert calls == 1
        await executor.stop()


class TestHarnessConcurrencyAuthAndHealth:
    @pytest.mark.asyncio
    async def test_backend_concurrency_gate_and_release(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        config = HTTPBackendConfig(
            name="http_harness",
            base_url="https://harness.example",
            max_concurrent_calls=1,
            acquire_timeout_seconds=0.01,
        )
        executor = HarnessExecutor(
            {"harness_echo": _echo_tool_spec()}, {"http_harness": config}
        )
        await executor.start()
        backend = executor._backends["http_harness"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = asyncio.create_task(
            executor.execute("harness_echo", {}, _fake_context())
        )
        await entered.wait()
        overloaded = await executor.execute("harness_echo", {}, _fake_context())
        assert overloaded.error_code == "harness_overloaded"
        assert calls == 1
        release.set()
        assert (await first).status == "success"
        assert executor.backend_statuses()[0].in_flight == 0
        await executor.stop()

    @pytest.mark.asyncio
    async def test_harness_metrics_cover_calls_queue_in_flight_and_overload(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        backend_name = f"metrics_{uuid.uuid4().hex}"
        tool_name = f"tool_{uuid.uuid4().hex}"
        spec = HarnessToolSpec(tool_name=tool_name, harness=backend_name)
        config = HTTPBackendConfig(
            name=backend_name,
            base_url="https://harness.example",
            max_concurrent_calls=1,
            acquire_timeout_seconds=0.01,
        )
        executor = HarnessExecutor({tool_name: spec}, {backend_name: config})
        await executor.start()
        backend = executor._backends[backend_name]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        first = asyncio.create_task(executor.execute(tool_name, {}, _fake_context()))
        await entered.wait()
        assert _metric_value(
            HARNESS_IN_FLIGHT,
            "loop_controller_harness_in_flight",
            {"backend": backend_name},
        ) == 1
        overloaded = await executor.execute(tool_name, {}, _fake_context())
        assert overloaded.error_code == "harness_overloaded"
        release.set()
        assert (await first).status == "success"

        assert _metric_value(
            HARNESS_CALLS_TOTAL,
            "loop_controller_harness_calls_total",
            {
                "backend": backend_name,
                "tool": tool_name,
                "status": "success",
                "error_code": "none",
            },
        ) == 1
        assert _metric_value(
            HARNESS_CALLS_TOTAL,
            "loop_controller_harness_calls_total",
            {
                "backend": backend_name,
                "tool": tool_name,
                "status": "error",
                "error_code": "harness_overloaded",
            },
        ) == 1
        assert _metric_value(
            HARNESS_OVERLOADED_TOTAL,
            "loop_controller_harness_overloaded_total",
            {"backend": backend_name},
        ) == 1
        assert _metric_value(
            HARNESS_IN_FLIGHT,
            "loop_controller_harness_in_flight",
            {"backend": backend_name},
        ) == 0
        assert _metric_value(
            HARNESS_QUEUE_WAIT,
            "loop_controller_harness_queue_wait_seconds_count",
            {"backend": backend_name},
        ) == 2
        assert _metric_value(
            HARNESS_CALL_DURATION,
            "loop_controller_harness_call_duration_seconds_count",
            {"backend": backend_name, "tool": tool_name},
        ) == 2
        await executor.stop()

    @pytest.mark.asyncio
    async def test_api_key_and_hmac_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HARNESS_KEY", "test-secret")
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        for auth in (
            HarnessAuthConfig(type="api_key", key_env="HARNESS_KEY"),
            HarnessAuthConfig(
                type="hmac_sha256", key_env="HARNESS_KEY", key_id="key-1"
            ),
        ):
            config = HTTPBackendConfig(
                name="http_harness", base_url="https://harness.example", auth=auth
            )
            executor = HarnessExecutor(
                {"harness_echo": _echo_tool_spec()}, {"http_harness": config}
            )
            await executor.start()
            backend = executor._backends["http_harness"]
            backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await executor.execute("harness_echo", {}, _fake_context())
            await executor.stop()

        assert captured[0].headers["x-harness-api-key"] == "test-secret"
        signed = captured[1]
        body_hash = hashlib.sha256(signed.content).hexdigest()
        canonical = "\n".join(
            [
                "POST",
                "/harness/v1/execute",
                "1",
                "key-1",
                signed.headers["x-harness-timestamp"],
                signed.headers["x-harness-nonce"],
                body_hash,
            ]
        )
        expected = base64.b64encode(
            hmac.new(b"test-secret", canonical.encode(), hashlib.sha256).digest()
        ).decode()
        assert signed.headers["x-harness-signature"] == expected

    @pytest.mark.asyncio
    async def test_health_lifecycle_blocks_and_recovers(self) -> None:
        healthy = False
        execute_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal execute_calls
            if request.url.path == "/health":
                return httpx.Response(200 if healthy else 503)
            execute_calls += 1
            return httpx.Response(200, json={"status": "success", "content": "ok"})

        config = HTTPBackendConfig(
            name="http_harness",
            base_url="https://harness.example",
            health=HarnessHealthConfig(
                enabled=True,
                startup_required=False,
                interval_seconds=0.01,
                unhealthy_threshold=1,
            ),
        )
        executor = HarnessExecutor(
            {"harness_echo": _echo_tool_spec()}, {"http_harness": config}
        )
        backend = executor._backends["http_harness"]
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await executor.start()
        executor._record_health("http_harness", False)
        assert _metric_value(
            HARNESS_HEALTH,
            "loop_controller_harness_health",
            {"backend": "http_harness"},
        ) == 0
        blocked = await executor.execute("harness_echo", {}, _fake_context())
        assert blocked.error_code == "harness_backend_unavailable"
        assert execute_calls == 0
        healthy = True
        executor._record_health("http_harness", await backend.check_health())
        assert _metric_value(
            HARNESS_HEALTH,
            "loop_controller_harness_health",
            {"backend": "http_harness"},
        ) == 1
        restored = await executor.execute("harness_echo", {}, _fake_context())
        assert restored.status == "success"
        assert executor.backend_statuses()[0].status == "healthy"
        await executor.stop()


@pytest.mark.asyncio
async def test_runtime_explicitly_starts_and_stops_harness_executor() -> None:
    runtime = object.__new__(Runtime)
    gateway = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    harness_executor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    object.__setattr__(runtime, "audit_store", object())
    object.__setattr__(runtime, "gateway", gateway)
    object.__setattr__(runtime, "http_client", None)
    object.__setattr__(runtime, "hot_reloader", None)
    object.__setattr__(runtime, "harness_executor", harness_executor)

    await runtime.start()
    harness_executor.start.assert_awaited_once_with()

    await runtime.aclose()
    harness_executor.stop.assert_awaited_once_with()
