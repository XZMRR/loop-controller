"""集成测试共享 fixture。

提供临时工作目录、集成专用配置和已启动的真实 LoopController。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from loop_controller.controller import LoopController
from tests.conftest import write_trusted_local_harness_config
from tests.controller_helpers import controller_for

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def integration_workdir(tmp_path: Path) -> Path:
    """复制 config/policies 到临时目录，返回项目根目录。"""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    return root


def _write_profiles(
    config_dir: Path,
    tools: dict[str, dict[str, Any]],
) -> None:
    """写入仅包含指定工具的 profiles.yaml。"""
    profile = {
        "profile_id": "integration_profile",
        "description": "集成测试岗位",
        "max_budget_token": 100000,
        "max_budget_payment": 0.0,
        "tools": tools,
    }
    (config_dir / "profiles.yaml").write_text(
        yaml.safe_dump({"profiles": [profile]}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_local_functions(
    config_dir: Path,
    functions: dict[str, tuple[str, str]],
    timeouts: dict[str, int] | None = None,
) -> None:
    """写入 local_functions.yaml。"""
    timeouts = timeouts or {}
    lines = ["tools:"]
    for name, (module, function) in functions.items():
        lines.append(f"  {name}:")
        lines.append(f"    module: {module}")
        lines.append(f"    function: {function}")
        lines.append(f"    description: {name}")
        lines.append("    input_schema:")
        lines.append("      type: object")
        lines.append("      additionalProperties: true")
        lines.append("    cost_per_call: 1")
        lines.append("    default_risk: low")
        lines.append("    sandbox:")
        lines.append(f"      timeout_seconds: {timeouts.get(name, 10)}")
        lines.append("      max_output_bytes: 65536")
        lines.append("      allowed_paths: []")
        lines.append("      env_whitelist: []")
    (config_dir / "local_functions.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_agents(
    config_dir: Path,
    agent_id: str = "integration_agent",
    owner_id: str = "alice",
) -> None:
    """写入 agents.yaml，确保测试 agent 可识别。"""
    content = f"""agents:
  - agent_id: {agent_id}
    name: Integration Agent
    profile_id: integration_profile
    owner_id: {owner_id}
    identity:
      issuer: https://test.local
      subject: agent://{agent_id}/test

users:
  - user_id: alice
    display_name: Alice
  - user_id: bob
    display_name: Bob
  - user_id: zhang_manager
    display_name: 张经理
"""
    (config_dir / "agents.yaml").write_text(content, encoding="utf-8")
    (config_dir / "interaction_profiles.yaml").write_text(
        "interaction_profiles: []\n", encoding="utf-8"
    )
    (config_dir / "agent_trust.yaml").write_text(
        "agent_trust: []\n", encoding="utf-8"
    )
    (config_dir / "delegation_policies.yaml").write_text(
        "delegation_policies: []\n", encoding="utf-8"
    )


def _clear_mcp_servers(config_dir: Path) -> None:
    """清空 mcp_servers.yaml，避免启动真实 MCP server。"""
    (config_dir / "mcp_servers.yaml").write_text(
        "servers: {}\ntool_mapping: {}\n",
        encoding="utf-8",
    )


@pytest.fixture
def simple_governance_workdir(integration_workdir: Path) -> Path:
    """配置简单本地函数工具的集成测试目录。

    工具：
    - add(a, b): 返回 a + b
    - echo(text): 返回 text
    """
    config_dir = integration_workdir / "config"
    _write_profiles(
        config_dir,
        {
            "add": {"allowed": True, "max_calls_per_task": 10},
            "echo": {"allowed": True, "max_calls_per_task": 10},
        },
    )
    _write_local_functions(
        config_dir,
        {
            "add": ("tests.integration.local_tools", "add"),
            "echo": ("tests.integration.local_tools", "echo"),
        },
    )
    _write_agents(config_dir)
    _clear_mcp_servers(config_dir)
    write_trusted_local_harness_config(config_dir, ["add", "echo"])
    return integration_workdir


@pytest.fixture
def approval_governance_workdir(integration_workdir: Path) -> Path:
    """配置需要审批的敏感工具。"""
    config_dir = integration_workdir / "config"
    _write_profiles(
        config_dir,
        {
            "add": {"allowed": True, "max_calls_per_task": 10},
            "send_email": {
                "allowed": True,
                "require_approval": True,
                "max_calls_per_task": 1,
                "allowed_args": {"to": ["*@company.com"]},
            },
        },
    )
    _write_local_functions(
        config_dir,
        {
            "add": ("tests.integration.local_tools", "add"),
            "send_email": ("tests.integration.local_tools", "send_email"),
        },
    )
    _write_agents(config_dir, owner_id="zhang_manager")
    _clear_mcp_servers(config_dir)
    write_trusted_local_harness_config(config_dir, ["add", "send_email"])
    return integration_workdir


@pytest.fixture
async def simple_controller(
    simple_governance_workdir: Path,
    opa_server: str,
) -> LoopController:
    """已启动、使用 simple_governance_workdir 的真实 LoopController。"""
    controller = await controller_for(simple_governance_workdir, opa_server)
    try:
        yield controller
    finally:
        await controller.aclose()


@pytest.fixture
async def approval_controller(
    approval_governance_workdir: Path,
    opa_server: str,
) -> LoopController:
    """已启动、使用 approval_governance_workdir 的真实 LoopController。"""
    controller = await controller_for(approval_governance_workdir, opa_server)
    try:
        yield controller
    finally:
        await controller.aclose()


@pytest.fixture
def boundary_governance_workdir(integration_workdir: Path) -> Path:
    """配置边界场景本地函数的集成测试目录。

    工具：
    - raise_error(message): 抛出 RuntimeError
    - hang_forever(seconds): 睡眠指定秒数，用于超时测试
    """
    config_dir = integration_workdir / "config"
    _write_profiles(
        config_dir,
        {
            "add": {"allowed": True, "max_calls_per_task": 10},
            "echo": {"allowed": True, "max_calls_per_task": 10},
            "raise_error": {"allowed": True, "max_calls_per_task": 10},
            "hang_forever": {"allowed": True, "max_calls_per_task": 10},
        },
    )
    _write_local_functions(
        config_dir,
        {
            "add": ("tests.integration.local_tools", "add"),
            "echo": ("tests.integration.local_tools", "echo"),
            "raise_error": ("tests.integration.local_tools", "raise_error"),
            "hang_forever": ("tests.integration.local_tools", "hang_forever"),
        },
        timeouts={"hang_forever": 2},
    )
    _write_agents(config_dir)
    _clear_mcp_servers(config_dir)
    write_trusted_local_harness_config(
        config_dir, ["add", "echo", "raise_error", "hang_forever"]
    )
    return integration_workdir


@pytest.fixture
async def boundary_controller(
    boundary_governance_workdir: Path,
    opa_server: str,
) -> LoopController:
    """已启动、使用 boundary_governance_workdir 的真实 LoopController。"""
    controller = await controller_for(boundary_governance_workdir, opa_server)
    try:
        yield controller
    finally:
        await controller.aclose()
