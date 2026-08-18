"""RiskStateManager 单元测试（v1.2 §3.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_controller.risk_state import (
    JsonlRiskStateStore,
    RiskEvent,
    RiskStateManager,
)


class TestRiskStateManager:
    def test_score_deny_increases(self):
        manager = RiskStateManager()
        manager.update("s1", "deny")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == pytest.approx(0.20)
        assert profile.recent_tags == ["deny"]

    def test_score_decay_and_cap(self):
        manager = RiskStateManager()
        # 连续 4 次 deny：每次先乘 0.9 再加 0.20
        # 0 -> 0.2 -> 0.38 -> 0.542 -> 0.6878
        for _ in range(4):
            manager.update("s1", "deny")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == pytest.approx(0.6878)

    def test_low_risk_success_decreases_score(self):
        manager = RiskStateManager()
        manager.update("s1", "deny")
        manager.update("s1", "low_risk_success")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == pytest.approx(0.20 * 0.9 - 0.05)
        assert profile.recent_tags == ["deny"]  # low_risk_success 不进入 tags

    def test_score_floor_zero(self):
        manager = RiskStateManager()
        manager.update("s1", "low_risk_success")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == 0.0

    def test_score_ceiling_one(self):
        manager = RiskStateManager()
        for _ in range(20):
            manager.update("s1", "critical")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == 1.0

    def test_recent_tags_fifo(self):
        manager = RiskStateManager()
        for i in range(11):
            manager.update("s1", "deny")
        profile = manager.get_profile("s1")
        assert len(profile.recent_tags) == 10
        # 第 1 次被挤出
        assert "deny" in profile.recent_tags
        assert profile.recent_tags.count("deny") == 10

    def test_require_approval_has_no_score_but_tag(self):
        manager = RiskStateManager()
        manager.update("s1", "require_approval")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == 0.0
        assert profile.recent_tags == ["require_approval"]

    def test_approval_granted_and_denied_counts(self):
        manager = RiskStateManager()
        manager.update("s1", "approval_granted")
        manager.update("s1", "approval_denied")
        profile = manager.get_profile("s1")
        assert profile.approval_count == 1
        assert profile.denied_count == 1
        assert profile.recent_tags == ["approval_granted", "approval_denied"]

    def test_critical_from_risk_level(self):
        manager = RiskStateManager()
        manager.update("s1", "allow", risk_level="critical")
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == pytest.approx(0.30)
        assert profile.recent_tags == ["critical"]

    def test_new_session_starts_zero(self):
        manager = RiskStateManager()
        manager.update("s1", "deny")
        profile = manager.get_profile("s2")
        assert profile.cumulative_risk_score == 0.0
        assert profile.recent_tags == []


class TestJsonlRiskStateStore:
    def test_persist_and_replay(self, tmp_path: Path):
        path = tmp_path / "risk_state.jsonl"
        store = JsonlRiskStateStore(path)
        store.append_event(RiskEvent(session_id="s1", event_type="deny", score_delta=0.20, tag="deny"))
        store.append_event(
            RiskEvent(session_id="s1", event_type="approval_granted", score_delta=0.05, tag="approval_granted")
        )

        # 重新打开（模拟重启）
        manager = RiskStateManager(JsonlRiskStateStore(path))
        profile = manager.get_profile("s1")
        # deny: 0*0.9+0.2=0.2；approval_granted: 0.2*0.9+0.05=0.23
        assert profile.cumulative_risk_score == pytest.approx(0.23)
        assert profile.denied_count == 1
        assert profile.approval_count == 1

    def test_trailing_corrupt_line_ignored(self, tmp_path: Path, caplog):
        path = tmp_path / "risk_state.jsonl"
        path.write_text(
            json.dumps(
                {"session_id": "s1", "event_type": "deny", "score_delta": 0.20, "tag": "deny"},
                ensure_ascii=False,
            )
            + "\nthis-is-not-json\n",
            encoding="utf-8",
        )
        manager = RiskStateManager(JsonlRiskStateStore(path))
        profile = manager.get_profile("s1")
        assert profile.cumulative_risk_score == pytest.approx(0.20)
        assert "末行" in caplog.text or "第 2 行" in caplog.text

    def test_parent_dir_unwritable_raises(self, tmp_path: Path):
        # Windows 下不易构造真正不可写目录；此测试验证父路径为已存在文件时 mkdir 失败
        path = tmp_path / "is_a_file" / "risk_state.jsonl"
        path.parent.write_text("x", encoding="utf-8")  # 把父路径占成文件，导致 mkdir 失败
        with pytest.raises((PermissionError, FileExistsError)):
            JsonlRiskStateStore(path)
