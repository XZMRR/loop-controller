"""Interaction Governance 配置加载与校验（v0.38.0）."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from loop_controller.interaction.models import (
    AgentTrust,
    DelegationCapability,
    DelegationPolicy,
    InteractionProfile,
)


class InteractionConfigError(ValueError):
    """交互治理配置无效。"""


def load_interaction_config(
    config_dir: Path,
) -> tuple[dict[str, InteractionProfile], dict[str, AgentTrust], dict[str, DelegationPolicy]]:
    """加载交互治理配置：profiles、trust、policies。

    Args:
        config_dir: config/ 目录路径。

    Returns:
        (interaction_profiles, agent_trust_map, delegation_policies)

    Raises:
        InteractionConfigError: 任一配置校验失败。
    """
    profiles = _load_interaction_profiles(config_dir / "interaction_profiles.yaml")
    trust_map = _load_agent_trust(config_dir / "agent_trust.yaml")
    policies = _load_delegation_policies(config_dir / "delegation_policies.yaml")
    _validate_cross_references(profiles, policies)
    return profiles, trust_map, policies


@dataclass(frozen=True)
class InteractionConfig:
    """交互治理配置容器，供 Runtime/AppConfig 使用。"""

    profiles: dict[str, InteractionProfile] = field(default_factory=dict)
    trust: dict[str, AgentTrust] = field(default_factory=dict)
    policies: dict[str, DelegationPolicy] = field(default_factory=dict)
    policy_dir: str = ""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise InteractionConfigError(f"无法解析 {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InteractionConfigError(f"{path} 根节点必须是 mapping")
    return data


def _config_items(data: dict[str, Any], key: str, path: Path) -> list[dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise InteractionConfigError(f"{path} 中 {key} 必须是 list")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InteractionConfigError(f"{path} 中 {key}[{index}] 必须是 mapping")
        items.append(dict(item))
    return items


def _load_interaction_profiles(path: Path) -> dict[str, InteractionProfile]:
    data = _read_yaml(path)
    profiles: dict[str, InteractionProfile] = {}
    for item in _config_items(data, "interaction_profiles", path):
        profile_id = item.get("profile_id", "<unknown>")
        try:
            caps_raw = item.pop("capabilities", {})
            if not isinstance(caps_raw, dict):
                raise TypeError("capabilities 必须是 mapping")
            capabilities: dict[str, DelegationCapability] = {}
            for cap_name, cap_raw in caps_raw.items():
                if not isinstance(cap_name, str) or not isinstance(cap_raw, dict):
                    raise TypeError("capabilities 的名称必须是字符串且值必须是 mapping")
                cap = dict(cap_raw)
                cap["tool_name"] = cap_name
                capabilities[cap_name] = DelegationCapability(**cap)
            profile = InteractionProfile(capabilities=capabilities, **item)
        except (ValidationError, TypeError, ValueError) as exc:
            raise InteractionConfigError(
                f"interaction_profile {profile_id} 配置非法：{exc}"
            ) from exc
        if profile.profile_id in profiles:
            raise InteractionConfigError(
                f"interaction_profile {profile.profile_id} 重复定义"
            )
        profiles[profile.profile_id] = profile
    return profiles


def _load_agent_trust(path: Path) -> dict[str, AgentTrust]:
    data = _read_yaml(path)
    trust_map: dict[str, AgentTrust] = {}
    for item in _config_items(data, "agent_trust", path):
        source = item.get("source_agent_id", "<unknown>")
        target = item.get("target_agent_id", "<unknown>")
        try:
            trust = AgentTrust(**item)
        except (ValidationError, TypeError, ValueError) as exc:
            raise InteractionConfigError(
                f"agent_trust {source}->{target} 配置非法：{exc}"
            ) from exc
        key = f"{trust.source_agent_id}:{trust.target_agent_id}"
        if key in trust_map:
            raise InteractionConfigError(f"agent_trust {key} 重复定义")
        trust_map[key] = trust
    return trust_map


def _load_delegation_policies(path: Path) -> dict[str, DelegationPolicy]:
    data = _read_yaml(path)
    policies: dict[str, DelegationPolicy] = {}
    for item in _config_items(data, "delegation_policies", path):
        tool_name = item.get("tool_name", "<unknown>")
        try:
            policy = DelegationPolicy(**item)
        except (ValidationError, TypeError, ValueError) as exc:
            raise InteractionConfigError(
                f"delegation_policy {tool_name} 配置非法：{exc}"
            ) from exc
        if policy.tool_name in policies:
            raise InteractionConfigError(
                f"delegation_policy {policy.tool_name} 重复定义"
            )
        policies[policy.tool_name] = policy
    return policies


def _validate_cross_references(
    profiles: dict[str, InteractionProfile],
    policies: dict[str, DelegationPolicy],
) -> None:
    """校验目标 profile 引用一致性。"""
    for policy in policies.values():
        for profile_id in policy.allowed_target_profiles:
            if profile_id not in profiles:
                raise InteractionConfigError(
                    f"delegation_policy {policy.tool_name} 引用未知 profile {profile_id}"
                )
        for profile_id in policy.denied_target_profiles:
            if profile_id not in profiles:
                raise InteractionConfigError(
                    f"delegation_policy {policy.tool_name} 引用未知 profile {profile_id}"
                )
