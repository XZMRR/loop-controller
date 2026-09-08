"""适配器共享辅助测试（v0.15.0）。"""

from __future__ import annotations

import pytest

from loop_controller.models import GovernanceResult

from .._shared import format_governance_result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            GovernanceResult(
                status="allow",
                call_id="c1",
                tool_name="web_search",
                arguments={"query": "AI"},
                content="ok",
            ),
            "ok",
        ),
        (
            GovernanceResult(
                status="allow",
                call_id="c1",
                tool_name="web_search",
                arguments={},
            ),
            "",
        ),
        (
            GovernanceResult(
                status="deny",
                call_id="c1",
                tool_name="send_email",
                arguments={},
                reason="not permitted",
            ),
            "[denied] not permitted",
        ),
        (
            GovernanceResult(
                status="require_approval",
                call_id="c1",
                tool_name="send_email",
                arguments={},
                request_id="req-1",
            ),
            "[requires approval] request_id=req-1. Approve via 'lc approvals approve req-1', then retry.",
        ),
        (
            GovernanceResult(
                status="error",
                call_id="c1",
                tool_name="send_email",
                arguments={},
                reason="boom",
                error_code="gateway_error",
            ),
            "[error] gateway_error: boom",
        ),
        (
            GovernanceResult(
                status="blocked",
                call_id="c1",
                tool_name="send_email",
                arguments={},
                reason="expired",
                error_code="decision_expired",
            ),
            "[blocked] decision_expired: expired",
        ),
    ],
)
def test_format_governance_result(result: GovernanceResult, expected: str) -> None:
    assert format_governance_result(result) == expected
