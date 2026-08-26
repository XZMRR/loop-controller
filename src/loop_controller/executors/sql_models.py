"""SQL 执行器配置模型（v0.24.0）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.models import Tool

RiskLevel = Literal["low", "medium", "high", "critical"]


class SecretRef(BaseModel):
    """引用 Secret Broker 中的密钥。"""

    model_config = ConfigDict(frozen=True)

    name: str
    key: str | None = None


class DataSourceConfig(BaseModel):
    """SQL 数据源配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    driver: str  # sqlite / postgresql / mysql 等
    host: str | None = None
    port: int | None = None
    database: str | None = None
    read_only_user: str | None = None
    write_user: str | None = None
    secret_ref: SecretRef | None = None


class SQLToolSpec(BaseModel):
    """单个 SQL 工具规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    data_source: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    parameterize: bool = True
    # allowed/forbidden patterns 作为二次语义校验，不替代参数化
    allowed_patterns: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=lambda: [";", "--"])
    default_risk: RiskLevel = "high"
    cost_per_call: int = 0
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)

    def _input_schema(self) -> dict[str, Any]:
        """未显式提供 input_schema 时使用默认结构。"""
        if self.input_schema:
            return self.input_schema
        return {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "parameters": {"type": "object"},
            },
            "required": ["sql"],
        }

    def to_tool(self) -> Tool:
        """转换为治理链路使用的 Tool 元数据。"""
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.tool_name,
            description=self.description,
            input_schema=self._input_schema(),
        )

    @property
    def allowed_regexes(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.allowed_patterns]

    @property
    def forbidden_regexes(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.forbidden_patterns]
