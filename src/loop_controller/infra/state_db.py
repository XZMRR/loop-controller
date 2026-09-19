"""统一 SQLite 状态数据库。

v0.34.0 引入，用于把原本基于 JSONL 追加 + 内存索引的 Decision、Risk、Approval、
Reservation 等状态升级为可长期运行、可多进程安全访问的耐久性存储。

设计原则：
- 使用标准库 ``sqlite3`` + WAL 模式，避免额外异步依赖；
- 每个公开方法内部新建连接并用显式事务包裹，保证多进程/多线程安全；
- Schema 与 JSONL 语义保持一致，支持从 JSONL 一键迁移；
- 失败时 fail-closed，抛出 ``StateDatabaseError``。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    modified_args TEXT,
    original_args TEXT,
    policy_modified_args TEXT,
    effective_args TEXT,
    escalation_target TEXT,
    policy_hits TEXT,
    policy_version TEXT,
    profile_version TEXT,
    expires_at TEXT NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    finalized INTEGER NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_call_id ON decisions(call_id);
CREATE INDEX IF NOT EXISTS idx_decisions_task_id ON decisions(task_id);
CREATE INDEX IF NOT EXISTS idx_decisions_expires_at ON decisions(expires_at);

CREATE TABLE IF NOT EXISTS risk_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    score_delta REAL NOT NULL,
    tag TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_session ON risk_events(session_id);

CREATE TABLE IF NOT EXISTS risk_profiles (
    session_id TEXT PRIMARY KEY,
    cumulative_risk_score REAL NOT NULL DEFAULT 0,
    recent_tags TEXT NOT NULL DEFAULT '[]',
    denied_count INTEGER NOT NULL DEFAULT 0,
    approval_count INTEGER NOT NULL DEFAULT 0,
    consecutive_deny_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    request_id TEXT PRIMARY KEY,
    decision_id TEXT,
    amount REAL,
    currency TEXT,
    status TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_status_expires
    ON reservations(status, expires_at);

CREATE TABLE IF NOT EXISTS budget_ledger (
    task_id TEXT PRIMARY KEY,
    max_budget_token INTEGER NOT NULL DEFAULT 1000000,
    reserved INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    cost_token_count INTEGER NOT NULL DEFAULT 0,
    cost_payment_amount REAL NOT NULL DEFAULT 0,
    cost_currency TEXT NOT NULL DEFAULT 'USD',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_call_id ON budget_reservations(call_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_task_id ON budget_reservations(task_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_state ON budget_reservations(state);

CREATE TABLE IF NOT EXISTS authority_tokens (
    token_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    granted_capabilities_json TEXT NOT NULL DEFAULT '[]',
    budget_token_count INTEGER NOT NULL DEFAULT 0,
    budget_payment_amount REAL NOT NULL DEFAULT 0,
    budget_currency TEXT NOT NULL DEFAULT 'USD',
    remaining_token_count INTEGER NOT NULL DEFAULT 0,
    remaining_payment_amount REAL NOT NULL DEFAULT 0,
    remaining_currency TEXT NOT NULL DEFAULT 'USD',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    audit_record_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authority_tokens_task ON authority_tokens(task_id);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    description TEXT NOT NULL,
    tenant_id TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_session ON alerts(session_id);
CREATE INDEX IF NOT EXISTS idx_alerts_task ON alerts(task_id);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    generated_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    alert_ids_json TEXT NOT NULL DEFAULT '[]',
    event_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reports_session ON reports(session_id);
CREATE INDEX IF NOT EXISTS idx_reports_task ON reports(task_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_task_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_agent ON sessions(user_id, agent_id);

CREATE TABLE IF NOT EXISTS conversations (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);

CREATE TABLE IF NOT EXISTS approval_requests (
    decision_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);

CREATE TABLE IF NOT EXISTS approval_responses (
    decision_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    response_json TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES approval_requests(decision_id)
);

CREATE TABLE IF NOT EXISTS approval_notification_outbox (
    delivery_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    claim_token TEXT,
    lease_until TEXT,
    delivered_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_outbox_ready
    ON approval_notification_outbox(delivered_at, next_attempt_at, lease_until);

CREATE TABLE IF NOT EXISTS approval_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN ('pending', 'decided')),
    decision_id TEXT NOT NULL,
    tenant_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES approval_requests(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_approval_events_tenant_id
    ON approval_events(tenant_id, event_id);

CREATE TABLE IF NOT EXISTS policy_candidates (
    candidate_id TEXT PRIMARY KEY,
    revision TEXT,
    state TEXT NOT NULL CHECK (state IN
      ('draft','validated','published','loaded','failed','superseded')),
    base_revision TEXT,
    source_manifest_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_candidate_id TEXT,
    rollback_of_revision TEXT,
    artifact_path TEXT,
    artifact_sha256 TEXT,
    artifact_size INTEGER,
    artifact_ready INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    published_at TEXT,
    loaded_at TEXT,
    superseded_at TEXT,
    failure_stage TEXT,
    failure_code TEXT,
    failure_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_candidates_state ON policy_candidates(state);
CREATE INDEX IF NOT EXISTS idx_policy_candidates_revision ON policy_candidates(revision);

CREATE TABLE IF NOT EXISTS policy_validations (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    opa_version TEXT,
    check_ok INTEGER NOT NULL,
    test_ok INTEGER NOT NULL,
    tool_default_deny_ok INTEGER NOT NULL,
    interaction_default_deny_ok INTEGER NOT NULL,
    result_sha256 TEXT NOT NULL,
    result_summary_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES policy_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS policy_current (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision TEXT,
    candidate_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT,
    updated_at TEXT,
    FOREIGN KEY(candidate_id) REFERENCES policy_candidates(candidate_id)
);
INSERT OR IGNORE INTO policy_current(singleton, generation) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS opa_instance_status (
    instance_id TEXT PRIMARY KEY,
    revision TEXT,
    bundle_name TEXT NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    opa_version TEXT,
    opa_reported_at TEXT,
    report_sequence INTEGER,
    generation INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    report_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_status_revision ON opa_instance_status(revision);

CREATE TABLE IF NOT EXISTS opa_instance_load_evidence (
    generation INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    opa_reported_at TEXT,
    report_sequence INTEGER,
    received_at TEXT NOT NULL,
    PRIMARY KEY (generation, instance_id)
);

CREATE TABLE IF NOT EXISTS policy_change_audit (
    audit_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    candidate_id TEXT,
    base_revision TEXT,
    target_revision TEXT,
    source_sha256 TEXT,
    artifact_sha256 TEXT,
    result TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_audit_created ON policy_change_audit(created_at);

CREATE TABLE IF NOT EXISTS rbac_role_bindings (
    binding_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    tenant_id TEXT,
    role TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rbac_bindings_principal ON rbac_role_bindings(principal, revoked_at);

CREATE TABLE IF NOT EXISTS rbac_cross_tenant_grants (
    grant_id TEXT PRIMARY KEY,
    source_principal TEXT NOT NULL,
    source_tenant TEXT NOT NULL,
    target_tenant TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS rbac_denials (
    denial_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    required_permission TEXT NOT NULL,
    principal_tenant TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rbac_denials_created ON rbac_denials(created_at);
"""


_TERMINAL_RESERVATION_STATES = {"committed", "refunded", "expired"}


class StateDatabaseError(Exception):
    """统一状态数据库异常。"""


@dataclass(frozen=True)
class DecisionRecord:
    """Decision 在 SQLite 中的扁平记录。"""

    decision_id: str
    call_id: str
    task_id: str
    verdict: str
    reason: str
    modified_args: dict[str, Any] | None
    original_args: dict[str, Any] | None
    policy_modified_args: dict[str, Any] | None
    effective_args: dict[str, Any] | None
    escalation_target: str | None
    policy_hits: list[str]
    policy_version: str
    profile_version: str
    expires_at: datetime
    max_uses: int
    finalized: bool
    used_count: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> DecisionRecord:
        def _load(col: str) -> dict[str, Any] | None:
            value = row[col]
            return json.loads(value) if value else None

        policy_hits = row["policy_hits"]
        return cls(
            decision_id=row["decision_id"],
            call_id=row["call_id"],
            task_id=row["task_id"],
            verdict=row["verdict"],
            reason=row["reason"],
            modified_args=_load("modified_args"),
            original_args=_load("original_args"),
            policy_modified_args=_load("policy_modified_args"),
            effective_args=_load("effective_args"),
            escalation_target=row["escalation_target"],
            policy_hits=json.loads(policy_hits) if policy_hits else [],
            policy_version=row["policy_version"] or "",
            profile_version=row["profile_version"] or "",
            expires_at=datetime.fromisoformat(row["expires_at"]),
            max_uses=row["max_uses"],
            finalized=bool(row["finalized"]),
            used_count=row["used_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StateDatabase:
    """统一状态数据库：Decision / Risk / Approval / Reservation。

    每个公开方法内部新建连接并用显式事务包裹，保证多进程/多线程安全。
    连接启用 WAL 模式与外键约束；失败时统一抛出 ``StateDatabaseError``。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000;")
            # 多个进程首次打开同一数据库时，切换 WAL 本身需要写锁。SQLite 的
            # busy_timeout 对该 PRAGMA 并非始终生效，因此在初始化竞争窗口内有限退避。
            for attempt in range(5):
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.Error as exc:
            if "conn" in locals():
                conn.close()
            raise StateDatabaseError(f"无法连接状态数据库 {self._db_path}: {exc}") from exc

    @contextmanager
    def _immediate(self, conn: sqlite3.Connection) -> Iterator[None]:
        """显式 ``BEGIN IMMEDIATE`` 事务。

        由于连接以 ``isolation_level=None``（autocommit）打开，``with conn:`` 不会
        隐式开启事务；check-then-act 类方法必须显式 ``BEGIN IMMEDIATE`` 以获取写锁，
        避免多进程在读取与写入之间产生竞争。
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _migrate_decisions_columns(self) -> None:
        """v0.36.1：增量为 decisions 表添加 modify 参数字段。"""
        try:
            with self._connect() as conn:
                with conn:
                    cur = conn.execute("PRAGMA table_info(decisions)")
                    existing = {row["name"] for row in cur.fetchall()}
                    for col in ("original_args", "policy_modified_args", "effective_args"):
                        if col not in existing:
                            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} TEXT")
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"无法升级 decisions 表 Schema: {exc}") from exc

    def _migrate_policy_columns(self) -> None:
        try:
            with self._connect() as conn:
                existing = {row["name"] for row in conn.execute("PRAGMA table_info(opa_instance_status)")}
                for column, declaration in (
                    ("report_sequence", "INTEGER"),
                    ("generation", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE opa_instance_status ADD COLUMN {column} {declaration}")
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"无法升级 policy 表 Schema: {exc}") from exc

    def _migrate_rbac_columns(self) -> None:
        """v0.52：RBAC 与租户/双人复核列的增量迁移（幂等）。"""
        try:
            with self._connect() as conn, self._immediate(conn):
                candidates = {row["name"] for row in conn.execute("PRAGMA table_info(policy_candidates)")}
                if "tenant_id" not in candidates:
                    conn.execute("ALTER TABLE policy_candidates ADD COLUMN tenant_id TEXT")
                validations = {row["name"] for row in conn.execute("PRAGMA table_info(policy_validations)")}
                if "validated_by" not in validations:
                    conn.execute("ALTER TABLE policy_validations ADD COLUMN validated_by TEXT")
                    conn.execute(
                        """UPDATE policy_validations SET validated_by = (
                           SELECT created_by FROM policy_candidates
                           WHERE policy_candidates.candidate_id = policy_validations.candidate_id)
                           WHERE validated_by IS NULL"""
                    )
                if "separation_ok" not in validations:
                    conn.execute(
                        "ALTER TABLE policy_validations ADD COLUMN separation_ok INTEGER NOT NULL DEFAULT 0"
                    )
                approval_requests = {row["name"] for row in conn.execute("PRAGMA table_info(approval_requests)")}
                if "tenant_id" not in approval_requests:
                    conn.execute("ALTER TABLE approval_requests ADD COLUMN tenant_id TEXT")
                approval_responses = {row["name"] for row in conn.execute("PRAGMA table_info(approval_responses)")}
                if "tenant_id" not in approval_responses:
                    conn.execute("ALTER TABLE approval_responses ADD COLUMN tenant_id TEXT")
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"无法升级 RBAC/租户 Schema: {exc}") from exc

    def init_schema(self) -> None:
        """初始化/校验 Schema；幂等。"""
        try:
            with self._connect() as conn:
                conn.executescript(SCHEMA)
            self._migrate_decisions_columns()
            self._migrate_policy_columns()
            self._migrate_rbac_columns()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"无法初始化状态数据库 Schema: {exc}") from exc

    # ------------------------------------------------------------------
    # Proposal
    # ------------------------------------------------------------------

    def record_proposal(self, call_id: str, task_id: str) -> None:
        """记录 call_id 已出现；重复时抛出 ``StateDatabaseError``。"""
        try:
            with self._connect() as conn:
                with conn:
                    cur = conn.execute(
                        "SELECT 1 FROM proposals WHERE call_id = ?",
                        (call_id,),
                    )
                    if cur.fetchone() is not None:
                        raise StateDatabaseError(f"call_id {call_id} 已存在，不允许重复记录")
                    conn.execute(
                        "INSERT INTO proposals (call_id, task_id, created_at) VALUES (?, ?, ?)",
                        (call_id, task_id, _utc_now().isoformat()),
                    )
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"记录 proposal 失败: {exc}") from exc

    def is_call_id_seen(self, call_id: str) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT 1 FROM proposals WHERE call_id = ? UNION SELECT 1 FROM decisions WHERE call_id = ?",
                    (call_id, call_id),
                )
                return cur.fetchone() is not None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 call_id 失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def record_decision(self, record: DecisionRecord) -> None:
        """持久化一条 Decision；decision_id 与 call_id 均唯一。"""
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO decisions (
                            decision_id, call_id, task_id, verdict, reason,
                            modified_args, original_args, policy_modified_args, effective_args,
                            escalation_target, policy_hits,
                            policy_version, profile_version, expires_at,
                            max_uses, finalized, used_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.decision_id,
                            record.call_id,
                            record.task_id,
                            record.verdict,
                            record.reason,
                            json.dumps(record.modified_args, ensure_ascii=False)
                            if record.modified_args is not None
                            else None,
                            json.dumps(record.original_args, ensure_ascii=False)
                            if record.original_args is not None
                            else None,
                            json.dumps(record.policy_modified_args, ensure_ascii=False)
                            if record.policy_modified_args is not None
                            else None,
                            json.dumps(record.effective_args, ensure_ascii=False)
                            if record.effective_args is not None
                            else None,
                            record.escalation_target,
                            json.dumps(record.policy_hits, ensure_ascii=False)
                            if record.policy_hits
                            else None,
                            record.policy_version,
                            record.profile_version,
                            record.expires_at.isoformat(),
                            record.max_uses,
                            int(record.finalized),
                            record.used_count,
                            record.created_at.isoformat(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise StateDatabaseError(f"decision 或 call_id 已存在: {exc}") from exc
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"记录 decision 失败: {exc}") from exc

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM decisions WHERE decision_id = ?",
                    (decision_id,),
                )
                row = cur.fetchone()
                return DecisionRecord.from_row(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 decision 失败: {exc}") from exc

    def get_decision_by_call_id(self, call_id: str) -> DecisionRecord | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM decisions WHERE call_id = ?",
                    (call_id,),
                )
                row = cur.fetchone()
                return DecisionRecord.from_row(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 decision 失败: {exc}") from exc

    def use_decision(self, decision_id: str, now: datetime) -> bool:
        """原子性地使用一次 Decision。

        返回 ``True`` 表示使用成功；``False`` 表示决策不存在、已过期或
        使用次数已达上限。
        """
        try:
            with self._connect() as conn:
                with conn:
                    cur = conn.execute(
                        "SELECT expires_at, max_uses, used_count FROM decisions WHERE decision_id = ?",
                        (decision_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return False
                    expires_at = datetime.fromisoformat(row["expires_at"])
                    if now >= expires_at:
                        return False
                    if row["used_count"] >= row["max_uses"]:
                        return False
                    conn.execute(
                        "UPDATE decisions SET used_count = used_count + 1 WHERE decision_id = ?",
                        (decision_id,),
                    )
                    return True
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"使用 decision 失败: {exc}") from exc

    def record_finalized(self, decision_id: str) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        "UPDATE decisions SET finalized = 1 WHERE decision_id = ?",
                        (decision_id,),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"标记 decision finalized 失败: {exc}") from exc

    def is_decision_finalized(self, decision_id: str) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT finalized FROM decisions WHERE decision_id = ?",
                    (decision_id,),
                )
                row = cur.fetchone()
                return bool(row["finalized"]) if row else False
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 decision finalized 失败: {exc}") from exc

    def iter_all_decisions(self) -> list[DecisionRecord]:
        """按创建顺序返回全部 Decision；用于迁移与启动校验。"""
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT * FROM decisions ORDER BY created_at")
                return [DecisionRecord.from_row(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举 decision 失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Risk
    # ------------------------------------------------------------------

    def load_risk_events(self) -> list[dict[str, Any]]:
        """返回全部风险事件字典；用于启动重放或校验。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT session_id, event_type, score_delta, tag, timestamp "
                    "FROM risk_events ORDER BY seq"
                )
                return [
                    {
                        "session_id": row["session_id"],
                        "event_type": row["event_type"],
                        "score_delta": row["score_delta"],
                        "tag": row["tag"],
                        "timestamp": row["timestamp"],
                    }
                    for row in cur.fetchall()
                ]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"加载风险事件失败: {exc}") from exc

    def append_risk_event(self, event: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO risk_events (session_id, event_type, score_delta, tag, timestamp) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            event["session_id"],
                            event["event_type"],
                            event["score_delta"],
                            event["tag"],
                            event["timestamp"],
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"追加风险事件失败: {exc}") from exc

    def get_risk_profile(self, session_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM risk_profiles WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "session_id": row["session_id"],
                    "cumulative_risk_score": row["cumulative_risk_score"],
                    "recent_tags": json.loads(row["recent_tags"]),
                    "denied_count": row["denied_count"],
                    "approval_count": row["approval_count"],
                    "consecutive_deny_count": row["consecutive_deny_count"],
                    "updated_at": row["updated_at"],
                }
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询风险画像失败: {exc}") from exc

    def upsert_risk_profile(self, profile: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO risk_profiles (
                            session_id, cumulative_risk_score, recent_tags,
                            denied_count, approval_count, consecutive_deny_count, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            cumulative_risk_score = excluded.cumulative_risk_score,
                            recent_tags = excluded.recent_tags,
                            denied_count = excluded.denied_count,
                            approval_count = excluded.approval_count,
                            consecutive_deny_count = excluded.consecutive_deny_count,
                            updated_at = excluded.updated_at
                        """,
                        (
                            profile["session_id"],
                            profile["cumulative_risk_score"],
                            json.dumps(profile["recent_tags"], ensure_ascii=False),
                            profile["denied_count"],
                            profile["approval_count"],
                            profile["consecutive_deny_count"],
                            profile.get("updated_at", _utc_now().isoformat()),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"更新风险画像失败: {exc}") from exc

    def iter_all_risk_events(self) -> list[dict[str, Any]]:
        """用于迁移。"""
        return self.load_risk_events()

    # ------------------------------------------------------------------
    # Budget ledger
    # ------------------------------------------------------------------

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        """幂等设置任务预算上限，保留现有 reserved/committed。"""
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO budget_ledger (task_id, max_budget_token, reserved, committed)
                        VALUES (?, ?, 0, 0)
                        ON CONFLICT(task_id) DO UPDATE SET
                            max_budget_token = excluded.max_budget_token
                        """,
                        (task_id, max_budget_token),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"设置预算失败: {exc}") from exc

    def check_and_reserve(
        self, task_id: str, token_count: int, default_max_budget_token: int
    ) -> bool:
        """原子预留预算；超限返回 ``False``。"""
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO budget_ledger
                            (task_id, max_budget_token, reserved, committed)
                        VALUES (?, ?, 0, 0)
                        """,
                        (task_id, default_max_budget_token),
                    )
                    cur = conn.execute(
                        """
                        UPDATE budget_ledger
                        SET reserved = reserved + ?
                        WHERE task_id = ?
                          AND reserved + committed + ? <= max_budget_token
                        """,
                        (token_count, task_id, token_count),
                    )
                    return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"预留预算失败: {exc}") from exc

    def commit_budget(self, task_id: str, token_count: int) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        UPDATE budget_ledger
                        SET reserved = reserved - ?, committed = committed + ?
                        WHERE task_id = ?
                        """,
                        (token_count, token_count, task_id),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"提交预算失败: {exc}") from exc

    def refund_budget(self, task_id: str, token_count: int) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        "UPDATE budget_ledger SET reserved = MAX(0, reserved - ?) WHERE task_id = ?",
                        (token_count, task_id),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"返还预算失败: {exc}") from exc

    def iter_reserved_budget(self) -> list[tuple[str, int]]:
        """返回 ``reserved > 0`` 的 ``(task_id, reserved)``；用于启动孤儿预留告警。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT task_id, reserved FROM budget_ledger WHERE reserved > 0"
                )
                return [(row["task_id"], row["reserved"]) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举预留预算失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Budget reservation
    # ------------------------------------------------------------------

    @staticmethod
    def _reservation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "task_id": row["task_id"],
            "call_id": row["call_id"],
            "tool_name": row["tool_name"],
            "cost": {
                "token_count": row["cost_token_count"],
                "payment_amount": row["cost_payment_amount"],
                "currency": row["cost_currency"],
            },
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def save_reservation(self, reservation: dict[str, Any]) -> None:
        """保存/流转预留；已终态且对象不等时 fail-closed。"""
        reservation_id = reservation["reservation_id"]
        cost = reservation["cost"]
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                        (reservation_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        existing = self._reservation_row_to_dict(row)
                        if existing["state"] in _TERMINAL_RESERVATION_STATES:
                            if existing == reservation:
                                return
                            raise StateDatabaseError(
                                f"reservation {reservation_id} 已处于终态 {existing['state']}"
                            )
                    conn.execute(
                        """
                        INSERT INTO budget_reservations (
                            reservation_id, task_id, call_id, tool_name,
                            cost_token_count, cost_payment_amount, cost_currency,
                            state, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(reservation_id) DO UPDATE SET
                            task_id = excluded.task_id,
                            call_id = excluded.call_id,
                            tool_name = excluded.tool_name,
                            cost_token_count = excluded.cost_token_count,
                            cost_payment_amount = excluded.cost_payment_amount,
                            cost_currency = excluded.cost_currency,
                            state = excluded.state,
                            created_at = excluded.created_at,
                            expires_at = excluded.expires_at
                        """,
                        (
                            reservation_id,
                            reservation["task_id"],
                            reservation["call_id"],
                            reservation["tool_name"],
                            cost["token_count"],
                            cost["payment_amount"],
                            cost["currency"],
                            reservation["state"],
                            reservation["created_at"],
                            reservation["expires_at"],
                        ),
                    )
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存预留失败: {exc}") from exc

    def get_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                row = cur.fetchone()
                return self._reservation_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询预留失败: {exc}") from exc

    def get_reservation_by_call_id(self, call_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM budget_reservations WHERE call_id = ?",
                    (call_id,),
                )
                row = cur.fetchone()
                return self._reservation_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询预留失败: {exc}") from exc

    def list_reservations_by_task(self, task_id: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM budget_reservations WHERE task_id = ?",
                    (task_id,),
                )
                return [self._reservation_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举预留失败: {exc}") from exc

    def list_all_reservations(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT * FROM budget_reservations")
                return [self._reservation_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举预留失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Authority token
    # ------------------------------------------------------------------

    @staticmethod
    def _authority_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "token_id": row["token_id"],
            "request_id": row["request_id"],
            "agent_id": row["agent_id"],
            "task_id": row["task_id"],
            "granted_capabilities": json.loads(row["granted_capabilities_json"]),
            "budget": {
                "token_count": row["budget_token_count"],
                "payment_amount": row["budget_payment_amount"],
                "currency": row["budget_currency"],
            },
            "remaining_budget": {
                "token_count": row["remaining_token_count"],
                "payment_amount": row["remaining_payment_amount"],
                "currency": row["remaining_currency"],
            },
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
            "audit_record_id": row["audit_record_id"],
        }

    @staticmethod
    def _authority_params(token: dict[str, Any]) -> tuple[Any, ...]:
        budget = token["budget"]
        remaining = token["remaining_budget"]
        return (
            token["token_id"],
            token["request_id"],
            token["agent_id"],
            token["task_id"],
            json.dumps(token["granted_capabilities"], ensure_ascii=False),
            budget["token_count"],
            budget["payment_amount"],
            budget["currency"],
            remaining["token_count"],
            remaining["payment_amount"],
            remaining["currency"],
            token["expires_at"],
            token["created_at"],
            token["revoked_at"],
            token["audit_record_id"],
        )

    def save_authority_token(self, token: dict[str, Any], event_type: str) -> None:
        """保存/更新 token；对齐 JSONL 的严格 fail-closed 校验。"""
        token_id = token["token_id"]
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT * FROM authority_tokens WHERE token_id = ?",
                        (token_id,),
                    )
                    row = cur.fetchone()
                    existing = self._authority_row_to_dict(row) if row is not None else None
                    if event_type == "token_created" and existing is not None:
                        if existing == token:
                            return
                        raise StateDatabaseError(f"token {token_id} 已存在")
                    if event_type != "token_created" and existing is None:
                        raise StateDatabaseError(f"token {token_id} 不存在")
                    if (
                        existing is not None
                        and existing["revoked_at"] is not None
                        and existing != token
                    ):
                        raise StateDatabaseError(f"token {token_id} 已撤销")
                    if event_type == "token_used" and existing is not None:
                        if (
                            token["remaining_budget"]["token_count"]
                            >= existing["remaining_budget"]["token_count"]
                        ):
                            raise StateDatabaseError(f"token {token_id} 消费状态冲突")
                    if event_type == "token_refunded" and existing is not None:
                        if (
                            token["remaining_budget"]["token_count"]
                            <= existing["remaining_budget"]["token_count"]
                            or token["remaining_budget"]["token_count"]
                            > token["budget"]["token_count"]
                        ):
                            raise StateDatabaseError(f"token {token_id} 返还状态冲突")
                    conn.execute(
                        """
                        INSERT INTO authority_tokens (
                            token_id, request_id, agent_id, task_id,
                            granted_capabilities_json,
                            budget_token_count, budget_payment_amount, budget_currency,
                            remaining_token_count, remaining_payment_amount, remaining_currency,
                            expires_at, created_at, revoked_at, audit_record_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(token_id) DO UPDATE SET
                            request_id = excluded.request_id,
                            agent_id = excluded.agent_id,
                            task_id = excluded.task_id,
                            granted_capabilities_json = excluded.granted_capabilities_json,
                            budget_token_count = excluded.budget_token_count,
                            budget_payment_amount = excluded.budget_payment_amount,
                            budget_currency = excluded.budget_currency,
                            remaining_token_count = excluded.remaining_token_count,
                            remaining_payment_amount = excluded.remaining_payment_amount,
                            remaining_currency = excluded.remaining_currency,
                            expires_at = excluded.expires_at,
                            created_at = excluded.created_at,
                            revoked_at = excluded.revoked_at,
                            audit_record_id = excluded.audit_record_id
                        """,
                        self._authority_params(token),
                    )
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存权限令牌失败: {exc}") from exc

    def create_authority_token_if_available(
        self, token: dict[str, Any], now: datetime
    ) -> bool:
        """能力交集为空时创建 token；否则返回 ``False``。"""
        task_id = token["task_id"]
        requested = set(token["granted_capabilities"])
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT granted_capabilities_json, revoked_at, expires_at "
                        "FROM authority_tokens WHERE task_id = ?",
                        (task_id,),
                    )
                    for row in cur.fetchall():
                        if row["revoked_at"] is None and now < datetime.fromisoformat(
                            row["expires_at"]
                        ):
                            caps = set(json.loads(row["granted_capabilities_json"]))
                            if requested.intersection(caps):
                                return False
                    conn.execute(
                        """
                        INSERT INTO authority_tokens (
                            token_id, request_id, agent_id, task_id,
                            granted_capabilities_json,
                            budget_token_count, budget_payment_amount, budget_currency,
                            remaining_token_count, remaining_payment_amount, remaining_currency,
                            expires_at, created_at, revoked_at, audit_record_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        self._authority_params(token),
                    )
                    return True
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"创建权限令牌失败: {exc}") from exc

    def get_authority_token(self, token_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM authority_tokens WHERE token_id = ?",
                    (token_id,),
                )
                row = cur.fetchone()
                return self._authority_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询权限令牌失败: {exc}") from exc

    def list_all_authority_tokens(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT * FROM authority_tokens")
                return [self._authority_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举权限令牌失败: {exc}") from exc

    def list_active_authority_tokens(self, now: datetime) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM authority_tokens WHERE revoked_at IS NULL AND expires_at > ?",
                    (now.isoformat(),),
                )
                return [self._authority_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举活跃权限令牌失败: {exc}") from exc

    def list_authority_tokens_by_task(self, task_id: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM authority_tokens WHERE task_id = ?",
                    (task_id,),
                )
                return [self._authority_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举权限令牌失败: {exc}") from exc

    def consume_authority_token(
        self,
        token_id: str,
        cost_token_count: int,
        now: datetime,
        task_id: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        """原子消费 token 预算；无效/余额不足返回 ``None``。"""
        try:
            with self._connect() as conn:
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE authority_tokens
                        SET remaining_token_count = remaining_token_count - ?
                        WHERE token_id = ?
                          AND task_id = ?
                          AND agent_id = ?
                          AND revoked_at IS NULL
                          AND expires_at > ?
                          AND remaining_token_count >= ?
                        """,
                        (
                            cost_token_count,
                            token_id,
                            task_id,
                            agent_id,
                            now.isoformat(),
                            cost_token_count,
                        ),
                    )
                    if cur.rowcount != 1:
                        return None
                    row = conn.execute(
                        "SELECT * FROM authority_tokens WHERE token_id = ?",
                        (token_id,),
                    ).fetchone()
                    return self._authority_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"消费权限令牌失败: {exc}") from exc

    def refund_authority_token(
        self, token_id: str, expected_remaining: int, cost_token_count: int
    ) -> dict[str, Any] | None:
        """CAS 返还 token 预算；``remaining_token_count`` 不匹配或超上限返回 ``None``。"""
        try:
            with self._connect() as conn:
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE authority_tokens
                        SET remaining_token_count = remaining_token_count + ?
                        WHERE token_id = ?
                          AND remaining_token_count = ?
                          AND revoked_at IS NULL
                          AND remaining_token_count + ? <= budget_token_count
                        """,
                        (cost_token_count, token_id, expected_remaining, cost_token_count),
                    )
                    if cur.rowcount != 1:
                        return None
                    row = conn.execute(
                        "SELECT * FROM authority_tokens WHERE token_id = ?",
                        (token_id,),
                    ).fetchone()
                    return self._authority_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"返还权限令牌失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    @staticmethod
    def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "agent_id": row["agent_id"],
            "description": row["description"],
            "tenant_id": row["tenant_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    def save_task(self, task: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, session_id, user_id, agent_id, description,
                            tenant_id, status, created_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            session_id = excluded.session_id,
                            user_id = excluded.user_id,
                            agent_id = excluded.agent_id,
                            description = excluded.description,
                            tenant_id = excluded.tenant_id,
                            status = excluded.status,
                            created_at = excluded.created_at,
                            completed_at = excluded.completed_at
                        """,
                        (
                            task["task_id"],
                            task["session_id"],
                            task["user_id"],
                            task["agent_id"],
                            task["description"],
                            task["tenant_id"],
                            task["status"],
                            task["created_at"],
                            task["completed_at"],
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存任务失败: {exc}") from exc

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """读取任务；``completed`` 状态返回 ``None``（对齐 JSONL 语义）。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (task_id,),
                )
                row = cur.fetchone()
                if row is None or row["status"] == "completed":
                    return None
                return self._task_row_to_dict(row)
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询任务失败: {exc}") from exc

    def complete_task(self, task_id: str) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        "UPDATE tasks SET status = 'completed', completed_at = ? WHERE task_id = ?",
                        (_utc_now().isoformat(), task_id),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"完成任务失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Alert / Report
    # ------------------------------------------------------------------

    @staticmethod
    def _alert_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "alert_id": row["alert_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "rule_id": row["rule_id"],
            "severity": row["severity"],
            "title": row["title"],
            "description": row["description"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _report_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "report_id": row["report_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "generated_at": row["generated_at"],
            "summary": row["summary"],
            "alert_ids": json.loads(row["alert_ids_json"] or "[]"),
            "event_count": row["event_count"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def save_alert(self, alert: dict[str, Any]) -> None:
        """保存/覆盖告警；``alert_id`` 为主键，幂等覆盖。"""
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO alerts (
                            alert_id, session_id, task_id, rule_id, severity,
                            title, description, evidence_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(alert_id) DO UPDATE SET
                            session_id = excluded.session_id,
                            task_id = excluded.task_id,
                            rule_id = excluded.rule_id,
                            severity = excluded.severity,
                            title = excluded.title,
                            description = excluded.description,
                            evidence_json = excluded.evidence_json,
                            created_at = excluded.created_at
                        """,
                        (
                            alert["alert_id"],
                            alert["session_id"],
                            alert["task_id"],
                            alert["rule_id"],
                            alert["severity"],
                            alert["title"],
                            alert["description"],
                            json.dumps(alert["evidence"], ensure_ascii=False),
                            alert["created_at"],
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存告警失败: {exc}") from exc

    def list_alerts(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        """按可选 ``session_id`` / ``task_id`` 过滤告警。"""
        try:
            with self._connect() as conn:
                clauses: list[str] = []
                params: list[Any] = []
                if session_id is not None:
                    clauses.append("session_id = ?")
                    params.append(session_id)
                if task_id is not None:
                    clauses.append("task_id = ?")
                    params.append(task_id)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                cur = conn.execute("SELECT * FROM alerts" + where, tuple(params))
                return [self._alert_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举告警失败: {exc}") from exc

    def save_report(self, report: dict[str, Any]) -> None:
        """保存/覆盖报告；``report_id`` 为主键，幂等覆盖。"""
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO reports (
                            report_id, session_id, task_id, generated_at, summary,
                            alert_ids_json, event_count, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(report_id) DO UPDATE SET
                            session_id = excluded.session_id,
                            task_id = excluded.task_id,
                            generated_at = excluded.generated_at,
                            summary = excluded.summary,
                            alert_ids_json = excluded.alert_ids_json,
                            event_count = excluded.event_count,
                            metadata_json = excluded.metadata_json
                        """,
                        (
                            report["report_id"],
                            report["session_id"],
                            report["task_id"],
                            report["generated_at"],
                            report["summary"],
                            json.dumps(report["alert_ids"], ensure_ascii=False),
                            report["event_count"],
                            json.dumps(report["metadata"], ensure_ascii=False),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存报告失败: {exc}") from exc

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM reports WHERE report_id = ?",
                    (report_id,),
                )
                row = cur.fetchone()
                return self._report_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询报告失败: {exc}") from exc

    def list_reports(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        """按可选 ``session_id`` / ``task_id`` 过滤报告。"""
        try:
            with self._connect() as conn:
                clauses: list[str] = []
                params: list[Any] = []
                if session_id is not None:
                    clauses.append("session_id = ?")
                    params.append(session_id)
                if task_id is not None:
                    clauses.append("task_id = ?")
                    params.append(task_id)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                cur = conn.execute("SELECT * FROM reports" + where, tuple(params))
                return [self._report_row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举报告失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @staticmethod
    def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "agent_id": row["agent_id"],
            "created_at": row["created_at"],
            "last_task_at": row["last_task_at"],
            "active": bool(row["active"]),
        }

    def save_session(self, session: dict[str, Any]) -> None:
        """保存/更新 session；拒绝已关闭 session 的重新激活。"""
        session_id = session["session_id"]
        active = 1 if session["active"] else 0
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT active FROM sessions WHERE session_id = ?",
                        (session_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and not row["active"] and active:
                        raise StateDatabaseError(f"session {session_id} 已结束")
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            session_id, user_id, agent_id, created_at, last_task_at, active
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            user_id = excluded.user_id,
                            agent_id = excluded.agent_id,
                            created_at = excluded.created_at,
                            last_task_at = excluded.last_task_at,
                            active = excluded.active
                        """,
                        (
                            session_id,
                            session["user_id"],
                            session["agent_id"],
                            session["created_at"],
                            session["last_task_at"],
                            active,
                        ),
                    )
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存 session 失败: {exc}") from exc

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                return self._session_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 session 失败: {exc}") from exc

    def get_active_session(self, user_id: str, agent_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE user_id = ? AND agent_id = ? AND active = 1 "
                    "ORDER BY rowid DESC LIMIT 1",
                    (user_id, agent_id),
                )
                row = cur.fetchone()
                return self._session_row_to_dict(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询活跃 session 失败: {exc}") from exc

    def touch_session(
        self,
        session_id: str,
        last_task_at: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT * FROM sessions WHERE session_id = ?",
                        (session_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise StateDatabaseError(f"session {session_id} 不存在")
                    if not row["active"]:
                        raise StateDatabaseError(f"session {session_id} 已结束")
                    if user_id is not None and (
                        row["user_id"] != user_id or row["agent_id"] != agent_id
                    ):
                        raise StateDatabaseError(
                            f"session {session_id} 绑定 ({row['user_id']}, {row['agent_id']}) "
                            f"与 task 的 ({user_id}, {agent_id}) 不一致"
                        )
                    conn.execute(
                        "UPDATE sessions SET last_task_at = ? WHERE session_id = ?",
                        (last_task_at, session_id),
                    )
                    updated = conn.execute(
                        "SELECT * FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    return self._session_row_to_dict(updated)
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"刷新 session 失败: {exc}") from exc

    def close_session(self, session_id: str) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT * FROM sessions WHERE session_id = ?",
                        (session_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise StateDatabaseError(f"session {session_id} 不存在")
                    if row["active"]:
                        conn.execute(
                            "UPDATE sessions SET active = 0 WHERE session_id = ?",
                            (session_id,),
                        )
                    updated = conn.execute(
                        "SELECT * FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    return self._session_row_to_dict(updated)
        except StateDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"关闭 session 失败: {exc}") from exc

    def get_or_create_active_session(
        self,
        user_id: str,
        agent_id: str,
        now_iso: str,
        timeout_seconds: float,
        new_session: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "SELECT * FROM sessions WHERE user_id = ? AND agent_id = ? AND active = 1 "
                        "ORDER BY rowid DESC LIMIT 1",
                        (user_id, agent_id),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        last_task_at = datetime.fromisoformat(row["last_task_at"])
                        now = datetime.fromisoformat(now_iso)
                        if (now - last_task_at).total_seconds() <= timeout_seconds:
                            return self._session_row_to_dict(row)
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            session_id, user_id, agent_id, created_at, last_task_at, active
                        ) VALUES (?, ?, ?, ?, ?, 1)
                        """,
                        (
                            new_session["session_id"],
                            user_id,
                            agent_id,
                            new_session["created_at"],
                            new_session["last_task_at"],
                        ),
                    )
                    return dict(new_session)
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"创建或复用 session 失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------

    @staticmethod
    def _conversation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }

    def save_message(self, message: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO conversations (
                            message_id, session_id, task_id, role, content, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message["message_id"],
                            message["session_id"],
                            message["task_id"],
                            message["role"],
                            message["content"],
                            message["created_at"],
                        ),
                    )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"保存会话消息失败: {exc}") from exc

    def list_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        """返回指定 session 的最近 ``limit`` 条消息（按插入顺序）。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE session_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                )
                rows = list(cur.fetchall())
                rows.reverse()
                return [self._conversation_row_to_dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举会话消息失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Approval / notification outbox
    # ------------------------------------------------------------------

    def submit_approval_request_and_enqueue_notification(
        self,
        *,
        decision_id: str,
        request_id: str,
        request_json: str,
        request_hash: str,
        created_at: str,
        delivery_id: str | None,
        destination: str | None,
        payload_json: str | None,
        tenant_id: str | None = None,
    ) -> None:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    row = conn.execute(
                        "SELECT request_id, request_hash FROM approval_requests WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()
                    if row is not None:
                        if row["request_id"] == request_id and row["request_hash"] == request_hash:
                            return
                        raise StateDatabaseError(
                            f"decision {decision_id} 或 request {request_id} 已存在且内容冲突"
                        )
                    conn.execute(
                        "INSERT INTO approval_requests "
                        "(decision_id, request_id, request_json, request_hash, status, created_at, tenant_id) "
                        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                        (decision_id, request_id, request_json, request_hash, created_at, tenant_id),
                    )
                    conn.execute(
                        "INSERT INTO approval_events "
                        "(event_type, decision_id, tenant_id, created_at) VALUES ('pending', ?, ?, ?)",
                        (decision_id, tenant_id, created_at),
                    )
                    conn.execute(
                        "DELETE FROM approval_events WHERE event_id <= "
                        "(SELECT COALESCE(MAX(event_id), 0) - 10000 FROM approval_events)"
                    )
                    if delivery_id is not None:
                        conn.execute(
                            "INSERT INTO approval_notification_outbox "
                            "(delivery_id, request_id, decision_id, event_type, destination, "
                            "payload_json, next_attempt_at, created_at) "
                            "VALUES (?, ?, ?, 'created', ?, ?, ?, ?)",
                            (
                                delivery_id,
                                request_id,
                                decision_id,
                                destination,
                                payload_json,
                                created_at,
                                created_at,
                            ),
                        )
        except StateDatabaseError:
            raise
        except sqlite3.IntegrityError as exc:
            raise StateDatabaseError(f"审批请求已存在且内容冲突: {exc}") from exc
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"提交审批请求失败: {exc}") from exc

    def record_approval_response_and_enqueue_notification(
        self,
        *,
        decision_id: str,
        request_id: str,
        response_json: str,
        status: str,
        decided_at: str,
        delivery_id: str | None,
        destination: str | None,
        payload_json: str | None,
    ) -> None:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    request = conn.execute(
                        "SELECT request_id, tenant_id FROM approval_requests WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()
                    if request is None or request["request_id"] != request_id:
                        raise StateDatabaseError(f"decision {decision_id} 的审批请求不存在或不匹配")
                    existing = conn.execute(
                        "SELECT response_json FROM approval_responses WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing["response_json"] == response_json:
                            return
                        raise StateDatabaseError(
                            f"decision {decision_id} 已有审批结果，不允许覆盖"
                        )
                    conn.execute(
                        "INSERT INTO approval_responses "
                        "(decision_id, request_id, response_json, decided_at) VALUES (?, ?, ?, ?)",
                        (decision_id, request_id, response_json, decided_at),
                    )
                    conn.execute(
                        "UPDATE approval_requests SET status = ? WHERE decision_id = ?",
                        (status, decision_id),
                    )
                    conn.execute(
                        "INSERT INTO approval_events "
                        "(event_type, decision_id, tenant_id, created_at) VALUES ('decided', ?, ?, ?)",
                        (decision_id, request["tenant_id"], decided_at),
                    )
                    conn.execute(
                        "DELETE FROM approval_events WHERE event_id <= "
                        "(SELECT COALESCE(MAX(event_id), 0) - 10000 FROM approval_events)"
                    )
                    if delivery_id is not None:
                        conn.execute(
                            "INSERT INTO approval_notification_outbox "
                            "(delivery_id, request_id, decision_id, event_type, destination, "
                            "payload_json, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                delivery_id,
                                request_id,
                                decision_id,
                                status,
                                destination,
                                payload_json,
                                decided_at,
                                decided_at,
                            ),
                        )
        except StateDatabaseError:
            raise
        except sqlite3.IntegrityError as exc:
            raise StateDatabaseError(f"审批响应已存在且内容冲突: {exc}") from exc
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"记录审批响应失败: {exc}") from exc

    def get_approval_request_json(self, decision_id: str) -> str | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT request_json FROM approval_requests WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                return row["request_json"] if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询审批请求失败: {exc}") from exc

    def get_approval_request_json_by_id(self, request_id: str) -> str | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT request_json FROM approval_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                return row["request_json"] if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询审批请求失败: {exc}") from exc

    def list_pending_approval_request_json(self) -> list[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT request_json FROM approval_requests WHERE status = 'pending' ORDER BY rowid"
                ).fetchall()
                return [row["request_json"] for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举待审批请求失败: {exc}") from exc

    def list_recent_approval_json(self, limit: int) -> list[dict[str, str | None]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT r.request_json, p.response_json "
                    "FROM approval_requests AS r "
                    "LEFT JOIN approval_responses AS p ON p.decision_id = r.decision_id "
                    "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举最近审批请求失败: {exc}") from exc

    def get_approval_response_json(self, decision_id: str) -> str | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT response_json FROM approval_responses WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                return row["response_json"] if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询审批响应失败: {exc}") from exc

    def list_approval_history_json(
        self, *, tenant_id: str | None, all_tenants: bool
    ) -> list[dict[str, str | None]]:
        try:
            with self._connect() as conn:
                where = "" if all_tenants else "WHERE r.tenant_id IS ?"
                params: tuple[Any, ...] = () if all_tenants else (tenant_id,)
                rows = conn.execute(
                    "SELECT r.request_json, p.response_json FROM approval_requests AS r "
                    "LEFT JOIN approval_responses AS p ON p.decision_id = r.decision_id "
                    f"{where} ORDER BY r.created_at DESC, r.rowid DESC",
                    params,
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举审批历史失败: {exc}") from exc

    def list_approval_events(
        self, *, after_id: int, tenant_id: str | None, all_tenants: bool, limit: int
    ) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                scope = "" if all_tenants else "AND e.tenant_id IS ?"
                params: tuple[Any, ...] = (
                    (after_id, limit) if all_tenants else (after_id, tenant_id, limit)
                )
                rows = conn.execute(
                    "SELECT e.event_id, e.event_type, e.tenant_id, e.created_at, "
                    "r.request_json, p.response_json FROM approval_events AS e "
                    "JOIN approval_requests AS r ON r.decision_id = e.decision_id "
                    "LEFT JOIN approval_responses AS p ON p.decision_id = e.decision_id "
                    f"WHERE e.event_id > ? {scope} ORDER BY e.event_id LIMIT ?",
                    params,
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举审批事件失败: {exc}") from exc

    def approval_event_bounds(
        self, *, tenant_id: str | None, all_tenants: bool
    ) -> tuple[int | None, int]:
        try:
            with self._connect() as conn:
                where = "" if all_tenants else "WHERE tenant_id IS ?"
                params: tuple[Any, ...] = () if all_tenants else (tenant_id,)
                row = conn.execute(
                    "SELECT MIN(event_id) AS oldest, COALESCE(MAX(event_id), 0) AS latest "
                    f"FROM approval_events {where}",
                    params,
                ).fetchone()
                return row["oldest"], row["latest"]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询审批事件边界失败: {exc}") from exc

    def claim_approval_notifications(
        self,
        *,
        now: str,
        lease_until: str,
        claim_token: str,
        limit: int,
        destination: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    destination_sql = " AND destination = ?" if destination is not None else ""
                    params: tuple[Any, ...] = (
                        (now, now, destination, limit)
                        if destination is not None
                        else (now, now, limit)
                    )
                    rows = conn.execute(
                        "SELECT delivery_id FROM approval_notification_outbox "
                        "WHERE delivered_at IS NULL AND next_attempt_at <= ? "
                        "AND (lease_until IS NULL OR lease_until <= ?)"
                        f"{destination_sql} ORDER BY created_at, delivery_id LIMIT ?",
                        params,
                    ).fetchall()
                    ids = [row["delivery_id"] for row in rows]
                    if not ids:
                        return []
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE approval_notification_outbox SET claim_token = ?, lease_until = ?, "
                        f"attempts = attempts + 1 WHERE delivery_id IN ({placeholders})",
                        (claim_token, lease_until, *ids),
                    )
                    claimed = conn.execute(
                        f"SELECT * FROM approval_notification_outbox "
                        f"WHERE delivery_id IN ({placeholders}) ORDER BY created_at, delivery_id",
                        ids,
                    ).fetchall()
                    return [dict(row) for row in claimed]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"claim 审批通知失败: {exc}") from exc

    def ack_approval_notification(
        self, *, delivery_id: str, claim_token: str, delivered_at: str
    ) -> bool:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "UPDATE approval_notification_outbox SET delivered_at = ?, "
                        "claim_token = NULL, lease_until = NULL, last_error = NULL "
                        "WHERE delivery_id = ? AND claim_token = ? AND delivered_at IS NULL",
                        (delivered_at, delivery_id, claim_token),
                    )
                    return cur.rowcount == 1
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"ACK 审批通知失败: {exc}") from exc

    def fail_approval_notification(
        self,
        *,
        delivery_id: str,
        claim_token: str,
        next_attempt_at: str,
        last_error: str,
    ) -> bool:
        try:
            with self._connect() as conn:
                with self._immediate(conn):
                    cur = conn.execute(
                        "UPDATE approval_notification_outbox SET next_attempt_at = ?, "
                        "last_error = ?, claim_token = NULL, lease_until = NULL "
                        "WHERE delivery_id = ? AND claim_token = ? AND delivered_at IS NULL",
                        (next_attempt_at, last_error, delivery_id, claim_token),
                    )
                    return cur.rowcount == 1
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"重试审批通知失败: {exc}") from exc

    def list_approval_notifications(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM approval_notification_outbox ORDER BY created_at, delivery_id"
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"枚举审批通知失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------

    def close_wal(self) -> None:
        """在备份/迁移前将 WAL 落回主库。"""
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"WAL checkpoint 失败: {exc}") from exc
