"""R1 轻量分类器：基于 YAML 规则配置表对 ActionProposal 做预检。

对应文档《05_mvp_core_abstractions.md》3.5 LightweightClassifier 的规则版实现。
分类器只输出风险信号（RiskSignal），不决定是否执行，最终判定由 R2 负责。
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

import yaml

from r1_classifier.models import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    RiskLevel,
    RiskSignal,
    Task,
)


class LightweightClassifier(Protocol):
    """分类器接口协议：未来可替换为专用小模型，不影响调用方。"""

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal: ...


class RuleBasedClassifier:
    """基于 YAML 规则配置表的轻量分类器（MVP 规则版）。"""

    def __init__(
        self,
        rules: dict[str, Any] | None = None,
        rules_path: str | Path | None = None,
    ) -> None:
        """优先级：显式传入 rules > 指定 rules_path > 包内默认 rules.yaml。"""
        if rules is not None:
            self._rules = rules
        elif rules_path is not None:
            self._rules = self._load_yaml(Path(rules_path))
        else:
            self._rules = self._load_default_rules()

    @staticmethod
    def _validate(loaded: Any) -> dict[str, Any]:
        if not isinstance(loaded, dict) or "classifier" not in loaded:
            raise ValueError("规则文件格式错误，缺少顶层 'classifier' 键")
        return loaded

    @classmethod
    def _load_yaml(cls, path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as f:
            return cls._validate(yaml.safe_load(f))

    @classmethod
    def _load_default_rules(cls) -> dict[str, Any]:
        text = files("r1_classifier").joinpath("rules.yaml").read_text(encoding="utf-8")
        return cls._validate(yaml.safe_load(text))

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal:
        cfg = self._rules["classifier"]
        tool_name = proposal.tool_name

        # 1. CapabilityProfile 未授权 -> 直接 high，不查后续规则
        if not profile.is_tool_authorized(tool_name):
            level = RiskLevel(cfg.get("unauthorized_tool_level", "high"))
            return RiskSignal(
                risk_level=level,
                tags=["unauthorized_tool"],
                reason=f"工具 '{tool_name}' 未在 CapabilityProfile '{profile.profile_id}' 中授权",
                suggestion="请确认该工具已在岗位说明书（CapabilityProfile）中授权",
            )

        tool_cfg = cfg["tools"].get(tool_name)
        if tool_cfg is None:
            # 配置表中无规则的工具：默认常规操作
            return RiskSignal(risk_level=RiskLevel.LOW, reason="配置表中无该工具规则，默认常规操作")

        # 2. 工具默认等级 + 参数级规则（命中多条取最高）
        level = RiskLevel(tool_cfg.get("default", "low"))
        tags: list[str] = []
        reasons: list[str] = []

        for arg_rule in tool_cfg.get("args", []):
            value = proposal.arguments.get(arg_rule["key"])
            if value is None or not isinstance(value, str):
                continue
            if self._match(value, arg_rule["match"]):
                candidate = RiskLevel(arg_rule["level"])
                if candidate > level:
                    level = candidate
                tags.append(arg_rule.get("tag", f"{tool_name}:{arg_rule['key']}"))
                reasons.append(arg_rule["reason"])

        if reasons:
            return RiskSignal(risk_level=level, tags=tags, reason="; ".join(reasons))
        return RiskSignal(risk_level=level, reason="常规操作")

    @staticmethod
    def _match(value: str, match: dict[str, Any]) -> bool:
        match_type = match.get("type", "regex")
        if match_type != "regex":
            raise ValueError(f"不支持的匹配器类型: {match_type}")
        return re.search(match["pattern"], value, flags=re.IGNORECASE) is not None
