"""OPA 策略候选快照、确定性 artifact 与发布事务。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

from loop_controller.infra.state_db import StateDatabase, StateDatabaseError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SUFFIXES = {".rego", ".json"}
_BUNDLE_ROOTS = ("loop_controller",)
_BUNDLE_FORMAT = "opa-bundle-v1"
_ANY_TENANT = object()  # list_candidates 的"不过滤"哨兵


class PolicyDeliveryError(Exception):
    """策略交付核心错误。"""


class InvalidCandidateError(PolicyDeliveryError):
    pass


class CandidateLimitError(PolicyDeliveryError):
    pass


class CandidateStateError(PolicyDeliveryError):
    pass


class PolicyCASConflictError(PolicyDeliveryError):
    pass


class ArtifactConflictError(PolicyDeliveryError):
    pass


class SeparationOfDutiesError(PolicyDeliveryError):
    """双人复核（职责分离）不满足。"""


class CandidateState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    LOADED = "loaded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


_ALLOWED_TRANSITIONS = {
    CandidateState.DRAFT: {CandidateState.VALIDATED, CandidateState.FAILED},
    CandidateState.VALIDATED: {CandidateState.PUBLISHED},
    CandidateState.PUBLISHED: {
        CandidateState.LOADED,
        CandidateState.FAILED,
        CandidateState.SUPERSEDED,
    },
    CandidateState.LOADED: {CandidateState.SUPERSEDED},
    CandidateState.FAILED: {CandidateState.SUPERSEDED},
    CandidateState.SUPERSEDED: set(),
}


def check_transition(current: CandidateState | str, target: CandidateState | str) -> None:
    current_state = CandidateState(current)
    target_state = CandidateState(target)
    if target_state not in _ALLOWED_TRANSITIONS[current_state]:
        raise CandidateStateError(f"非法 candidate 状态迁移: {current_state} -> {target_state}")


@dataclass(frozen=True)
class CandidateLimits:
    max_files: int = 128
    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 8_388_608

    def __post_init__(self) -> None:
        if self.max_files <= 0 or self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("candidate 限制必须为正整数")


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    state: CandidateState
    base_revision: str | None
    source_manifest: tuple[SourceFile, ...]
    source_sha256: str
    revision: str | None
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_size: int | None
    artifact_ready: bool
    created_by: str
    created_at: str
    tenant_id: str | None = None  # v0.52：candidate 归属租户（creator 租户）
    source_candidate_id: str | None = None
    rollback_of_revision: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class BundleArtifact:
    revision: str
    path: Path
    sha256: str
    size: int
    data: bytes | None = None


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_revision(value: str | None, *, field: str) -> None:
    if value is not None and not _SHA256_RE.fullmatch(value):
        raise InvalidCandidateError(f"{field} 必须是完整 64 位小写 SHA-256")


def _validate_source_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise InvalidCandidateError("candidate 路径必须是非空 POSIX 相对路径")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise InvalidCandidateError(f"禁止绝对路径: {raw!r}")
    path = PurePosixPath(raw)
    parts = path.parts
    if not parts or str(path) != raw or any(part in {"", ".", ".."} for part in parts):
        raise InvalidCandidateError(f"路径未规范化或包含逃逸段: {raw!r}")
    if any(part.startswith(".") for part in parts):
        raise InvalidCandidateError(f"禁止隐藏文件或目录: {raw!r}")
    if path.suffix not in _ALLOWED_SUFFIXES or path.name == ".manifest":
        raise InvalidCandidateError(f"不允许的 candidate 文件类型: {raw!r}")
    return str(path)


def _manifest_bytes(files: tuple[SourceFile, ...]) -> bytes:
    return _canonical_json(
        [{"path": item.path, "sha256": item.sha256, "size": item.size} for item in files]
    )


def _logical_revision(files: tuple[SourceFile, ...], roots: tuple[str, ...]) -> str:
    value = bytearray()
    for item in files:
        value.extend(item.path.encode("utf-8"))
        value.extend(b"\0")
        value.extend(str(item.size).encode("ascii"))
        value.extend(b"\0")
        value.extend(item.sha256.encode("ascii"))
        value.extend(b"\n")
    value.extend(_canonical_json({"bundle_format": _BUNDLE_FORMAT, "roots": list(roots)}))
    return _sha256(bytes(value))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


class PolicyDeliveryStore:
    """以 StateDatabase 为事实源的策略交付存储。"""

    def __init__(self, database: StateDatabase) -> None:
        self._db = database

    @staticmethod
    def _candidate(row: sqlite3.Row) -> Candidate:
        manifest = tuple(SourceFile(**item) for item in json.loads(row["source_manifest_json"]))
        return Candidate(
            candidate_id=row["candidate_id"],
            state=CandidateState(row["state"]),
            base_revision=row["base_revision"],
            source_manifest=manifest,
            source_sha256=row["source_sha256"],
            revision=row["revision"],
            artifact_path=row["artifact_path"],
            artifact_sha256=row["artifact_sha256"],
            artifact_size=row["artifact_size"],
            artifact_ready=bool(row["artifact_ready"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            tenant_id=row["tenant_id"] if "tenant_id" in row.keys() else None,
            source_candidate_id=row["source_candidate_id"],
            rollback_of_revision=row["rollback_of_revision"],
            published_at=row["published_at"],
        )

    def list_candidates(
        self, *, limit: int = 100, tenant_id: str | None | object = _ANY_TENANT
    ) -> list[Candidate]:
        """列出 candidate；tenant_id 为 _ANY_TENANT 表示不过滤（platform_admin 视角）。

        显式传入 None 表示只查平台级（tenant_id IS NULL）；传入字符串按归属过滤。
        """
        try:
            with self._db._connect() as conn:
                if tenant_id is _ANY_TENANT:
                    rows = conn.execute(
                        "SELECT * FROM policy_candidates ORDER BY created_at DESC LIMIT ?", (limit,)
                    ).fetchall()
                elif tenant_id is None:
                    rows = conn.execute(
                        "SELECT * FROM policy_candidates WHERE tenant_id IS NULL"
                        " ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM policy_candidates WHERE tenant_id = ?"
                        " ORDER BY created_at DESC LIMIT ?",
                        (str(tenant_id), limit),
                    ).fetchall()
                return [self._candidate(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 policy candidates 失败: {exc}") from exc

    def list_audit(
        self, *, limit: int = 100, tenant_id: str | None | object = _ANY_TENANT
    ) -> list[dict[str, Any]]:
        """列出策略变更审计；tenant_id 语义同 list_candidates（经 candidate join 过滤）。"""
        try:
            with self._db._connect() as conn:
                if tenant_id is _ANY_TENANT:
                    rows = conn.execute(
                        "SELECT * FROM policy_change_audit ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                elif tenant_id is None:
                    rows = conn.execute(
                        """SELECT a.* FROM policy_change_audit a
                           LEFT JOIN policy_candidates c ON a.candidate_id = c.candidate_id
                           WHERE c.candidate_id IS NULL OR c.tenant_id IS NULL
                           ORDER BY a.created_at DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT a.* FROM policy_change_audit a
                           JOIN policy_candidates c ON a.candidate_id = c.candidate_id
                           WHERE c.tenant_id = ? ORDER BY a.created_at DESC LIMIT ?""",
                        (str(tenant_id), limit),
                    ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 policy audit 失败: {exc}") from exc

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        try:
            with self._db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM policy_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                return self._candidate(row) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 policy candidate 失败: {exc}") from exc

    def current(self) -> dict[str, Any]:
        try:
            with self._db._connect() as conn:
                row = conn.execute("SELECT * FROM policy_current WHERE singleton = 1").fetchone()
                return dict(row)
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 policy current 失败: {exc}") from exc

    def insert_candidate(self, candidate: Candidate) -> None:
        manifest_json = _manifest_bytes(candidate.source_manifest).decode().rstrip("\n")
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                conn.execute(
                    """INSERT INTO policy_candidates (
                       candidate_id, revision, state, base_revision, source_manifest_json,
                       source_sha256, source_candidate_id, rollback_of_revision, artifact_path,
                       artifact_sha256, artifact_size, artifact_ready, created_by, created_at,
                       tenant_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.candidate_id, candidate.revision, candidate.state,
                        candidate.base_revision, manifest_json, candidate.source_sha256,
                        candidate.source_candidate_id, candidate.rollback_of_revision,
                        candidate.artifact_path, candidate.artifact_sha256, candidate.artifact_size,
                        int(candidate.artifact_ready), candidate.created_by, candidate.created_at,
                        candidate.tenant_id,
                    ),
                )
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"创建 policy candidate 失败: {exc}") from exc

    def validation(self, candidate_id: str) -> dict[str, Any] | None:
        try:
            with self._db._connect() as conn:
                row = conn.execute(
                    "SELECT result_summary_json FROM policy_validations WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                return json.loads(row["result_summary_json"]) if row else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 validation 失败: {exc}") from exc

    def record_validation(
        self, candidate_id: str, artifact: BundleArtifact | None, result: Any, *, actor: str
    ) -> Candidate:
        """在同一事务写入校验证据、状态迁移与 change audit。

        v0.52：记录触发者 `actor` 到 validated_by；`separation_ok` 在
        actor 与 created_by 不同时为 1（双人复核证据）。validate 审计事件
        的 actor 由 created_by 修正为实际触发者。
        """
        if not actor:
            raise CandidateStateError("validate actor 不能为空")
        summary = result.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        stages = {stage.stage: stage.ok for stage in result.stages}
        target = CandidateState.VALIDATED if result.ok else CandidateState.FAILED
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                row = conn.execute("SELECT * FROM policy_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                if row is None:
                    raise CandidateStateError("candidate 不存在")
                existing = conn.execute("SELECT result_sha256 FROM policy_validations WHERE candidate_id=?", (candidate_id,)).fetchone()
                if existing:
                    if existing["result_sha256"] != result.result_sha256:
                        raise CandidateStateError("candidate 已有不同 validation 证据")
                    return self._candidate(row)
                check_transition(row["state"], target)
                if result.ok and artifact is None:
                    raise CandidateStateError("成功 validation 缺少 artifact")
                separation_ok = int(actor != row["created_by"])
                conn.execute("""INSERT INTO policy_validations (validation_id,candidate_id,opa_version,
                    check_ok,test_ok,tool_default_deny_ok,interaction_default_deny_ok,result_sha256,
                    result_summary_json,started_at,finished_at,validated_by,separation_ok)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    uuid.uuid4().hex, candidate_id, result.opa_version, int(stages.get("check", False)),
                    int(stages.get("test", False)), int(result.package_default_deny.get("loop_controller.tool_permission", False)),
                    int(result.package_default_deny.get("loop_controller.interaction.delegation", False)),
                    result.result_sha256, json.dumps(summary, sort_keys=True, separators=(",", ":")), now, now,
                    actor, separation_ok))
                if result.ok:
                    assert artifact is not None
                    conn.execute("""UPDATE policy_candidates SET state='validated',revision=?,
                        artifact_path=?,artifact_sha256=?,artifact_size=?,artifact_ready=1,validated_at=? WHERE candidate_id=?""",
                        (artifact.revision, str(artifact.path), artifact.sha256, artifact.size, now, candidate_id))
                else:
                    conn.execute("""UPDATE policy_candidates SET state='failed',failure_stage='validation',
                        failure_code=?,failure_summary=? WHERE candidate_id=?""",
                        (result.failure_code, json.dumps(summary, sort_keys=True, separators=(",", ":"))[:512], candidate_id))
                conn.execute("""INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
                    base_revision,target_revision,source_sha256,artifact_sha256,result,detail_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, "validate", actor, candidate_id,
                    row["base_revision"], artifact.revision if artifact else None, row["source_sha256"],
                    artifact.sha256 if artifact else None, "success" if result.ok else "failed",
                    json.dumps({"failure_code": result.failure_code, "opa_version": result.opa_version,
                                "result_sha256": result.result_sha256}, sort_keys=True, separators=(",", ":")), now))
                updated = conn.execute("SELECT * FROM policy_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                return self._candidate(updated)
        except (PolicyDeliveryError, sqlite3.Error) as exc:
            if isinstance(exc, PolicyDeliveryError):
                raise
            raise StateDatabaseError(f"保存 validation 失败: {exc}") from exc

    def record_external_validation(
        self, candidate_id: str, artifact: BundleArtifact, *, actor: str
    ) -> Candidate:
        """记录外部（OPA 之外流程，如测试/灾备恢复）完成的校验证据。

        与 record_validation 同事务写入 validated_by / separation_ok / change audit，
        使双人复核证据链对外部校验路径同样成立。仅 draft candidate 可用。
        """
        if not actor:
            raise CandidateStateError("validate actor 不能为空")
        now = datetime.now(UTC).isoformat()
        summary = json.dumps({"external_validation": True}, sort_keys=True)
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                row = conn.execute(
                    "SELECT * FROM policy_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                if row is None:
                    raise CandidateStateError("candidate 不存在")
                existing = conn.execute(
                    "SELECT result_sha256 FROM policy_validations WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if existing:
                    if existing["result_sha256"] != artifact.sha256:
                        raise CandidateStateError("candidate 已有不同 validation 证据")
                    return self._candidate(row)
                check_transition(row["state"], CandidateState.VALIDATED)
                conn.execute(
                    """INSERT INTO policy_validations (validation_id,candidate_id,opa_version,
                       check_ok,test_ok,tool_default_deny_ok,interaction_default_deny_ok,result_sha256,
                       result_summary_json,started_at,finished_at,validated_by,separation_ok)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, candidate_id, None, 1, 1, 1, 1,
                     artifact.sha256, summary, now, now,
                     actor, int(actor != row["created_by"])),
                )
                conn.execute(
                    """UPDATE policy_candidates SET state = 'validated', revision = ?,
                       artifact_path = ?, artifact_sha256 = ?, artifact_size = ?, artifact_ready = 1,
                       validated_at = ? WHERE candidate_id = ?""",
                    (artifact.revision, str(artifact.path), artifact.sha256, artifact.size, now, candidate_id),
                )
                conn.execute(
                    """INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
                       base_revision,target_revision,source_sha256,artifact_sha256,result,detail_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, "validate", actor, candidate_id,
                     row["base_revision"], artifact.revision, row["source_sha256"],
                     artifact.sha256, "success", summary, now),
                )
                updated = conn.execute(
                    "SELECT * FROM policy_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                return self._candidate(updated)
        except (PolicyDeliveryError, sqlite3.Error) as exc:
            if isinstance(exc, PolicyDeliveryError):
                raise
            raise StateDatabaseError(f"记录外部校验证据失败: {exc}") from exc

    def transition(self, candidate_id: str, target: CandidateState) -> Candidate:
        column = {
            CandidateState.LOADED: "loaded_at",
            CandidateState.SUPERSEDED: "superseded_at",
        }.get(target)
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                row = conn.execute(
                    "SELECT state FROM policy_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                if row is None:
                    raise CandidateStateError("candidate 不存在")
                check_transition(row["state"], target)
                if column:
                    conn.execute(
                        f"UPDATE policy_candidates SET state = ?, {column} = ? WHERE candidate_id = ?",
                        (target, datetime.now(UTC).isoformat(), candidate_id),
                    )
                else:
                    conn.execute(
                        "UPDATE policy_candidates SET state = ? WHERE candidate_id = ?",
                        (target, candidate_id),
                    )
        except (CandidateStateError, sqlite3.Error) as exc:
            if isinstance(exc, CandidateStateError):
                raise
            raise StateDatabaseError(f"迁移 candidate 状态失败: {exc}") from exc
        result = self.get_candidate(candidate_id)
        assert result is not None
        return result

    @staticmethod
    def _check_separation(
        conn: sqlite3.Connection,
        candidate_id: str,
        created_by: str,
        *,
        waive_separation: bool,
        waiver_reason: str | None,
        actor: str,
        now: str,
    ) -> None:
        """双人复核检查：存在 separation_ok=1 且验证者 != 创建者的成功校验证据。

        waive 仅由 server 在 enforcer.can_waive_separation（platform_admin + 配置允许）
        通过后传入；waiver 理由必填并写 separation_waived 审计。
        """
        ok = conn.execute(
            """SELECT 1 FROM policy_validations
               WHERE candidate_id = ? AND separation_ok = 1 AND validated_by != ?
               LIMIT 1""",
            (candidate_id, created_by),
        ).fetchone()
        if ok is not None:
            return
        if waive_separation:
            if not waiver_reason or not waiver_reason.strip():
                raise CandidateStateError("separation waiver 必须提供理由")
            conn.execute(
                """INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
                   base_revision,target_revision,source_sha256,artifact_sha256,result,detail_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, "separation_waived", actor, candidate_id,
                 None, None, None, None, "success",
                 json.dumps({"waiver_reason": waiver_reason.strip()}, sort_keys=True), now),
            )
            return
        raise SeparationOfDutiesError(
            "双人复核不满足：缺少 separation_ok=1 且 validated_by != created_by 的校验证据"
        )

    def publish(
        self,
        candidate_id: str,
        base_revision: str | None,
        actor: str,
        *,
        enforce_separation: bool = True,
        waive_separation: bool = False,
        waiver_reason: str | None = None,
    ) -> dict[str, Any]:
        _validate_revision(base_revision, field="base_revision")
        now = datetime.now(UTC).isoformat()
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                current = conn.execute(
                    "SELECT * FROM policy_current WHERE singleton = 1"
                ).fetchone()
                candidate = conn.execute(
                    "SELECT * FROM policy_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                if candidate is None:
                    raise CandidateStateError("candidate 不存在")
                if current["revision"] != base_revision or candidate["base_revision"] != base_revision:
                    raise PolicyCASConflictError("base_revision 与 current 不匹配")
                if candidate["state"] != CandidateState.VALIDATED:
                    raise CandidateStateError("仅 validated candidate 可发布")
                if not candidate["artifact_ready"]:
                    raise CandidateStateError("artifact 尚未就绪")
                if enforce_separation:
                    self._check_separation(
                        conn, candidate_id, candidate["created_by"],
                        waive_separation=waive_separation, waiver_reason=waiver_reason,
                        actor=actor, now=now,
                    )
                artifact = Path(candidate["artifact_path"])
                if artifact.is_symlink() or not artifact.is_file():
                    raise ArtifactConflictError("artifact 文件不存在或类型非法")
                artifact_data = artifact.read_bytes()
                if (_sha256(artifact_data) != candidate["artifact_sha256"]
                        or len(artifact_data) != candidate["artifact_size"]):
                    raise ArtifactConflictError("artifact 与 SQLite 元数据不一致")
                old_id = current["candidate_id"]
                if old_id:
                    conn.execute(
                        """UPDATE policy_candidates SET state = 'superseded', superseded_at = ?
                           WHERE candidate_id = ? AND state IN ('published','loaded','failed')""",
                        (now, old_id),
                    )
                generation = current["generation"] + 1
                conn.execute(
                    """UPDATE policy_current SET revision = ?, candidate_id = ?, generation = ?,
                       updated_by = ?, updated_at = ? WHERE singleton = 1""",
                    (candidate["revision"], candidate_id, generation, actor, now),
                )
                conn.execute(
                    "UPDATE policy_candidates SET state = 'published', published_at = ? WHERE candidate_id = ?",
                    (now, candidate_id),
                )
                event_type = "rollback" if candidate["rollback_of_revision"] else "publish"
                conn.execute(
                    """INSERT INTO policy_change_audit (
                       audit_id, event_type, actor, candidate_id, base_revision, target_revision,
                       source_sha256, artifact_sha256, result, detail_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?)""",
                    (
                        uuid.uuid4().hex, event_type, actor, candidate_id, base_revision,
                        candidate["revision"], candidate["source_sha256"],
                        candidate["artifact_sha256"], json.dumps(
                            {"generation": generation,
                             "rollback_of_revision": candidate["rollback_of_revision"]},
                            sort_keys=True, separators=(",", ":"),
                        ), now,
                    ),
                )
                return {"revision": candidate["revision"], "generation": generation}
        except (PolicyDeliveryError, sqlite3.Error) as exc:
            if isinstance(exc, PolicyDeliveryError):
                raise
            raise StateDatabaseError(f"发布 policy candidate 失败: {exc}") from exc

    def rollback(
        self,
        historical_revision: str,
        base_revision: str | None,
        actor: str,
        *,
        enforce_separation: bool = True,
        waive_separation: bool = False,
        waiver_reason: str | None = None,
    ) -> tuple[Candidate, dict[str, Any]]:
        _validate_revision(historical_revision, field="historical_revision")
        _validate_revision(base_revision, field="base_revision")
        now = datetime.now(UTC).isoformat()
        candidate_id = uuid.uuid4().hex
        try:
            with self._db._connect() as conn, self._db._immediate(conn):
                current = conn.execute("SELECT * FROM policy_current WHERE singleton=1").fetchone()
                if current["revision"] != base_revision:
                    raise PolicyCASConflictError("base_revision 与 current 不匹配")
                row = conn.execute("""SELECT c.* FROM policy_candidates c
                    WHERE c.revision=? AND c.artifact_ready=1 AND EXISTS (
                        SELECT 1 FROM policy_change_audit a WHERE a.target_revision=c.revision
                        AND a.event_type='loaded' AND a.result='success')
                    ORDER BY c.loaded_at IS NULL, c.loaded_at LIMIT 1""", (historical_revision,)).fetchone()
                if row is None:
                    raise InvalidCandidateError("revision 从未成功 loaded")
                if enforce_separation:
                    self._check_separation(
                        conn, row["candidate_id"], row["created_by"],
                        waive_separation=waive_separation, waiver_reason=waiver_reason,
                        actor=actor, now=now,
                    )
                artifact = Path(row["artifact_path"])
                if artifact.is_symlink() or not artifact.is_file():
                    raise ArtifactConflictError("历史 artifact 类型非法")
                data = artifact.read_bytes()
                if _sha256(data) != row["artifact_sha256"] or len(data) != row["artifact_size"]:
                    raise ArtifactConflictError("历史 artifact 完整性校验失败")
                generation = current["generation"] + 1
                manifest = row["source_manifest_json"]
                conn.execute("""INSERT INTO policy_candidates (candidate_id,revision,state,base_revision,
                    source_manifest_json,source_sha256,source_candidate_id,rollback_of_revision,
                    artifact_path,artifact_sha256,artifact_size,artifact_ready,created_by,created_at,
                    validated_at,published_at,tenant_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    candidate_id, historical_revision, "published", base_revision, manifest,
                    row["source_sha256"], row["candidate_id"], historical_revision, row["artifact_path"],
                    row["artifact_sha256"], row["artifact_size"], 1, actor, now, now, now,
                    row["tenant_id"] if "tenant_id" in row.keys() else None))
                if current["candidate_id"]:
                    conn.execute("""UPDATE policy_candidates SET state='superseded',superseded_at=?
                        WHERE candidate_id=? AND state IN ('published','loaded','failed')""", (now, current["candidate_id"]))
                changed = conn.execute("""UPDATE policy_current SET revision=?,candidate_id=?,generation=?,
                    updated_by=?,updated_at=? WHERE singleton=1 AND revision IS ? AND generation=?""",
                    (historical_revision, candidate_id, generation, actor, now, base_revision, current["generation"])).rowcount
                if changed != 1:
                    raise PolicyCASConflictError("rollback CAS 冲突")
                conn.execute("""INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
                    base_revision,target_revision,source_sha256,artifact_sha256,result,detail_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, "rollback", actor, candidate_id,
                    base_revision, historical_revision, row["source_sha256"], row["artifact_sha256"], "success",
                    json.dumps({"generation": generation, "rollback_of_revision": historical_revision},
                               sort_keys=True, separators=(",", ":")), now))
                created = conn.execute("SELECT * FROM policy_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                return self._candidate(created), {"revision": historical_revision, "generation": generation}
        except (PolicyDeliveryError, sqlite3.Error) as exc:
            if isinstance(exc, PolicyDeliveryError):
                raise
            raise StateDatabaseError(f"rollback policy 失败: {exc}") from exc

    def create_rollback(
        self, historical_revision: str, base_revision: str | None, actor: str
    ) -> Candidate:
        raise CandidateStateError("请使用原子 rollback 方法")


class PolicyDelivery:
    """文件系统快照和 bundle 构建协调器。"""

    def __init__(
        self,
        data_dir: str | Path,
        database: StateDatabase,
        *,
        limits: CandidateLimits | None = None,
        roots: tuple[str, ...] = _BUNDLE_ROOTS,
    ) -> None:
        self.root = Path(data_dir) / "policy_delivery"
        self.candidates_dir = self.root / "candidates"
        self.artifacts_dir = self.root / "artifacts"
        self.tmp_dir = self.root / "tmp"
        for directory in (self.candidates_dir, self.artifacts_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = PolicyDeliveryStore(database)
        self.limits = limits or CandidateLimits()
        if not roots or any(not root or root.startswith("/") or ".." in root.split("/") for root in roots):
            raise ValueError("bundle roots 非法")
        self.roots = roots

    def create_candidate(
        self,
        files: Mapping[str, str | bytes],
        *,
        base_revision: str | None,
        actor: str,
        candidate_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Candidate:
        _validate_revision(base_revision, field="base_revision")
        if not actor:
            raise InvalidCandidateError("actor 不能为空")
        if not files:
            raise InvalidCandidateError("candidate 至少包含一个文件")
        if len(files) > self.limits.max_files:
            raise CandidateLimitError("candidate 文件数超限")
        normalized: dict[str, bytes] = {}
        total = 0
        for raw_path, raw_content in files.items():
            path = _validate_source_path(raw_path)
            if path in normalized:
                raise InvalidCandidateError(f"candidate 路径重复: {path}")
            if not isinstance(raw_content, (str, bytes)):
                raise InvalidCandidateError(f"文件内容类型非法: {path}")
            try:
                content = raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
                content.decode("utf-8")
                if path.endswith(".json"):
                    json.loads(content)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise InvalidCandidateError(f"文件不是合法 UTF-8/JSON: {path}") from exc
            if len(content) > self.limits.max_file_bytes:
                raise CandidateLimitError(f"单文件大小超限: {path}")
            total += len(content)
            if total > self.limits.max_total_bytes:
                raise CandidateLimitError("candidate 总大小超限")
            normalized[path] = content
        ordered = sorted(normalized, key=lambda item: item.encode("utf-8"))
        manifest = tuple(
            SourceFile(path=path, size=len(normalized[path]), sha256=_sha256(normalized[path]))
            for path in ordered
        )
        source_sha256 = _sha256(_manifest_bytes(manifest))
        candidate_id = candidate_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate_id):
            raise InvalidCandidateError("candidate_id 非法")
        temp = self.tmp_dir / f"candidate-{uuid.uuid4().hex}"
        final = self.candidates_dir / candidate_id / "source"
        if final.exists():
            raise InvalidCandidateError("candidate_id 已存在")
        temp.mkdir(mode=0o700)
        try:
            for path in ordered:
                destination = temp.joinpath(*PurePosixPath(path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as stream:
                    stream.write(normalized[path])
                    stream.flush()
                    os.fsync(stream.fileno())
            final.parent.mkdir(mode=0o700)
            os.replace(temp, final)
            _fsync_directory(final.parent)
            _make_read_only(final)
            candidate = Candidate(
                candidate_id=candidate_id,
                state=CandidateState.DRAFT,
                base_revision=base_revision,
                source_manifest=manifest,
                source_sha256=source_sha256,
                revision=None,
                artifact_path=None,
                artifact_sha256=None,
                artifact_size=None,
                artifact_ready=False,
                created_by=actor,
                created_at=datetime.now(UTC).isoformat(),
                tenant_id=tenant_id,
            )
            try:
                self.store.insert_candidate(candidate)
            except BaseException:
                # 仅删除本调用刚安装的随机/显式 identity；数据库写入成功后不会执行。
                if final.parent.name == candidate_id and final.exists():
                    for item in sorted(final.rglob("*"), reverse=True):
                        item.chmod(0o700)
                    final.chmod(0o700)
                    shutil.rmtree(final.parent)
                raise
            return candidate
        finally:
            if temp.exists():
                for item in sorted(temp.rglob("*"), reverse=True):
                    item.unlink() if item.is_file() else item.rmdir()
                temp.rmdir()

    def _source_bytes(self, candidate: Candidate) -> dict[str, bytes]:
        source = self.candidates_dir / candidate.candidate_id / "source"
        result: dict[str, bytes] = {}
        expected = {item.path: item for item in candidate.source_manifest}
        actual_paths: set[str] = set()
        for path in source.rglob("*"):
            mode = path.lstat().st_mode
            if path.is_symlink() or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise InvalidCandidateError("snapshot 包含链接或特殊文件")
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                actual_paths.add(relative)
                item = expected.get(relative)
                data = path.read_bytes()
                if item is None or len(data) != item.size or _sha256(data) != item.sha256:
                    raise InvalidCandidateError("snapshot 已被修改")
                result[relative] = data
        if actual_paths != set(expected):
            raise InvalidCandidateError("snapshot 文件清单不一致")
        return result

    def build_artifact(self, candidate_id: str) -> BundleArtifact:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise InvalidCandidateError("candidate 不存在")
        if candidate.state not in {CandidateState.DRAFT, CandidateState.VALIDATED}:
            raise CandidateStateError("当前状态不允许构建 artifact")
        files = self._source_bytes(candidate)
        revision = _logical_revision(candidate.source_manifest, self.roots)
        manifest_data = _canonical_json({"revision": revision, "roots": list(self.roots)})
        members = {**files, ".manifest": manifest_data}
        temp = self.tmp_dir / f"artifact-{uuid.uuid4().hex}.tar.gz"
        with temp.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as gz:
                with tarfile.open(
                    fileobj=cast(IO[bytes], gz), mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for name in sorted(members, key=lambda item: item.encode("utf-8")):
                        data = members[name]
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mode = 0o644
                        archive.addfile(info, io.BytesIO(data))
            raw.flush()
            os.fsync(raw.fileno())
        try:
            self._verify_archive(temp, candidate, revision, self.roots)
            digest = _sha256(temp.read_bytes())
            destination = self.artifacts_dir / f"{revision}.tar.gz"
            if not destination.exists():
                try:
                    os.link(temp, destination)
                    _fsync_directory(self.artifacts_dir)
                except FileExistsError:
                    pass
            if _sha256(destination.read_bytes()) != digest:
                raise ArtifactConflictError("revision 已存在但 artifact hash 冲突")
            # Windows 上硬链接共享文件属性：必须先删除 temp 再 chmod，
            # 否则 temp 也变为只读导致清理失败。
            temp.unlink()
            destination.chmod(0o444)
            data = destination.read_bytes()
            if _sha256(data) != digest:
                raise ArtifactConflictError("artifact 安装后发生变化")
            artifact = BundleArtifact(revision, destination, digest, len(data), data)
            return artifact
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _verify_archive(
        path: Path, candidate: Candidate, revision: str, roots: tuple[str, ...]
    ) -> None:
        expected = {item.path: item for item in candidate.source_manifest}
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            names = [item.name for item in members]
            if names != sorted(names, key=lambda item: item.encode("utf-8")):
                raise ArtifactConflictError("artifact 成员排序不确定")
            if any(not item.isfile() or item.issym() or item.islnk() for item in members):
                raise ArtifactConflictError("artifact 包含非普通文件")
            if set(names) != set(expected) | {".manifest"}:
                raise ArtifactConflictError("artifact 文件清单不一致")
            manifest_file = archive.extractfile(".manifest")
            if manifest_file is None or json.loads(manifest_file.read()) != {
                "revision": revision,
                "roots": list(roots),
            }:
                raise ArtifactConflictError("artifact manifest 不一致")
            for name, item in expected.items():
                stream = archive.extractfile(name)
                if stream is None:
                    raise ArtifactConflictError("artifact 文件缺失")
                data = stream.read()
                if len(data) != item.size or _sha256(data) != item.sha256:
                    raise ArtifactConflictError("artifact 文件 hash 不一致")

    def validate_without_opa(
        self, candidate_id: str, *, actor: str = "external-validator"
    ) -> Candidate:
        """仅完成本任务范围内的 snapshot/artifact 校验并进入 validated。

        同时写入外部校验证据（validated_by=actor），使 v0.52 双人复核对
        该路径同样可判定；actor 与 created_by 相同时 separation_ok=0。
        """
        artifact = self.build_artifact(candidate_id)
        return self.store.record_external_validation(candidate_id, artifact, actor=actor)
