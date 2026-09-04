"""SqliteAuthorityStore 持久化与并发语义测试（v0.43.0）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop_controller.infra.sqlite_authority_store import SqliteAuthorityStore
from loop_controller.models import AuthorityToken, BudgetCost


def _token(
    token_id: str = "tok1",
    task_id: str = "t1",
    agent_id: str = "a1",
    capabilities: tuple[str, ...] = ("email_external",),
    budget_token: int = 5,
    remaining_token: int = 5,
    expires_delta: int = 300,
) -> AuthorityToken:
    now = datetime.now(UTC)
    return AuthorityToken(
        token_id=token_id,
        request_id="req-" + token_id,
        agent_id=agent_id,
        task_id=task_id,
        granted_capabilities=list(capabilities),
        budget=BudgetCost(token_count=budget_token),
        remaining_budget=BudgetCost(token_count=remaining_token),
        expires_at=now + timedelta(seconds=expires_delta),
        audit_record_id="audit-" + token_id,
    )


def test_create_and_capability_conflict(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    # 相同 task 下活跃 token 能力交集冲突 -> 拒绝
    conflicting = _token(token_id="tok2", capabilities=("email_external", "network_external"))
    assert store.create_if_capabilities_available(conflicting, now) is False

    # 能力无交集 -> 允许
    disjoint = _token(token_id="tok3", capabilities=("network_external",))
    assert store.create_if_capabilities_available(disjoint, now) is True


def test_consume_and_insufficient(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    consumed = store.validate_and_consume(
        "tok1", BudgetCost(token_count=2), now, "t1", "a1"
    )
    assert consumed is not None
    assert consumed.remaining_budget.token_count == 3

    # 余额不足 -> 返回 None 且不改变状态
    over = store.validate_and_consume(
        "tok1", BudgetCost(token_count=10), now, "t1", "a1"
    )
    assert over is None
    assert store.get("tok1").remaining_budget.token_count == 3


def test_consume_guards_task_and_agent(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    assert (
        store.validate_and_consume("tok1", BudgetCost(token_count=1), now, "other", "a1")
        is None
    )
    assert (
        store.validate_and_consume("tok1", BudgetCost(token_count=1), now, "t1", "other")
        is None
    )


def test_refund_cas(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    consumed = store.validate_and_consume(
        "tok1", BudgetCost(token_count=2), now, "t1", "a1"
    )
    assert consumed is not None

    # 陈旧 token（remaining 仍为 5）不匹配当前状态 -> 拒绝返还
    assert store.refund_if_unchanged(token, BudgetCost(token_count=2)) is None

    # 当前 token（remaining == 3）-> 返还成功回到上限
    refunded = store.refund_if_unchanged(consumed, BudgetCost(token_count=2))
    assert refunded is not None
    assert refunded.remaining_budget.token_count == 5


def test_refund_exceeding_budget(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    # 已在上限，再返还将超过预算上限 -> 拒绝
    assert store.refund_if_unchanged(token, BudgetCost(token_count=1)) is None


def test_revoke_and_expire(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    store.save(token.model_copy(update={"revoked_at": now}), "token_revoked")
    got = store.get("tok1")
    assert got is not None
    assert got.revoked_at is not None
    assert store.list_active() == []


def test_expire_marks_inactive(tmp_path: Path) -> None:
    store = SqliteAuthorityStore.from_path(tmp_path / "authority.db")
    now = datetime.now(UTC)
    token = _token()
    assert store.create_if_capabilities_available(token, now) is True

    store.save(
        token.model_copy(update={"revoked_at": token.expires_at}), "token_expired"
    )
    got = store.get("tok1")
    assert got is not None
    assert got.revoked_at is not None
    assert store.list_active() == []


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    now = datetime.now(UTC)
    first = SqliteAuthorityStore.from_path(path)
    assert first.create_if_capabilities_available(_token(), now) is True

    second = SqliteAuthorityStore.from_path(path)
    got = second.get("tok1")
    assert got is not None
    assert got.granted_capabilities == ["email_external"]
    assert got.remaining_budget.token_count == 5
