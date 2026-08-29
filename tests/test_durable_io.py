from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import portalocker
import pytest

from loop_controller.infra.durable_io import (
    CorruptedJsonlError,
    DurableIOError,
    DurableJsonlFile,
    durable_atomic_replace,
)


def test_append_writes_compact_utf8_record_with_single_newline(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    with DurableJsonlFile(path).transaction() as transaction:
        transaction.append_json({"message": "你好", "count": 1})

    assert path.read_bytes() == b'{"message":"\xe4\xbd\xa0\xe5\xa5\xbd","count":1}\n'


def test_append_calls_fsync_and_can_disable_it(tmp_path: Path, monkeypatch) -> None:
    fsync = Mock()
    monkeypatch.setattr(os, "fsync", fsync)

    with DurableJsonlFile(tmp_path / "enabled.jsonl").transaction() as transaction:
        transaction.append_json({"ok": True})
    assert fsync.call_count == 1

    fsync.reset_mock()
    with DurableJsonlFile(
        tmp_path / "disabled.jsonl", fsync_enabled=False
    ).transaction() as transaction:
        transaction.append_json({"ok": True})
    fsync.assert_not_called()


def test_fsync_failure_is_wrapped_and_preserves_cause(tmp_path: Path, monkeypatch) -> None:
    error = OSError("disk unavailable")
    monkeypatch.setattr(os, "fsync", Mock(side_effect=error))

    with pytest.raises(DurableIOError) as captured:
        with DurableJsonlFile(tmp_path / "events.jsonl").transaction() as transaction:
            transaction.append_json({"ok": True})

    assert captured.value.__cause__ is error


def test_serialization_failure_is_wrapped_before_write(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    with pytest.raises(DurableIOError, match="serialize"):
        with DurableJsonlFile(path).transaction() as transaction:
            transaction.append_json({"unsupported": object()})

    assert path.read_bytes() == b""


def test_lock_timeout_has_stable_error_and_cause(tmp_path: Path, monkeypatch) -> None:
    error = portalocker.LockException("busy")
    lock = Mock()
    lock.__enter__ = Mock(side_effect=error)
    lock.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(portalocker, "Lock", Mock(return_value=lock))

    with pytest.raises(DurableIOError, match="timed out acquiring durable I/O lock") as captured:
        with DurableJsonlFile(tmp_path / "events.jsonl").transaction():
            pass

    assert captured.value.__cause__ is error


def test_read_complete_lines_accepts_crlf_without_text_offsets(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}\r\n{"b":2}\r\n')

    with DurableJsonlFile(path).transaction() as transaction:
        assert transaction.read_complete_lines() == [b'{"a":1}', b'{"b":2}']


def test_read_complete_lines_rejects_corrupted_complete_record(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}\nnot-json\n')

    with pytest.raises(CorruptedJsonlError, match="line 2"):
        with DurableJsonlFile(path).transaction() as transaction:
            transaction.read_complete_lines()


def test_repair_invalid_incomplete_tail_then_append(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}\n{"broken"')

    with DurableJsonlFile(path).transaction() as transaction:
        assert transaction.repair_incomplete_tail() is True
        transaction.append_json({"b": 2})

    assert [json.loads(line) for line in path.read_bytes().splitlines()] == [{"a": 1}, {"b": 2}]


def test_repair_adds_newline_to_valid_json_without_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}')

    with DurableJsonlFile(path).transaction() as transaction:
        assert transaction.repair_incomplete_tail() is True

    assert path.read_bytes() == b'{"a":1}\n'


def test_valid_json_without_newline_is_separated_before_append(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}')

    with DurableJsonlFile(path).transaction() as transaction:
        transaction.repair_incomplete_tail()
        transaction.append_json({"b": 2})

    assert [json.loads(line) for line in path.read_bytes().splitlines()] == [{"a": 1}, {"b": 2}]


def test_large_append_retries_partial_binary_writes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    payload = {"value": "x" * 100_000}

    with DurableJsonlFile(path).transaction() as transaction:
        original_write = transaction._stream.write

        def partial_write(data) -> int:
            return original_write(data[: max(1, len(data) // 3)])

        transaction._stream.write = partial_write
        transaction.append_json(payload)

    assert json.loads(path.read_bytes()) == payload
    assert path.read_bytes().endswith(b"\n")


def test_atomic_replace_uses_unique_same_directory_temp_and_cleans_it(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    durable_atomic_replace(path, b"first")
    durable_atomic_replace(path, b"second")

    assert path.read_bytes() == b"second"
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "checkpoint.json.lock").exists()


def test_atomic_replace_fsyncs_temp_before_replace_and_parent_after(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "checkpoint.json"
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        original_fsync(fd)

    def tracked_replace(source, target) -> None:
        events.append("replace")
        original_replace(source, target)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "replace", tracked_replace)
    durable_atomic_replace(path, b"content")

    assert events[0:2] == ["fsync", "replace"]
    if os.name != "nt":
        assert events == ["fsync", "replace", "fsync"]


def test_atomic_replace_failure_cleans_only_own_temp(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "checkpoint.json"
    unrelated = tmp_path / ".checkpoint.json.other.tmp"
    unrelated.write_bytes(b"keep")
    error = OSError("replace failed")
    monkeypatch.setattr(os, "replace", Mock(side_effect=error))

    with pytest.raises(DurableIOError) as captured:
        durable_atomic_replace(path, b"new")

    assert captured.value.__cause__ is error
    assert unrelated.read_bytes() == b"keep"
    assert sorted(tmp_path.glob("*.tmp")) == [unrelated]


def test_atomic_replace_can_disable_all_fsync(tmp_path: Path, monkeypatch) -> None:
    fsync = Mock()
    monkeypatch.setattr(os, "fsync", fsync)

    durable_atomic_replace(tmp_path / "checkpoint.json", b"content", fsync_enabled=False)

    fsync.assert_not_called()
