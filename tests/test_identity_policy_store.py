"""identity / policy_store 单元测试."""

from __future__ import annotations

from pathlib import Path

from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.models import Agent


def test_identity_get_agent_ok():
    agent = Agent(agent_id="a1", name="n", profile_id="p1", owner_id="o")
    provider = ConfigIdentityProvider(agents={"a1": agent}, users={"alice": "Alice"})
    assert provider.get_agent("a1") is agent
    assert provider.get_user("alice") == "Alice"


def test_identity_unknown_id_returns_none():
    provider = ConfigIdentityProvider(agents={}, users={})
    assert provider.get_agent("ghost") is None
    assert provider.get_user("ghost") is None


def test_policy_store_version_content_sensitive(tmp_path: Path):
    store = FilePolicyStore(tmp_path)
    empty_version = store.current_version()  # 空目录：空输入哈希（稳定值）
    assert len(empty_version) == 12

    (tmp_path / "b.rego").write_text("package b\n", encoding="utf-8")
    (tmp_path / "a.rego").write_text("package a\n", encoding="utf-8")
    v1 = store.current_version()
    assert len(v1) == 12
    assert v1 != empty_version  # 新增策略 → 版本变化
    assert store.list_policies() == ["a", "b"]  # 按文件名排序

    # 内容变化 → 版本变化
    (tmp_path / "a.rego").write_text("package a2\n", encoding="utf-8")
    v2 = store.current_version()
    assert v2 != v1


def test_policy_store_policy_path(tmp_path: Path):
    store = FilePolicyStore(tmp_path)
    assert store.policy_path("default") == str(tmp_path / "default.rego")
