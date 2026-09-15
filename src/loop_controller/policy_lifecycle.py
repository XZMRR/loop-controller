"""OPA status 接收、required instance 聚合与 Bundle 读取。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loop_controller.infra.policy_delivery import ArtifactConflictError, CandidateState
from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.utils.canonical import canonical_json

_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SECRET_RE = re.compile(r"(?i)(authorization|bearer|token|secret|password|api[-_]?key)\s*[:=]\s*\S+")


class PolicyStatusError(ValueError):
    pass


class PolicyStatusForbiddenError(PolicyStatusError):
    pass


@dataclass(frozen=True)
class PolicyLifecycleConfig:
    bundle_name: str
    required_instance_ids: tuple[str, ...]
    status_ttl_seconds: int = 60
    max_status_payload_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if not self.bundle_name or not self.required_instance_ids:
            raise ValueError("bundle_name 和 required_instance_ids 不能为空")
        if len(set(self.required_instance_ids)) != len(self.required_instance_ids):
            raise ValueError("required_instance_ids 不得重复")
        if any(not _INSTANCE_RE.fullmatch(item) for item in self.required_instance_ids):
            raise ValueError("required instance id 非法")
        if self.status_ttl_seconds <= 0 or self.max_status_payload_bytes <= 0:
            raise ValueError("status TTL 和 payload 限制必须为正数")


class PolicyLifecycleService:
    def __init__(self, database: StateDatabase, config: PolicyLifecycleConfig) -> None:
        self._db = database
        self.config = config
        self._lock = threading.Lock()

    def receive_status(self, payload: dict[str, Any], *, raw_size: int) -> dict[str, Any]:
        if raw_size > self.config.max_status_payload_bytes:
            raise PolicyStatusError("status payload 超限")
        report = self._parse(payload)
        now = datetime.now(UTC)
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        with self._lock:
            try:
                with self._db._connect() as conn, self._db._immediate(conn):
                    current = conn.execute("SELECT * FROM policy_current WHERE singleton=1").fetchone()
                    generation = int(current["generation"])
                    previous = conn.execute(
                        "SELECT * FROM opa_instance_status WHERE instance_id=?",
                        (report["instance_id"],),
                    ).fetchone()
                    if previous is not None:
                        if previous["report_sha256"] == digest:
                            return self.status(now=now)
                        old_seq, new_seq = previous["report_sequence"], report["report_sequence"]
                        old_time, new_time = previous["opa_reported_at"], report["opa_reported_at"]
                        if old_seq is not None and new_seq is not None:
                            if new_seq <= old_seq:
                                raise PolicyStatusError("status report replay")
                        elif old_time is not None and new_time is not None:
                            if new_time <= old_time:
                                raise PolicyStatusError("status report replay")
                        else:
                            raise PolicyStatusError("status report 缺少可比较单调证据")
                    received_at = now.isoformat()
                    conn.execute(
                        """INSERT INTO opa_instance_status (
                           instance_id,revision,bundle_name,state,error_code,error_summary,
                           opa_version,opa_reported_at,report_sequence,generation,received_at,report_sha256
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(instance_id) DO UPDATE SET revision=excluded.revision,
                           bundle_name=excluded.bundle_name,state=excluded.state,
                           error_code=excluded.error_code,error_summary=excluded.error_summary,
                           opa_version=excluded.opa_version,opa_reported_at=excluded.opa_reported_at,
                           report_sequence=excluded.report_sequence,generation=excluded.generation,
                           received_at=excluded.received_at,report_sha256=excluded.report_sha256""",
                        (report["instance_id"], report["revision"], self.config.bundle_name,
                         report["state"], report["error_code"], report["error_summary"],
                         report["opa_version"], report["opa_reported_at"], report["report_sequence"],
                         generation, received_at, digest),
                    )
                    self._aggregate(conn, now)
            except sqlite3.Error as exc:
                raise StateDatabaseError(f"写入 OPA status 失败: {exc}") from exc
        return self.status(now=now)

    def _parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PolicyStatusError("status payload 必须是对象")
        schema_version = payload.get("schema_version")
        if schema_version is None:
            labels = payload.get("labels")
            bundles = payload.get("bundles")
            if not isinstance(labels, dict) or not isinstance(bundles, dict):
                raise PolicyStatusError("标准 status 缺少 labels/bundles")
            instance_id = labels.get("id") or labels.get("instance_id") or payload.get("partition_name")
            bundle = bundles.get(self.config.bundle_name)
            if not isinstance(bundle, dict):
                raise PolicyStatusForbiddenError("status 缺少目标 bundle")
            revision = bundle.get("active_revision") or bundle.get("revision")
            state = bundle.get("status") or bundle.get("state")
            error = bundle.get("errors") or bundle.get("error")
            error_code = bundle.get("error_code")
            reported = payload.get("timestamp")
            sequence = payload.get("sequence")
            version = payload.get("version") or payload.get("opa_version")
        elif schema_version == "loop-controller-status-v1":
            instance_id = payload.get("instance_id") or payload.get("labels", {}).get("id")
            if payload.get("bundle_name") != self.config.bundle_name:
                raise PolicyStatusForbiddenError("未知 bundle")
            revision = payload.get("revision") or payload.get("active_revision")
            state = payload.get("state") or payload.get("status")
            error, error_code = payload.get("error"), payload.get("error_code")
            reported = payload.get("timestamp") or payload.get("reported_at")
            sequence = payload.get("sequence")
            version = payload.get("opa_version")
        else:
            raise PolicyStatusError("status schema_version 非法")
        if instance_id not in self.config.required_instance_ids:
            raise PolicyStatusForbiddenError("未知 OPA instance")
        if revision is not None and (not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision)):
            raise PolicyStatusError("revision 格式非法")
        if state in {"ok", "active", "loaded", "success"}:
            state = "loaded"
        elif state in {"error", "failed"}:
            state = "error"
        else:
            raise PolicyStatusError("status state 非法")
        error_summary = None
        if error:
            state = "error"
            if isinstance(error, dict):
                error_code = error_code or error.get("code") or "opa_load_error"
                error_summary = error.get("message")
            elif isinstance(error, list):
                error_code = error_code or "opa_load_error"
                error_summary = canonical_json(error)
            else:
                error_code = error_code or "opa_load_error"
                error_summary = str(error)
        if error_code:
            state = "error"
        reported_at = None
        if reported is not None:
            if not isinstance(reported, str):
                raise PolicyStatusError("status timestamp 非法")
            try:
                parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                reported_at = parsed.astimezone(UTC).isoformat()
            except ValueError as exc:
                raise PolicyStatusError("status timestamp 非法") from exc
        if sequence is not None and (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0):
            raise PolicyStatusError("status sequence 非法")
        if reported_at is None and sequence is None:
            raise PolicyStatusError("status 必须包含 timestamp 或 sequence")
        if version is not None and not isinstance(version, str):
            raise PolicyStatusError("opa_version 非法")
        return {"instance_id": instance_id, "revision": revision, "state": state,
                "error_code": str(error_code)[:128] if error_code else None,
                "error_summary": _SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", " ".join(str(error_summary).split()))[:512] if error_summary else None,
                "opa_version": version[:128] if version else None,
                "opa_reported_at": reported_at, "report_sequence": sequence}

    def _aggregate(self, conn: sqlite3.Connection, now: datetime) -> None:
        current = conn.execute("SELECT * FROM policy_current WHERE singleton=1").fetchone()
        if not current["revision"] or not current["candidate_id"]:
            return
        candidate = conn.execute("SELECT * FROM policy_candidates WHERE candidate_id=?", (current["candidate_id"],)).fetchone()
        if candidate is None or candidate["state"] != CandidateState.PUBLISHED:
            return
        placeholders = ",".join("?" for _ in self.config.required_instance_ids)
        rows = {row["instance_id"]: row for row in conn.execute(
            f"SELECT * FROM opa_instance_status WHERE instance_id IN ({placeholders})",
            self.config.required_instance_ids)}
        valid: list[bool] = []
        has_error = False
        for instance in self.config.required_instance_ids:
            row = rows.get(instance)
            if row is None:
                valid.append(False)
                continue
            bound = row["generation"] == current["generation"] and row["revision"] == current["revision"]
            fresh = now - datetime.fromisoformat(row["received_at"]) <= timedelta(seconds=self.config.status_ttl_seconds)
            if bound and row["state"] == "error":
                has_error = True
            accepted = bound and fresh and row["state"] == "loaded" and not row["error_code"]
            valid.append(accepted)
            if accepted:
                conn.execute("""INSERT OR IGNORE INTO opa_instance_load_evidence
                    (generation,instance_id,revision,report_sha256,opa_reported_at,report_sequence,received_at)
                    VALUES (?,?,?,?,?,?,?)""", (current["generation"], instance, current["revision"],
                    row["report_sha256"], row["opa_reported_at"], row["report_sequence"], row["received_at"]))
        target = "failed" if has_error else "loaded" if all(valid) else None
        if target:
            params = ((target, now.isoformat(), target, current["candidate_id"])
                      if target == "loaded" else (target, target, current["candidate_id"]))
            changed = conn.execute(f"UPDATE policy_candidates SET state=?, {'loaded_at=?,' if target == 'loaded' else ''} failure_stage=CASE WHEN ?='failed' THEN 'opa_load' ELSE failure_stage END WHERE candidate_id=? AND state='published'", params).rowcount
            if changed:
                conn.execute("""INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
                    base_revision,target_revision,source_sha256,artifact_sha256,result,detail_json,created_at)
                    VALUES (lower(hex(randomblob(16))),?,?,?,?,?,?,?,?,?,?)""", (
                    "loaded" if target == "loaded" else "load_failed", "opa-status", current["candidate_id"],
                    candidate["base_revision"], current["revision"], candidate["source_sha256"], candidate["artifact_sha256"],
                    "success" if target == "loaded" else "failed", json.dumps({"generation": current["generation"]}, separators=(",", ":")), now.isoformat()))

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        try:
            with self._db._connect() as conn:
                current = conn.execute("SELECT * FROM policy_current WHERE singleton=1").fetchone()
                placeholders = ",".join("?" for _ in self.config.required_instance_ids)
                rows = {row["instance_id"]: dict(row) for row in conn.execute(
                    f"SELECT * FROM opa_instance_status WHERE instance_id IN ({placeholders})", self.config.required_instance_ids)}
                candidate = conn.execute("SELECT state FROM policy_candidates WHERE candidate_id=?", (current["candidate_id"],)).fetchone() if current["candidate_id"] else None
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 OPA status 失败: {exc}") from exc
        stale, errors, loaded, details = [], [], 0, {}
        for instance in self.config.required_instance_ids:
            row = rows.get(instance)
            active = False
            if row:
                fresh = now - datetime.fromisoformat(row["received_at"]) <= timedelta(seconds=self.config.status_ttl_seconds)
                bound = row["generation"] == current["generation"] and row["revision"] == current["revision"]
                active = fresh and bound and row["state"] == "loaded" and not row["error_code"]
                if bound and (row["state"] == "error" or row["error_code"]):
                    errors.append(instance)
            if active:
                loaded += 1
            else:
                stale.append(instance)
            details[instance] = row
        expected = current["revision"]
        return {"expected_revision": expected,
                "active_revision": expected if expected and loaded == len(self.config.required_instance_ids) and not errors else None,
                "state": candidate["state"] if candidate else "unpublished", "generation": current["generation"],
                "required_instances": len(self.config.required_instance_ids), "fresh_instances": len(self.config.required_instance_ids) - len(stale),
                "loaded_instances": loaded, "stale_instances": stale, "error_instances": errors,
                "status_ttl_seconds": self.config.status_ttl_seconds, "instances": details}

    def bundle(self, revision: str | None = None) -> tuple[bytes, str, int, str]:
        try:
            with self._db._connect() as conn:
                if revision is None:
                    revision = conn.execute("SELECT revision FROM policy_current WHERE singleton=1").fetchone()[0]
                if not revision or not _REVISION_RE.fullmatch(revision):
                    raise FileNotFoundError
                row = conn.execute("""SELECT artifact_path,artifact_sha256,artifact_size FROM policy_candidates
                    WHERE revision=? AND artifact_ready=1 AND state IN ('published','loaded','failed','superseded')
                    ORDER BY published_at DESC LIMIT 1""", (revision,)).fetchone()
        except sqlite3.Error as exc:
            raise StateDatabaseError(f"查询 bundle 失败: {exc}") from exc
        if row is None:
            raise FileNotFoundError
        path = Path(row["artifact_path"])
        if path.is_symlink() or not path.is_file():
            raise ArtifactConflictError("artifact 文件不存在或类型非法")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["artifact_sha256"] or len(data) != row["artifact_size"]:
            raise ArtifactConflictError("artifact 与 SQLite 元数据不一致")
        return data, digest, len(data), revision
