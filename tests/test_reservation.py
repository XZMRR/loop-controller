"""BudgetReservation 状态机测试（v0.6.1 / v0.8.0）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.infra.reservation_store import (
    InMemoryReservationStore,
    JsonlReservationStore,
    ReservationStoreError,
)
from loop_controller.models import (
    ActionProposal,
    Agent,
    BudgetCost,
    BudgetReservation,
    CapabilityProfile,
    ToolPermission,
)
from tests.test_checkpoint import make_checkpoint


def _make_task():
    from loop_controller.models import Task

    return Task(
        task_id="task-1",
        session_id="session-1",
        user_id="alice",
        agent_id="agent-1",
        description="test",
    )


def _make_agent():
    return Agent(agent_id="agent-1", name="A", profile_id="p1", owner_id="owner")


def _make_identity():
    from loop_controller.infra.identity import ConfigIdentityProvider

    return ConfigIdentityProvider(
        agents={"agent-1": _make_agent()},
        users={"alice": "Alice"},
    )


def _make_profile():
    return CapabilityProfile(
        profile_id="p1",
        version="v1",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
        },
    )


def _make_proposal(task, tool_name="web_search", arguments=None):
    return ActionProposal(
        task_id=task.task_id,
        call_id="call-1",
        agent_id=task.agent_id,
        tool_name=tool_name,
        arguments=arguments or {"query": "x"},
        task_context="",
    )


def test_create_reservation() -> None:
    """evaluate allow 路径创建 pending reservation。"""
    cp, _, _ = make_checkpoint(_make_profile(), _make_identity())
    task = _make_task()
    proposal = _make_proposal(task)

    reservations = cp.get_pending_reservations(task.task_id)
    assert len(reservations) == 0

    # 手动预留并创建 reservation
    cp.reserve_for_execution(task.task_id, proposal)

    reservations = cp.get_pending_reservations(task.task_id)
    assert len(reservations) == 1
    assert reservations[0].state == "pending"
    assert reservations[0].call_id == proposal.call_id


def test_pending_to_refunded_on_deny() -> None:
    """deny 路径 reservation 转为 refunded。"""
    cp, _, _ = make_checkpoint(_make_profile(), _make_identity())
    task = _make_task()
    proposal = _make_proposal(task)

    reservation = cp._create_reservation(proposal, datetime.now(UTC))
    cp._save_reservation(reservation)
    assert reservation.state == "pending"

    refunded = cp._refund_reservation(reservation)
    assert refunded.state == "refunded"
    assert cp.get_pending_reservation(proposal.call_id) is None


def test_pending_to_pending_approval() -> None:
    """require_approval 路径 reservation 保持预算并转为 pending_approval。"""
    cp, _, _ = make_checkpoint(_make_profile(), _make_identity())
    task = _make_task()
    proposal = _make_proposal(task)

    reservation = cp._create_reservation(proposal, datetime.now(UTC))
    cp._save_reservation(reservation)

    now = datetime.now(UTC)
    pending = cp._to_pending_approval(reservation, now)
    assert pending.state == "pending_approval"
    assert pending.expires_at == now + timedelta(minutes=15)
    assert cp.get_pending_reservation(proposal.call_id) is not None


def test_commit_reservation() -> None:
    """forward success 后 reservation 转为 committed。"""
    cp, _, _ = make_checkpoint(_make_profile(), _make_identity())
    task = _make_task()
    proposal = _make_proposal(task)

    reservation = cp._create_reservation(proposal, datetime.now(UTC))
    cp._save_reservation(reservation)

    committed = cp._commit_reservation(reservation)
    assert committed.state == "committed"
    assert cp.get_pending_reservation(proposal.call_id) is None


def test_store_update_overwrites() -> None:
    """同一 reservation_id 多次 save 返回最新状态。"""
    store = InMemoryReservationStore()
    r = BudgetReservation(
        reservation_id="r1",
        task_id="t1",
        call_id="c1",
        tool_name="web_search",
        cost=BudgetCost(token_count=1),
        state="pending",
    )
    store.save(r)
    r2 = r.model_copy(update={"state": "committed"})
    store.save(r2)

    got = store.get("r1")
    assert got is not None
    assert got.state == "committed"

    by_call = store.get_by_call_id("c1")
    assert by_call is not None
    assert by_call.state == "committed"


def test_list_by_task_filters() -> None:
    """list_by_task 返回同一 task 下所有 reservation。"""
    store = InMemoryReservationStore()
    for i in range(3):
        store.save(
            BudgetReservation(
                reservation_id=f"r{i}",
                task_id="t1",
                call_id=f"c{i}",
                tool_name="web_search",
                cost=BudgetCost(token_count=1),
                state="pending",
            )
        )
    store.save(
        BudgetReservation(
            reservation_id="r-other",
            task_id="t2",
            call_id="c-other",
            tool_name="web_search",
            cost=BudgetCost(token_count=1),
            state="pending",
        )
    )
    assert len(store.list_by_task("t1")) == 3
    assert len(store.list_by_task("t2")) == 1


def test_get_pending_reservation_only_active() -> None:
    """get_pending_reservation 只返回 pending / pending_approval 状态。"""
    cp, _, _ = make_checkpoint(_make_profile(), _make_identity())
    task = _make_task()
    proposal = _make_proposal(task)

    reservation = cp._create_reservation(proposal, datetime.now(UTC))
    cp._save_reservation(reservation)
    assert cp.get_pending_reservation(proposal.call_id) is not None

    refunded = cp._refund_reservation(reservation)
    assert refunded.state == "refunded"
    assert cp.get_pending_reservation(proposal.call_id) is None


# v0.8.0：JsonlReservationStore 持久化测试


def _make_reservation(reservation_id="r1", task_id="t1", call_id="c1", state="pending"):
    return BudgetReservation(
        reservation_id=reservation_id,
        task_id=task_id,
        call_id=call_id,
        tool_name="web_search",
        cost=BudgetCost(token_count=10),
        state=state,
    )


def test_jsonl_store_save_and_get(tmp_path) -> None:
    """JsonlReservationStore 保存后按 id / call_id 读取。"""
    store = JsonlReservationStore(tmp_path / "reservations.jsonl")
    r = _make_reservation()
    store.save(r)

    got = store.get("r1")
    assert got is not None
    assert got.state == "pending"

    by_call = store.get_by_call_id("c1")
    assert by_call is not None
    assert by_call.reservation_id == "r1"


def test_jsonl_store_transition_overwrite(tmp_path) -> None:
    """多次 save 返回最新状态。"""
    store = JsonlReservationStore(tmp_path / "reservations.jsonl")
    r = _make_reservation()
    store.save(r)
    r2 = r.model_copy(update={"state": "pending_approval", "expires_at": datetime.now(UTC) + timedelta(minutes=15)})
    store.save(r2)

    got = store.get("r1")
    assert got is not None
    assert got.state == "pending_approval"
    assert got.expires_at is not None


def test_jsonl_store_list_by_task(tmp_path) -> None:
    """按 task_id 过滤 reservation。"""
    store = JsonlReservationStore(tmp_path / "reservations.jsonl")
    store.save(_make_reservation(reservation_id="r1", task_id="t1", call_id="c1"))
    store.save(_make_reservation(reservation_id="r2", task_id="t1", call_id="c2"))
    store.save(_make_reservation(reservation_id="r3", task_id="t2", call_id="c3"))

    assert len(store.list_by_task("t1")) == 2
    assert len(store.list_by_task("t2")) == 1


def test_jsonl_store_persistence_across_reconstruction(tmp_path) -> None:
    """重建 store 对象后状态恢复。"""
    path = tmp_path / "reservations.jsonl"
    store1 = JsonlReservationStore(path)
    r = _make_reservation()
    store1.save(r)
    r2 = r.model_copy(update={"state": "committed"})
    store1.save(r2)

    store2 = JsonlReservationStore(path)
    got = store2.get("r1")
    assert got is not None
    assert got.state == "committed"
    assert store2.get_by_call_id("c1") is not None
    assert len(store2.list_by_task("t1")) == 1


def test_jsonl_store_corrupted_file_fail_closed(tmp_path) -> None:
    """损坏文件 fail-closed。"""
    path = tmp_path / "reservations.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ReservationStoreError):  # noqa: PT012
        JsonlReservationStore(path)


def test_jsonl_store_datetime_roundtrip(tmp_path) -> None:
    """datetime 字段正确序列化/反序列化。"""
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    expires = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)
    r = BudgetReservation(
        reservation_id="r1",
        task_id="t1",
        call_id="c1",
        tool_name="web_search",
        cost=BudgetCost(token_count=10),
        state="pending",
        created_at=created,
        expires_at=expires,
    )
    store = JsonlReservationStore(tmp_path / "reservations.jsonl")
    store.save(r)

    got = store.get("r1")
    assert got is not None
    assert got.created_at == created
    assert got.expires_at == expires
