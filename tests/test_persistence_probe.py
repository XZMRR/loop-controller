from __future__ import annotations

import os
from pathlib import Path

import pytest

from loop_controller.infra.persistence_probe import PersistenceProbe, PersistenceTarget


def test_probe_repairs_tail_and_reports_status(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_bytes(b'{"ok":true}\n{"broken"')
    path.chmod(0o600)

    status = PersistenceProbe([PersistenceTarget("state", path)]).run()

    assert status.status == "tail_repaired"
    assert status.tail_repairs == 1
    assert path.read_bytes() == b'{"ok":true}\n'


def test_replace_probe_locks_and_reads_real_target(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_bytes(b"existing")
    path.chmod(0o600)

    status = PersistenceProbe([PersistenceTarget("checkpoint", path, replace=True)]).run()

    assert status.status == "healthy"
    assert path.read_bytes() == b"existing"
    assert Path(f"{path}.lock").exists()


def test_probe_reports_corruption_as_write_blocked(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_bytes(b'{"ok":true}\nnot-json\n')
    path.chmod(0o600)

    status = PersistenceProbe([PersistenceTarget("state", path)]).run()

    assert status.status == "write_blocked"
    assert status.corrupted_stores == ["state"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
@pytest.mark.parametrize(
    ("unsafe_component", "unsafe_mode"),
    [("directory", 0o750), ("file", 0o640), ("lock", 0o640)],
)
def test_probe_requires_exact_private_permissions(
    tmp_path: Path, unsafe_component: str, unsafe_mode: int
) -> None:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    path = directory / "state.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    lock_path = Path(f"{path}.lock")
    lock_path.touch(mode=0o600)

    checked = {"directory": directory, "file": path, "lock": lock_path}[unsafe_component]
    checked.chmod(unsafe_mode)

    status = PersistenceProbe([PersistenceTarget("state", path)]).run()

    assert status.status == "write_blocked"
    assert status.unsafe_permissions == ["state"]
