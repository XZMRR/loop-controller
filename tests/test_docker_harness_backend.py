"""Docker Harness backend 测试（v0.32.0）。

不依赖真实 Docker daemon；通过 mock ``asyncio.create_subprocess_exec`` 验证命令构造与协议处理。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from loop_controller.executors.docker_harness_backend import DockerHarnessBackend
from loop_controller.executors.harness_models import DockerBackendConfig
from loop_controller.executors.harness_protocol import HarnessExecuteResponse, HarnessSandbox
from loop_controller.models import ToolResult


def _fake_context() -> SimpleNamespace:
    return SimpleNamespace(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
        session_id="s1",
        tenant_id="tenant",
    )


def _success_response(content: Any = "ok") -> bytes:
    return HarnessExecuteResponse(
        status="success",
        content=content,
        effective_sandbox=HarnessSandbox(),
    ).model_dump_json().encode("utf-8")


def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = AsyncMock()
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_docker_backend_start_checks_docker_cli() -> None:
    config = DockerBackendConfig(name="docker", image="test:latest")
    backend = DockerHarnessBackend(config)

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(returncode=0)),
    ) as mock_exec:
        await backend.start()

    mock_exec.assert_called_once()
    args = mock_exec.call_args[0]
    assert args[:2] == ("docker", "--version")


@pytest.mark.asyncio
async def test_docker_backend_start_fails_without_docker() -> None:
    config = DockerBackendConfig(name="docker", image="test:latest")
    backend = DockerHarnessBackend(config)

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(returncode=1)),
    ):
        with pytest.raises(RuntimeError, match="docker CLI"):
            await backend.start()


@pytest.mark.asyncio
async def test_docker_backend_build_command_default_network_none() -> None:
    config = DockerBackendConfig(name="docker", image="test:latest")
    backend = DockerHarnessBackend(config)
    cmd = backend._build_command()
    assert cmd[:5] == ["docker", "run", "--rm", "-i", "--network"]
    assert cmd[5] == "none"
    assert cmd[-1] == "test:latest"


@pytest.mark.asyncio
async def test_docker_backend_build_command_with_env_and_mounts() -> None:
    config = DockerBackendConfig(
        name="docker",
        image="test:latest",
        network_mode="bridge",
        env={"FOO": "bar"},
        mounts=[{"source": "/host", "target": "/container", "read_only": True}],
    )
    backend = DockerHarnessBackend(config)
    cmd = backend._build_command()
    assert "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == "bridge"
    assert "-e" in cmd
    assert "FOO=bar" in cmd
    assert "-v" in cmd
    assert "/host:/container:ro" in cmd


@pytest.mark.asyncio
async def test_docker_backend_execute_success() -> None:
    config = DockerBackendConfig(name="docker", image="harness:latest")
    backend = DockerHarnessBackend(config)

    proc = _make_proc(stdout=_success_response({"echo": "hello"}))
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        result = await backend.execute(
            "echo",
            {"message": "hello"},
            _fake_context(),
            HarnessSandbox(),
        )

    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert result.content == {"echo": "hello"}
    mock_exec.assert_called_once()
    cmd = mock_exec.call_args[0]
    assert cmd[0] == "docker"
    assert "harness:latest" in cmd


@pytest.mark.asyncio
async def test_docker_backend_execute_invalid_json() -> None:
    config = DockerBackendConfig(name="docker", image="harness:latest")
    backend = DockerHarnessBackend(config)

    proc = _make_proc(stdout=b"not-json")
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await backend.execute(
            "echo",
            {"message": "hello"},
            _fake_context(),
            HarnessSandbox(),
        )

    assert result.status == "error"
    assert result.error_code == "harness_invalid_response"


@pytest.mark.asyncio
async def test_docker_backend_execute_missing_effective_sandbox() -> None:
    config = DockerBackendConfig(name="docker", image="harness:latest")
    backend = DockerHarnessBackend(config)

    stdout = HarnessExecuteResponse(
        status="success",
        content="ok",
    ).model_dump_json().encode("utf-8")
    proc = _make_proc(stdout=stdout)
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await backend.execute(
            "echo",
            {"message": "hello"},
            _fake_context(),
            HarnessSandbox(),
        )

    assert result.status == "error"
    assert result.error_code == "harness_sandbox_attestation_missing"


@pytest.mark.asyncio
async def test_docker_backend_check_health() -> None:
    config = DockerBackendConfig(name="docker", image="harness:latest")
    backend = DockerHarnessBackend(config)

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(returncode=0)),
    ):
        assert await backend.check_health() is True

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(returncode=1)),
    ):
        assert await backend.check_health() is False
