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
"""


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
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"无法连接状态数据库 {self._db_path}: {exc}") from exc

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

    def init_schema(self) -> None:
        """初始化/校验 Schema；幂等。"""
        try:
            with self._connect() as conn:
                conn.executescript(SCHEMA)
            self._migrate_decisions_columns()
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
    # Migration helpers
    # ------------------------------------------------------------------

    def close_wal(self) -> None:
        """在备份/迁移前将 WAL 落回主库。"""
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"WAL checkpoint 失败: {exc}") from exc
