from __future__ import annotations

import asyncio
import base64
import multiprocessing
from datetime import UTC, datetime
from pathlib import Path

from loop_controller.audit.anchors import AnchorPayload, AnchorReceipt
from loop_controller.audit.evidence import EvidenceChain, HMACEvidenceSigner
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.authority import EarnedAuthorityManager
from loop_controller.identity.revocation import RevocationEntry, RevocationList
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.authority_store import JsonlAuthorityStore
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.models import (
    AuditEvent,
    AuthorityConditions,
    AuthorityEvaluationContext,
    AuthorityGrantRule,
    AuthorityRequest,
    AuthorityRules,
    AuthorityToken,
    BudgetCost,
    ConversationMessage,
)
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager
from loop_controller.session import JsonlSessionBackend, SessionManager


def _append_evidence(path: str, number: int) -> None:
    event = AuditEvent(
        event_id=f"event-{number}",
        trace_id=f"trace-{number}",
        session_id="session",
        actor_type="agent",
        actor_id="agent",
        action="propose",
        target="tool",
    )
    chain = EvidenceChain(
        LocalFileEvidenceBackend(Path(path)),
        HMACEvidenceSigner(b"multiprocess-key", key_id="test"),
    )
    asyncio.run(chain.append(event))


class _ValidReceiptVerifier:
    def verify(self, receipt: AnchorReceipt) -> bool:
        return True


class _BlockingEmptyAnchorBackend:
    def __init__(self, append_started, release) -> None:
        self._append_started = append_started
        self._release = release

    def latest(self, stream_id: str) -> AnchorReceipt | None:
        self._append_started.wait(10)
        self._release.wait(10)
        return None

    def publish(self, payload: AnchorPayload, *, idempotency_key: str) -> AnchorReceipt:
        return AnchorReceipt(
            receipt_id=f"receipt-{payload.audit_seq}",
            payload=payload,
            anchored_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            service_key_id="test",
            algorithm="ed25519",
            signature=base64.b64encode(bytes(64)).decode("ascii"),
        )

    def close(self) -> None:
        pass


def _audit_store(path: str, anchor_backend=None) -> JsonlAuditStore:
    root = Path(path)
    chain = EvidenceChain(
        LocalFileEvidenceBackend(root / "evidence"),
        HMACEvidenceSigner(b"multiprocess-key", key_id="test"),
        checkpoint_path=root / "checkpoint.json",
    )
    return JsonlAuditStore(
        root / "audit.jsonl",
        evidence_chain=chain,
        anchor_backend=anchor_backend,
        anchor_stream_id="stream" if anchor_backend is not None else None,
        anchor_receipt_verifier=_ValidReceiptVerifier() if anchor_backend is not None else None,
    )


def _bootstrap_anchor(path: str, append_started, release, results) -> None:
    backend = _BlockingEmptyAnchorBackend(append_started, release)
    store = _audit_store(path, backend)
    event = AuditEvent(
        event_id="bootstrap",
        trace_id="bootstrap",
        session_id="admin",
        actor_type="user",
        actor_id="admin",
        action="anchor_bootstrap",
        target="anchor",
    )
    results.put(asyncio.run(store.bootstrap_anchor(event))["anchor_status"])


def _append_audit(path: str, append_started, results) -> None:
    append_started.set()
    store = _audit_store(path)
    store.append(AuditEvent(
        event_id="concurrent",
        trace_id="concurrent",
        session_id="session",
        actor_type="agent",
        actor_id="agent",
        action="execute",
        target="tool",
    ))
    results.put("appended")


def _risk_worker(path: str, start) -> None:
    manager = RiskStateManager(JsonlRiskStateStore(path))
    start.wait()
    manager.update("session", "deny")


def _session_worker(path: str, session_id: str, operation: str, start, results) -> None:
    manager = SessionManager(backend=JsonlSessionBackend(path))
    start.wait()
    try:
        if operation == "close":
            manager.close_session(session_id)
        else:
            manager.touch_session(session_id)
        results.put(True)
    except ValueError:
        results.put(False)


def _resident_worker(kind: str, path: str, ready, command, results) -> None:
    if kind == "revocation":
        resource = RevocationList.from_file(path)
    elif kind == "risk":
        resource = RiskStateManager(JsonlRiskStateStore(path))
    elif kind == "session":
        resource = SessionManager(backend=JsonlSessionBackend(path))
    else:
        resource = JsonlConversationStore(path)
    ready.set()
    while True:
        action, value = command.get()
        if action == "stop":
            return
        if kind == "revocation":
            resource.add(RevocationEntry(type="tool", id=value))
            results.put(True)
        elif kind == "risk":
            if action == "write":
                resource.update("session", "deny")
                results.put(True)
            else:
                results.put(resource.get_profile("session").denied_count)
        elif kind == "session":
            if action == "create":
                results.put(resource.get_or_create_session("user", "agent").session_id)
            else:
                session = resource.get_session(value)
                results.put(session.session_id if session is not None else None)
        elif action == "write":
            resource.append_message(ConversationMessage(
                message_id=value, session_id="session", task_id="task", role="user", content=value
            ))
            results.put(True)
        else:
            results.put([message.message_id for message in resource.get_context("session").messages])


def _authority_rules() -> AuthorityRules:
    return AuthorityRules(
        enabled=True,
        grants={
            "email_external": AuthorityGrantRule(
                capability="email_external",
                description="test",
                conditions=AuthorityConditions(),
                max_duration_seconds=300,
                budget_limit=BudgetCost(token_count=1),
            )
        },
    )


def _authority_grant_worker(path: str, start, results) -> None:
    manager = EarnedAuthorityManager(_authority_rules(), JsonlAuthorityStore(path))
    start.wait()
    result = manager.request_authority(
        AuthorityRequest(
            request_id=multiprocessing.current_process().name,
            agent_id="agent",
            task_id="task",
            requested_capabilities=["email_external"],
            reason="test",
        ),
        AuthorityEvaluationContext(
            task_budget_remaining=10,
            recent_denial_count=0,
            task_context="test",
        ),
    )
    results.put(isinstance(result, AuthorityToken))


def test_evidence_multiprocess_append_has_single_chain(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_evidence, args=(str(tmp_path / "evidence"), number))
        for number in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    async def verify() -> tuple[bool, list[int]]:
        backend = LocalFileEvidenceBackend(tmp_path / "evidence")
        chain = EvidenceChain(backend, HMACEvidenceSigner(b"multiprocess-key", key_id="test"))
        records = [record async for record in backend.iter_evidence(None)]
        return await chain.verify(), [record.seq for record in records]

    valid, sequences = asyncio.run(verify())
    assert valid
    assert sequences == list(range(1, 9))


def test_anchor_bootstrap_holds_audit_transaction_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "anchor"
    initial = _audit_store(str(root))
    initial.append(AuditEvent(
        event_id="initial",
        trace_id="initial",
        session_id="session",
        actor_type="agent",
        actor_id="agent",
        action="execute",
        target="tool",
    ))
    context = multiprocessing.get_context("spawn")
    append_started = context.Event()
    release = context.Event()
    results = context.Queue()
    bootstrap = context.Process(
        target=_bootstrap_anchor,
        args=(str(root), append_started, release, results),
    )
    append = context.Process(target=_append_audit, args=(str(root), append_started, results))
    bootstrap.start()
    append.start()
    assert append_started.wait(10)
    release.set()
    bootstrap.join(20)
    append.join(20)
    assert bootstrap.exitcode == 0
    assert append.exitcode == 0
    assert {results.get(timeout=1), results.get(timeout=1)} == {"healthy", "appended"}
    final = _audit_store(str(root))
    assert final.verify_chain()
    assert [event.seq for event in final._audit_events()] == [1, 2, 3]


def test_risk_state_multiprocess_updates_refresh_before_append(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    path = tmp_path / "risk.jsonl"
    processes = [context.Process(target=_risk_worker, args=(str(path), start)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    profile = RiskStateManager(JsonlRiskStateStore(path)).get_profile("session")
    assert profile.denied_count == 2
    assert profile.cumulative_risk_score == 0.38


def test_session_close_is_terminal_against_multiprocess_touch(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    manager = SessionManager(backend=JsonlSessionBackend(path))
    session = manager.get_or_create_session("user", "agent")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_session_worker,
            args=(str(path), session.session_id, operation, start, results),
        )
        for operation in ("close", "touch")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert results.get(timeout=1) in {True, False}
    assert results.get(timeout=1) in {True, False}
    reopened = JsonlSessionBackend(path).get_by_id(session.session_id)
    assert reopened is not None
    assert reopened.active is False


def _start_resident_workers(kind: str, path: Path, count: int = 2):
    context = multiprocessing.get_context("spawn")
    workers = []
    channels = []
    for _ in range(count):
        ready = context.Event()
        command = context.Queue()
        results = context.Queue()
        process = context.Process(
            target=_resident_worker, args=(kind, str(path), ready, command, results)
        )
        process.start()
        assert ready.wait(10)
        workers.append(process)
        channels.append((command, results))
    return workers, channels


def _stop_workers(workers, channels) -> None:
    for command, _ in channels:
        command.put(("stop", None))
    for process in workers:
        process.join(10)
        assert process.exitcode == 0


def test_revocation_resident_workers_merge_locked_replacements(tmp_path: Path) -> None:
    path = tmp_path / "revocation.yaml"
    RevocationList(path=path).set_kill_switch(RevocationList().kill_switch)
    workers, channels = _start_resident_workers("revocation", path)
    try:
        for index, (command, _) in enumerate(channels):
            command.put(("write", f"tool-{index}"))
        assert all(results.get(timeout=10) for _, results in channels)
    finally:
        _stop_workers(workers, channels)
    assert {entry.id for entry in RevocationList.from_file(path).entries} == {"tool-0", "tool-1"}


def test_risk_resident_worker_get_profile_refreshes(tmp_path: Path) -> None:
    path = tmp_path / "risk.jsonl"
    workers, channels = _start_resident_workers("risk", path)
    try:
        channels[0][0].put(("write", None))
        assert channels[0][1].get(timeout=10) is True
        channels[1][0].put(("read", None))
        assert channels[1][1].get(timeout=10) == 1
    finally:
        _stop_workers(workers, channels)


def test_session_resident_workers_get_or_create_one_active_and_refresh_query(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.jsonl"
    workers, channels = _start_resident_workers("session", path)
    try:
        for command, _ in channels:
            command.put(("create", None))
        session_ids = [results.get(timeout=10) for _, results in channels]
        assert len(set(session_ids)) == 1
        channels[0][0].put(("read", session_ids[0]))
        assert channels[0][1].get(timeout=10) == session_ids[0]
    finally:
        _stop_workers(workers, channels)


def test_conversation_resident_worker_get_context_refreshes(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    workers, channels = _start_resident_workers("conversation", path)
    try:
        channels[0][0].put(("write", "message-1"))
        assert channels[0][1].get(timeout=10) is True
        channels[1][0].put(("read", None))
        assert channels[1][1].get(timeout=10) == ["message-1"]
    finally:
        _stop_workers(workers, channels)


def test_authority_duplicate_check_and_issue_share_multiprocess_transaction(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    path = tmp_path / "authority.jsonl"
    processes = [
        context.Process(target=_authority_grant_worker, args=(str(path), start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=1) for _ in processes) == [False, True]
    assert len(JsonlAuthorityStore(path).list_by_task("task")) == 1
