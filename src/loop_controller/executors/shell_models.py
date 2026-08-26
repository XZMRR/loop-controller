"""Shell 执行器配置模型（v0.24.0）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.models import Tool

RiskLevel = Literal["low", "medium", "high", "critical"]


DEFAULT_FORBIDDEN_CHARS = [";", "|", "&", "`", "$", "(", ")", ">", "<", "\\"]

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ShellCommandConfig(BaseModel):
    """Shell 命令沙箱参数。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    env_whitelist: list[str] = Field(default_factory=list)


class ShellToolSpec(BaseModel):
    """单个 Shell 命令工具规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    description: str = ""
    command_template: list[str]
    allowed_args: dict[str, list[str]] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    forbidden_chars: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_CHARS))
    default_risk: RiskLevel = "critical"
    cost_per_call: int = 0
    sandbox: ShellCommandConfig = Field(default_factory=ShellCommandConfig)

    def _input_schema(self) -> dict[str, Any]:
        """未显式提供 input_schema 时，从 allowed_args 推导。"""
        if self.input_schema:
            return self.input_schema
        if self.allowed_args:
            return {
                "type": "object",
                "properties": {
                    name: {"type": "string"} for name in self.allowed_args
                },
                "required": list(self.allowed_args.keys()),
            }
        return {}

    def to_tool(self) -> Tool:
        """转换为治理链路使用的 Tool 元数据。"""
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.tool_name,
            description=self.description,
            input_schema=self._input_schema(),
        )

    @property
    def forbidden_pattern(self) -> re.Pattern[str]:
        """将 forbidden_chars 编译为用于参数值校验的正则。"""
        chars = self.forbidden_chars or DEFAULT_FORBIDDEN_CHARS
        escaped = [re.escape(c) for c in chars]
        return re.compile("|".join(escaped))

    @property
    def placeholders(self) -> set[str]:
        """从 command_template 中提取的占位符集合。"""
        names: set[str] = set()
        for token in self.command_template:
            names.update(_PLACEHOLDER_RE.findall(token))
        return names
