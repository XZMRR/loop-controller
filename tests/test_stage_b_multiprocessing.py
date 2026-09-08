from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop_controller.budget import JsonlBudgetLedger
from loop_controller.infra.approval_store import ApprovalStoreError, JsonlApprovalStore
from loop_controller.infra.authority_store import AuthorityStoreError, JsonlAuthorityStore
from loop_controller.infra.decision_store import JsonlDecisionStore
from loop_controller.infra.reservation_store import JsonlReservationStore, ReservationStoreError
from loop_controller.models import (
    ApprovalRecord,
    ApprovalRequest,
    AuthorityToken,
    BudgetCost,
    BudgetReservation,
    Decision,
)


def _approval_worker(path: str, verdict: str, start, results) -> None:
    store = JsonlApprovalStore(path)
    start.wait()
    try:
        store.record_response(ApprovalRecord(
            request_id="r1", decision_id="d1", verdict=verdict,
            approver_id="approver", comment="competition",
        ))
        results.put(True)
    except ApprovalStoreError:
        results.put(False)


def _decision_worker(path: str, start, results) -> None:
    store = JsonlDecisionStore(path)
    start.wait()
    results.put(store.use_decision("d1", datetime.now(UTC)))


def _budget_worker(path: str, start, results) -> None:
    ledger = JsonlBudgetLedger(path, default_max_budget_token=1)
    start.wait()
    results.put(ledger.check_and_reserve("t1", BudgetCost(token_count=1)))


def _reservation_worker(path: str, state: str, start, results) -> None:
    store = JsonlReservationStore(path)
    current = store.get("r1")
    assert current is not None
    start.wait()
    try:
        store.save(current.model_copy(update={"state": state}))
        results.put(True)
    except ReservationStoreError:
        results.put(False)


def _authority_worker(path: str, start, results) -> None:
    store = JsonlAuthorityStore(path)
    start.wait()
    try:
        updated = store.validate_and_consume(
            "token-1",
            BudgetCost(token_count=1),
            datetime.now(UTC),
            "t1",
            "agent",
        )
        results.put(updated is not None)
    except AuthorityStoreError:
        results.put(False)


def _race(target, path: Path, args: list[tuple]) -> list[bool]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [context.Process(target=target, args=(str(path), *arg, start, results)) for arg in args]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    return [results.get(timeout=1) for _ in processes]


def test_approval_competing_responses_only_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "approvals.jsonl"
    JsonlApprovalStore(path).submit_request(ApprovalRequest(
        request_id="r1", decision_id="d1", call_id="c1", task_id="t1",
        agent_id="agent", tool_name="tool", arguments_masked={}, tool_arguments={},
        reason="test", requester_id="user", approver_id="approver",
    ))
    assert sorted(_race(_approval_worker, path, [("approve",), ("deny",)])) == [False, True]


def test_decision_competing_use_only_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    JsonlDecisionStore(path).record_decision(Decision(
        decision_id="d1", call_id="c1", task_id="t1", verdict="allow", reason="test",
        expires_at=datetime.now(UTC) + timedelta(minutes=1), max_uses=1,
    ))
    assert sorted(_race(_decision_worker, path, [(), ()])) == [False, True]


def test_budget_competing_reserve_does_not_overspend(tmp_path: Path) -> None:
    assert sorted(_race(_budget_worker, tmp_path / "budget.jsonl", [(), ()])) == [False, True]


def test_reservation_competing_terminal_transitions_only_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "reservations.jsonl"
    JsonlReservationStore(path).save(BudgetReservation(
        reservation_id="r1", task_id="t1", call_id="c1", tool_name="tool",
        cost=BudgetCost(token_count=1), state="pending",
    ))
    assert sorted(_race(_reservation_worker, path, [("committed",), ("refunded",)])) == [False, True]


def test_critical_stores_repair_incomplete_tail_before_write(tmp_path: Path) -> None:
    decision_path = tmp_path / "decisions-tail.jsonl"
    decision_path.write_bytes(b'{"broken"')
    JsonlDecisionStore(decision_path).record_proposal("t1", "c1")

    budget_path = tmp_path / "budget-tail.jsonl"
    budget_path.write_bytes(b'{"broken"')
    assert JsonlBudgetLedger(budget_path, default_max_budget_token=1).check_and_reserve(
        "t1", BudgetCost(token_count=1)
    )

    reservation_path = tmp_path / "reservations-tail.jsonl"
    reservation_path.write_bytes(b'{"broken"')
    JsonlReservationStore(reservation_path).save(BudgetReservation(
        reservation_id="r1", task_id="t1", call_id="c1", tool_name="tool",
        cost=BudgetCost(token_count=1), state="pending",
    ))

    authority_path = tmp_path / "authority-tail.jsonl"
    authority_path.write_bytes(b'{"broken"')
    JsonlAuthorityStore(authority_path).save(AuthorityToken(
        token_id="token-1", request_id="r1", agent_id="agent", task_id="t1",
        granted_capabilities=["tool"], budget=BudgetCost(token_count=1),
        remaining_budget=BudgetCost(token_count=1),
        expires_at=datetime.now(UTC) + timedelta(minutes=1), audit_record_id="audit-1",
    ), "token_created")

    for path in (decision_path, budget_path, reservation_path, authority_path):
        assert path.read_bytes().endswith(b"\n")
        assert b'{"broken"' not in path.read_bytes()


def test_authority_competing_consumes_only_once(tmp_path: Path) -> None:
    path = tmp_path / "authority.jsonl"
    JsonlAuthorityStore(path).save(AuthorityToken(
        token_id="token-1", request_id="r1", agent_id="agent", task_id="t1",
        granted_capabilities=["tool"], budget=BudgetCost(token_count=1),
        remaining_budget=BudgetCost(token_count=1),
        expires_at=datetime.now(UTC) + timedelta(minutes=1), audit_record_id="audit-1",
    ), "token_created")
    assert sorted(_race(_authority_worker, path, [(), ()])) == [False, True]
    assert JsonlAuthorityStore(path).get("token-1").remaining_budget.token_count == 0
