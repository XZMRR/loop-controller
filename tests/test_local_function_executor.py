"""本地函数执行器测试（v0.23.0）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from loop_controller.executors import ExecutionContext, LocalFunctionExecutor
from loop_controller.executors.local_function_models import LocalFunctionSpec
from loop_controller.models import CapabilityProfile


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """创建一个临时 Python 模块，供子进程 runner 导入。"""
    module_path = tmp_path / "demo_local_tools.py"
    module_path.write_text(
        """from typing import Any
import os


def add(a: int, b: int) -> int:
    return a + b


def echo(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def divide(a: int, b: int) -> float:
    return a / b


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_with_os_open(path: str) -> str:
    fd = os.open(path, os.O_RDONLY)
    try:
        data = os.read(fd, 65536)
        return data.decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def huge_output(size: int) -> str:
    return "x" * size


def sleep_forever() -> None:
    import time
    time.sleep(10)
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_local_function_success(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    specs = {
        "add": LocalFunctionSpec(
            tool_name="add",
            module="demo_local_tools",
            function="add",
            sandbox={"timeout_seconds": 5},
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute("add", {"a": 2, "b": 3}, _fake_context())
    assert result.status == "success"
    assert result.content == 5


@pytest.mark.asyncio
async def test_local_function_echo(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    specs = {
        "echo": LocalFunctionSpec(
            tool_name="echo",
            module="demo_local_tools",
            function="echo",
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute("echo", {"x": 1, "y": "hello"}, _fake_context())
    assert result.status == "success"
    assert result.content == {"x": 1, "y": "hello"}


@pytest.mark.asyncio
async def test_local_function_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    specs = {
        "divide": LocalFunctionSpec(
            tool_name="divide",
            module="demo_local_tools",
            function="divide",
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute("divide", {"a": 1, "b": 0}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "local_function_runtime_error"


@pytest.mark.asyncio
async def test_local_function_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = {
        "bad": LocalFunctionSpec(
            tool_name="bad",
            module="nonexistent_module_12345",
            function="bad",
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute("bad", {}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "local_function_import_error"


@pytest.mark.asyncio
async def test_local_function_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = LocalFunctionExecutor({})
    result = await executor.execute("missing", {}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "local_function_not_found"


@pytest.mark.asyncio
async def test_local_function_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    specs = {
        "sleep_forever": LocalFunctionSpec(
            tool_name="sleep_forever",
            module="demo_local_tools",
            function="sleep_forever",
            sandbox={"timeout_seconds": 0.5},
        )
    }
    executor = LocalFunctionExecutor(specs)
    start = time.monotonic()
    result = await executor.execute("sleep_forever", {}, _fake_context())
    elapsed = time.monotonic() - start
    assert result.status == "error"
    assert result.error_code == "local_function_timeout"
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_local_function_output_too_large(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
) -> None:
    """子进程输出超过 max_output_bytes 时 executor 不应 OOM。"""
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    specs = {
        "huge_output": LocalFunctionSpec(
            tool_name="huge_output",
            module="demo_local_tools",
            function="huge_output",
            sandbox={"max_output_bytes": 1024},
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute("huge_output", {"size": 2048}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "local_function_output_too_large"


@pytest.mark.asyncio
async def test_local_function_path_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    allowed_file = tmp_path / "allowed.txt"
    allowed_file.write_text("allowed-content", encoding="utf-8")
    blocked_file = tmp_path / "blocked.txt"
    blocked_file.write_text("blocked-content", encoding="utf-8")

    specs = {
        "read_file": LocalFunctionSpec(
            tool_name="read_file",
            module="demo_local_tools",
            function="read_file",
            sandbox={"allowed_paths": [str(tmp_path / "allowed.txt")]},
        )
    }
    executor = LocalFunctionExecutor(specs)

    allowed_result = await executor.execute(
        "read_file", {"path": str(allowed_file)}, _fake_context()
    )
    assert allowed_result.status == "success"
    assert allowed_result.content == "allowed-content"

    blocked_result = await executor.execute(
        "read_file", {"path": str(blocked_file)}, _fake_context()
    )
    assert blocked_result.status == "error"
    assert blocked_result.error_code == "local_function_sandbox_violation"


@pytest.mark.asyncio
async def test_local_function_os_open_sandbox_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tools_dir: Path,
    tmp_path: Path,
) -> None:
    """os.open 绕过 builtins.open 时仍应被沙箱拦截。"""
    monkeypatch.setenv("PYTHONPATH", str(tools_dir))
    blocked_file = tmp_path / "blocked_os_open.txt"
    blocked_file.write_text("blocked-content", encoding="utf-8")

    specs = {
        "read_file": LocalFunctionSpec(
            tool_name="read_file",
            module="demo_local_tools",
            function="read_with_os_open",
            sandbox={"allowed_paths": [str(tmp_path / "allowed.txt")]},
        )
    }
    executor = LocalFunctionExecutor(specs)
    result = await executor.execute(
        "read_file", {"path": str(blocked_file)}, _fake_context()
    )
    assert result.status == "error"
    assert result.error_code == "local_function_sandbox_violation"


@pytest.mark.asyncio
async def test_list_tools_filtered_by_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = {
        "fn_a": LocalFunctionSpec(tool_name="fn_a", module="m", function="a"),
        "fn_b": LocalFunctionSpec(tool_name="fn_b", module="m", function="b"),
    }
    executor = LocalFunctionExecutor(specs)
    profile = CapabilityProfile(
        profile_id="p1",
        tools={"fn_a": {"tool_name": "fn_a", "allowed": True}},
    )
    tools = await executor.list_tools(profile)
    assert len(tools) == 1
    assert tools[0].canonical_name == "fn_a"
