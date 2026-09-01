"""审计事件 SQLite 索引（v0.34.0）。

保留 ``audit.jsonl`` 作为追加日志与证据链物理载体，但将关键索引写入 SQLite，
使 ``list_recent`` / ``query_by_*`` 从 O(n) 线性扫描降级为 O(log n) 索引查询。

索引失败时不丢审计事件：JSONL 写入成功后才会插入索引；若索引插入失败，
记录 degraded 原因，但审计完整性优先。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from loop_controller.models import AuditEvent

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    timestamp REAL NOT NULL,
    trace_id TEXT,
    session_id TEXT,
    action TEXT,
    json_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
"""


class AuditIndexError(Exception):
    """审计索引异常。"""


@dataclass(frozen=True)
class AuditIndexStatus:
    """审计索引健康状态。"""

    healthy: bool
    degraded_reason: str | None
    indexed_count: int


class AuditIndex:
    """审计事件 SQLite 索引。

    每个公开方法内部新建连接并用显式事务包裹，保证多进程/多线程安全。
    连接启用 WAL 模式；失败时统一抛出 ``AuditIndexError``。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._degraded_reason: str | None = None
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.Error as exc:
            raise AuditIndexError(f"无法连接审计索引数据库 {self._db_path}: {exc}") from exc

    def init_schema(self) -> None:
        """初始化/校验 Schema；幂等。"""
        try:
            with self._connect() as conn:
                conn.executescript(AUDIT_SCHEMA)
        except sqlite3.Error as exc:
            raise AuditIndexError(f"无法初始化审计索引 Schema: {exc}") from exc

    @property
    def degraded(self) -> bool:
        """索引是否处于降级状态。"""
        with self._lock:
            return self._degraded_reason is not None

    @property
    def degraded_reason(self) -> str | None:
        with self._lock:
            return self._degraded_reason

    def mark_degraded(self, reason: str) -> None:
        """标记索引为降级状态。"""
        with self._lock:
            self._degraded_reason = reason

    def reset_degraded(self) -> None:
        """重置降级状态；通常在修复后由运维显式调用。"""
        with self._lock:
            self._degraded_reason = None

    def status(self) -> AuditIndexStatus:
        """返回索引健康状态。"""
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT COUNT(*) FROM audit_events")
                count = cur.fetchone()[0]
        except sqlite3.Error as exc:
            return AuditIndexStatus(
                healthy=False,
                degraded_reason=f"index count failed: {exc}",
                indexed_count=-1,
            )
        return AuditIndexStatus(
            healthy=not self.degraded,
            degraded_reason=self.degraded_reason,
            indexed_count=count,
        )

    def append(self, event: AuditEvent) -> None:
        """追加单条审计事件到索引。

        调用方应保证 JSONL 已写入成功；本方法只维护索引。
        """
        try:
            with self._connect() as conn:
                with conn:
                    payload = json.dumps(
                        event.model_dump(mode="json", exclude_none=True),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        """
                        INSERT INTO audit_events
                            (seq, event_id, timestamp, trace_id, session_id,
                             action, json_payload, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.seq,
                            event.event_id,
                            event.timestamp.timestamp(),
                            event.trace_id or None,
                            event.session_id or None,
                            event.action,
                            payload,
                            event.timestamp.isoformat(),
                        ),
                    )
        except sqlite3.Error as exc:
            self.mark_degraded(f"audit index append failed: {exc}")
            raise AuditIndexError(f"审计索引追加失败: {exc}") from exc

    def list_recent(self, limit: int = 100, before: float | None = None) -> list[AuditEvent]:
        """使用索引返回最近的审计事件。"""
        try:
            with self._connect() as conn:
                if before is not None:
                    cur = conn.execute(
                        "SELECT json_payload FROM audit_events WHERE timestamp < ? "
                        "ORDER BY seq DESC LIMIT ?",
                        (before, limit),
                    )
                else:
                    cur = conn.execute(
                        "SELECT json_payload FROM audit_events ORDER BY seq DESC LIMIT ?",
                        (limit,),
                    )
                return [self._parse_event(row["json_payload"]) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise AuditIndexError(f"审计索引查询失败: {exc}") from exc

    def query_by_trace(self, trace_id: str) -> list[AuditEvent]:
        return self._query_by_field("trace_id", trace_id)

    def query_by_session(self, session_id: str) -> list[AuditEvent]:
        return self._query_by_field("session_id", session_id)

    def _query_by_field(self, field: str, value: str) -> list[AuditEvent]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"SELECT json_payload FROM audit_events WHERE {field} = ? ORDER BY seq",
                    (value,),
                )
                return [self._parse_event(row["json_payload"]) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise AuditIndexError(f"审计索引查询失败: {exc}") from exc

    def _parse_event(self, payload: str) -> AuditEvent:
        return AuditEvent.model_validate_json(payload)

    def last_seq(self) -> int:
        """返回已索引的最大 seq；空表返回 0。"""
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM audit_events")
                return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            raise AuditIndexError(f"审计索引 seq 查询失败: {exc}") from exc

    def rebuild_from_jsonl(self, jsonl_path: Path) -> int:
        """从 audit.jsonl 全量重建索引；返回重建的事件数。"""
        count = 0
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute("DELETE FROM audit_events")
                    if not jsonl_path.exists():
                        return 0
                    with jsonl_path.open("r", encoding="utf-8") as fh:
                        for raw in fh:
                            line = raw.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            # 支持旧格式：事件可能嵌套在 "event" 下
                            event_data = record.get("event") or record
                            try:
                                event = AuditEvent.model_validate(event_data)
                            except Exception:
                                continue
                            conn.execute(
                                """
                                INSERT INTO audit_events
                                    (seq, event_id, timestamp, trace_id, session_id,
                                     action, json_payload, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    event.seq,
                                    event.event_id,
                                    event.timestamp.timestamp(),
                                    event.trace_id or None,
                                    event.session_id or None,
                                    event.action,
                                    json.dumps(
                                        event.model_dump(mode="json", exclude_none=True),
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                    event.timestamp.isoformat(),
                                ),
                            )
                            count += 1
        except sqlite3.Error as exc:
            self.mark_degraded(f"audit index rebuild failed: {exc}")
            raise AuditIndexError(f"审计索引重建失败: {exc}") from exc
        self.reset_degraded()
        return count
