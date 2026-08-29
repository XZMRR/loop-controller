"""证据链存储后端。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from loop_controller.audit.evidence import SignedEvidence
from loop_controller.infra.durable_io import DurableJsonlFile
from loop_controller.utils.canonical import canonical_json


class LocalFileEvidenceBackend:
    """按租户将签名证据追加写入本地 JSONL 文件。"""

    def __init__(
        self,
        base_path: str | Path,
        *,
        lock_timeout_seconds: float = 5.0,
        fsync_enabled: bool = True,
    ) -> None:
        self._base = Path(base_path)
        self._lock_timeout_seconds = lock_timeout_seconds
        self._fsync_enabled = fsync_enabled

    def _path(self, tenant_id: str | None) -> Path:
        name = tenant_id or "default"
        if name in {".", ".."} or Path(name).name != name:
            raise ValueError("tenant_id 不能包含路径分隔符")
        return self._base / f"{name}.jsonl"

    def _durable(self, tenant_id: str | None) -> DurableJsonlFile:
        return DurableJsonlFile(
            self._path(tenant_id),
            lock_timeout_seconds=self._lock_timeout_seconds,
            fsync_enabled=self._fsync_enabled,
        )

    async def append(self, tenant_id: str | None, signed_evidence: SignedEvidence) -> None:
        line = canonical_json(signed_evidence.model_dump(mode="json"))
        await asyncio.to_thread(self._append_line, self._path(tenant_id), line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with DurableJsonlFile(path).transaction() as transaction:
            transaction.repair_incomplete_tail()
            transaction.read_complete_lines()
            transaction.append_json(json.loads(line))

    async def append_from_tail(
        self,
        tenant_id: str | None,
        build: Callable[[tuple[int, str] | None], SignedEvidence],
    ) -> SignedEvidence:
        return await asyncio.to_thread(self._append_from_tail_sync, tenant_id, build)

    def _append_from_tail_sync(
        self,
        tenant_id: str | None,
        build: Callable[[tuple[int, str] | None], SignedEvidence],
    ) -> SignedEvidence:
        self._read_records_sync(self._path(tenant_id))
        with self._durable(tenant_id).transaction() as transaction:
            transaction.repair_incomplete_tail()
            lines = transaction.read_complete_lines()
            tail = None
            if lines:
                last = SignedEvidence.model_validate(json.loads(lines[-1]))
                tail = (last.seq, last.current_hash)
            evidence = build(tail)
            transaction.append_json(evidence.model_dump(mode="json"))
            return evidence

    async def tail_state(self, tenant_id: str | None) -> tuple[int, str] | None:
        records = await self._read_records(tenant_id)
        if not records:
            return None
        last = records[-1]
        return last.seq, last.current_hash

    async def last_hash(self, tenant_id: str | None) -> str | None:
        state = await self.tail_state(tenant_id)
        return state[1] if state is not None else None

    async def _read_records(self, tenant_id: str | None) -> list[SignedEvidence]:
        return await asyncio.to_thread(self._read_records_sync, self._path(tenant_id))

    @staticmethod
    def _read_records_sync(path: Path) -> list[SignedEvidence]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            return [
                SignedEvidence.model_validate_json(line)
                for raw_line in fh
                if (line := raw_line.strip())
            ]

    async def iter_evidence(self, tenant_id: str | None) -> AsyncIterator[SignedEvidence]:
        for evidence in await self._read_records(tenant_id):
            yield evidence


__all__ = ["LocalFileEvidenceBackend"]
