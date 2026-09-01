from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import portalocker

logger = logging.getLogger(__name__)

try:
    from loop_controller.metrics import (
        observe_persistence_fsync,
        observe_persistence_lock_wait,
        observe_persistence_tail_repair,
    )
except ImportError:
    def observe_persistence_fsync(_duration: float) -> None:  # type: ignore[misc]
        pass

    def observe_persistence_lock_wait(_duration: float) -> None:  # type: ignore[misc]
        pass

    def observe_persistence_tail_repair() -> None:  # type: ignore[misc]
        pass


class DurableIOError(RuntimeError):
    pass


class CorruptedJsonlError(DurableIOError):
    pass


class DurableJsonlFile:
    def __init__(
        self,
        path: Path,
        *,
        lock_timeout_seconds: float = 5.0,
        fsync_enabled: bool = True,
    ) -> None:
        if lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be greater than zero")
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.fsync_enabled = fsync_enabled

    @contextmanager
    def transaction(self) -> Iterator[DurableJsonlTransaction]:
        lock_wait_start = time.perf_counter()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_existed = self.lock_path.exists()
            with portalocker.Lock(
                str(self.lock_path),
                mode="a+b",
                timeout=self.lock_timeout_seconds,
            ):
                observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
                if not lock_existed and os.name != "nt":
                    os.chmod(self.lock_path, 0o600)
                file_existed = self.path.exists()
                stream = self.path.open("a+b")
                try:
                    if not file_existed and os.name != "nt":
                        os.chmod(self.path, 0o600)
                    yield DurableJsonlTransaction(stream, self.fsync_enabled)
                finally:
                    stream.close()
        except DurableIOError:
            raise
        except portalocker.LockException as exc:
            observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
            raise DurableIOError(f"timed out acquiring durable I/O lock: {self.lock_path}") from exc
        except (OSError, ValueError) as exc:
            raise DurableIOError(f"durable JSONL transaction failed: {self.path}") from exc


def durable_locked_read(
    path: Path,
    *,
    lock_timeout_seconds: float = 5.0,
) -> bytes | None:
    if lock_timeout_seconds <= 0:
        raise ValueError("lock_timeout_seconds must be greater than zero")

    path = Path(path)
    lock_path = Path(f"{path}.lock")
    lock_wait_start = time.perf_counter()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with portalocker.Lock(
            str(lock_path),
            mode="a+b",
            timeout=lock_timeout_seconds,
        ):
            observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
            return path.read_bytes() if path.exists() else None
    except portalocker.LockException as exc:
        observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
        raise DurableIOError(f"timed out acquiring durable I/O lock: {lock_path}") from exc
    except OSError as exc:
        raise DurableIOError(f"durable locked read failed: {path}") from exc


class DurableJsonlTransaction:
    def __init__(self, stream: BinaryIO, fsync_enabled: bool) -> None:
        self._stream = stream
        self._fsync_enabled = fsync_enabled

    def read_complete_lines(self, *, allow_corrupt_last: bool = False) -> list[bytes]:
        try:
            self._stream.seek(0)
            content = self._stream.read()
            lines = content.splitlines(keepends=True)
            complete: list[bytes] = []
            for index, line in enumerate(lines):
                if not line.endswith(b"\n"):
                    break
                raw = line[:-1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                try:
                    json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if allow_corrupt_last and index == len(lines) - 1:
                        logger.warning("ignored corrupted final JSONL record at line %d", index + 1)
                        break
                    raise CorruptedJsonlError(
                        f"corrupted JSONL record at line {index + 1}"
                    ) from exc
                complete.append(raw)
            return complete
        except DurableIOError:
            raise
        except OSError as exc:
            raise DurableIOError("failed to read durable JSONL file") from exc

    def repair_incomplete_tail(self) -> bool:
        try:
            self._stream.seek(0)
            content = self._stream.read()
            if not content or content.endswith(b"\n"):
                return False
            tail_start = content.rfind(b"\n") + 1
            tail = content[tail_start:]
            try:
                json.loads(tail)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._stream.seek(tail_start)
                self._stream.truncate()
                self._sync()
                observe_persistence_tail_repair()
                return True
            self._stream.seek(0, os.SEEK_END)
            self._stream.write(b"\n")
            self._sync()
            observe_persistence_tail_repair()
            return True
        except OSError as exc:
            raise DurableIOError("failed to repair incomplete JSONL tail") from exc

    def append_json(self, payload: Mapping[str, object]) -> None:
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError) as exc:
            raise DurableIOError("failed to serialize durable JSONL record") from exc

        try:
            self._stream.seek(0, os.SEEK_END)
            view = memoryview(serialized)
            written = 0
            while written < len(view):
                count = self._stream.write(view[written:])
                if count is None or count <= 0:
                    raise OSError("short write while appending JSONL record")
                written += count
            self._sync()
        except OSError as exc:
            raise DurableIOError("failed to append durable JSONL record") from exc

    def _sync(self) -> None:
        self._stream.flush()
        if self._fsync_enabled:
            fsync_start = time.perf_counter()
            os.fsync(self._stream.fileno())
            observe_persistence_fsync(time.perf_counter() - fsync_start)


def durable_atomic_replace(
    path: Path,
    content: bytes | Callable[[bytes | None], bytes],
    *,
    mode: int = 0o600,
    lock_timeout_seconds: float = 5.0,
    fsync_enabled: bool = True,
) -> None:
    if lock_timeout_seconds <= 0:
        raise ValueError("lock_timeout_seconds must be greater than zero")

    path = Path(path)
    lock_path = Path(f"{path}.lock")
    temp_path: Path | None = None
    lock_wait_start = time.perf_counter()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_existed = lock_path.exists()
        with portalocker.Lock(
            str(lock_path),
            mode="a+b",
            timeout=lock_timeout_seconds,
        ):
            observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
            if not lock_existed and os.name != "nt":
                os.chmod(lock_path, 0o600)
            replacement = content(path.read_bytes() if path.exists() else None) if callable(content) else content
            temp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(temp_path, flags, mode)
            try:
                view = memoryview(replacement)
                written = 0
                while written < len(view):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError("short write while writing atomic replacement")
                    written += count
                if fsync_enabled:
                    fsync_start = time.perf_counter()
                    os.fsync(fd)
                    observe_persistence_fsync(time.perf_counter() - fsync_start)
            finally:
                os.close(fd)
            os.replace(temp_path, path)
            temp_path = None
            if fsync_enabled and os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    fsync_start = time.perf_counter()
                    os.fsync(directory_fd)
                    observe_persistence_fsync(time.perf_counter() - fsync_start)
                finally:
                    os.close(directory_fd)
    except DurableIOError:
        raise
    except portalocker.LockException as exc:
        observe_persistence_lock_wait(time.perf_counter() - lock_wait_start)
        raise DurableIOError(f"timed out acquiring durable I/O lock: {lock_path}") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise DurableIOError(f"durable atomic replace failed: {path}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
