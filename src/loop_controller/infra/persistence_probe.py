from __future__ import annotations

import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import portalocker

from loop_controller.infra.durable_io import (
    DurableIOError,
    DurableJsonlFile,
    durable_atomic_replace,
)

try:
    from loop_controller.metrics import (
        observe_persistence_corruption,
        observe_persistence_lock_failure,
        observe_persistence_lock_wait,
        set_persistence_durability,
    )
except ImportError:
    def observe_persistence_corruption(_store: str) -> None:  # type: ignore[misc]
        pass

    def observe_persistence_lock_failure() -> None:  # type: ignore[misc]
        pass

    def observe_persistence_lock_wait(_duration: float) -> None:  # type: ignore[misc]
        pass

    def set_persistence_durability(_safe: bool, _fsync_enabled: bool) -> None:  # type: ignore[misc]
        pass


@dataclass(frozen=True)
class PersistenceTarget:
    name: str
    path: Path
    replace: bool = False
    critical: bool = True


@dataclass
class PersistenceStatus:
    status: str = "healthy"
    fsync_enabled: bool = True
    tail_repairs: int = 0
    lock_failures: int = 0
    corrupted_stores: list[str] = field(default_factory=list)
    unsafe_permissions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "fsync_enabled": self.fsync_enabled,
            "tail_repairs": self.tail_repairs,
            "lock_failures": self.lock_failures,
            "corrupted_stores": list(self.corrupted_stores),
            "unsafe_permissions": list(self.unsafe_permissions),
        }


class PersistenceProbe:
    def __init__(
        self,
        targets: list[PersistenceTarget],
        *,
        fsync_enabled: bool = True,
        lock_timeout_seconds: float = 5.0,
        repair_incomplete_tail: bool = True,
        enforce_permissions: bool = True,
        fail_on_unsafe_permissions: bool = True,
    ) -> None:
        self._targets = targets
        self._fsync_enabled = fsync_enabled
        self._lock_timeout_seconds = lock_timeout_seconds
        self._repair_tail = repair_incomplete_tail
        self._enforce_permissions = enforce_permissions
        self._fail_on_unsafe_permissions = fail_on_unsafe_permissions

    def run(self) -> PersistenceStatus:
        result = PersistenceStatus(fsync_enabled=self._fsync_enabled)
        for target in self._targets:
            try:
                self._probe_target(target, result)
            except DurableIOError as exc:
                if "timed out acquiring" in str(exc):
                    result.lock_failures += 1
                    result.status = "lock_unavailable"
                    observe_persistence_lock_failure()
                else:
                    result.corrupted_stores.append(target.name)
                    result.status = "write_blocked" if target.critical else "degraded"
                    observe_persistence_corruption(target.name)
            except (OSError, ValueError, PermissionError):
                result.corrupted_stores.append(target.name)
                result.status = "write_blocked" if target.critical else "degraded"
                observe_persistence_corruption(target.name)
        if result.unsafe_permissions and self._fail_on_unsafe_permissions:
            result.status = "write_blocked"
        elif result.tail_repairs and result.status == "healthy":
            result.status = "tail_repaired"
        safe = self._fsync_enabled and result.status in {"healthy", "tail_repaired"}
        set_persistence_durability(safe, self._fsync_enabled)
        return result

    def _probe_target(self, target: PersistenceTarget, result: PersistenceStatus) -> None:
        path = target.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = path.parent / f".{path.name}.probe.{os.getpid()}.{uuid.uuid4().hex}"
        if target.replace:
            target_lock = Path(f"{path}.lock")
            lock_existed = target_lock.exists()
            lock_wait_start = time.perf_counter()
            try:
                with portalocker.Lock(
                    str(target_lock), mode="a+b", timeout=self._lock_timeout_seconds
                ):
                    observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
                    if not lock_existed and os.name != "nt":
                        os.chmod(target_lock, 0o600)
                    if path.exists():
                        path.read_bytes()
                    durable_atomic_replace(
                        probe,
                        b"probe\n",
                        lock_timeout_seconds=self._lock_timeout_seconds,
                        fsync_enabled=self._fsync_enabled,
                    )
                    probe.unlink(missing_ok=True)
                    Path(f"{probe}.lock").unlink(missing_ok=True)
            except portalocker.LockException as exc:
                observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
                raise DurableIOError(
                    f"timed out acquiring durable I/O lock: {target_lock}"
                ) from exc
        else:
            durable = DurableJsonlFile(
                path,
                lock_timeout_seconds=self._lock_timeout_seconds,
                fsync_enabled=self._fsync_enabled,
            )
            with durable.transaction() as transaction:
                if self._repair_tail and transaction.repair_incomplete_tail():
                    result.tail_repairs += 1
                transaction.read_complete_lines()
        if path.exists() and not os.access(path, os.R_OK | os.W_OK):
            raise PermissionError(path)
        if self._enforce_permissions and os.name != "nt":
            required_modes = (
                (path.parent, 0o700),
                (path, 0o600),
                (Path(f"{path}.lock"), 0o600),
            )
            for checked, required_mode in required_modes:
                if checked.exists() and stat.S_IMODE(checked.stat().st_mode) != required_mode:
                    result.unsafe_permissions.append(target.name)
                    break
