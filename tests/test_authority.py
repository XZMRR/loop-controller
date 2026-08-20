"""EarnedAuthorityManager 单元测试（v0.11.0）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from loop_controller.authority import DenyReason, EarnedAuthorityManager
from loop_controller.infra.authority_store import InMemoryAuthorityStore
from loop_controller.models import (
    ActionProposal,
    AuthorityConditions,
    AuthorityEvaluationContext,
    AuthorityGrantRule,
    AuthorityRequest,
    AuthorityRules,
    AuthorityToken,
    BudgetCost,
)


def _manager(rules: AuthorityRules | None = None) -> EarnedAuthorityManager:
    if rules is None:
        rules = AuthorityRules(enabled=False)
    return EarnedAuthorityManager(rules=rules, store=InMemoryAuthorityStore())


def _request(
    capabilities: list[str],
    user_confirmation: bool = True,
    task_id: str = "t1",
) -> AuthorityRequest:
    return AuthorityRequest(
        request_id=uuid.uuid4().hex,
        agent_id="a1",
        task_id=task_id,
        requested_capabilities=capabilities,
        reason="need to send external report",
        user_confirmation=user_confirmation,
    )


def _context(
    budget_remaining: int = 100,
    recent_denial_count: int = 0,
    task_context: str = "send report",
    history: list[ActionProposal] | None = None,
) -> AuthorityEvaluationContext:
    return AuthorityEvaluationContext(
        task_budget_remaining=budget_remaining,
        recent_denial_count=recent_denial_count,
        task_context=task_context,
        history=history or [],
    )


def _email_external_rule() -> AuthorityGrantRule:
    return AuthorityGrantRule(
        capability="email_external",
        description="external email",
        conditions=AuthorityConditions(
            user_confirmation=True,
            budget_remaining=10,
            no_recent_denials_within_steps=5,
        ),
        max_duration_seconds=300,
        budget_limit=BudgetCost(token_count=5),
    )


def test_disabled_returns_deny() -> None:
    manager = _manager(AuthorityRules(enabled=False))
    result = manager.request_authority(
        _request(["email_external"]), _context()
    )
    assert isinstance(result, DenyReason)
    assert "disabled" in result.reason


def test_no_rule_returns_deny() -> None:
    manager = _manager(AuthorityRules(enabled=True))
    result = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(result, DenyReason)
    assert "no grant rule" in result.reason


def test_grant_email_external() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    result = manager.request_authority(
        _request(["email_external"]), _context()
    )
    assert isinstance(result, AuthorityToken)  # type: ignore[has-type]
    assert "email_external" in result.granted_capabilities
    assert result.budget.token_count == 5
    assert result.remaining_budget.token_count == 5
    assert result.expires_at > result.created_at


def test_missing_user_confirmation_denied() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    result = manager.request_authority(
        _request(["email_external"], user_confirmation=False), _context()
    )
    assert isinstance(result, DenyReason)
    assert "user_confirmation" in result.reason


def test_low_budget_denied() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    result = manager.request_authority(
        _request(["email_external"]), _context(budget_remaining=5)
    )
    assert isinstance(result, DenyReason)
    assert "budget" in result.reason


def test_recent_denial_denied() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    history = [ActionProposal(task_id="t1", call_id="c1", agent_id="a1", tool_name="x", arguments={}, task_context="")]
    result = manager.request_authority(
        _request(["email_external"]),
        _context(recent_denial_count=1, history=history),
    )
    assert isinstance(result, DenyReason)
    assert "recent denials" in result.reason


def test_duplicate_grant_denied() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    first = manager.request_authority(
        _request(["email_external"]), _context()
    )
    assert isinstance(first, AuthorityToken)
    second = manager.request_authority(
        _request(["email_external"]), _context()
    )
    assert isinstance(second, DenyReason)
    assert "already granted" in second.reason


def test_validate_for_proposal() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={"to": "x@external.com"},
        task_context="",
        authority_token_ids=[token.token_id],
    )
    valid = manager.validate_for_proposal(proposal, ["email_external"])
    assert len(valid) == 1
    assert valid[0].token_id == token.token_id


def test_validate_wrong_capability() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={},
            task_context="",
            authority_token_ids=[token.token_id],
    )
    valid = manager.validate_for_proposal(proposal, ["network_external"])
    assert valid == []


def test_validate_expired_token() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    past = datetime.now(UTC) - timedelta(seconds=400)
    manager = EarnedAuthorityManager(
        rules=rules,
        store=InMemoryAuthorityStore(),
        now=lambda: past,
    )
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    # 恢复到当前时间
    manager_now = EarnedAuthorityManager(rules=rules, store=manager._store)
    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={},
            task_context="",
            authority_token_ids=[token.token_id],
    )
    valid = manager_now.validate_for_proposal(proposal, ["email_external"])
    assert valid == []


def test_consume_budget() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    updated = manager.consume(token.token_id, BudgetCost(token_count=2))
    assert updated is not None
    assert updated.remaining_budget.token_count == 3

    # 超额消费失败
    over = manager.consume(token.token_id, BudgetCost(token_count=10))
    assert over is None


def test_revoke_token() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    manager = _manager(rules)
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    assert manager.revoke_token(token.token_id, "user cancelled") is True
    assert manager.revoke_token(token.token_id, "again") is False

    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={},
            task_context="",
            authority_token_ids=[token.token_id],
    )
    assert manager.validate_for_proposal(proposal, ["email_external"]) == []


def test_revoke_expired_tokens() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={"email_external": _email_external_rule()},
    )
    past = datetime.now(UTC) - timedelta(seconds=400)
    manager = EarnedAuthorityManager(
        rules=rules,
        store=InMemoryAuthorityStore(),
        now=lambda: past,
    )
    token = manager.request_authority(_request(["email_external"]), _context())
    assert isinstance(token, AuthorityToken)

    manager_now = EarnedAuthorityManager(rules=rules, store=manager._store)
    expired = manager_now.revoke_expired_tokens()
    assert token.token_id in expired


def test_multiple_capabilities_in_one_token() -> None:
    rules = AuthorityRules(
        enabled=True,
        grants={
            "email_external": _email_external_rule(),
            "network_external": AuthorityGrantRule(
                capability="network_external",
                description="external http",
                conditions=AuthorityConditions(user_confirmation=True),
                max_duration_seconds=300,
                budget_limit=BudgetCost(token_count=5),
            ),
        },
    )
    manager = _manager(rules)
    result = manager.request_authority(
        _request(["email_external", "network_external"]), _context()
    )
    assert isinstance(result, AuthorityToken)
    assert set(result.granted_capabilities) == {"email_external", "network_external"}
    assert result.budget.token_count == 10
