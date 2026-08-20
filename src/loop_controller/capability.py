"""Capability-Based Permission Interaction Analyzer（v0.10.0）.

把工具调用抽象为"能力"（Capability），在会话级能力集合上通过声明式规则
检测 A+B>C 的组合风险。Python 负责图分析与规则匹配，Rego 做最终裁决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loop_controller.infra.config_loader import CapabilityCombinationRule, CapabilityDef
from loop_controller.models import ActionProposal
from loop_controller.utils.globmatch import glob_match


@dataclass(frozen=True)
class Capability:
    """能力实例：名称 + 产生该能力的动作上下文（调试用）。"""

    name: str


@dataclass(frozen=True)
class CapabilityGraph:
    """会话级能力图/集合（v0.10.0 仅维护集合，未来可扩展为时序图）。"""

    capabilities: set[Capability] = field(default_factory=set)

    def add(self, capability: Capability) -> CapabilityGraph:
        return CapabilityGraph(self.capabilities | {capability})

    def names(self) -> set[str]:
        return {c.name for c in self.capabilities}


class CapabilityGraphAnalyzer:
    """基于能力图的组合风险分析器。

    1. 根据 ``capability_rules.yaml`` 中声明的工具→能力映射，从单个动作中提取能力；
    2. 累积历史动作的能力集合；
    3. 匹配 ``combination_rules``，输出风险标签与分数。
    """

    def __init__(
        self,
        capability_defs: dict[str, CapabilityDef],
        combination_rules: list[CapabilityCombinationRule],
    ) -> None:
        self._capability_defs = dict(capability_defs)
        self._combination_rules = list(combination_rules)

    def extract_capabilities(self, proposal: ActionProposal) -> set[str]:
        """从单个 ActionProposal 中提取其产生的能力名集合。"""
        found: set[str] = set()
        for name, cap_def in self._capability_defs.items():
            for producer in cap_def.produced_by:
                if producer.tool != proposal.tool_name:
                    continue
                if not _args_match_patterns(proposal.arguments, producer.arg_match or {}):
                    continue
                if producer.arg_not_match and _args_match_patterns(
                    proposal.arguments, producer.arg_not_match
                ):
                    continue
                found.add(name)
                break
        return found

    def build_graph(self, proposals: list[ActionProposal]) -> CapabilityGraph:
        """从历史动作列表构建能力图。"""
        graph = CapabilityGraph()
        for proposal in proposals:
            for name in self.extract_capabilities(proposal):
                graph = graph.add(Capability(name=name))
        return graph

    def analyze(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> tuple[list[str], int, list[CapabilityCombinationRule]]:
        """分析当前动作与历史动作的组合风险。

        Returns:
            (risk_tags, max_score, matched_rules)
        """
        history_graph = self.build_graph(history)
        current_caps = self.extract_capabilities(current)
        history_caps = history_graph.names()

        matched: list[CapabilityCombinationRule] = []
        for rule in self._combination_rules:
            requires_hit = any(req in history_caps for req in rule.requires_any)
            triggers_hit = any(trig in current_caps for trig in rule.triggers_any)
            if requires_hit and triggers_hit:
                matched.append(rule)

        if not matched:
            return [], 0, []

        tags: set[str] = set()
        max_score = 0
        for rule in matched:
            tags.update(rule.risk_tags)
            if rule.score > max_score:
                max_score = rule.score
        return sorted(tags), max_score, matched


def _args_match_patterns(args: dict[str, Any], patterns: dict[str, str]) -> bool:
    """args 中每个 key 至少有一个 glob pattern 匹配其字符串值。"""
    for key, pattern in patterns.items():
        if key not in args:
            return False
        value = str(args[key])
        if not glob_match(pattern, value):
            return False
    return True
