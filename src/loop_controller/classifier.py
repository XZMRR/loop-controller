"""轻量分类器（§3.5）：只输出风险信号，不构成任何判定效力。

``RuleBasedClassifier`` 是规则版打桩：给 R1 自检用，产出 ``RiskSignal``，
由 R1 执行循环写回 ``ActionProposal.risk_level / risk_tags`` 进入 Rego input。
真正的判定权在 R2（Rego + 组合规则）——分类器没有否决权（§3.5 冲突规则）。
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from loop_controller.models import ActionProposal, Agent, CapabilityProfile, RiskSignal, Task

# 敏感模式（与 config/masking_rules.yaml 的 value_patterns / field_name_blacklist 对齐，
# 避免"日志里脱敏了但分类器没认出来"的口径漂移）。
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_SENSITIVE_FIELD_NAMES = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "authorization",
    "credential",
)


@runtime_checkable
class LightweightClassifier(Protocol):
    """R1 轻量分类器接口（§3.5）。"""

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal: ...


class RuleBasedClassifier:
    """规则版分类器（MVP 打桩），四条规则：

    | 规则 | risk_level | tags |
    |---|---|---|
    | ``tool_name == "send_email"`` | high | ``[external_communication]`` |
    | ``tool_name == "read_file"`` | medium | ``[data_access]`` |
    | 参数值匹配敏感模式（邮箱） | high | 追加 ``[pii_involved]`` |
    | 参数值匹配敏感模式（Bearer token / 密码字段名） | high | 追加 ``[credential_involved]`` |
    | 其他 | low | ``[]`` |
    """

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal:
        level = "low"
        tags: list[str] = []
        hits: list[str] = []

        if proposal.tool_name == "send_email":
            level = "high"
            tags.append("external_communication")
            hits.append("tool=send_email")
        elif proposal.tool_name == "read_file":
            level = "medium"
            tags.append("data_access")
            hits.append("tool=read_file")

        # 参数级敏感模式：邮箱 / Bearer token / 密码字段名
        pii, cred = self._scan_arguments(proposal.arguments)
        if pii:
            level = "high"
            tags.append("pii_involved")
            hits.append("value=email_address")
        if cred:
            level = "high"
            tags.append("credential_involved")
            hits.append("value=credential")

        return RiskSignal(
            risk_level=level,
            tags=tags,
            reason="; ".join(hits) or "no rule matched",
        )

    @staticmethod
    def _scan_arguments(arguments: dict) -> tuple[bool, bool]:
        """浅层扫描参数（与掩码同口径），返回 (含 PII?, 含凭证?)。"""
        pii = False
        cred = False
        for key, value in arguments.items():
            lowered_key = str(key).lower()
            if any(field in lowered_key for field in _SENSITIVE_FIELD_NAMES):
                cred = True
            text = str(value)
            if _EMAIL_RE.search(text):
                pii = True
            if _BEARER_RE.search(text):
                cred = True
        return pii, cred
