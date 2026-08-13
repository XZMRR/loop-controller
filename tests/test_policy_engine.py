"""PolicyEngine 单元测试."""

from __future__ import annotations

import pytest

from loop_controller import MockPolicyEngine, PolicyEngineError


@pytest.fixture
def engine() -> MockPolicyEngine:
    return MockPolicyEngine()


def test_allowed_tool(engine):
    result = engine.evaluate(
        "loop_controller.tool_permission",
        {
            "proposal": {
                "tool_name": "read_file",
                "arguments": {"path": "/tmp/report.md"},
            },
            "profile": {"allowed_tools": ["read_file", "write_file"]},
        },
    )
    assert result["verdict"] == "allow"


def test_denied_tool_not_in_allowed_list(engine):
    result = engine.evaluate(
        "loop_controller.tool_permission",
        {
            "proposal": {
                "tool_name": "send_email",
                "arguments": {"to": "zhang@company.com"},
            },
            "profile": {"allowed_tools": ["read_file", "write_file"]},
        },
    )
    assert result["verdict"] == "deny"


def test_send_email_external_requires_approval(engine):
    result = engine.evaluate(
        "loop_controller.tool_permission",
        {
            "proposal": {
                "tool_name": "send_email",
                "arguments": {"to": "external@gmail.com"},
            },
            "profile": {"allowed_tools": ["send_email"]},
        },
    )
    assert result["verdict"] == "require_approval"


def test_send_email_internal_allowed(engine):
    result = engine.evaluate(
        "loop_controller.tool_permission",
        {
            "proposal": {
                "tool_name": "send_email",
                "arguments": {"to": "zhang@company.com"},
            },
            "profile": {"allowed_tools": ["send_email"]},
        },
    )
    assert result["verdict"] == "allow"


def test_write_file_unallowed_path(engine):
    result = engine.evaluate(
        "loop_controller.tool_permission",
        {
            "proposal": {
                "tool_name": "write_file",
                "arguments": {"path": "/etc/passwd", "content": "x"},
            },
            "profile": {"allowed_tools": ["write_file"]},
        },
    )
    assert result["verdict"] == "deny"
