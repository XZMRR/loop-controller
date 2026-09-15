"""Profile 工具策略在线编辑与持久化（管理配置闭环）。

提供 ``update_profile_tools``：先以 ``ToolPermission`` 模型校验入参，
再原子写回 ``profiles.yaml``（临时文件 + ``os.replace``），最后从磁盘
重新加载该 Profile，保证内存态与文件态一致。

注意：基于 PyYAML 的读写是``加载-修改-全量写回``，文件中的注释与
排版会在首次写回后丢失；这是当前“最小可维护闭环”的取舍。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import CapabilityProfile, ToolPermission


class ProfileConfigError(Exception):
    """Profile 配置写回失败（校验错误 / 文件缺失 / IO 错误）。"""


def update_profile_tools(
    config_dir: str | Path,
    profile_id: str,
    tools: dict[str, dict[str, Any]],
) -> CapabilityProfile:
    """更新指定 Profile 的 tools 小节并返回磁盘最新版本。

    Args:
        config_dir: config/ 目录路径。
        profile_id: 目标 Profile ID；不存在时抛 ``ProfileConfigError``。
        tools: 完整的工具权限映射（整体替换该 Profile 的 tools 小节）。

    Raises:
        ProfileConfigError: 任一工具权限非法、Profile 不存在或写文件失败。
    """
    config_dir = Path(config_dir)
    path = config_dir / "profiles.yaml"
    if not path.exists():
        raise ProfileConfigError(f"profiles.yaml 不存在：{path}")

    validated = _validate_tools(tools)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileConfigError(f"profiles.yaml 解析失败：{exc}") from exc

    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        raise ProfileConfigError("profiles.yaml 缺少 profiles 列表")

    target: dict[str, Any] | None = None
    for item in profiles:
        if isinstance(item, dict) and item.get("profile_id") == profile_id:
            target = item
            break
    if target is None:
        raise ProfileConfigError(f"Profile 不存在：{profile_id}")

    target["tools"] = {
        name: _dump_permission(perm) for name, perm in sorted(validated.items())
    }
    _atomic_write(path, data)

    # 从磁盘重载，确保返回值与运行时一致（version 也会更新为最新文件哈希）。
    reloaded = ConfigLoader().reload_profiles(config_dir)
    profile = reloaded.get(profile_id)
    if profile is None:
        raise ProfileConfigError(f"写回后重载失败：{profile_id}")
    return profile


def _validate_tools(tools: dict[str, dict[str, Any]]) -> dict[str, ToolPermission]:
    if not isinstance(tools, dict) or not tools:
        raise ProfileConfigError("tools 不能为空")
    validated: dict[str, ToolPermission] = {}
    for name, perm in tools.items():
        if not isinstance(name, str) or not name.strip():
            raise ProfileConfigError("工具名不能为空")
        if not isinstance(perm, dict):
            raise ProfileConfigError(f"工具 {name} 的权限必须是对象")
        try:
            validated[name] = ToolPermission(tool_name=name, **perm)
        except ValidationError as exc:
            raise ProfileConfigError(f"工具 {name} 权限非法：{exc}") from exc
    return validated


def _dump_permission(perm: ToolPermission) -> dict[str, Any]:
    """序列化为 profiles.yaml 中的紧凑形式（省略默认值，便于人工维护）。"""
    data: dict[str, Any] = {"allowed": perm.allowed}
    if perm.require_approval:
        data["require_approval"] = True
    if perm.allowed_args:
        data["allowed_args"] = perm.allowed_args
    if perm.denied_args:
        data["denied_args"] = perm.denied_args
    if perm.max_calls_per_task is not None:
        data["max_calls_per_task"] = perm.max_calls_per_task
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ProfileConfigError(f"profiles.yaml 写入失败：{exc}") from exc
