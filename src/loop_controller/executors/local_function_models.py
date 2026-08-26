"""本地函数执行器配置模型（v0.23.0）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.models import Tool

RiskLevel = Literal["low", "medium", "high", "critical"]


class LocalFunctionSandboxConfig(BaseModel):
    """本地函数沙箱参数。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)


class LocalFunctionSpec(BaseModel):
    """单个本地函数工具规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    module: str
    function: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    cost_per_call: int = 0
    default_risk: RiskLevel = "medium"
    sandbox: LocalFunctionSandboxConfig = Field(default_factory=LocalFunctionSandboxConfig)

    def to_tool(self) -> Tool:
        """转换为治理链路使用的 Tool 元数据。"""
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.function,
            description=self.description,
            input_schema=self.input_schema,
        )
