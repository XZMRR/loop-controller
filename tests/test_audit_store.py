"""JsonlAuditStore 哈希链与查询测试（T3.1 / P0 HMAC / A12）。"""

from __future__ import annotations

import json

import pytest

from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.models import AuditEvent


def _make_event(seq: int = 0, trace_id: str = "t1") -> AuditEvent:
    return AuditEvent(
        event_id="e1",
        seq=seq,
        prev_hash="",
        trace_id=trace_id,
        session_id=trace_id,
        actor_type="agent",
        actor_id="researcher_001",
        action="task_start",
    )


def test_chain_initially_passes(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)

    store.append(_make_event())
    store.append(_make_event())

    assert store.verify_chain()
    assert store._seq == 2


def test_seq_and_prev_hash_assigned(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)

    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["seq"] == 1
    assert first["prev_hash"] == "GENESIS"
    assert second["seq"] == 2
    assert second["prev_hash"] != "GENESIS"


def test_detects_deleted_line(tmp_path) -> None:
    """删除中间行会破坏后续行的 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    # 删除第二行：第三行的 prev_hash 将指向不存在的行
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_modified_line(tmp_path) -> None:
    """修改中间行会破坏下一行的 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    record = json.loads(lines[1])
    record["reason"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_inserted_line(tmp_path) -> None:
    """插入一行会破坏 seq 连续性或 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    fake = json.loads(lines[0])
    fake["seq"] = 3
    lines.insert(1, json.dumps(fake, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_swapped_lines(tmp_path) -> None:
    """交换相邻行会破坏 seq 连续性。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_resumes_chain_after_restart(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditStore(path)
    first.append(_make_event())

    second = JsonlAuditStore(path)
    second.append(_make_event())

    assert second.verify_chain()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2
    assert json.loads(lines[1])["prev_hash"] != "GENESIS"


def test_query_by_trace(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(
        AuditEvent(
            event_id="e1",
            trace_id="t1",
            session_id="t1",
            actor_type="agent",
            actor_id="a1",
            action="task_start",
        )
    )
    store.append(
        AuditEvent(
            event_id="e2",
            trace_id="t2",
            session_id="t2",
            actor_type="agent",
            actor_id="a2",
            action="task_start",
        )
    )

    assert len(store.query_by_trace("t1")) == 1
    assert store.query_by_trace("t1")[0].event_id == "e1"
    assert len(store.query_by_trace("t2")) == 1
    assert len(store.query_by_trace("t3")) == 0


# ---------------------------------------------------------------------------
# P0 HMAC 新增测试
# ---------------------------------------------------------------------------


def _hmac_key() -> bytes:
    """返回 32 字节测试 key（hex 编码 64 字符）。"""
    return bytes.fromhex("a" * 64)


def _other_key() -> bytes:
    """不同的测试 key。"""
    return bytes.fromhex("b" * 64)


def test_hmac_chain_passes(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key())
    store.append(_make_event())
    store.append(_make_event(trace_id="t2"))
    assert store.verify_chain()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["hash_algo"] == "hmac-sha256"


def test_hmac_detects_wrong_key_on_resume(tmp_path) -> None:
    """用错误 key 恢复 store 后，verify_chain 应失败（HMAC 不匹配）。"""
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key())
    first.append(_make_event())
    first.append(_make_event())

    evil = JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_other_key())
    assert not evil.verify_chain()


def test_seal_detects_deletion_before_seal(tmp_path) -> None:
    """seal 之后删除 seal 之前的事件行，校验应失败。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key())
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())
    store.seal()

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    # 删除倒数第二行（第 3 条事件），保留 seal。
    del lines[-2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key()).verify_chain()


def test_seal_signature_domain_separation(tmp_path) -> None:
    """seal_signature 使用 seal_key，与 event hash 不同。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key())
    store.append(_make_event())
    store.seal()

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    seal_record = json.loads(lines[-1])
    assert seal_record["action"] == "seal"
    assert seal_record["metadata"]["seal_signature"]
    # 篡改 seal_signature 后校验失败。
    seal_record["metadata"]["seal_signature"] = "0" * 64
    lines[-1] = json.dumps(seal_record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path, hash_algo="hmac-sha256", hmac_key=_hmac_key()).verify_chain()


def test_hmac_requires_key(tmp_path) -> None:
    with pytest.raises(ValueError, match="hmac-sha256"):
        JsonlAuditStore(tmp_path / "audit.jsonl", hash_algo="hmac-sha256")


def test_sha256_ignores_hmac_key(tmp_path) -> None:
    """sha256 模式下即使传了 key 也不应使用（保持默认无密钥行为）。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path, hash_algo="sha256", hmac_key=_hmac_key())
    store.append(_make_event())
    # 用另一个 key 创建 store 仍然能验证，因为 sha256 不依赖 key。
    other = JsonlAuditStore(path, hash_algo="sha256", hmac_key=_other_key())
    assert other.verify_chain()
