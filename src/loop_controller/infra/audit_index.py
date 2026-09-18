"""JSONL 审计事实源的 SQLite 查询索引。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from loop_controller.models import AuditEvent

CORRELATION_COLUMNS = (
    "request_id", "interaction_id", "decision_id", "task_id", "call_id",
    "delegation_jti", "receipt_id",
)
EXTRA_COLUMNS = (
    "tenant_id", "workload_id", "authenticated_instance_id", "process_instance_id",
    "delegated_agent_id", "delegated_user_id", "executor", "backend", "receipt_type",
    "receipt_status", "result_sha256", "credential_ref_digest",
    "resolved_credential_version",
)
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
CREATE TABLE IF NOT EXISTS interaction_audit_events (
    seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL,
    interaction_id TEXT NOT NULL, request_id TEXT, source_agent_id TEXT NOT NULL,
    target_agent_id TEXT, verdict TEXT NOT NULL, policy_hits TEXT NOT NULL,
    target_entrypoint TEXT,
    FOREIGN KEY (seq) REFERENCES audit_events(seq) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_interaction_audit_interaction ON interaction_audit_events(interaction_id);
CREATE INDEX IF NOT EXISTS idx_interaction_audit_source ON interaction_audit_events(source_agent_id);
CREATE INDEX IF NOT EXISTS idx_interaction_audit_target ON interaction_audit_events(target_agent_id);
CREATE INDEX IF NOT EXISTS idx_interaction_audit_verdict ON interaction_audit_events(verdict);
"""


class AuditIndexError(Exception):
    """审计索引异常。"""


@dataclass(frozen=True)
class AuditIndexStatus:
    healthy: bool
    degraded_reason: str | None
    indexed_count: int


class AuditIndex:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._degraded_reason: str | None = None
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.Error as exc:
            raise AuditIndexError("audit_index_unavailable") from exc

    def init_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(AUDIT_SCHEMA)
                existing = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
                for column in (*CORRELATION_COLUMNS, *EXTRA_COLUMNS):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE audit_events ADD COLUMN {column} TEXT")
                for column in CORRELATION_COLUMNS:
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_audit_{column} ON audit_events({column})"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id)"
                )
        except (sqlite3.Error, AuditIndexError) as exc:
            raise AuditIndexError("audit_index_schema_unavailable") from exc

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self._degraded_reason is not None

    @property
    def degraded_reason(self) -> str | None:
        with self._lock:
            return self._degraded_reason

    def mark_degraded(self, reason: str) -> None:
        with self._lock:
            self._degraded_reason = reason

    def reset_degraded(self) -> None:
        with self._lock:
            self._degraded_reason = None

    def status(self) -> AuditIndexStatus:
        try:
            with self._connect() as conn:
                count = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        except (sqlite3.Error, AuditIndexError):
            return AuditIndexStatus(False, "audit_index_unavailable", -1)
        return AuditIndexStatus(not self.degraded, self.degraded_reason, count)

    def append(self, event: AuditEvent) -> None:
        try:
            with self._connect() as conn, conn:
                payload = json.dumps(event.model_dump(mode="json", exclude_none=True), ensure_ascii=False, separators=(",", ":"))
                self._insert_event(conn, event, payload)
        except (sqlite3.Error, AuditIndexError) as exc:
            self.mark_degraded("audit_index_write_failed")
            raise AuditIndexError("audit_index_write_failed") from exc

    def _insert_event(self, conn: sqlite3.Connection, event: AuditEvent, payload: str) -> None:
        fields = ("seq", "event_id", "timestamp", "trace_id", "session_id", "action", "json_payload", "created_at", *CORRELATION_COLUMNS, *EXTRA_COLUMNS)
        values = (
            event.seq, event.event_id, event.timestamp.timestamp(), event.trace_id or None,
            event.session_id or None, event.action, payload, event.timestamp.isoformat(),
            *(getattr(event, name) for name in CORRELATION_COLUMNS),
            *(getattr(event, name) for name in EXTRA_COLUMNS),
        )
        placeholders = ",".join("?" for _ in fields)
        conn.execute(f"INSERT INTO audit_events ({','.join(fields)}) VALUES ({placeholders})", values)
        metadata = event.metadata or {}
        interaction_id = event.interaction_id or metadata.get("interaction_id")
        if not interaction_id:
            return
        verdict = metadata.get("verdict") or event.decision
        if verdict not in {"allow", "deny", "modify", "require_approval"}:
            raise sqlite3.IntegrityError("interaction audit verdict is invalid")
        conn.execute(
            """INSERT INTO interaction_audit_events
            (seq,event_id,timestamp,interaction_id,request_id,source_agent_id,target_agent_id,verdict,policy_hits,target_entrypoint)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (event.seq, event.event_id, event.timestamp.timestamp(), interaction_id,
             event.request_id or metadata.get("request_id"), metadata.get("source_agent_id") or event.actor_id,
             metadata.get("target_agent_id"), verdict,
             json.dumps(metadata.get("policy_hits", []), ensure_ascii=False),
             json.dumps(metadata.get("target_entrypoint"), ensure_ascii=False)),
        )

    def list_recent(self, limit: int = 100, before: float | None = None) -> list[AuditEvent]:
        sql = "SELECT json_payload FROM audit_events"
        params: tuple[object, ...]
        if before is None:
            params = (limit,)
        else:
            sql += " WHERE timestamp < ?"
            params = (before, limit)
        return self._query(sql + " ORDER BY seq DESC LIMIT ?", params)

    def query_by_trace(self, trace_id: str) -> list[AuditEvent]:
        return self._query_field("trace_id", trace_id)

    def query_by_session(self, session_id: str) -> list[AuditEvent]:
        return self._query_field("session_id", session_id)

    def query_by_task(self, task_id: str) -> list[AuditEvent]:
        return self._query(
            "SELECT json_payload FROM audit_events WHERE task_id=? OR (task_id IS NULL AND trace_id=?) ORDER BY seq",
            (task_id, task_id),
        )

    def query_by_correlation(self, correlation_id: str, limit: int = 100) -> list[AuditEvent]:
        events = self._query("SELECT json_payload FROM audit_events ORDER BY seq", ())
        identifiers = {correlation_id}
        selected: list[AuditEvent] = []
        selected_ids: set[str] = set()
        changed = True
        while changed:
            changed = False
            for event in events:
                values = {
                    value
                    for field in CORRELATION_COLUMNS
                    if (value := getattr(event, field, None))
                }
                if event.event_id not in selected_ids and values & identifiers:
                    selected.append(event)
                    selected_ids.add(event.event_id)
                    before = len(identifiers)
                    identifiers.update(values)
                    changed = changed or len(identifiers) != before
        selected.sort(key=lambda event: event.seq)
        return selected[:limit]

    def query_interactions(self, *, interaction_id: str | None = None, source_agent_id: str | None = None, target_agent_id: str | None = None, verdict: str | None = None, limit: int = 100) -> list[AuditEvent]:
        clauses, params = [], []
        for field, value in (("interaction_id", interaction_id), ("source_agent_id", source_agent_id), ("target_agent_id", target_agent_id), ("verdict", verdict)):
            if value is not None:
                clauses.append(f"i.{field}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._query("SELECT a.json_payload FROM interaction_audit_events i JOIN audit_events a ON a.seq=i.seq" + where + " ORDER BY i.seq DESC LIMIT ?", (*params, limit))

    def _query_field(self, field: str, value: str) -> list[AuditEvent]:
        return self._query(f"SELECT json_payload FROM audit_events WHERE {field}=? ORDER BY seq", (value,))

    def _query(self, sql: str, params: tuple[object, ...]) -> list[AuditEvent]:
        try:
            with self._connect() as conn:
                return [AuditEvent.model_validate_json(row["json_payload"]) for row in conn.execute(sql, params)]
        except (sqlite3.Error, AuditIndexError) as exc:
            raise AuditIndexError("audit_index_query_failed") from exc

    def last_seq(self) -> int:
        try:
            with self._connect() as conn:
                return int(conn.execute("SELECT COALESCE(MAX(seq),0) FROM audit_events").fetchone()[0])
        except (sqlite3.Error, AuditIndexError) as exc:
            raise AuditIndexError("audit_index_query_failed") from exc

    def rebuild_from_jsonl(self, jsonl_path: Path) -> int:
        count = 0
        try:
            with self._connect() as conn, conn:
                conn.execute("DELETE FROM interaction_audit_events")
                conn.execute("DELETE FROM audit_events")
                if not jsonl_path.exists():
                    return 0
                for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
                    try:
                        record = json.loads(raw)
                        event = AuditEvent.model_validate(record.get("event") or record)
                    except Exception:
                        continue
                    payload = json.dumps(event.model_dump(mode="json", exclude_none=True), ensure_ascii=False, separators=(",", ":"))
                    self._insert_event(conn, event, payload)
                    count += 1
        except (sqlite3.Error, AuditIndexError) as exc:
            self.mark_degraded("audit_index_rebuild_failed")
            raise AuditIndexError("audit_index_rebuild_failed") from exc
        self.reset_degraded()
        return count
