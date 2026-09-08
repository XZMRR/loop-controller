"""ApprovalStore 敏感载荷 AES-256-GCM 加密测试（FZ-05）。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from loop_controller.infra.approval_crypto import ApprovalCrypto, ApprovalCryptoError
from loop_controller.infra.approval_store import (
    ApprovalStoreCorruptedError,
    ApprovalStoreError,
    JsonlApprovalStore,
    migrate_approval_store,
)
from loop_controller.models import (
    ApprovalRequest,
    Decision,
)


@pytest.fixture
def crypto(tmp_path: Path) -> ApprovalCrypto:
    key = os.urandom(32)
    return ApprovalCrypto(key)


@pytest.fixture
def decision() -> Decision:
    return Decision(
        decision_id=uuid4().hex,
        call_id=uuid4().hex,
        task_id=uuid4().hex,
        verdict="require_approval",
        reason="high risk",
        policy_hits=["unique-policy-hit-12345"],
        policy_version="v1",
        profile_version="v1",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


@pytest.fixture
def request_obj(decision: Decision) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=uuid4().hex,
        decision_id=decision.decision_id,
        call_id=decision.call_id,
        task_id=decision.task_id,
        agent_id="agent-1",
        tool_name="read_file",
        arguments_masked={"path": "/data/kb/doc.md"},
        tool_arguments={"path": "/data/kb/doc.md", "secret_token": "s3cr3t-t0k3n-xyz"},
        original_decision=decision,
        reason="high risk",
        requester_id="user-1",
        approver_id="approver-1",
    )


def _plain_file_contains_sensitive(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return needle in text


def test_encrypted_store_disk_no_plaintext(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    assert path.exists()
    assert not _plain_file_contains_sensitive(path, "s3cr3t-t0k3n-xyz")
    assert not _plain_file_contains_sensitive(path, "unique-policy-hit-12345")


def test_encrypted_store_roundtrip(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    fresh = JsonlApprovalStore(path, crypto=crypto)
    loaded = fresh.get_request(request_obj.decision_id)
    assert loaded is not None
    assert loaded.tool_arguments == request_obj.tool_arguments
    assert loaded.original_decision == request_obj.original_decision


def test_encrypted_store_missing_key_fail_closed(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    other_key = ApprovalCrypto(os.urandom(32))
    with pytest.raises(ApprovalStoreError):
        JsonlApprovalStore(path, crypto=other_key)


def test_encrypted_store_no_crypto_fail_closed(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    with pytest.raises(ApprovalStoreCorruptedError):
        JsonlApprovalStore(path)


def test_tampered_ciphertext_fail_closed(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    raw = json.loads(path.read_text(encoding="utf-8").strip())
    raw["encrypted_payload"]["ciphertext"] = "dGFtcGVyZWQ="
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ApprovalStoreCorruptedError):
        JsonlApprovalStore(path, crypto=crypto)


def test_tampered_aad_fail_closed(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path, crypto=crypto)
    store.submit_request(request_obj)

    raw = json.loads(path.read_text(encoding="utf-8").strip())
    raw["call_id"] = uuid4().hex
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ApprovalStoreCorruptedError):
        JsonlApprovalStore(path, crypto=crypto)


def test_migrate_plaintext_to_encrypted(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    plain_store = JsonlApprovalStore(path)
    plain_store.submit_request(request_obj)

    assert _plain_file_contains_sensitive(path, "s3cr3t-t0k3n-xyz")

    migrate_approval_store(path, crypto)

    backup = path.with_suffix(path.suffix + ".plaintext-backup")
    assert backup.exists()
    assert not _plain_file_contains_sensitive(path, "s3cr3t-t0k3n-xyz")

    encrypted_store = JsonlApprovalStore(path, crypto=crypto)
    loaded = encrypted_store.get_request(request_obj.decision_id)
    assert loaded is not None
    assert loaded.tool_arguments == request_obj.tool_arguments
    assert loaded.original_decision == request_obj.original_decision


def test_migrate_idempotent(
    tmp_path: Path, crypto: ApprovalCrypto, request_obj: ApprovalRequest
) -> None:
    path = tmp_path / "approvals.jsonl"
    plain_store = JsonlApprovalStore(path)
    plain_store.submit_request(request_obj)

    migrate_approval_store(path, crypto)
    migrate_approval_store(path, crypto)

    # 每次加密 nonce 不同，ciphertext 会变，但语义保持一致即可
    encrypted_store = JsonlApprovalStore(path, crypto=crypto)
    loaded = encrypted_store.get_request(request_obj.decision_id)
    assert loaded is not None
    assert loaded.tool_arguments == request_obj.tool_arguments


def test_env_key_resolution_valid() -> None:
    key = os.urandom(32)
    os.environ["LC_APPROVAL_ENCRYPTION_KEY"] = key.hex()
    try:
        crypto = ApprovalCrypto.from_env()
        assert crypto._key == key
    finally:
        del os.environ["LC_APPROVAL_ENCRYPTION_KEY"]


def test_env_key_resolution_invalid() -> None:
    os.environ["LC_APPROVAL_ENCRYPTION_KEY"] = "too-short"
    try:
        with pytest.raises(ApprovalCryptoError):
            ApprovalCrypto.from_env()
    finally:
        del os.environ["LC_APPROVAL_ENCRYPTION_KEY"]
