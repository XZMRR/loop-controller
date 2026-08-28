"""Harness 工具与后端配置模型（v0.25.0）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.models import RiskLevel, Tool


class HarnessSandboxConfig(BaseModel):
    """Harness 工具的沙箱限制参数。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)


class HarnessToolSpec(BaseModel):
    """Harness 工具规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    harness: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    default_risk: RiskLevel = "critical"
    cost_per_call: int = 0
    secret_refs: list[str] = Field(default_factory=list)
    sandbox: HarnessSandboxConfig = Field(default_factory=HarnessSandboxConfig)

    def to_tool(self) -> Tool:
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.tool_name,
            description=self.description,
            input_schema=self.input_schema,
        )


class SubprocessBackendConfig(BaseModel):
    """子进程 Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["subprocess"] = "subprocess"
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    max_concurrent_calls: int = Field(default=10, ge=1)


class DockerBackendConfig(BaseModel):
    """Docker Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["docker"] = "docker"
    image: str
    network_mode: str | None = "none"
    env: dict[str, str] = Field(default_factory=dict)
    mounts: list[dict[str, Any]] = Field(default_factory=list)
    max_concurrent_calls: int = Field(default=5, ge=1)


class HTTPBackendConfig(BaseModel):
    """远程 HTTP Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["http"] = "http"
    base_url: str
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=30.0, ge=1.0)
    max_concurrent_calls: int = Field(default=10, ge=1)


HarnessBackendConfig = (
    SubprocessBackendConfig | DockerBackendConfig | HTTPBackendConfig
)
