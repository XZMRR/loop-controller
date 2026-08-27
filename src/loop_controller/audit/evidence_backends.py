"""证据链存储后端。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from loop_controller.audit.evidence import SignedEvidence
from loop_controller.utils.canonical import canonical_json


class LocalFileEvidenceBackend:
    """按租户将签名证据追加写入本地 JSONL 文件。"""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, tenant_id: str | None) -> Path:
        name = tenant_id or "default"
        if name in {".", ".."} or Path(name).name != name:
            raise ValueError("tenant_id 不能包含路径分隔符")
        return self._base / f"{name}.jsonl"

    async def append(self, tenant_id: str | None, signed_evidence: SignedEvidence) -> None:
        line = canonical_json(signed_evidence.model_dump(mode="json"))
        with self._path(tenant_id).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    async def last_hash(self, tenant_id: str | None) -> str | None:
        last: SignedEvidence | None = None
        async for evidence in self.iter_evidence(tenant_id):
            last = evidence
        return last.current_hash if last is not None else None

    async def iter_evidence(self, tenant_id: str | None) -> AsyncIterator[SignedEvidence]:
        path = self._path(tenant_id)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield SignedEvidence.model_validate_json(line)


__all__ = ["LocalFileEvidenceBackend"]
