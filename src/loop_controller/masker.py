"""参数掩码（§7.4 / T3.2）：字段名黑名单 + 值模式正则；分级掩码 + 超长截断。

- ``audit_log`` 档：应用 ``field_name_blacklist`` + ``value_patterns`` 全部规则；
- ``approval_request`` 档：仅应用 ``field_name_blacklist``（收件人/路径/正文须对审批人可见）；
- 单个字段值超过 500 字符：截断为 ``{sha256, length, 前 100 字符预览}``，防止审计日志膨胀。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from loop_controller.infra.config_loader import MaskingRules

MaskLevel = Literal["audit_log", "approval_request"]

# 字段值超长截断阈值（§7.4 自审#2）
_TRUNCATE_THRESHOLD = 500
_TRUNCATE_PREVIEW = 100


def _truncate_value(value: str) -> dict[str, Any]:
    """超长字段截断：只存 sha256、长度、前 100 字符预览。"""
    encoded = value.encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "length": len(value),
        "preview": value[:_TRUNCATE_PREVIEW],
    }


class Masker:
    """按配置执行分级掩码与超长截断。"""

    def __init__(self, rules: MaskingRules) -> None:
        self._field_blacklist = [name.lower() for name in rules.field_name_blacklist]
        self._value_patterns = rules.value_patterns
        self._applies_to = rules.masking_applies_to

    def mask(self, arguments: dict[str, Any], level: MaskLevel) -> dict[str, Any]:
        """返回掩码后的参数副本，不修改原始字典。"""
        active_rules = self._applies_to.get(level, [])
        use_field = "field_name_blacklist" in active_rules
        use_value = "value_patterns" in active_rules

        result: dict[str, Any] = {}
        for key, value in arguments.items():
            result[key] = self._mask_value(key, value, use_field, use_value)
        return result

    def _mask_value(
        self,
        key: str,
        value: Any,
        use_field: bool,
        use_value: bool,
    ) -> Any:
        """递归处理 dict/list/str；命中规则则替换，超长字符串截断。"""
        if isinstance(value, dict):
            return {
                k: self._mask_value(k, v, use_field, use_value) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._mask_value(key, item, use_field, use_value) for item in value]

        if isinstance(value, str):
            if len(value) > _TRUNCATE_THRESHOLD:
                return _truncate_value(value)

            if use_field and self._field_match(key):
                return "***"

            if use_value:
                return self._pattern_replace(value)

        return value

    def _field_match(self, key: str) -> bool:
        """字段名是否命中黑名单（不区分大小写、子串匹配）。"""
        lower = key.lower()
        return any(black in lower for black in self._field_blacklist)

    def _pattern_replace(self, value: str) -> str:
        """依次应用所有值模式正则。"""
        for vp in self._value_patterns:
            value = re.sub(vp.pattern, vp.replacement, value)
        return value
