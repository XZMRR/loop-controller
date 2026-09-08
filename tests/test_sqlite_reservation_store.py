"""SqliteReservationStore 持久化与终态守卫测试（v0.43.0）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop_controller.infra.reservation_store import ReservationStoreError
from loop_controller.infra.sqlite_reservation_store import SqliteReservationStore
from loop_controller.models import BudgetCost, BudgetReservation


def _make_reservation(
    reservation_id="r1", task_id="t1", call_id="c1", state="pending"
) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=reservation_id,
        task_id=task_id,
        call_id=call_id,
        tool_name="web_search",
        cost=BudgetCost(token_count=10),
        state=state,
    )


def test_save_and_get(tmp_path: Path) -> None:
    store = SqliteReservationStore.from_path(tmp_path / "reservations.db")
    store.save(_make_reservation())

    got = store.get("r1")
    assert got is not None
    assert got.state == "pending"

    by_call = store.get_by_call_id("c1")
    assert by_call is not None
    assert by_call.reservation_id == "r1"


def test_transition_overwrite(tmp_path: Path) -> None:
    store = SqliteReservationStore.from_path(tmp_path / "reservations.db")
    r = _make_reservation()
    store.save(r)
    r2 = r.model_copy(
        update={
            "state": "pending_approval",
            "expires_at": datetime.now(UTC) + timedelta(minutes=15),
        }
    )
    store.save(r2)

    got = store.get("r1")
    assert got is not None
    assert got.state == "pending_approval"
    assert got.expires_at is not None


def test_list_by_task_and_all(tmp_path: Path) -> None:
    store = SqliteReservationStore.from_path(tmp_path / "reservations.db")
    store.save(_make_reservation(reservation_id="r1", task_id="t1", call_id="c1"))
    store.save(_make_reservation(reservation_id="r2", task_id="t1", call_id="c2"))
    store.save(_make_reservation(reservation_id="r3", task_id="t2", call_id="c3"))

    assert len(store.list_by_task("t1")) == 2
    assert len(store.list_by_task("t2")) == 1
    assert len(store.list_all()) == 3


def test_terminal_state_guard(tmp_path: Path) -> None:
    """终态后拒绝被不同对象覆盖。"""
    store = SqliteReservationStore.from_path(tmp_path / "reservations.db")
    r = _make_reservation()
    store.save(r)
    committed = r.model_copy(update={"state": "committed"})
    store.save(committed)

    with pytest.raises(ReservationStoreError):
        store.save(r.model_copy(update={"state": "refunded"}))


def test_terminal_state_idempotent(tmp_path: Path) -> None:
    """保存同一终态对象幂等返回。"""
    store = SqliteReservationStore.from_path(tmp_path / "reservations.db")
    r = _make_reservation()
    store.save(r)
    committed = r.model_copy(update={"state": "committed"})
    store.save(committed)
    store.save(committed)  # 幂等

    got = store.get("r1")
    assert got is not None
    assert got.state == "committed"


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "reservations.db"
    first = SqliteReservationStore.from_path(path)
    r = _make_reservation()
    first.save(r)
    first.save(r.model_copy(update={"state": "committed"}))

    second = SqliteReservationStore.from_path(path)
    got = second.get("r1")
    assert got is not None
    assert got.state == "committed"
    assert second.get_by_call_id("c1") is not None
    assert len(second.list_by_task("t1")) == 1
