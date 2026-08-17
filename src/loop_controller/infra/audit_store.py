"""审计存储（§4.4 / §7.1）：T3.1 完整版。

``JsonlAuditStore`` 追加写入 ``audit.jsonl``，并为每个事件分配 ``seq``、计算
``prev_hash``（上一条事件规范 JSON 的 SHA-256），提供 ``verify_chain`` 检测
删除/改写/插入/顺序变更，以及 ``query_by_trace`` 按 trace_id 检索。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.models import AuditEvent
from loop_controller.utils.canonical import canonical_json


@runtime_checkable
class AuditStore(Protocol):
    """审计存储接口（§4.4）。"""

    def append(self, event: AuditEvent) -> None: ...
    def verify_chain(self) -> bool: ...
    def query_by_trace(self, trace_id: str) -> list[AuditEvent]: ...


def _hash_text(text: str) -> str:
    """SHA-256 文本摘要（UTF-8）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class JsonlAuditStore:
    """JSONL 审计存储 + SHA-256 哈希链。"""

    _GENESIS = "GENESIS"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._prev_hash = self._load_tail()

    def _load_tail(self) -> tuple[int, str]:
        """启动时重放文件末行，恢复 ``seq`` 与 ``prev_hash``，保证重启后续写不断链。"""
        if not self._path.exists():
            return 0, self._GENESIS
        with self._path.open("r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if not lines:
            return 0, self._GENESIS
        try:
            last = json.loads(lines[-1])
        except json.JSONDecodeError:
            # 文件末行已损坏：无法安全追加，视为空链重启（调用方应运行 verify_chain 发现）。
            return 0, self._GENESIS
        return int(last.get("seq", 0)), _hash_text(lines[-1])

    def append(self, event: AuditEvent) -> None:
        """分配 seq/prev_hash 后追加写入 JSONL。"""
        self._seq += 1
        to_write = event.model_copy(
            update={
                "seq": self._seq,
                "prev_hash": self._prev_hash,
            }
        )
        # 规范 JSON 计算哈希，避免字段顺序/空白差异导致校验漂移（§7.3 / 踩坑 #6）。
        # mode="json" 把 datetime 等不可 JSON 序列化的类型先转成字符串。
        line = canonical_json(to_write.model_dump(mode="json", exclude_none=True))
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        self._prev_hash = _hash_text(line)

    def verify_chain(self) -> bool:
        """重放全文件校验三件事：seq 连续递增、prev_hash 链接正确、每行可解析。"""
        if not self._path.exists():
            return True
        expected_prev = self._GENESIS
        expected_seq = 1
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if record.get("seq") != expected_seq:
                    return False
                if record.get("prev_hash") != expected_prev:
                    return False
                expected_seq += 1
                expected_prev = _hash_text(line)
        return True

    def query_by_trace(self, trace_id: str) -> list[AuditEvent]:
        """按 trace_id 全文件扫描并返回 AuditEvent 列表（MVP 数据量小，可接受）。"""
        results: list[AuditEvent] = []
        if not self._path.exists():
            return results
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("trace_id") == trace_id:
                    results.append(AuditEvent(**record))
        return results
