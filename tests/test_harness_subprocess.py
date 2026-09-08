"""Harness 子进程后端集成测试（v0.25.0）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from loop_controller.executors import ExecutionContext, HarnessExecutor
from loop_controller.executors.harness_models import (
    HarnessToolSpec,
    SubprocessBackendConfig,
)


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


@pytest.fixture
def subprocess_config() -> SubprocessBackendConfig:
    project_root = Path(__file__).parent.parent
    return SubprocessBackendConfig(
        name="subprocess_harness",
        type="subprocess",
        command=[
            sys.executable,
            "-m",
            "examples.contrib.harness.harness_server",
        ],
        env={"PYTHONPATH": str(project_root / "src")},
    )


@pytest.mark.asyncio
async def test_subprocess_harness_echo(
    subprocess_config: SubprocessBackendConfig,
) -> None:
    spec = HarnessToolSpec(
        tool_name="echo",
        harness="subprocess_harness",
        cost_per_call=10,
    )
    executor = HarnessExecutor(
        {"echo": spec},
        {"subprocess_harness": subprocess_config},
    )
    try:
        await executor.start()
        result = await executor.execute(
            "echo",
            {"text": "hello harness"},
            _fake_context(),
        )
        assert result.status == "success"
        assert result.content == {"echo": "hello harness"}
    finally:
        await executor.stop()
