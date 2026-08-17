"""T1.5 MCPGateway + mock email server 测试.

- 真实拉起官方 filesystem MCP server（npx，已缓存）读写临时目录；
- mock email server：send_email 落盘 sent_emails.jsonl、web_search 返回固定结果；
- list_tools 按 CapabilityProfile 过滤。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from loop_controller.infra.config_loader import MCPServerConfig, ToolMappingEntry
from loop_controller.mcp_gateway import MCPGateway, MCPGatewayError
from loop_controller.models import CapabilityProfile, ToolPermission

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _make_profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="research_assistant_v1",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(tool_name="read_file", allowed=True),
            "write_file": ToolPermission(tool_name="write_file", allowed=True),
            "send_email": ToolPermission(
                tool_name="send_email", allowed=True, require_approval=True
            ),
        },
    )


@pytest.fixture
def fs_config(tmp_path: Path) -> MCPServerConfig:
    kb = tmp_path / "kb"
    out = tmp_path / "output"
    kb.mkdir()
    out.mkdir()
    (kb / "ai_compliance_checklist.md").write_text(
        "# AI 合规 checklist 测试内容\n", encoding="utf-8"
    )
    return MCPServerConfig(
        name="filesystem",
        command=[
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            str(kb),
            str(out),
        ],
    )


@pytest.fixture
def email_config(tmp_path: Path) -> MCPServerConfig:
    return MCPServerConfig(
        name="email_mock",
        command=[sys.executable, "-m", "loop_controller.mocks.email_server"],
    )


@pytest.fixture
def mapping() -> dict[str, ToolMappingEntry]:
    return {
        "read_file": ToolMappingEntry(server="filesystem", mcp_name="read_text_file"),
        "write_file": ToolMappingEntry(server="filesystem", mcp_name="write_file"),
        "web_search": ToolMappingEntry(server="email_mock", mcp_name="web_search"),
        "send_email": ToolMappingEntry(server="email_mock", mcp_name="send_email"),
    }


@pytest.fixture
def env_extra(tmp_path: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(SRC_DIR),
        "SENT_EMAILS_PATH": str(tmp_path / "sent_emails.jsonl"),
    }


@pytest.fixture
def sent_emails_path(tmp_path: Path) -> Path:
    return tmp_path / "sent_emails.jsonl"


async def test_list_tools_filters_by_profile(
    fs_config: MCPServerConfig,
    email_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    tmp_path: Path,
) -> None:
    gateway = MCPGateway(
        {"filesystem": fs_config, "email_mock": email_config},
        mapping,
        env_extra=env_extra,
        cwd=str(tmp_path),
    )
    await gateway.start()
    try:
        tools = await gateway.list_tools(_make_profile())
        assert sorted(t.canonical_name for t in tools) == [
            "read_file",
            "send_email",
            "web_search",
            "write_file",
        ]
        by_name = {t.canonical_name: t for t in tools}
        assert by_name["read_file"].mcp_name == "read_text_file"
        assert by_name["send_email"].input_schema["type"] == "object"
    finally:
        await gateway.aclose()


async def test_send_email_writes_sent_emails_log(
    email_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    sent_emails_path: Path,
    tmp_path: Path,
) -> None:
    gateway = MCPGateway({"email_mock": email_config}, mapping, env_extra=env_extra, cwd=str(tmp_path))
    await gateway.start()
    try:
        result = await gateway.call_tool(
            "send_email",
            {"to": "zhang@company.com", "subject": "摘要", "body": "hi"},
            call_id="c1",
            task_id="t1",
        )
        assert result.status == "success"
        assert json.loads(result.content) == {"status": "queued"}
        records = [
            json.loads(line) for line in sent_emails_path.read_text(encoding="utf-8").splitlines()
        ]
        assert records[0]["tool"] == "send_email"
        assert records[0]["arguments"]["to"] == "zhang@company.com"
    finally:
        await gateway.aclose()


async def test_web_search_mock_returns_fixed_result(
    email_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    tmp_path: Path,
) -> None:
    gateway = MCPGateway({"email_mock": email_config}, mapping, env_extra=env_extra, cwd=str(tmp_path))
    await gateway.start()
    try:
        result = await gateway.call_tool(
            "web_search", {"query": "OpenAI 合规"}, call_id="c2", task_id="t1"
        )
        assert result.status == "success"
        payload = json.loads(result.content)
        assert payload["results"][0]["title"]
    finally:
        await gateway.aclose()


async def test_filesystem_read_and_write(
    fs_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    tmp_path: Path,
) -> None:
    kb = tmp_path / "kb"
    gateway = MCPGateway({"filesystem": fs_config}, mapping, env_extra=env_extra, cwd=str(tmp_path))
    await gateway.start()
    try:
        read = await gateway.call_tool(
            "read_file",
            {"path": str(kb / "ai_compliance_checklist.md")},
            call_id="c3",
            task_id="t1",
        )
        assert read.status == "success"
        assert "AI 合规" in read.content

        write = await gateway.call_tool(
            "write_file",
            {"path": str(tmp_path / "output" / "summary.md"), "content": "# 摘要\n"},
            call_id="c4",
            task_id="t1",
        )
        assert write.status == "success"
        assert (tmp_path / "output" / "summary.md").read_text(encoding="utf-8") == "# 摘要\n"
    finally:
        await gateway.aclose()


async def test_missing_file_returns_error_status(
    fs_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    tmp_path: Path,
) -> None:
    gateway = MCPGateway({"filesystem": fs_config}, mapping, env_extra=env_extra, cwd=str(tmp_path))
    await gateway.start()
    try:
        result = await gateway.call_tool(
            "read_file",
            {"path": str(tmp_path / "kb" / "nope.md")},
            call_id="c5",
            task_id="t1",
        )
        assert result.status == "error"
    finally:
        await gateway.aclose()


async def test_unknown_mapping_raises(
    email_config: MCPServerConfig,
    mapping: dict[str, ToolMappingEntry],
    env_extra: dict[str, str],
    tmp_path: Path,
) -> None:
    gateway = MCPGateway({"email_mock": email_config}, mapping, env_extra=env_extra, cwd=str(tmp_path))
    await gateway.start()
    try:
        with pytest.raises(MCPGatewayError):
            await gateway.call_tool("no_such_tool", {}, call_id="c6", task_id="t1")
    finally:
        await gateway.aclose()
