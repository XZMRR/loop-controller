"""证据链存储后端。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from loop_controller.audit.evidence import SignedEvidence
from loop_controller.utils.canonical import canonical_json


class LocalFileEvidenceBackend:
    """按租户将签名证据追加写入本地 JSONL 文件。"""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)

    def _path(self, tenant_id: str | None) -> Path:
        name = tenant_id or "default"
        if name in {".", ".."} or Path(name).name != name:
            raise ValueError("tenant_id 不能包含路径分隔符")
        return self._base / f"{name}.jsonl"

    async def append(self, tenant_id: str | None, signed_evidence: SignedEvidence) -> None:
        line = canonical_json(signed_evidence.model_dump(mode="json"))
        await asyncio.to_thread(self._append_line, self._path(tenant_id), line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

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
