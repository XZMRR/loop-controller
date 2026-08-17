"""policy_engine 单元测试（§6.3 每条规则一个用例 + fail-closed）.

Rego 策略用例（本文件）与 Python 判定流水线用例（test_checkpoint.py）分开：
前者测策略逻辑，后者测流水线组装。
"""

from __future__ import annotations

import pytest

from loop_controller.models import ActionProposal, Agent, CapabilityProfile, ToolPermission
from loop_controller.policy_engine import OPAPolicyEngine, build_policy_input

PACKAGE = "loop_controller.tool_permission"


@pytest.fixture
def agent() -> Agent:
    return Agent(agent_id="researcher_001", name="RA", profile_id="p1", owner_id="zhang_manager")


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(
                tool_name="read_file", allowed=True,
                allowed_args={"path": ["/data/kb/**"]},
            ),
            "write_file": ToolPermission(
                tool_name="write_file", allowed=True,
                allowed_args={"path": ["/data/output/**"]},
            ),
            "send_email": ToolPermission(
                tool_name="send_email", allowed=True, require_approval=True,
                allowed_args={"to": ["*@company.com"]},
            ),
        },
    )


def _proposal(tool_name: str, arguments: dict, risk_level: str = "low") -> ActionProposal:
    return ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="researcher_001",
        tool_name=tool_name,
        arguments=arguments,
        task_context="ctx",
        risk_level=risk_level,
    )


def _make_engine(opa_server: str) -> OPAPolicyEngine:
    return OPAPolicyEngine(base_url=opa_server)


async def _evaluate(engine: OPAPolicyEngine, proposal: ActionProposal, agent: Agent, profile: CapabilityProfile) -> dict:
    return await engine.evaluate(PACKAGE, build_policy_input(proposal, agent, profile))


async def test_web_search_allow(opa_server, agent, profile):
    decision = await _evaluate(_make_engine(opa_server), _proposal("web_search", {"query": "q"}), agent, profile)
    assert decision["verdict"] == "allow"
    assert decision["policy_hits"] == ["web_search_allow"]


async def test_read_file_within_dir_allow(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("read_file", {"path": "/data/kb/ai_compliance_checklist.md"}),
        agent, profile,
    )
    assert decision["verdict"] == "allow"
    assert decision["policy_hits"] == ["read_file_allow"]


async def test_read_file_outside_dir_deny(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("read_file", {"path": "/etc/passwd"}),
        agent, profile,
    )
    assert decision["verdict"] == "deny"
    assert decision["policy_hits"] == ["default_deny"]


async def test_write_file_within_allow(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("write_file", {"path": "/data/output/summary.md", "content": "x"}),
        agent, profile,
    )
    assert decision["verdict"] == "allow"


async def test_write_file_outside_deny(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("write_file", {"path": "/tmp/evil.md", "content": "x"}),
        agent, profile,
    )
    assert decision["verdict"] == "deny"


async def test_send_email_whitelist_requires_approval(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("send_email", {"to": "zhang@company.com", "subject": "s"}),
        agent, profile,
    )
    assert decision["verdict"] == "require_approval"
    assert decision["policy_hits"] == ["send_email_approval"]
    assert decision["escalation_target"] == "zhang_manager"


async def test_send_email_whitelist_no_approval_allowed(opa_server, agent, profile):
    no_approval = profile.model_copy(
        update={"tools": {
            **profile.tools,
            "send_email": profile.tools["send_email"].model_copy(update={"require_approval": False}),
        }}
    )
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("send_email", {"to": "zhang@company.com", "subject": "s"}),
        agent, no_approval,
    )
    assert decision["verdict"] == "allow"
    assert decision["policy_hits"] == ["send_email_allow"]


async def test_send_email_external_deny(opa_server, agent, profile):
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("send_email", {"to": "external@gmail.com", "subject": "s"}),
        agent, profile,
    )
    assert decision["verdict"] == "deny"
    assert decision["policy_hits"] == ["send_email_deny_external"]


async def test_critical_signal_gate(opa_server, agent, profile):
    # critical 风险信号必须人工审批，即使 web_search 常规可放行
    decision = await _evaluate(
        _make_engine(opa_server),
        _proposal("web_search", {"query": "q"}, risk_level="critical"),
        agent, profile,
    )
    assert decision["verdict"] == "require_approval"
    assert decision["policy_hits"] == ["critical_signal_gate"]


async def test_opa_down_fail_closed(agent, profile):
    engine = OPAPolicyEngine(base_url="http://127.0.0.1:1")  # 必然连接失败
    decision = await _evaluate(engine, _proposal("web_search", {"query": "q"}), agent, profile)
    assert decision["verdict"] == "deny"
    assert decision["policy_hits"] == ["fail_closed"]
