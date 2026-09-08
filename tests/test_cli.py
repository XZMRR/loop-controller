"""CLI 单元测试（v0.3.0 Iteration 5）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.cli import main
from loop_controller.infra.approval_store import ApprovalStoreError, JsonlApprovalStore
from loop_controller.models import ApprovalRequest, Decision


@pytest.fixture
def config_dir(tmp_path) -> str:
    """构造一个最小可用配置目录，供 ConfigLoader 加载。"""
    base = tmp_path / "project"
    base.mkdir()
    (base / "agents.yaml").write_text(
        "agents:\n"
        "  - agent_id: researcher_001\n"
        "    name: RA\n"
        "    profile_id: p1\n"
        "    owner_id: zhang_manager\n"
        "users:\n"
        "  - user_id: alice\n"
        "    display_name: Alice\n"
        "  - user_id: zhang_manager\n"
        "    display_name: 张经理\n",
        encoding="utf-8",
    )
    (base / "profiles.yaml").write_text(
        "profiles:\n  - profile_id: p1\n    tools:\n      web_search:\n        allowed: true\n",
        encoding="utf-8",
    )
    (base / "mcp_servers.yaml").write_text(
        "mcp_servers: {}\n"
        "tool_mapping:\n"
        "  web_search:\n"
        "    server: fake\n"
        "    mcp_name: web_search\n"
        "    cost_per_call: 1\n",
        encoding="utf-8",
    )
    (base / "permission_rules.yaml").write_text("permission_rules: []\n", encoding="utf-8")
    (base / "masking_rules.yaml").write_text(
        "field_name_blacklist: []\n"
        "value_patterns: []\n"
        "masking_applies_to:\n"
        "  audit_log: []\n"
        "  approval_request: []\n",
        encoding="utf-8",
    )
    (base / "approval.yaml").write_text(
        "default: zhang_manager\nrules: []\n",
        encoding="utf-8",
    )
    (base / "llm_planner.yaml").write_text("enabled: false\n", encoding="utf-8")
    (base / "harness_tools.yaml").write_text(
        "execution:\n"
        "  default_mode: trusted_local\n"
        "trusted_local_tools:\n"
        "  - web_search\n"
        "  - send_email\n",
        encoding="utf-8",
    )
    return str(base)


def _put_pending_request(config_dir: str, decision_id: str) -> None:
    from loop_controller.infra.config_loader import ConfigLoader

    cfg = ConfigLoader().load(config_dir)
    store = JsonlApprovalStore(cfg.approval_store_path)
    store.submit_request(
        ApprovalRequest(
            request_id="req-1",
            decision_id=decision_id,
            call_id="c1",
            task_id="t1",
            agent_id="researcher_001",
            tool_name="send_email",
            arguments_masked={"to": "zhang@company.com"},
            reason="test",
            requester_id="alice",
            approver_id="zhang_manager",
            created_at=datetime.now(UTC),
        )
    )


def _load_record(config_dir: str, decision_id: str):
    from loop_controller.infra.config_loader import ConfigLoader

    cfg = ConfigLoader().load(config_dir)
    return JsonlApprovalStore(cfg.approval_store_path).get_record(decision_id)


def test_approvals_list_empty(config_dir: str, capsys) -> None:
    rc = main(["--config-dir", config_dir, "approvals", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "没有待审批请求" in captured.out


def test_approvals_list_pending(config_dir: str, capsys) -> None:
    _put_pending_request(config_dir, "d1")
    rc = main(["--config-dir", config_dir, "approvals", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "d1" in captured.out
    assert "send_email" in captured.out


def test_approvals_approve(config_dir: str) -> None:
    _put_pending_request(config_dir, "d1")
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "zhang_manager"]
    )
    assert rc == 0

    record = _load_record(config_dir, "d1")
    assert record is not None
    assert record.verdict == "approve"
    assert record.approver_id == "zhang_manager"


def test_approvals_deny(config_dir: str) -> None:
    _put_pending_request(config_dir, "d1")
    rc = main(
        [
            "--config-dir",
            config_dir,
            "approvals",
            "deny",
            "d1",
            "--approver",
            "zhang_manager",
            "--comment",
            "suspicious",
        ]
    )
    assert rc == 0

    record = _load_record(config_dir, "d1")
    assert record is not None
    assert record.verdict == "deny"
    assert record.comment == "suspicious"


def test_approvals_unknown_decision(config_dir: str, capsys) -> None:
    rc = main(
        [
            "--config-dir",
            config_dir,
            "approvals",
            "approve",
            "no-such",
            "--approver",
            "zhang_manager",
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "未找到" in captured.err


def test_approvals_double_approval(config_dir: str, capsys) -> None:
    _put_pending_request(config_dir, "d1")
    main(["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "zhang_manager"])
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "zhang_manager"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "已有审批结果" in captured.err


def test_approvals_deny_requires_comment(config_dir: str, capsys) -> None:
    """A9：deny 必须提供审批意见。"""
    _put_pending_request(config_dir, "d1")
    rc = main(
        ["--config-dir", config_dir, "approvals", "deny", "d1", "--approver", "zhang_manager"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "deny 必须提供审批意见" in captured.err


def test_approvals_approver_cannot_be_requester(config_dir: str, capsys) -> None:
    """A7：审批人不能是请求者本人。"""
    _put_pending_request(config_dir, "d1")
    rc = main(["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "alice"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "审批人不能是请求者本人" in captured.err


def test_approvals_approver_cannot_be_agent(config_dir: str, capsys) -> None:
    """A8：审批人不能是执行 Agent。"""
    _put_pending_request(config_dir, "d1")
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "researcher_001"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "审批人不能是执行 Agent" in captured.err


def test_approvals_approver_must_exist(config_dir: str, capsys) -> None:
    """审批人必须存在于用户列表。"""
    _put_pending_request(config_dir, "d1")
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "ghost_user"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "审批人 ghost_user 不存在" in captured.err


def test_proxy_rejects_identity_token_flag(config_dir: str) -> None:
    """P1.6：lc proxy 不再接受 --identity-token，避免敏感 token 泄露到进程列表。"""
    with pytest.raises(SystemExit):
        main(
            [
                "--config-dir",
                config_dir,
                "proxy",
                "--agent-id",
                "researcher_001",
                "--user-id",
                "alice",
                "--identity-token",
                "secret",
            ]
        )


def _put_expired_request(config_dir: str, decision_id: str) -> None:
    """构造一个 original_decision 已过期的待审批请求。"""
    from loop_controller.infra.config_loader import ConfigLoader

    cfg = ConfigLoader().load(config_dir)
    store = JsonlApprovalStore(cfg.approval_store_path)
    expired_decision = Decision(
        decision_id=decision_id,
        call_id="c1",
        task_id="t1",
        verdict="require_approval",
        reason="test",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    store.submit_request(
        ApprovalRequest(
            request_id="req-1",
            decision_id=decision_id,
            call_id="c1",
            task_id="t1",
            agent_id="researcher_001",
            tool_name="send_email",
            arguments_masked={"to": "zhang@company.com"},
            reason="test",
            requester_id="alice",
            approver_id="zhang_manager",
            original_decision=expired_decision,
            created_at=datetime.now(UTC),
        )
    )


def test_approve_rejects_expired_decision(config_dir: str, capsys) -> None:
    """A10：approve 时若 Decision 已过期，应拒绝审批。"""
    _put_expired_request(config_dir, "d1")
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "zhang_manager"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "过期" in captured.err


def test_list_marks_expired(config_dir: str, capsys) -> None:
    """list 应把 original_decision 已过期的请求标记为 [expired]。"""
    _put_expired_request(config_dir, "d1")
    rc = main(["--config-dir", config_dir, "approvals", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "d1" in captured.out
    assert "[expired]" in captured.out


def test_approve_store_error_friendly_message(config_dir: str, capsys, monkeypatch) -> None:
    """ApprovalStore 写失败时 CLI 返回非零并输出友好错误，无 traceback。"""
    _put_pending_request(config_dir, "d1")

    def _raise_store_error(self, record) -> None:
        raise ApprovalStoreError("simulated write failure")

    monkeypatch.setattr(JsonlApprovalStore, "record_response", _raise_store_error)
    rc = main(
        ["--config-dir", config_dir, "approvals", "approve", "d1", "--approver", "zhang_manager"]
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert "审批结果写入失败" in captured.err
    assert "simulated write failure" in captured.err
    assert "Traceback" not in captured.err
