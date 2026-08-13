"""R3 审计日志.

R3 异步、只读地采集 R1/R2/R0-delegate 的行为记录。
注意 R3 不记录原始敏感参数，而是记录哈希或掩码后的版本。
MVP 阶段用 JSONL 文件实现，未来可升级为 SQLite/不可篡改存储。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from json import dumps
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class AuditEvent:
    """R3 的最小审计日志单元.

    Attributes:
        event_id: 事件唯一 ID。
        trace_id: 一次完整任务执行的追踪 ID。
        timestamp: 事件发生时间（UTC）。
        actor_type: 行为者类型。
        actor_id: 行为者 ID。
        action: 行为类型。
        target: 目标，如工具名。
        args_hash: 参数 SHA-256 哈希；未来应升级为 HMAC/加盐哈希。
        args_mask: 脱敏后的结构化参数。
        decision: 关联的 R2 判定结果。
        reason: 原因说明。
        session_id: 所属会话 ID。
        schema_version: 审计事件 schema 版本。
        policy_version: 策略版本。
        profile_version: 画像版本。
        parent_event_id: 父事件 ID，支持审计链。
        metadata: 扩展字段。
    """

    event_id: str
    trace_id: str
    timestamp: datetime
    actor_type: Literal["agent", "user", "r0_delegate", "system", "checkpoint"]
    actor_id: str
    action: Literal[
        "task_start",
        "task_end",
        "propose",
        "classify",
        "evaluate",
        "execute",
        "approve",
        "deny",
        "modify",
        "require_approval",
        "escalate",
        "audit_report",
    ]
    target: str
    args_hash: str | None = None
    args_mask: dict[str, str] | None = None
    decision: str | None = None
    reason: str | None = None
    session_id: str | None = None
    schema_version: str = "1.0"
    policy_version: str | None = None
    profile_version: str | None = None
    parent_event_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuditLogger(Protocol):
    """审计日志接口."""

    def log(self, event: AuditEvent) -> None:
        """记录审计事件."""
        ...


class JsonlAuditLogger:
    """MVP JSONL 审计日志实现.

    每条 AuditEvent 序列化为 JSON 后追加到文件。
    """

    def __init__(self, log_path: str | Path) -> None:
        """初始化.

        Args:
            log_path: 日志文件路径。
        """
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: AuditEvent) -> None:
        """将事件追加到 JSONL."""
        record = {
            "event_id": event.event_id,
            "trace_id": event.trace_id,
            "timestamp": event.timestamp.isoformat(),
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "action": event.action,
            "target": event.target,
            "args_hash": event.args_hash,
            "args_mask": event.args_mask,
            "decision": event.decision,
            "reason": event.reason,
            "session_id": event.session_id,
            "schema_version": event.schema_version,
            "policy_version": event.policy_version,
            "profile_version": event.profile_version,
            "parent_event_id": event.parent_event_id,
            "metadata": event.metadata,
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def read_events(self) -> list[dict[str, Any]]:
        """读取所有事件；主要用于测试."""
        if not self._log_path.exists():
            return []
        with self._log_path.open("r", encoding="utf-8") as f:
            return [self._parse(line) for line in f if line.strip()]

    @staticmethod
    def _parse(line: str) -> dict[str, Any]:
        import json

        return json.loads(line)


def hash_arguments(arguments: dict[str, Any]) -> str:
    """对参数做 SHA-256 哈希；MVP 阶段简单实现."""
    return sha256(dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def mask_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    """对参数做结构化掩码.

    规则：
    - to / cc / bcc 只保留域名；
    - content / body / subject 只显示长度；
    - path 只保留文件名；
    - 其他字段直接标记为 hashed。
    """
    masked: dict[str, str] = {}
    for key, value in arguments.items():
        if key in ("to", "cc", "bcc"):
            masked[key] = _mask_email(str(value))
        elif key in ("content", "body", "subject"):
            masked[key] = f"<text:{len(str(value))}>"
        elif key == "path":
            masked[key] = f"<path:{Path(str(value)).name}>"
        else:
            masked[key] = "<hashed>"
    return masked


def _mask_email(email: str) -> str:
    """保留邮箱域名，隐藏本地部分."""
    if "@" not in email:
        return "<masked>"
    local, domain = email.rsplit("@", 1)
    return f"***@{domain}"
