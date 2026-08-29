"""会话级风险状态管理器（v1.2 §3.2）：确定性算分、本地 JSONL 持久化、启动重放。

RiskStateManager 按规则维护 cumulative_risk_score 与 recent_tags，并通过
RiskStateStore Protocol 写入 JSONL；启动时重放历史事件恢复状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.durable_io import DurableJsonlFile
from loop_controller.models import RiskProfile

# 风险证据标签集合：只有这些事件进入 recent_tags
_RISK_EVIDENCE_TAGS = {"deny", "critical", "require_approval", "approval_denied", "approval_granted"}

# 分数变动规则（v1.2 §3.2）
_SCORE_DELTA = {
    "deny": 0.20,
    "critical": 0.30,
    "approval_denied": 0.10,
    "approval_granted": 0.05,
    "low_risk_success": -0.05,
}

# 每条新事件的全量分数衰减系数
_DECAY_FACTOR = 0.9

# recent_tags FIFO 上限
_MAX_TAGS = 10


@dataclass
class RiskEvent:
    """风险状态单条事件。"""

    session_id: str
    event_type: str
    score_delta: float
    tag: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict:
        """序列化为 JSONL 行字典。"""
        return {
            "session_id": self.session_id,
            "event_type": self.event_type,
            "score_delta": self.score_delta,
            "tag": self.tag,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> RiskEvent:
        """从 JSONL 行字典反序列化。"""
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        else:
            ts = datetime.now(UTC)
        return cls(
            session_id=data["session_id"],
            event_type=data["event_type"],
            score_delta=data["score_delta"],
            tag=data["tag"],
            timestamp=ts,
        )


@runtime_checkable
class RiskStateStore(Protocol):
    """风险状态持久化协议。"""

    def load_all(self) -> list[RiskEvent]:
        """加载全部历史事件（启动重放用）。"""
        ...

    def append_event(self, event: RiskEvent) -> list[RiskEvent]:
        """追加单条事件并返回锁内刷新的全部历史。"""
        ...


class InMemoryRiskStateStore:
    """内存版风险状态存储（测试/单进程不持久化场景）。"""

    def __init__(self) -> None:
        self._events: list[RiskEvent] = []

    def load_all(self) -> list[RiskEvent]:
        return list(self._events)

    def append_event(self, event: RiskEvent) -> list[RiskEvent]:
        self._events.append(event)
        return list(self._events)


class JsonlRiskStateStore:
    """JSONL 追加 + 启动重放的风险状态存储（v1.2 §3.2）。

    - 父目录不存在时自动创建；
    - 初始化时检查父目录可写；
    - 启动重放时最后一行不完整则忽略并 WARNING；
    - 每次追加后 flush，必要时 fsync（P1 单 writer 假设）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._durable = DurableJsonlFile(self._path)
        self._ensure_writable()

    def _ensure_writable(self) -> None:
        """检查并确保父目录可写（启动校验）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        probe = self._path.parent / ".write_probe_risk_state"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PermissionError(f"risk_state 父目录 {self._path.parent} 不可写：{exc}") from exc
        # 若文件已存在则确认可追加
        if self._path.exists():
            try:
                with self._path.open("a", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise PermissionError(f"risk_state 文件 {self._path} 不可追加：{exc}") from exc

    def load_all(self) -> list[RiskEvent]:
        """锁内重放；仅允许忽略损坏的物理末行，中间损坏时 fail-closed。"""
        with self._durable.transaction() as transaction:
            transaction.repair_incomplete_tail()
            raw_lines = transaction.read_complete_lines(allow_corrupt_last=True)
        return [RiskEvent.from_dict(json.loads(raw)) for raw in raw_lines]

    def append_event(self, event: RiskEvent) -> list[RiskEvent]:
        """锁内刷新历史并追加单条事件。"""
        with self._durable.transaction() as transaction:
            transaction.repair_incomplete_tail()
            lines = transaction.read_complete_lines()
            events = [RiskEvent.from_dict(json.loads(raw)) for raw in lines]
            transaction.append_json(event.to_dict())
            events.append(event)
            return events


class RiskStateManager:
    """会话级风险状态管理器。

    Args:
        store: 持久化后端；None 时使用内存实现（测试默认）。
    """

    def __init__(self, store: RiskStateStore | None = None) -> None:
        self._store = store or InMemoryRiskStateStore()
        self._profiles: dict[str, RiskProfile] = {}
        self._replay()

    def _replay(self) -> None:
        """启动时重放历史事件恢复状态。"""
        for event in self._store.load_all():
            self._apply_in_memory(event)

    def _apply_in_memory(self, event: RiskEvent) -> None:
        """把单条事件应用到内存 RiskProfile（不再次落盘）。"""
        profile = self._profiles.get(event.session_id)
        if profile is None:
            profile = RiskProfile(session_id=event.session_id)
        # 1. 分数衰减（下限 0，上限 1.0）
        score = profile.cumulative_risk_score * _DECAY_FACTOR
        # 2. 加上本次事件分值变动
        score = max(0.0, min(1.0, score + event.score_delta))
        # 3. recent_tags：只记录风险证据
        tags = list(profile.recent_tags)
        if event.tag in _RISK_EVIDENCE_TAGS:
            tags.append(event.tag)
            if len(tags) > _MAX_TAGS:
                tags = tags[-_MAX_TAGS:]
        # 4. 统计计数
        denied_count = profile.denied_count
        approval_count = profile.approval_count
        consecutive_deny_count = profile.consecutive_deny_count
        if event.event_type == "deny" or event.event_type == "approval_denied":
            denied_count += 1
            consecutive_deny_count += 1
        elif event.event_type == "approval_granted":
            approval_count += 1
            consecutive_deny_count = 0
        elif event.event_type == "low_risk_success":
            consecutive_deny_count = 0
        # require_approval / critical 不改变 consecutive_deny_count
        self._profiles[event.session_id] = RiskProfile(
            session_id=event.session_id,
            cumulative_risk_score=round(score, 6),
            recent_tags=tags,
            denied_count=denied_count,
            approval_count=approval_count,
            consecutive_deny_count=consecutive_deny_count,
        )

    def update(self, session_id: str, event_type: str, risk_level: str | None = None) -> None:
        """按规则更新 cumulative_risk_score 和 recent_tags，并持久化事件。

        Args:
            session_id: 事件所属 session。
            event_type: deny | critical | approval_denied | approval_granted | low_risk_success |
                        require_approval（仅用于 tag，无分数变动）。
            risk_level: 分类器信号级别；为 "critical" 时按 critical 事件处理。
        """
        # critical 信号特殊处理：event_type 传入 "critical" 或由 risk_level 触发
        if risk_level == "critical" and event_type not in ("deny", "critical"):
            event_type = "critical"

        delta = _SCORE_DELTA.get(event_type, 0.0)
        tag = event_type if event_type in _RISK_EVIDENCE_TAGS else ""
        # 允许 require_approval 进入 recent_tags 但无分数变动
        if event_type == "require_approval":
            delta = 0.0
            tag = "require_approval"

        event = RiskEvent(
            session_id=session_id,
            event_type=event_type,
            score_delta=delta,
            tag=tag,
        )
        events = self._store.append_event(event)
        self._profiles.clear()
        for stored_event in events:
            self._apply_in_memory(stored_event)

    def get_profile(self, session_id: str) -> RiskProfile:
        """获取 session 的风险画像；不存在则返回零分画像。"""
        self._profiles.clear()
        for event in self._store.load_all():
            self._apply_in_memory(event)
        return self._profiles.get(session_id, RiskProfile(session_id=session_id))

    def reset(self, session_id: str) -> None:
        """清空指定 session 的内存状态（测试用）。"""
        self._profiles.pop(session_id, None)
